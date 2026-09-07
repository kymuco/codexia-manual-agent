from __future__ import annotations

import hmac
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

from codexia_manual_agent.lab.errors import (
    EvidenceBindingError,
    InvalidLabRecordError,
    LabError,
    LabIdentityConflictError,
    LabPersistenceError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
)
from codexia_manual_agent.lab.execution_evidence import (
    RunExecutionAuthorization,
    RunExecutionBinding,
    RunExecutionEvidence,
    run_execution_authorization_from_dict,
    run_execution_binding_from_dict,
    run_execution_evidence_from_dict,
)
from codexia_manual_agent.lab.registry import RegisteredRunSnapshot, SqliteLabRegistry
from codexia_manual_agent.session_events import (
    ActionRecoveryState,
    EventKind,
    SessionEventError,
    SqliteSessionEventStore,
)
from codexia_manual_agent.session_events.recovery import RecoveredAction, SessionRecovery


RUN_EXECUTION_EVENT_SCHEMA_VERSION = 1
MAX_RUN_EXECUTION_EVENT_PAYLOAD_BYTES = 2_097_152
MAX_RUN_EXECUTION_EVENT_RAW_JSON_CHARS = MAX_RUN_EXECUTION_EVENT_PAYLOAD_BYTES
MAX_RUN_EXECUTION_EVENT_TIMESTAMP_CHARS = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RunExecutionEventKind(StrEnum):
    BINDING_REGISTERED = "binding_registered"
    AUTHORIZATION_REGISTERED = "authorization_registered"
    EVIDENCE_REGISTERED = "evidence_registered"


class RunExecutionPhase(StrEnum):
    BOUND = "bound"
    AUTHORIZED = "authorized"
    OBSERVED = "observed"


