from __future__ import annotations

import hmac
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from codexia_manual_agent.work.admission import (
    ContinuationAdmission,
    ContinuationDecision,
    ContinuationProposal,
)
from codexia_manual_agent.work.attention import (
    AttentionDisposition,
    DynamicAttentionDecision,
)
from codexia_manual_agent.work.chat_peer import (
    CapturedChatPeerMessage,
    ChatGPTPeerLoop,
    ChatPeerCursor,
    ChatPeerMessageOrigin,
    ChatPeerObservation,
    ChatPeerTurn,
)
from codexia_manual_agent.work.contracts import (
    InvalidWorkRecordError,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
    _canonical_json,
    _digest,
    _exact_keys,
    _new_timestamp,
    _validate_digest,
    _validate_timestamp,
    _validate_uuid,
)

SUPERVISOR_DISPATCH_SCHEMA_VERSION = 1
SUPERVISOR_EVENT_SCHEMA_VERSION = 1


class SupervisorPersistenceError(RuntimeError):
    """Base failure for durable M6.5 supervisor state."""


class SupervisorIntegrityError(SupervisorPersistenceError):
    """Persisted supervisor history is incomplete, inconsistent, or tampered."""


class SupervisorStateError(SupervisorPersistenceError):
    """Requested transition is invalid for the exact recovered supervisor state."""


class SupervisorConcurrencyError(SupervisorPersistenceError):
    """Another supervisor process advanced the same delegated work first."""


class SupervisorStatus(StrEnum):
    """Orchestration readiness only; never execution authority."""

    READY = "ready"
    PREPARED = "prepared"
    IN_FLIGHT = "in_flight"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"


class SupervisorEventKind(StrEnum):
    REGISTERED = "registered"
    CHECKPOINT_DECIDED = "checkpoint_decided"
    DISPATCH_STARTED = "dispatch_started"
    PEER_TURN_RECORDED = "peer_turn_recorded"
    EXTERNAL_OBSERVED = "external_observed"
    COMPLETED = "completed"


def _bounded_peer_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidWorkRecordError(f"{field_name} must be text")
    value = value.strip()
    if not value or len(value) > 512 or "\x00" in value:
        raise InvalidWorkRecordError(f"{field_name} is invalid")
    return value


def _cursor_from_dict(payload: Mapping[str, Any]) -> ChatPeerCursor:
    value = _exact_keys(
        payload,
        {
            "schema_version",
            "conversation_id",
            "message_fingerprints",
            "cursor_digest",
        },
        "ChatPeerCursor",
    )
    fingerprints = value["message_fingerprints"]
    if not isinstance(fingerprints, list):
        raise InvalidWorkRecordError(
            "message_fingerprints must decode from a JSON array"
        )
    return ChatPeerCursor(
        schema_version=value["schema_version"],
        conversation_id=value["conversation_id"],
        message_fingerprints=tuple(fingerprints),
        cursor_digest=value["cursor_digest"],
    )


def _captured_message_from_dict(
    payload: Mapping[str, Any],
) -> CapturedChatPeerMessage:
    value = _exact_keys(
        payload,
        {
            "schema_version",
            "conversation_id",
            "node_id",
            "provider_message_id",
            "transport_role",
            "origin",
            "statement",
            "capture_digest",
        },
        "CapturedChatPeerMessage",
    )
    try:
        origin = ChatPeerMessageOrigin(value["origin"])
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported chat peer message origin") from exc
    return CapturedChatPeerMessage(
        schema_version=value["schema_version"],
        conversation_id=value["conversation_id"],
        node_id=value["node_id"],
        provider_message_id=value["provider_message_id"],
        transport_role=value["transport_role"],
        origin=origin,
        statement=WorkStatement.from_dict(value["statement"]),
        capture_digest=value["capture_digest"],
    )


def _observation_from_dict(payload: Mapping[str, Any]) -> ChatPeerObservation:
    value = _exact_keys(
        payload,
        {
            "schema_version",
            "before_cursor_digest",
            "after_cursor",
            "messages",
            "observation_digest",
        },
        "ChatPeerObservation",
    )
    messages = value["messages"]
    if not isinstance(messages, list):
        raise InvalidWorkRecordError("messages must decode from a JSON array")
    return ChatPeerObservation(
        schema_version=value["schema_version"],
        before_cursor_digest=value["before_cursor_digest"],
        after_cursor=_cursor_from_dict(value["after_cursor"]),
        messages=tuple(_captured_message_from_dict(item) for item in messages),
        observation_digest=value["observation_digest"],
    )


def _turn_from_dict(payload: Mapping[str, Any]) -> ChatPeerTurn:
    value = _exact_keys(
        payload,
        {
            "schema_version",
            "admission_id",
            "admission_digest",
            "handoff_id",
            "handoff_digest",
            "interpretation_id",
            "interpretation_digest",
            "before_cursor_digest",
            "codexia_message",
            "worker_message",
            "after_cursor",
            "turn_digest",
        },
        "ChatPeerTurn",
    )
    return ChatPeerTurn(
        schema_version=value["schema_version"],
        admission_id=value["admission_id"],
        admission_digest=value["admission_digest"],
        handoff_id=value["handoff_id"],
        handoff_digest=value["handoff_digest"],
        interpretation_id=value["interpretation_id"],
        interpretation_digest=value["interpretation_digest"],
        before_cursor_digest=value["before_cursor_digest"],
        codexia_message=_captured_message_from_dict(value["codexia_message"]),
        worker_message=_captured_message_from_dict(value["worker_message"]),
        after_cursor=_cursor_from_dict(value["after_cursor"]),
        turn_digest=value["turn_digest"],
    )


@dataclass(frozen=True, slots=True)
class SupervisorDispatch:
    """Exact provider-dispatch intent; this record itself grants no authority."""

    schema_version: int
    dispatch_id: str
    created_at: str
    conversation_id: str
    cursor_digest: str
    proposal: ContinuationProposal
    admission: ContinuationAdmission
    attention: DynamicAttentionDecision
    dispatch_digest: str

    @classmethod
    def create(
        cls,
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        cursor: ChatPeerCursor,
        proposal: ContinuationProposal,
        admission: ContinuationAdmission,
        attention: DynamicAttentionDecision,
        dispatch_id: str | None = None,
        created_at: str | None = None,
    ) -> SupervisorDispatch:
        cls._validate_exact(
            handoff=handoff,
            interpretation=interpretation,
            cursor=cursor,
            proposal=proposal,
            admission=admission,
            attention=attention,
        )
        dispatch_id = dispatch_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        base = {
            "schema_version": SUPERVISOR_DISPATCH_SCHEMA_VERSION,
            "dispatch_id": dispatch_id,
            "created_at": created_at,
            "conversation_id": cursor.conversation_id,
            "cursor_digest": cursor.cursor_digest,
            "proposal": proposal.to_dict(),
            "admission": admission.to_dict(),
            "attention": attention.to_dict(),
        }
        return cls(
            schema_version=SUPERVISOR_DISPATCH_SCHEMA_VERSION,
            dispatch_id=dispatch_id,
            created_at=created_at,
            conversation_id=cursor.conversation_id,
            cursor_digest=cursor.cursor_digest,
            proposal=proposal,
            admission=admission,
            attention=attention,
            dispatch_digest=_digest(base),
        )

    @staticmethod
    def _validate_exact(
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        cursor: ChatPeerCursor,
        proposal: ContinuationProposal,
        admission: ContinuationAdmission,
        attention: DynamicAttentionDecision,
    ) -> None:
        if not isinstance(cursor, ChatPeerCursor):
            raise InvalidWorkRecordError("cursor must be a ChatPeerCursor")
        if not isinstance(proposal, ContinuationProposal):
            raise InvalidWorkRecordError("proposal must be a ContinuationProposal")
        if not isinstance(admission, ContinuationAdmission):
            raise InvalidWorkRecordError("admission must be a ContinuationAdmission")
        if not isinstance(attention, DynamicAttentionDecision):
            raise InvalidWorkRecordError("attention must be a DynamicAttentionDecision")
        admission.assert_binds(handoff, interpretation, proposal)
        attention.context.assert_binds(handoff, interpretation, proposal, admission)
        attention.assert_binds(attention.context)
        if not hmac.compare_digest(proposal.checkpoint_digest, cursor.cursor_digest):
            raise InvalidWorkRecordError(
                "Supervisor dispatch proposal is stale for the exact peer cursor"
            )
        if admission.decision is not ContinuationDecision.ADMIT:
            raise InvalidWorkRecordError(
                "Only an ADMIT continuation can become a supervisor dispatch"
            )
        if attention.disposition is not AttentionDisposition.KEEP_MOVING:
            raise InvalidWorkRecordError(
                "Human-attention work cannot become a supervisor dispatch"
            )

    def __post_init__(self) -> None:
        if self.schema_version != SUPERVISOR_DISPATCH_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported supervisor dispatch schema")
        _validate_uuid(self.dispatch_id, "dispatch_id")
        _validate_timestamp(self.created_at, "created_at")
        object.__setattr__(
            self,
            "conversation_id",
            _bounded_peer_id(self.conversation_id, "conversation_id"),
        )
        _validate_digest(self.cursor_digest, "cursor_digest")
        if not isinstance(self.proposal, ContinuationProposal):
            raise InvalidWorkRecordError("proposal must be a ContinuationProposal")
        if not isinstance(self.admission, ContinuationAdmission):
            raise InvalidWorkRecordError("admission must be a ContinuationAdmission")
        if not isinstance(self.attention, DynamicAttentionDecision):
            raise InvalidWorkRecordError("attention must be a DynamicAttentionDecision")
        if self.admission.decision is not ContinuationDecision.ADMIT:
            raise InvalidWorkRecordError(
                "Supervisor dispatch cannot bind a non-ADMIT admission"
            )
        if self.attention.disposition is not AttentionDisposition.KEEP_MOVING:
            raise InvalidWorkRecordError(
                "Supervisor dispatch cannot suppress required human attention"
            )
        if (
            self.admission.proposal_id != self.proposal.proposal_id
            or not hmac.compare_digest(
                self.admission.proposal_digest,
                self.proposal.proposal_digest,
            )
            or self.attention.context.proposal_id != self.proposal.proposal_id
            or not hmac.compare_digest(
                self.attention.context.proposal_digest,
                self.proposal.proposal_digest,
            )
            or self.attention.context.admission_id != self.admission.admission_id
            or not hmac.compare_digest(
                self.attention.context.admission_digest,
                self.admission.admission_digest,
            )
            or not hmac.compare_digest(
                self.proposal.checkpoint_digest,
                self.cursor_digest,
            )
            or not hmac.compare_digest(
                self.attention.context.checkpoint_digest,
                self.cursor_digest,
            )
        ):
            raise InvalidWorkRecordError(
                "Supervisor dispatch lost its exact proposal/admission/checkpoint binding"
            )
        _validate_digest(self.dispatch_digest, "dispatch_digest")
        if not hmac.compare_digest(self.dispatch_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "Supervisor dispatch digest does not match its exact intent"
            )

    def assert_binds(
        self,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        cursor: ChatPeerCursor,
    ) -> None:
        self._validate_exact(
            handoff=handoff,
            interpretation=interpretation,
            cursor=cursor,
            proposal=self.proposal,
            admission=self.admission,
            attention=self.attention,
        )
        if (
            self.conversation_id != cursor.conversation_id
            or not hmac.compare_digest(self.cursor_digest, cursor.cursor_digest)
        ):
            raise InvalidWorkRecordError(
                "Supervisor dispatch does not bind the exact current conversation cursor"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dispatch_id": self.dispatch_id,
            "created_at": self.created_at,
            "conversation_id": self.conversation_id,
            "cursor_digest": self.cursor_digest,
            "proposal": self.proposal.to_dict(),
            "admission": self.admission.to_dict(),
            "attention": self.attention.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "dispatch_digest": self.dispatch_digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SupervisorDispatch:
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "dispatch_id",
                "created_at",
                "conversation_id",
                "cursor_digest",
                "proposal",
                "admission",
                "attention",
                "dispatch_digest",
            },
            "SupervisorDispatch",
        )
        return cls(
            schema_version=value["schema_version"],
            dispatch_id=value["dispatch_id"],
            created_at=value["created_at"],
            conversation_id=value["conversation_id"],
            cursor_digest=value["cursor_digest"],
            proposal=ContinuationProposal.from_dict(value["proposal"]),
            admission=ContinuationAdmission.from_dict(value["admission"]),
            attention=DynamicAttentionDecision.from_dict(value["attention"]),
            dispatch_digest=value["dispatch_digest"],
        )