def _normalize_kind(value: Any) -> RunExecutionEventKind:
    try:
        return RunExecutionEventKind(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError("Unknown M4.3.1 run execution event kind") from exc


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidLabRecordError(
            f"{field_name} must use canonical lowercase hyphenated UUID form"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_RUN_EXECUTION_EVENT_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidLabRecordError(
            f"{field_name} must be bounded canonical timezone-aware ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidLabRecordError(
            f"{field_name} must be canonical timezone-aware ISO-8601"
        )
    return value


def _timestamp(value: str, field_name: str) -> datetime:
    return datetime.fromisoformat(_validate_timestamp(value, field_name))


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LabPersistenceIntegrityError(
                f"Persisted run execution JSON contains duplicate key: {key}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise LabPersistenceIntegrityError(
        f"Persisted run execution JSON contains non-finite constant: {value}"
    )


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidLabRecordError(
            "Run execution event payload must be bounded JSON-compatible data"
        ) from exc


def _load_canonical_json(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise LabPersistenceIntegrityError("Persisted run execution JSON must be text")
    if len(raw) > MAX_RUN_EXECUTION_EVENT_RAW_JSON_CHARS:
        raise LabPersistenceIntegrityError(
            "Persisted run execution JSON exceeds the pre-parse character budget"
        )
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except LabPersistenceIntegrityError:
        raise
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise LabPersistenceIntegrityError(
            "Persisted run execution event payload is not valid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise LabPersistenceIntegrityError(
            "Persisted run execution event payload must be an object"
        )
    try:
        canonical = _canonical_json(value)
    except InvalidLabRecordError as exc:
        raise LabPersistenceIntegrityError(
            "Persisted run execution JSON violates canonical bounds"
        ) from exc
    if canonical != raw:
        raise LabPersistenceIntegrityError(
            "Persisted run execution event payload is not canonical JSON"
        )
    return value


def _exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidLabRecordError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise InvalidLabRecordError(
            f"{label} keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _validate_m3_anchor(value: Any) -> Mapping[str, Any]:
    anchor = _exact_keys(
        value,
        {"session_id", "event_id", "event_digest"},
        "m3 observation anchor",
    )
    _validate_uuid(anchor["session_id"], "m3 anchor session_id")
    _validate_uuid(anchor["event_id"], "m3 anchor event_id")
    _validate_digest(anchor["event_digest"], "m3 anchor event_digest")
    return anchor


def _validate_payload(
    kind: RunExecutionEventKind | str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    normalized = _normalize_kind(kind)
    if normalized is RunExecutionEventKind.BINDING_REGISTERED:
        value = _exact_keys(
            payload,
            {"binding", "m3_session_id"},
            normalized.value,
        )
        run_execution_binding_from_dict(value["binding"])
        _validate_uuid(value["m3_session_id"], "m3_session_id")
    elif normalized is RunExecutionEventKind.AUTHORIZATION_REGISTERED:
        value = _exact_keys(payload, {"authorization"}, normalized.value)
        run_execution_authorization_from_dict(value["authorization"])
    elif normalized is RunExecutionEventKind.EVIDENCE_REGISTERED:
        value = _exact_keys(
            payload,
            {"evidence", "m3_observation_anchor"},
            normalized.value,
        )
        run_execution_evidence_from_dict(value["evidence"])
        _validate_m3_anchor(value["m3_observation_anchor"])
    else:  # pragma: no cover - enum exhaustiveness
        raise InvalidLabRecordError("Unknown M4.3.1 run execution event kind")
    if len(_canonical_json(payload).encode("utf-8")) > MAX_RUN_EXECUTION_EVENT_PAYLOAD_BYTES:
        raise InvalidLabRecordError("Run execution event payload exceeds M4.3.1 byte budget")
    return payload


def _event_digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RunExecutionEventReceipt:
    schema_version: int
    event_id: str
    run_id: str
    sequence: int
    created_at: str
    kind: RunExecutionEventKind
    payload: Mapping[str, Any]
    previous_event_digest: str | None
    event_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        sequence: int,
        kind: RunExecutionEventKind | str,
        payload: Mapping[str, Any],
        previous_event_digest: str | None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> "RunExecutionEventReceipt":
        event_id = event_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        normalized_kind = _normalize_kind(kind)
        _validate_uuid(event_id, "event_id")
        _validate_uuid(run_id, "run_id")
        if type(sequence) is not int or sequence < 0:
            raise InvalidLabRecordError("Run execution event sequence must be non-negative")
        _validate_timestamp(created_at, "created_at")
        if sequence == 0:
            if previous_event_digest is not None:
                raise InvalidLabRecordError("First run execution event cannot have a previous digest")
        else:
            _validate_digest(previous_event_digest, "previous_event_digest")
        _validate_payload(normalized_kind, payload)
        base = {
            "schema_version": RUN_EXECUTION_EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "run_id": run_id,
            "sequence": sequence,
            "created_at": created_at,
            "kind": normalized_kind.value,
            "payload": payload,
            "previous_event_digest": previous_event_digest,
        }
        return cls(
            schema_version=RUN_EXECUTION_EVENT_SCHEMA_VERSION,
            event_id=event_id,
            run_id=run_id,
            sequence=sequence,
            created_at=created_at,
            kind=normalized_kind,
            payload=payload,
            previous_event_digest=previous_event_digest,
            event_digest=_event_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != RUN_EXECUTION_EVENT_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M4.3.1 run execution event schema")
        _validate_uuid(self.event_id, "event_id")
        _validate_uuid(self.run_id, "run_id")
        if type(self.sequence) is not int or self.sequence < 0:
            raise InvalidLabRecordError("Run execution event sequence must be non-negative")
        _validate_timestamp(self.created_at, "created_at")
        kind = _normalize_kind(self.kind)
        if self.sequence == 0:
            if self.previous_event_digest is not None:
                raise InvalidLabRecordError("First run execution event cannot have a previous digest")
        else:
            _validate_digest(self.previous_event_digest, "previous_event_digest")
        _validate_payload(kind, self.payload)
        _validate_digest(self.event_digest, "event_digest")
        object.__setattr__(self, "kind", kind)
        if not hmac.compare_digest(_event_digest(self._base_payload()), self.event_digest):
            raise InvalidLabRecordError("Run execution event digest does not match payload")

    def _base_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "kind": self.kind.value,
            "payload": self.payload,
            "previous_event_digest": self.previous_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_payload(), "event_digest": self.event_digest}


@dataclass(slots=True)
class _ReplayState:
    binding: RunExecutionBinding
    m3_session_id: str
    authorization: RunExecutionAuthorization | None = None
    evidence: RunExecutionEvidence | None = None
    m3_observation_event_id: str | None = None
    m3_observation_event_digest: str | None = None


@dataclass(frozen=True, slots=True)
class RunExecutionRecovery:
    run: RegisteredRunSnapshot
    binding: RunExecutionBinding
    m3_session_id: str
    authorization: RunExecutionAuthorization | None
    evidence: RunExecutionEvidence | None
    m3_observation_event_id: str | None
    m3_observation_event_digest: str | None
    events: tuple[RunExecutionEventReceipt, ...]

    @property
    def phase(self) -> RunExecutionPhase:
        if self.evidence is not None:
            return RunExecutionPhase.OBSERVED
        if self.authorization is not None:
            return RunExecutionPhase.AUTHORIZED
        return RunExecutionPhase.BOUND

    @property
    def execution_succeeded(self) -> bool:
        return self.evidence is not None and self.evidence.execution_succeeded


def _apply_event(
    state: _ReplayState | None,
    kind: RunExecutionEventKind,
    payload: Mapping[str, Any],
    *,
    run: RegisteredRunSnapshot,
) -> _ReplayState:
    value = _validate_payload(kind, payload)
    if kind is RunExecutionEventKind.BINDING_REGISTERED:
        if state is not None:
            raise LabRegistryStateError("binding_registered can appear only once")
        binding = run_execution_binding_from_dict(value["binding"])
        if binding.run.to_dict() != run.run.to_dict():
            raise EvidenceBindingError("Execution binding does not bind the exact durable M4 run")
        return _ReplayState(
            binding=binding,
            m3_session_id=_validate_uuid(value["m3_session_id"], "m3_session_id"),
        )
    if state is None:
        raise LabRegistryStateError("Run execution chronology must start with binding_registered")

    binding = state.binding
    if kind is RunExecutionEventKind.AUTHORIZATION_REGISTERED:
        if state.authorization is not None or state.evidence is not None:
            raise LabRegistryStateError("Run execution authorization transition is not fresh")
        authorization = run_execution_authorization_from_dict(value["authorization"])
        if (
            authorization.binding_id != binding.binding_id
            or not hmac.compare_digest(authorization.binding_digest, binding.binding_digest)
            or authorization.proposal_id != binding.proposal.proposal_id
            or not hmac.compare_digest(
                authorization.proposal_digest,
                binding.proposal.proposal_digest,
            )
        ):
            raise EvidenceBindingError(
                "Authorization does not bind the exact durable run execution binding"
            )
        state.authorization = authorization
        return state

    if kind is RunExecutionEventKind.EVIDENCE_REGISTERED:
        if state.authorization is None:
            raise LabRegistryStateError(
                "Execution evidence requires a durable authorization event first"
            )
        if state.evidence is not None:
            raise LabRegistryStateError("Run execution evidence is already registered")
        evidence = run_execution_evidence_from_dict(value["evidence"])
        authorization = state.authorization
        if (
            evidence.binding_id != binding.binding_id
            or not hmac.compare_digest(evidence.binding_digest, binding.binding_digest)
            or evidence.authorization_id != authorization.authorization_id
            or not hmac.compare_digest(
                evidence.authorization_digest,
                authorization.authorization_digest,
            )
            or evidence.run_id != binding.run.run_id
            or evidence.experiment_id != binding.run.experiment_id
            or not hmac.compare_digest(evidence.run_digest, binding.run.run_digest)
            or not hmac.compare_digest(evidence.manifest_digest, binding.run.manifest_digest)
            or evidence.proposal_id != binding.proposal.proposal_id
            or not hmac.compare_digest(evidence.proposal_digest, binding.proposal.proposal_digest)
            or evidence.receipt_id != authorization.receipt.receipt_id
            or not hmac.compare_digest(
                evidence.receipt_digest,
                authorization.receipt.receipt_digest,
            )
        ):
            raise EvidenceBindingError(
                "Execution evidence does not bind the exact durable run/authorization chain"
            )
        anchor = _validate_m3_anchor(value["m3_observation_anchor"])
        if anchor["session_id"] != state.m3_session_id:
            raise EvidenceBindingError("M3 observation anchor belongs to another session")
        state.evidence = evidence
        state.m3_observation_event_id = str(anchor["event_id"])
        state.m3_observation_event_digest = str(anchor["event_digest"])
        return state

    raise InvalidLabRecordError("Unknown M4.3.1 run execution event kind")


def _validate_temporal_chain(
    state: _ReplayState,
    events: tuple[RunExecutionEventReceipt, ...],
) -> None:
    if not events or events[0].kind is not RunExecutionEventKind.BINDING_REGISTERED:
        raise EvidenceBindingError("Run execution chronology has no durable pre-execution binding")
    binding_event = events[0]
    proposal_at = _timestamp(state.binding.proposal.created_at, "proposal.created_at")
    binding_record_at = _timestamp(state.binding.created_at, "binding.created_at")
    binding_event_at = _timestamp(binding_event.created_at, "binding event created_at")
    if not proposal_at <= binding_record_at <= binding_event_at:
        raise EvidenceBindingError(
            "Run execution binding must be created after its proposal and durably registered before later authority"
        )

    if state.authorization is None:
        if len(events) != 1:
            raise EvidenceBindingError("Bound run execution chronology has unexpected later events")
        return
    if len(events) < 2 or events[1].kind is not RunExecutionEventKind.AUTHORIZATION_REGISTERED:
        raise EvidenceBindingError("Run execution authorization lacks its exact durable event")
    authorization_event = events[1]
    receipt_at = _timestamp(
        state.authorization.receipt.created_at,
        "authorization receipt created_at",
    )
    authorization_record_at = _timestamp(
        state.authorization.created_at,
        "run execution authorization created_at",
    )
    authorization_event_at = _timestamp(
        authorization_event.created_at,
        "authorization event created_at",
    )
    if not binding_event_at <= receipt_at <= authorization_record_at <= authorization_event_at:
        raise EvidenceBindingError(
            "Run execution binding must be durable before authorization is issued and recorded"
        )

    if state.evidence is None:
        if len(events) != 2:
            raise EvidenceBindingError("Authorized run execution chronology has unexpected later events")
        return
    if len(events) != 3 or events[2].kind is not RunExecutionEventKind.EVIDENCE_REGISTERED:
        raise EvidenceBindingError("Run execution observation lacks its exact durable evidence event")
    evidence_event = events[2]
    observation_at = _timestamp(
        state.evidence.observation.created_at,
        "process observation created_at",
    )
    evidence_record_at = _timestamp(
        state.evidence.created_at,
        "run execution evidence created_at",
    )
    evidence_event_at = _timestamp(
        evidence_event.created_at,
        "evidence event created_at",
    )
    if not authorization_event_at <= observation_at <= evidence_record_at <= evidence_event_at:
        raise EvidenceBindingError(
            "Run execution authorization must be durable before execution is observed; evidence must be recorded afterward"
        )


def _exact_m3_action(recovery: SessionRecovery, proposal: Any) -> RecoveredAction:
    matches = [
        action
        for action in recovery.actions
        if action.proposal.proposal_id == proposal.proposal_id
    ]
    if len(matches) != 1 or matches[0].proposal.to_dict() != proposal.to_dict():
        raise EvidenceBindingError(
            "M4.3.1 requires one exact durable M3 action proposal for the bound process"
        )
    return matches[0]


def _recover_m3(
    store: SqliteSessionEventStore,
    session_id: str,
) -> SessionRecovery:
    try:
        return store.recover(session_id)
    except SessionEventError as exc:
        raise EvidenceBindingError(
            "M4.3.1 could not validate the authoritative M3 action chronology"
        ) from exc


def _require_m3_binding_candidate(
    store: SqliteSessionEventStore,
    *,
    session_id: str,
    binding: RunExecutionBinding,
) -> None:
    action = _exact_m3_action(_recover_m3(store, session_id), binding.proposal)
    if action.state is not ActionRecoveryState.PROPOSED:
        raise EvidenceBindingError(
            "M4 execution binding must be registered while the exact M3 action is only PROPOSED"
        )


def _require_m3_authorization_candidate(
    store: SqliteSessionEventStore,
    *,
    session_id: str,
    binding: RunExecutionBinding,
    authorization: RunExecutionAuthorization,
) -> None:
    action = _exact_m3_action(_recover_m3(store, session_id), binding.proposal)
    if (
        action.state is not ActionRecoveryState.AUTHORIZED_UNCONSUMED
        or action.receipt is None
        or action.receipt.to_dict() != authorization.receipt.to_dict()
    ):
        raise EvidenceBindingError(
            "M4 authorization requires the exact M3 ALLOW receipt before one-shot consumption"
        )


def _m3_observation_anchor(
    store: SqliteSessionEventStore,
    *,
    session_id: str,
    binding: RunExecutionBinding,
    authorization: RunExecutionAuthorization,
    evidence: RunExecutionEvidence,
) -> dict[str, str]:
    recovery = _recover_m3(store, session_id)
    action = _exact_m3_action(recovery, binding.proposal)
    observation = evidence.observation
    if (
        action.state is not ActionRecoveryState.OBSERVED
        or action.receipt is None
        or action.receipt.to_dict() != authorization.receipt.to_dict()
        or action.execution_id != observation.execution_id
        or action.observation_id != observation.observation_id
    ):
        raise EvidenceBindingError(
            "M4 evidence does not match the exact terminal M3 action chronology"
        )
    matches = [
        event
        for event in recovery.events
        if event.kind is EventKind.ACTION_OBSERVED
        and event.payload.get("proposal_id") == binding.proposal.proposal_id
    ]
    if len(matches) != 1:
        raise EvidenceBindingError(
            "M4 evidence requires one exact authoritative M3 observation event"
        )
    event = matches[0]
    payload = event.payload
    if (
        payload.get("proposal_digest") != binding.proposal.proposal_digest
        or payload.get("execution_id") != observation.execution_id
        or payload.get("observation_id") != observation.observation_id
        or payload.get("observation_digest") != observation.observation_digest
    ):
        raise EvidenceBindingError(
            "M4 evidence requires the digest-bound M3 observation emitted for this exact execution"
        )
    return {
        "session_id": session_id,
        "event_id": event.event_id,
        "event_digest": event.event_digest,
    }


def _revalidate_m3_history(
    store: SqliteSessionEventStore,
    state: _ReplayState,
) -> None:
    recovery = _recover_m3(store, state.m3_session_id)
    action = _exact_m3_action(recovery, state.binding.proposal)
    if state.authorization is not None:
        if action.receipt is None or action.receipt.to_dict() != state.authorization.receipt.to_dict():
            raise EvidenceBindingError(
                "Persisted M4 authorization no longer matches the exact M3 receipt"
            )
    if state.evidence is None:
        return
    if action.state is not ActionRecoveryState.OBSERVED:
        raise EvidenceBindingError(
            "Persisted M4 execution evidence lacks terminal M3 observation provenance"
        )
    anchor = _m3_observation_anchor(
        store,
        session_id=state.m3_session_id,
        binding=state.binding,
        authorization=state.authorization,
        evidence=state.evidence,
    )
    if (
        anchor["event_id"] != state.m3_observation_event_id
        or not hmac.compare_digest(
            anchor["event_digest"],
            state.m3_observation_event_digest or "",
        )
    ):
        raise EvidenceBindingError(
            "Persisted M4 observation anchor no longer matches authoritative M3"
        )


class SqliteRunExecutionRegistry:
    """Append-only M4.3.1 execution lineage backed by authoritative M3 provenance."""

    def __init__(
        self,
        lab_registry: SqliteLabRegistry,
        session_event_store: SqliteSessionEventStore,
    ) -> None:
        if not isinstance(lab_registry, SqliteLabRegistry):
            raise TypeError("lab_registry must be a SqliteLabRegistry")
        if not isinstance(session_event_store, SqliteSessionEventStore):
            raise TypeError("session_event_store must be a SqliteSessionEventStore")
        self._lab_registry = lab_registry
        self._database_path = lab_registry.database_path
        self._session_event_store = session_event_store
        if self._database_path.resolve() != session_event_store.path.resolve():
            raise ValueError(
                "M4.3.1 requires M3 and M4 registries to share one SQLite trust domain"
            )
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _sqlite_connection(self):
        try:
            with closing(self._connect()) as connection:
                yield connection
        except sqlite3.Error as exc:
            raise LabPersistenceError("SQLite run execution registry operation failed") from exc

    def _initialize(self) -> None:
        with self._sqlite_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lab_run_execution_roots (
                    run_id TEXT PRIMARY KEY,
                    binding_id TEXT NOT NULL UNIQUE,
                    experiment_id TEXT NOT NULL,
                    run_digest TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    head_sequence INTEGER NOT NULL,
                    head_event_digest TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES lab_registry_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS lab_run_execution_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_event_digest TEXT,
                    event_digest TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES lab_run_execution_roots(run_id)
                );
                """
            )

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("ROLLBACK")

    def _durable_run(self, run_id: str, *, require_open: bool) -> RegisteredRunSnapshot:
        recovery = self._lab_registry.recover_for_run(run_id)
        snapshot = recovery.run(run_id)
        if require_open and snapshot.evidence_sealed:
            raise LabRegistryStateError(
                "Cannot extend run execution chronology after the M4 run is sealed"
            )
        return snapshot

    @staticmethod
    def _binding_root(
        connection: sqlite3.Connection,
        binding_id: str,
    ) -> str:
        target = UUID(_validate_uuid(binding_id, "binding_id"))
        exact: str | None = None
        for row in connection.execute(
            "SELECT binding_id, run_id FROM lab_run_execution_roots"
        ).fetchall():
            raw = row["binding_id"]
            if not isinstance(raw, str):
                raise LabPersistenceIntegrityError("Persisted execution binding id is not text")
            try:
                parsed = UUID(raw)
            except (TypeError, ValueError, AttributeError) as exc:
                raise LabPersistenceIntegrityError(
                    "Persisted execution binding id is malformed"
                ) from exc
            if parsed != target:
                continue
            if str(parsed) != raw:
                raise LabPersistenceIntegrityError(
                    "Persisted execution binding has a noncanonical UUID alias"
                )
            if exact is not None:
                raise LabPersistenceIntegrityError(
                    "Execution binding identity is not globally unique"
                )
            exact = row["run_id"]
        if exact is None:
            raise InvalidLabRecordError("Unknown durable run execution binding")
        return _validate_uuid(exact, "execution root run_id")

    def register_binding(
        self,
        binding: RunExecutionBinding,
        *,
        m3_session_id: str,
    ) -> RunExecutionRecovery:
        if not isinstance(binding, RunExecutionBinding):
            raise TypeError("binding must be a RunExecutionBinding")
        _validate_uuid(m3_session_id, "m3_session_id")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = self._durable_run(binding.run.run_id, require_open=True)
                if binding.run.to_dict() != run.run.to_dict():
                    raise EvidenceBindingError(
                        "Execution binding does not bind the exact durable M4 run"
                    )
                _require_m3_binding_candidate(
                    self._session_event_store,
                    session_id=m3_session_id,
                    binding=binding,
                )
                if connection.execute(
                    "SELECT 1 FROM lab_run_execution_roots WHERE run_id = ?",
                    (binding.run.run_id,),
                ).fetchone() is not None:
                    self._load(connection, binding.run.run_id, run=run)
                    raise LabIdentityConflictError(
                        "Run already has a durable execution binding"
                    )
                try:
                    self._binding_root(connection, binding.binding_id)
                except InvalidLabRecordError:
                    pass
                else:
                    raise LabIdentityConflictError("Execution binding id is already registered")
                receipt = RunExecutionEventReceipt.create(
                    run_id=binding.run.run_id,
                    sequence=0,
                    kind=RunExecutionEventKind.BINDING_REGISTERED,
                    payload={
                        "binding": binding.to_dict(),
                        "m3_session_id": m3_session_id,
                    },
                    previous_event_digest=None,
                )
                state = _apply_event(None, receipt.kind, receipt.payload, run=run)
                _validate_temporal_chain(state, (receipt,))
                connection.execute(
                    """
                    INSERT INTO lab_run_execution_roots(
                        run_id, binding_id, experiment_id, run_digest, manifest_digest,
                        head_sequence, head_event_digest, bound_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        binding.run.run_id,
                        binding.binding_id,
                        binding.run.experiment_id,
                        binding.run.run_digest,
                        binding.run.manifest_digest,
                        receipt.event_digest,
                        receipt.created_at,
                    ),
                )
                self._insert_event(connection, receipt)
                connection.execute("COMMIT")
                return self._public(run, state, (receipt,))
            except Exception:
                self._rollback(connection)
                raise

    def register_authorization(
        self,
        authorization: RunExecutionAuthorization,
    ) -> RunExecutionRecovery:
        if not isinstance(authorization, RunExecutionAuthorization):
            raise TypeError("authorization must be a RunExecutionAuthorization")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run_id = self._binding_root(connection, authorization.binding_id)
                run = self._durable_run(run_id, require_open=True)
                state, events = self._load(connection, run_id, run=run)
                _require_m3_authorization_candidate(
                    self._session_event_store,
                    session_id=state.m3_session_id,
                    binding=state.binding,
                    authorization=authorization,
                )
                recovery = self._append_loaded(
                    connection,
                    run=run,
                    state=state,
                    events=events,
                    kind=RunExecutionEventKind.AUTHORIZATION_REGISTERED,
                    payload={"authorization": authorization.to_dict()},
                )
                connection.execute("COMMIT")
                return recovery
            except Exception:
                self._rollback(connection)
                raise

    def register_evidence(self, evidence: RunExecutionEvidence) -> RunExecutionRecovery:
        if not isinstance(evidence, RunExecutionEvidence):
            raise TypeError("evidence must be a RunExecutionEvidence")
        _validate_uuid(evidence.run_id, "evidence.run_id")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = self._durable_run(evidence.run_id, require_open=True)
                state, events = self._load(connection, evidence.run_id, run=run)
                if state.authorization is None:
                    raise LabRegistryStateError(
                        "Execution evidence requires a durable authorization event first"
                    )
                anchor = _m3_observation_anchor(
                    self._session_event_store,
                    session_id=state.m3_session_id,
                    binding=state.binding,
                    authorization=state.authorization,
                    evidence=evidence,
                )
                recovery = self._append_loaded(
                    connection,
                    run=run,
                    state=state,
                    events=events,
                    kind=RunExecutionEventKind.EVIDENCE_REGISTERED,
                    payload={
                        "evidence": evidence.to_dict(),
                        "m3_observation_anchor": anchor,
                    },
                )
                connection.execute("COMMIT")
                return recovery
            except Exception:
                self._rollback(connection)
                raise

    def recover(self, run_id: str) -> RunExecutionRecovery:
        _validate_uuid(run_id, "run_id")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN")
                if connection.execute(
                    "SELECT 1 FROM lab_run_execution_roots WHERE run_id = ?",
                    (run_id,),
                ).fetchone() is None:
                    raise InvalidLabRecordError("Unknown durable run execution binding")
                try:
                    run = self._durable_run(run_id, require_open=False)
                except InvalidLabRecordError as exc:
                    raise LabPersistenceIntegrityError(
                        "Execution chronology references a missing durable M4 run"
                    ) from exc
                state, events = self._load(connection, run_id, run=run)
                try:
                    _revalidate_m3_history(self._session_event_store, state)
                except LabError as exc:
                    raise LabPersistenceIntegrityError(
                        "Persisted M4 execution chronology disagrees with authoritative M3 provenance"
                    ) from exc
                connection.execute("COMMIT")
                return self._public(run, state, events)
            except Exception:
                self._rollback(connection)
                raise

    def _append_loaded(
        self,
        connection: sqlite3.Connection,
        *,
        run: RegisteredRunSnapshot,
        state: _ReplayState,
        events: tuple[RunExecutionEventReceipt, ...],
        kind: RunExecutionEventKind,
        payload: Mapping[str, Any],
    ) -> RunExecutionRecovery:
        previous = events[-1]
        receipt = RunExecutionEventReceipt.create(
            run_id=run.run.run_id,
            sequence=previous.sequence + 1,
            kind=kind,
            payload=payload,
            previous_event_digest=previous.event_digest,
        )
        state = _apply_event(state, receipt.kind, receipt.payload, run=run)
        candidate_events = events + (receipt,)
        _validate_temporal_chain(state, candidate_events)
        self._insert_event(connection, receipt)
        cursor = connection.execute(
            """
            UPDATE lab_run_execution_roots
            SET head_sequence = ?, head_event_digest = ?
            WHERE run_id = ? AND head_sequence = ? AND head_event_digest = ?
            """,
            (
                receipt.sequence,
                receipt.event_digest,
                receipt.run_id,
                previous.sequence,
                previous.event_digest,
            ),
        )
        if cursor.rowcount != 1:
            raise LabPersistenceIntegrityError("Run execution root changed during append")
        return self._public(run, state, candidate_events)

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        receipt: RunExecutionEventReceipt,
    ) -> None:
        connection.execute(
            """
            INSERT INTO lab_run_execution_events(
                run_id, sequence, event_id, created_at, kind, payload_json,
                previous_event_digest, event_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.run_id,
                receipt.sequence,
                receipt.event_id,
                receipt.created_at,
                receipt.kind.value,
                _canonical_json(receipt.payload),
                receipt.previous_event_digest,
                receipt.event_digest,
            ),
        )

    def _load(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        run: RegisteredRunSnapshot,
    ) -> tuple[_ReplayState, tuple[RunExecutionEventReceipt, ...]]:
        root = connection.execute(
            "SELECT * FROM lab_run_execution_roots WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if root is None:
            raise InvalidLabRecordError("Unknown durable run execution binding")
        try:
            root_run_id = _validate_uuid(root["run_id"], "persisted root run_id")
            root_binding_id = _validate_uuid(
                root["binding_id"],
                "persisted root binding_id",
            )
            root_experiment_id = _validate_uuid(
                root["experiment_id"],
                "persisted root experiment_id",
            )
            root_run_digest = _validate_digest(
                root["run_digest"],
                "persisted root run_digest",
            )
            root_manifest_digest = _validate_digest(
                root["manifest_digest"],
                "persisted root manifest_digest",
            )
            head_sequence = root["head_sequence"]
            if type(head_sequence) is not int or head_sequence < 0:
                raise InvalidLabRecordError("persisted root head_sequence is invalid")
            head_digest = _validate_digest(
                root["head_event_digest"],
                "persisted root head_event_digest",
            )
            bound_at = _validate_timestamp(root["bound_at"], "persisted root bound_at")
        except InvalidLabRecordError as exc:
            raise LabPersistenceIntegrityError(
                "Persisted run execution root is not canonical"
            ) from exc
        if (
            root_run_id != run.run.run_id
            or root_experiment_id != run.run.experiment_id
            or not hmac.compare_digest(root_run_digest, run.run.run_digest)
            or not hmac.compare_digest(root_manifest_digest, run.run.manifest_digest)
        ):
            raise LabPersistenceIntegrityError(
                "Run execution root disagrees with the authoritative M4 run"
            )

        rows = connection.execute(
            "SELECT * FROM lab_run_execution_events WHERE run_id = ? ORDER BY sequence ASC",
            (run_id,),
        ).fetchall()
        if not rows:
            raise LabPersistenceIntegrityError("Run execution root has no event chronology")
        state: _ReplayState | None = None
        events: list[RunExecutionEventReceipt] = []
        previous_digest: str | None = None
        for expected_sequence, row in enumerate(rows):
            try:
                sequence = row["sequence"]
                if type(sequence) is not int or sequence != expected_sequence:
                    raise InvalidLabRecordError("persisted execution sequence is not contiguous")
                event_id = _validate_uuid(row["event_id"], "persisted execution event_id")
                created_at = _validate_timestamp(
                    row["created_at"],
                    "persisted execution created_at",
                )
                kind = _normalize_kind(row["kind"])
                payload = _load_canonical_json(row["payload_json"])
                prior = row["previous_event_digest"]
                if expected_sequence == 0:
                    if prior is not None:
                        raise InvalidLabRecordError(
                            "persisted first execution event has previous digest"
                        )
                else:
                    prior = _validate_digest(
                        prior,
                        "persisted execution previous_event_digest",
                    )
                    if not hmac.compare_digest(prior, previous_digest or ""):
                        raise InvalidLabRecordError("persisted execution event chain is broken")
                event_digest = _validate_digest(
                    row["event_digest"],
                    "persisted execution event_digest",
                )
                event = RunExecutionEventReceipt(
                    schema_version=RUN_EXECUTION_EVENT_SCHEMA_VERSION,
                    event_id=event_id,
                    run_id=run_id,
                    sequence=sequence,
                    created_at=created_at,
                    kind=kind,
                    payload=payload,
                    previous_event_digest=prior,
                    event_digest=event_digest,
                )
                state = _apply_event(state, event.kind, event.payload, run=run)
            except LabPersistenceIntegrityError:
                raise
            except LabError as exc:
                raise LabPersistenceIntegrityError(
                    "Persisted run execution event failed semantic replay"
                ) from exc
            events.append(event)
            previous_digest = event.event_digest

        if state is None:  # pragma: no cover - rows is non-empty
            raise LabPersistenceIntegrityError("Run execution chronology did not produce state")
        first = events[0]
        if (
            first.kind is not RunExecutionEventKind.BINDING_REGISTERED
            or state.binding.binding_id != root_binding_id
            or first.created_at != bound_at
        ):
            raise LabPersistenceIntegrityError(
                "Run execution root binding metadata disagrees with chronology"
            )
        last = events[-1]
        if head_sequence != last.sequence or not hmac.compare_digest(
            head_digest,
            last.event_digest,
        ):
            raise LabPersistenceIntegrityError(
                "Run execution root head disagrees with event chronology"
            )
        try:
            _validate_temporal_chain(state, tuple(events))
        except LabError as exc:
            raise LabPersistenceIntegrityError(
                "Persisted run execution chronology violates temporal ordering"
            ) from exc
        return state, tuple(events)

    @staticmethod
    def _public(
        run: RegisteredRunSnapshot,
        state: _ReplayState,
        events: tuple[RunExecutionEventReceipt, ...],
    ) -> RunExecutionRecovery:
        return RunExecutionRecovery(
            run=run,
            binding=state.binding,
            m3_session_id=state.m3_session_id,
            authorization=state.authorization,
            evidence=state.evidence,
            m3_observation_event_id=state.m3_observation_event_id,
            m3_observation_event_digest=state.m3_observation_event_digest,
            events=events,
        )