@dataclass(frozen=True, slots=True)
class SupervisorDispatchLease:
    """Ephemeral one-process claim. It is intentionally not recoverable."""

    work_id: str
    dispatch_digest: str
    claim_id: str

    def __post_init__(self) -> None:
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.dispatch_digest, "dispatch_digest")
        _validate_uuid(self.claim_id, "claim_id")


@dataclass(frozen=True, slots=True)
class SupervisorWorkSnapshot:
    work_id: str
    handoff: WorkHandoff
    interpretation: WorkIntentInterpretation
    cursor: ChatPeerCursor
    status: SupervisorStatus
    last_proposal: ContinuationProposal | None
    last_admission: ContinuationAdmission | None
    last_attention: DynamicAttentionDecision | None
    pending_dispatch: SupervisorDispatch | None
    in_flight_claim_id: str | None
    completion: WorkStatement | None
    last_sequence: int
    last_event_digest: str

    @property
    def needs_human(self) -> bool:
        return self.status is SupervisorStatus.WAITING_HUMAN

    @property
    def requires_reconciliation(self) -> bool:
        return self.status is SupervisorStatus.IN_FLIGHT


@dataclass(frozen=True, slots=True)
class _SupervisorEvent:
    schema_version: int
    event_id: str
    work_id: str
    sequence: int
    created_at: str
    kind: SupervisorEventKind
    payload: Mapping[str, Any]
    previous_digest: str | None
    event_digest: str

    @classmethod
    def create(
        cls,
        *,
        work_id: str,
        sequence: int,
        kind: SupervisorEventKind,
        payload: Mapping[str, Any],
        previous_digest: str | None,
    ) -> _SupervisorEvent:
        _validate_uuid(work_id, "work_id")
        if type(sequence) is not int or sequence < 0:
            raise InvalidWorkRecordError("sequence must be a non-negative integer")
        if not isinstance(payload, Mapping):
            raise InvalidWorkRecordError("Supervisor event payload must be an object")
        event_id = str(uuid4())
        created_at = _new_timestamp()
        payload = dict(payload)
        base = {
            "schema_version": SUPERVISOR_EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "work_id": work_id,
            "sequence": sequence,
            "created_at": created_at,
            "kind": kind.value,
            "payload": payload,
            "previous_digest": previous_digest,
        }
        return cls(
            schema_version=SUPERVISOR_EVENT_SCHEMA_VERSION,
            event_id=event_id,
            work_id=work_id,
            sequence=sequence,
            created_at=created_at,
            kind=kind,
            payload=payload,
            previous_digest=previous_digest,
            event_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != SUPERVISOR_EVENT_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported supervisor event schema")
        _validate_uuid(self.event_id, "event_id")
        _validate_uuid(self.work_id, "work_id")
        if type(self.sequence) is not int or self.sequence < 0:
            raise InvalidWorkRecordError("sequence must be a non-negative integer")
        _validate_timestamp(self.created_at, "created_at")
        try:
            kind = SupervisorEventKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise InvalidWorkRecordError("Unsupported supervisor event kind") from exc
        object.__setattr__(self, "kind", kind)
        if not isinstance(self.payload, Mapping):
            raise InvalidWorkRecordError("Supervisor event payload must be an object")
        object.__setattr__(self, "payload", dict(self.payload))
        if self.previous_digest is not None:
            _validate_digest(self.previous_digest, "previous_digest")
        _validate_digest(self.event_digest, "event_digest")
        if not hmac.compare_digest(self.event_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "Supervisor event digest does not match its exact transition"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "work_id": self.work_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "kind": self.kind.value,
            "payload": dict(self.payload),
            "previous_digest": self.previous_digest,
        }


class BackgroundWorkSupervisor:
    """Durable M6.5 work supervisor with explicit no-replay dispatch claims."""

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path).expanduser().resolve(strict=False)
        if path.exists() and not path.is_file():
            raise SupervisorPersistenceError(
                "Supervisor database path must be a regular file"
            )
        if not path.parent.exists():
            raise SupervisorPersistenceError(
                "Supervisor database parent directory must already exist"
            )
        self.database_path = path
        self._live_claims: dict[str, tuple[str, str]] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS work_supervisor_events (
                        work_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        event_id TEXT NOT NULL UNIQUE,
                        schema_version INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        previous_digest TEXT,
                        event_digest TEXT NOT NULL UNIQUE,
                        PRIMARY KEY (work_id, sequence)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS work_supervisor_heads (
                        work_id TEXT PRIMARY KEY,
                        terminal_sequence INTEGER NOT NULL,
                        terminal_digest TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def register(
        self,
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        cursor: ChatPeerCursor,
    ) -> SupervisorWorkSnapshot:
        if not isinstance(handoff, WorkHandoff):
            raise InvalidWorkRecordError("handoff must be a WorkHandoff")
        if not isinstance(interpretation, WorkIntentInterpretation):
            raise InvalidWorkRecordError(
                "interpretation must be a WorkIntentInterpretation"
            )
        if not isinstance(cursor, ChatPeerCursor):
            raise InvalidWorkRecordError("cursor must be a ChatPeerCursor")
        interpretation.assert_binds(handoff)
        work_id = handoff.handoff_id
        payload = {
            "handoff": handoff.to_dict(),
            "interpretation": interpretation.to_dict(),
            "cursor": cursor.to_dict(),
        }
        event = _SupervisorEvent.create(
            work_id=work_id,
            sequence=0,
            kind=SupervisorEventKind.REGISTERED,
            payload=payload,
            previous_digest=None,
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT 1 FROM work_supervisor_heads WHERE work_id = ?",
                    (work_id,),
                ).fetchone()
                if existing is not None:
                    raise SupervisorStateError(
                        "Delegated work is already registered with the supervisor"
                    )
                self._insert_event(connection, event)
                connection.execute(
                    """
                    INSERT INTO work_supervisor_heads(
                        work_id, terminal_sequence, terminal_digest
                    ) VALUES (?, ?, ?)
                    """,
                    (work_id, event.sequence, event.event_digest),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.recover(work_id)

    def record_checkpoint(
        self,
        work_id: str,
        *,
        proposal: ContinuationProposal,
        admission: ContinuationAdmission,
        attention: DynamicAttentionDecision,
    ) -> SupervisorWorkSnapshot:
        snapshot = self.recover(work_id)
        if snapshot.status is not SupervisorStatus.READY:
            raise SupervisorStateError(
                "A new checkpoint decision requires READY supervisor state"
            )
        self._validate_checkpoint(snapshot, proposal, admission, attention)
        dispatch: SupervisorDispatch | None = None
        if (
            attention.disposition is AttentionDisposition.KEEP_MOVING
            and admission.decision is ContinuationDecision.ADMIT
        ):
            dispatch = SupervisorDispatch.create(
                handoff=snapshot.handoff,
                interpretation=snapshot.interpretation,
                cursor=snapshot.cursor,
                proposal=proposal,
                admission=admission,
                attention=attention,
            )
        payload = {
            "proposal": proposal.to_dict(),
            "admission": admission.to_dict(),
            "attention": attention.to_dict(),
            "dispatch": None if dispatch is None else dispatch.to_dict(),
        }
        return self._append(
            snapshot,
            SupervisorEventKind.CHECKPOINT_DECIDED,
            payload,
        )

    def claim_dispatch(self, work_id: str) -> SupervisorDispatchLease:
        snapshot = self.recover(work_id)
        if (
            snapshot.status is not SupervisorStatus.PREPARED
            or snapshot.pending_dispatch is None
        ):
            raise SupervisorStateError(
                "Only a PREPARED dispatch can be claimed for one provider attempt"
            )
        claim_id = str(uuid4())
        updated = self._append(
            snapshot,
            SupervisorEventKind.DISPATCH_STARTED,
            {
                "dispatch_digest": snapshot.pending_dispatch.dispatch_digest,
                "claim_id": claim_id,
            },
        )
        if updated.status is not SupervisorStatus.IN_FLIGHT:
            raise SupervisorIntegrityError("Dispatch claim did not enter IN_FLIGHT state")
        self._live_claims[claim_id] = (
            updated.work_id,
            updated.pending_dispatch.dispatch_digest
            if updated.pending_dispatch is not None
            else "",
        )
        return SupervisorDispatchLease(
            work_id=updated.work_id,
            dispatch_digest=updated.pending_dispatch.dispatch_digest,
            claim_id=claim_id,
        )

    def execute_claimed_chat(
        self,
        lease: SupervisorDispatchLease,
        peer_loop: ChatGPTPeerLoop,
    ) -> SupervisorWorkSnapshot:
        if not isinstance(lease, SupervisorDispatchLease):
            raise SupervisorStateError("lease must be a SupervisorDispatchLease")
        if not isinstance(peer_loop, ChatGPTPeerLoop):
            raise SupervisorStateError("peer_loop must be a ChatGPTPeerLoop")
        live = self._live_claims.pop(lease.claim_id, None)
        if live != (lease.work_id, lease.dispatch_digest):
            raise SupervisorStateError(
                "Dispatch lease is not live in this supervisor process"
            )
        snapshot = self.recover(lease.work_id)
        dispatch = snapshot.pending_dispatch
        if (
            snapshot.status is not SupervisorStatus.IN_FLIGHT
            or dispatch is None
            or snapshot.in_flight_claim_id != lease.claim_id
            or not hmac.compare_digest(
                dispatch.dispatch_digest,
                lease.dispatch_digest,
            )
        ):
            raise SupervisorStateError(
                "Live dispatch lease no longer matches exact durable supervisor state"
            )
        dispatch.assert_binds(
            snapshot.handoff,
            snapshot.interpretation,
            snapshot.cursor,
        )
        turn = peer_loop.continue_admitted(
            cursor=snapshot.cursor,
            handoff=snapshot.handoff,
            interpretation=snapshot.interpretation,
            proposal=dispatch.proposal,
            admission=dispatch.admission,
        )
        return self.record_peer_turn(snapshot.work_id, turn=turn)

    def reconcile_in_flight_chat(
        self,
        work_id: str,
        *,
        peer_loop: ChatGPTPeerLoop,
    ) -> SupervisorWorkSnapshot:
        """Recover an already-attempted exact peer turn without ever resending it."""

        if not isinstance(peer_loop, ChatGPTPeerLoop):
            raise SupervisorStateError("peer_loop must be a ChatGPTPeerLoop")
        snapshot = self.recover(work_id)
        dispatch = snapshot.pending_dispatch
        if (
            snapshot.status is not SupervisorStatus.IN_FLIGHT
            or dispatch is None
        ):
            raise SupervisorStateError(
                "Only an IN_FLIGHT dispatch can be reconciled without replay"
            )
        dispatch.assert_binds(
            snapshot.handoff,
            snapshot.interpretation,
            snapshot.cursor,
        )
        current = peer_loop.provider.read_messages(snapshot.cursor.conversation_id)
        delta = ChatGPTPeerLoop._require_prefix(snapshot.cursor, current)
        if not delta:
            # Absence is not proof that the provider side effect did not occur.
            # Keep IN_FLIGHT rather than minting a retry permission.
            return snapshot

        expected_prompt = ChatGPTPeerLoop._render_codexia_continuation(
            handoff=snapshot.handoff,
            interpretation=snapshot.interpretation,
            proposal=dispatch.proposal,
            admission=dispatch.admission,
        )
        first = delta[0]
        if first.role != "user" or first.text != expected_prompt:
            raise SupervisorStateError(
                "In-flight reconciliation found activity other than the exact "
                "Codexia dispatch envelope"
            )
        if len(delta) == 1:
            # The provider accepted the Codexia message but no worker reply is
            # visible yet. A later reconciliation may complete the same turn.
            return snapshot
        second = delta[1]
        if second.role != "assistant":
            raise SupervisorStateError(
                "In-flight reconciliation found an intervening non-worker message"
            )

        captured_codexia = CapturedChatPeerMessage.capture(
            conversation_id=snapshot.cursor.conversation_id,
            message=first,
            origin=ChatPeerMessageOrigin.CODEXIA_SEND,
            actor=peer_loop.codexia_actor,
        )
        captured_worker = CapturedChatPeerMessage.capture(
            conversation_id=snapshot.cursor.conversation_id,
            message=second,
            origin=ChatPeerMessageOrigin.ASSISTANT,
            actor=peer_loop.worker_actor,
        )
        prefix_length = len(snapshot.cursor.message_fingerprints)
        reconciled_messages = current[: prefix_length + 2]
        after = ChatPeerCursor.from_messages(
            snapshot.cursor.conversation_id,
            reconciled_messages,
        )
        turn = ChatPeerTurn.create(
            admission=dispatch.admission,
            before=snapshot.cursor,
            codexia_message=captured_codexia,
            worker_message=captured_worker,
            after=after,
        )
        return self.record_peer_turn(snapshot.work_id, turn=turn)

    def record_peer_turn(
        self,
        work_id: str,
        *,
        turn: ChatPeerTurn,
    ) -> SupervisorWorkSnapshot:
        snapshot = self.recover(work_id)
        if (
            snapshot.status is not SupervisorStatus.IN_FLIGHT
            or snapshot.pending_dispatch is None
        ):
            raise SupervisorStateError(
                "A peer turn can only close an exact IN_FLIGHT dispatch"
            )
        self._validate_peer_turn(snapshot, turn)
        return self._append(
            snapshot,
            SupervisorEventKind.PEER_TURN_RECORDED,
            {
                "dispatch_digest": snapshot.pending_dispatch.dispatch_digest,
                "turn": turn.to_dict(),
            },
        )

    def record_external_observation(
        self,
        work_id: str,
        *,
        observation: ChatPeerObservation,
    ) -> SupervisorWorkSnapshot:
        snapshot = self.recover(work_id)
        if snapshot.status in {
            SupervisorStatus.IN_FLIGHT,
            SupervisorStatus.COMPLETED,
        }:
            raise SupervisorStateError(
                "External observation cannot bypass in-flight reconciliation or completion"
            )
        self._validate_external_observation(snapshot, observation)
        return self._append(
            snapshot,
            SupervisorEventKind.EXTERNAL_OBSERVED,
            {"observation": observation.to_dict()},
        )

    def complete(
        self,
        work_id: str,
        *,
        completion: WorkStatement,
    ) -> SupervisorWorkSnapshot:
        snapshot = self.recover(work_id)
        if snapshot.status is not SupervisorStatus.READY:
            raise SupervisorStateError(
                "Work can only be completed from an exact READY checkpoint"
            )
        if not isinstance(completion, WorkStatement):
            raise InvalidWorkRecordError("completion must be a WorkStatement")
        if completion.author_kind is not WorkActorKind.CODEXIA:
            raise InvalidWorkRecordError(
                "Supervisor completion must preserve explicit Codexia authorship"
            )
        return self._append(
            snapshot,
            SupervisorEventKind.COMPLETED,
            {"completion": completion.to_dict()},
        )

    def recover(self, work_id: str) -> SupervisorWorkSnapshot:
        _validate_uuid(work_id, "work_id")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                rows = connection.execute(
                    """
                    SELECT work_id, sequence, event_id, schema_version, created_at,
                           kind, payload_json, previous_digest, event_digest
                    FROM work_supervisor_events
                    WHERE work_id = ?
                    ORDER BY sequence ASC
                    """,
                    (work_id,),
                ).fetchall()
                head = connection.execute(
                    """
                    SELECT terminal_sequence, terminal_digest
                    FROM work_supervisor_heads
                    WHERE work_id = ?
                    """,
                    (work_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if not rows or head is None:
            raise SupervisorStateError("Delegated work is not registered")
        events = tuple(self._event_from_row(row) for row in rows)
        snapshot: SupervisorWorkSnapshot | None = None
        previous: str | None = None
        for expected_sequence, event in enumerate(events):
            if event.sequence != expected_sequence:
                raise SupervisorIntegrityError(
                    "Supervisor event sequence is not contiguous"
                )
            if event.previous_digest != previous:
                raise SupervisorIntegrityError(
                    "Supervisor event chain previous digest mismatch"
                )
            snapshot = self._apply_event(snapshot, event)
            previous = event.event_digest
        assert snapshot is not None
        if (
            head["terminal_sequence"] != snapshot.last_sequence
            or not hmac.compare_digest(
                head["terminal_digest"],
                snapshot.last_event_digest,
            )
        ):
            raise SupervisorIntegrityError(
                "Supervisor head metadata does not match the recovered event chain"
            )
        return snapshot

    def list_active(self) -> tuple[SupervisorWorkSnapshot, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT work_id FROM work_supervisor_heads ORDER BY work_id ASC"
            ).fetchall()
        recovered = tuple(self.recover(row["work_id"]) for row in rows)
        return tuple(
            item for item in recovered if item.status is not SupervisorStatus.COMPLETED
        )

    def _append(
        self,
        snapshot: SupervisorWorkSnapshot,
        kind: SupervisorEventKind,
        payload: Mapping[str, Any],
    ) -> SupervisorWorkSnapshot:
        event = _SupervisorEvent.create(
            work_id=snapshot.work_id,
            sequence=snapshot.last_sequence + 1,
            kind=kind,
            payload=payload,
            previous_digest=snapshot.last_event_digest,
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                head = connection.execute(
                    """
                    SELECT terminal_sequence, terminal_digest
                    FROM work_supervisor_heads
                    WHERE work_id = ?
                    """,
                    (snapshot.work_id,),
                ).fetchone()
                if head is None:
                    raise SupervisorIntegrityError(
                        "Supervisor head disappeared during transition"
                    )
                if (
                    head["terminal_sequence"] != snapshot.last_sequence
                    or not hmac.compare_digest(
                        head["terminal_digest"],
                        snapshot.last_event_digest,
                    )
                ):
                    raise SupervisorConcurrencyError(
                        "Delegated work advanced concurrently; recover before retrying"
                    )
                self._insert_event(connection, event)
                changed = connection.execute(
                    """
                    UPDATE work_supervisor_heads
                    SET terminal_sequence = ?, terminal_digest = ?
                    WHERE work_id = ?
                      AND terminal_sequence = ?
                      AND terminal_digest = ?
                    """,
                    (
                        event.sequence,
                        event.event_digest,
                        snapshot.work_id,
                        snapshot.last_sequence,
                        snapshot.last_event_digest,
                    ),
                ).rowcount
                if changed != 1:
                    raise SupervisorConcurrencyError(
                        "Supervisor head changed during exact transition"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.recover(snapshot.work_id)

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        event: _SupervisorEvent,
    ) -> None:
        connection.execute(
            """
            INSERT INTO work_supervisor_events(
                work_id, sequence, event_id, schema_version, created_at,
                kind, payload_json, previous_digest, event_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.work_id,
                event.sequence,
                event.event_id,
                event.schema_version,
                event.created_at,
                event.kind.value,
                _canonical_json(dict(event.payload)),
                event.previous_digest,
                event.event_digest,
            ),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> _SupervisorEvent:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SupervisorIntegrityError(
                "Supervisor event payload is not valid JSON"
            ) from exc
        try:
            event = _SupervisorEvent(
                schema_version=row["schema_version"],
                event_id=row["event_id"],
                work_id=row["work_id"],
                sequence=row["sequence"],
                created_at=row["created_at"],
                kind=row["kind"],
                payload=payload,
                previous_digest=row["previous_digest"],
                event_digest=row["event_digest"],
            )
        except InvalidWorkRecordError as exc:
            raise SupervisorIntegrityError(
                "Supervisor event failed canonical integrity validation"
            ) from exc
        return event

    def _apply_event(
        self,
        snapshot: SupervisorWorkSnapshot | None,
        event: _SupervisorEvent,
    ) -> SupervisorWorkSnapshot:
        try:
            if event.kind is SupervisorEventKind.REGISTERED:
                if snapshot is not None or event.sequence != 0:
                    raise SupervisorIntegrityError(
                        "REGISTERED must be the first supervisor event"
                    )
                value = _exact_keys(
                    event.payload,
                    {"handoff", "interpretation", "cursor"},
                    "Supervisor REGISTERED payload",
                )
                handoff = WorkHandoff.from_dict(value["handoff"])
                interpretation = WorkIntentInterpretation.from_dict(
                    value["interpretation"]
                )
                interpretation.assert_binds(handoff)
                cursor = _cursor_from_dict(value["cursor"])
                if event.work_id != handoff.handoff_id:
                    raise SupervisorIntegrityError(
                        "Supervisor work identity does not match exact handoff"
                    )
                return SupervisorWorkSnapshot(
                    work_id=event.work_id,
                    handoff=handoff,
                    interpretation=interpretation,
                    cursor=cursor,
                    status=SupervisorStatus.READY,
                    last_proposal=None,
                    last_admission=None,
                    last_attention=None,
                    pending_dispatch=None,
                    in_flight_claim_id=None,
                    completion=None,
                    last_sequence=event.sequence,
                    last_event_digest=event.event_digest,
                )

            if snapshot is None:
                raise SupervisorIntegrityError(
                    "Supervisor history has a transition before registration"
                )
            if event.work_id != snapshot.work_id:
                raise SupervisorIntegrityError(
                    "Supervisor event crossed delegated-work identity"
                )

            if event.kind is SupervisorEventKind.CHECKPOINT_DECIDED:
                if snapshot.status is not SupervisorStatus.READY:
                    raise SupervisorIntegrityError(
                        "CHECKPOINT_DECIDED requires READY state"
                    )
                value = _exact_keys(
                    event.payload,
                    {"proposal", "admission", "attention", "dispatch"},
                    "Supervisor CHECKPOINT_DECIDED payload",
                )
                proposal = ContinuationProposal.from_dict(value["proposal"])
                admission = ContinuationAdmission.from_dict(value["admission"])
                attention = DynamicAttentionDecision.from_dict(value["attention"])
                self._validate_checkpoint(
                    snapshot,
                    proposal,
                    admission,
                    attention,
                )
                dispatch_value = value["dispatch"]
                dispatch = (
                    None
                    if dispatch_value is None
                    else SupervisorDispatch.from_dict(dispatch_value)
                )
                if attention.disposition is AttentionDisposition.ASK_HUMAN:
                    if dispatch is not None:
                        raise SupervisorIntegrityError(
                            "Human-attention checkpoint cannot carry a dispatch"
                        )
                    status = SupervisorStatus.WAITING_HUMAN
                elif admission.decision is ContinuationDecision.ADMIT:
                    if dispatch is None:
                        raise SupervisorIntegrityError(
                            "ADMIT + KEEP_MOVING checkpoint lost its dispatch"
                        )
                    dispatch.assert_binds(
                        snapshot.handoff,
                        snapshot.interpretation,
                        snapshot.cursor,
                    )
                    if (
                        dispatch.proposal.proposal_digest != proposal.proposal_digest
                        or dispatch.admission.admission_digest
                        != admission.admission_digest
                        or dispatch.attention.decision_digest
                        != attention.decision_digest
                    ):
                        raise SupervisorIntegrityError(
                            "Supervisor dispatch differs from checkpoint decision"
                        )
                    status = SupervisorStatus.PREPARED
                else:
                    if dispatch is not None:
                        raise SupervisorIntegrityError(
                            "Non-ADMIT checkpoint cannot carry a provider dispatch"
                        )
                    status = SupervisorStatus.READY
                return replace(
                    snapshot,
                    status=status,
                    last_proposal=proposal,
                    last_admission=admission,
                    last_attention=attention,
                    pending_dispatch=dispatch,
                    in_flight_claim_id=None,
                    last_sequence=event.sequence,
                    last_event_digest=event.event_digest,
                )

            if event.kind is SupervisorEventKind.DISPATCH_STARTED:
                if (
                    snapshot.status is not SupervisorStatus.PREPARED
                    or snapshot.pending_dispatch is None
                ):
                    raise SupervisorIntegrityError(
                        "DISPATCH_STARTED requires PREPARED state"
                    )
                value = _exact_keys(
                    event.payload,
                    {"dispatch_digest", "claim_id"},
                    "Supervisor DISPATCH_STARTED payload",
                )
                _validate_digest(value["dispatch_digest"], "dispatch_digest")
                _validate_uuid(value["claim_id"], "claim_id")
                if not hmac.compare_digest(
                    value["dispatch_digest"],
                    snapshot.pending_dispatch.dispatch_digest,
                ):
                    raise SupervisorIntegrityError(
                        "Dispatch claim does not bind exact pending dispatch"
                    )
                return replace(
                    snapshot,
                    status=SupervisorStatus.IN_FLIGHT,
                    in_flight_claim_id=value["claim_id"],
                    last_sequence=event.sequence,
                    last_event_digest=event.event_digest,
                )

            if event.kind is SupervisorEventKind.PEER_TURN_RECORDED:
                if (
                    snapshot.status is not SupervisorStatus.IN_FLIGHT
                    or snapshot.pending_dispatch is None
                ):
                    raise SupervisorIntegrityError(
                        "PEER_TURN_RECORDED requires IN_FLIGHT state"
                    )
                value = _exact_keys(
                    event.payload,
                    {"dispatch_digest", "turn"},
                    "Supervisor PEER_TURN_RECORDED payload",
                )
                _validate_digest(value["dispatch_digest"], "dispatch_digest")
                if not hmac.compare_digest(
                    value["dispatch_digest"],
                    snapshot.pending_dispatch.dispatch_digest,
                ):
                    raise SupervisorIntegrityError(
                        "Peer turn closes a different pending dispatch"
                    )
                turn = _turn_from_dict(value["turn"])
                self._validate_peer_turn(snapshot, turn)
                return replace(
                    snapshot,
                    cursor=turn.after_cursor,
                    status=SupervisorStatus.READY,
                    pending_dispatch=None,
                    in_flight_claim_id=None,
                    last_sequence=event.sequence,
                    last_event_digest=event.event_digest,
                )

            if event.kind is SupervisorEventKind.EXTERNAL_OBSERVED:
                if snapshot.status in {
                    SupervisorStatus.IN_FLIGHT,
                    SupervisorStatus.COMPLETED,
                }:
                    raise SupervisorIntegrityError(
                        "EXTERNAL_OBSERVED cannot bypass in-flight/completed state"
                    )
                value = _exact_keys(
                    event.payload,
                    {"observation"},
                    "Supervisor EXTERNAL_OBSERVED payload",
                )
                observation = _observation_from_dict(value["observation"])
                self._validate_external_observation(snapshot, observation)
                return replace(
                    snapshot,
                    cursor=observation.after_cursor,
                    status=SupervisorStatus.READY,
                    last_proposal=None,
                    last_admission=None,
                    last_attention=None,
                    pending_dispatch=None,
                    in_flight_claim_id=None,
                    last_sequence=event.sequence,
                    last_event_digest=event.event_digest,
                )

            if event.kind is SupervisorEventKind.COMPLETED:
                if snapshot.status is not SupervisorStatus.READY:
                    raise SupervisorIntegrityError("COMPLETED requires READY state")
                value = _exact_keys(
                    event.payload,
                    {"completion"},
                    "Supervisor COMPLETED payload",
                )
                completion = WorkStatement.from_dict(value["completion"])
                if completion.author_kind is not WorkActorKind.CODEXIA:
                    raise SupervisorIntegrityError(
                        "Completion must preserve Codexia authorship"
                    )
                return replace(
                    snapshot,
                    status=SupervisorStatus.COMPLETED,
                    completion=completion,
                    last_sequence=event.sequence,
                    last_event_digest=event.event_digest,
                )

            raise SupervisorIntegrityError("Unsupported supervisor event kind")
        except InvalidWorkRecordError as exc:
            raise SupervisorIntegrityError(
                f"Supervisor event {event.sequence} failed exact record validation"
            ) from exc

    @staticmethod
    def _validate_checkpoint(
        snapshot: SupervisorWorkSnapshot,
        proposal: ContinuationProposal,
        admission: ContinuationAdmission,
        attention: DynamicAttentionDecision,
    ) -> None:
        admission.assert_binds(
            snapshot.handoff,
            snapshot.interpretation,
            proposal,
        )
        attention.context.assert_binds(
            snapshot.handoff,
            snapshot.interpretation,
            proposal,
            admission,
        )
        attention.assert_binds(attention.context)
        if not hmac.compare_digest(
            proposal.checkpoint_digest,
            snapshot.cursor.cursor_digest,
        ):
            raise InvalidWorkRecordError(
                "Supervisor checkpoint decision is stale for current peer cursor"
            )
        if (
            attention.disposition is AttentionDisposition.KEEP_MOVING
            and admission.decision is ContinuationDecision.ASK_HUMAN
        ):
            raise InvalidWorkRecordError(
                "M6.2 ASK_HUMAN cannot be suppressed by supervisor state"
            )

    @staticmethod
    def _validate_peer_turn(
        snapshot: SupervisorWorkSnapshot,
        turn: ChatPeerTurn,
    ) -> None:
        if not isinstance(turn, ChatPeerTurn):
            raise InvalidWorkRecordError("turn must be a ChatPeerTurn")
        dispatch = snapshot.pending_dispatch
        if dispatch is None:
            raise InvalidWorkRecordError("No exact pending dispatch exists")
        if (
            turn.admission_id != dispatch.admission.admission_id
            or not hmac.compare_digest(
                turn.admission_digest,
                dispatch.admission.admission_digest,
            )
            or turn.handoff_id != snapshot.handoff.handoff_id
            or not hmac.compare_digest(
                turn.handoff_digest,
                snapshot.handoff.handoff_digest,
            )
            or turn.interpretation_id != snapshot.interpretation.interpretation_id
            or not hmac.compare_digest(
                turn.interpretation_digest,
                snapshot.interpretation.interpretation_digest,
            )
            or not hmac.compare_digest(
                turn.before_cursor_digest,
                snapshot.cursor.cursor_digest,
            )
            or turn.after_cursor.conversation_id != snapshot.cursor.conversation_id
        ):
            raise InvalidWorkRecordError(
                "Peer turn does not close the exact pending supervisor dispatch"
            )
        expected_prompt = ChatGPTPeerLoop._render_codexia_continuation(
            handoff=snapshot.handoff,
            interpretation=snapshot.interpretation,
            proposal=dispatch.proposal,
            admission=dispatch.admission,
        )
        if turn.codexia_message.statement.text != expected_prompt:
            raise InvalidWorkRecordError(
                "Peer turn Codexia message differs from exact pending dispatch envelope"
            )

    @staticmethod
    def _validate_external_observation(
        snapshot: SupervisorWorkSnapshot,
        observation: ChatPeerObservation,
    ) -> None:
        if not isinstance(observation, ChatPeerObservation):
            raise InvalidWorkRecordError(
                "observation must be a ChatPeerObservation"
            )
        if not observation.messages:
            raise InvalidWorkRecordError(
                "Supervisor external observation must advance the conversation"
            )
        if not hmac.compare_digest(
            observation.before_cursor_digest,
            snapshot.cursor.cursor_digest,
        ):
            raise InvalidWorkRecordError(
                "External observation is stale for exact supervisor cursor"
            )
        if observation.after_cursor.conversation_id != snapshot.cursor.conversation_id:
            raise InvalidWorkRecordError(
                "External observation changed conversation identity"
            )
        if snapshot.status is SupervisorStatus.WAITING_HUMAN and not any(
            item.origin is ChatPeerMessageOrigin.EXTERNAL_USER
            for item in observation.messages
        ):
            raise InvalidWorkRecordError(
                "WAITING_HUMAN work requires an observed external human turn to resume"
            )
