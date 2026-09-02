from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from codexia_manual_agent.authority.models import (
    ActionProposal,
    AuthorizationDecision,
    AuthorizationReceipt,
)
from codexia_manual_agent.domain.errors import AuthorizationConsumedError
from codexia_manual_agent.session_events.models import (
    EventKind,
    SessionEventIntegrityError,
    SessionEventReceipt,
    SessionEventStateError,
    UnknownSessionError,
    canonical_json,
    proposal_from_event_payload,
    receipt_from_event_payload,
    validate_event_payload,
)


_AUTHORITY_EVENT_KINDS = frozenset(
    {
        EventKind.ACTION_PROPOSED,
        EventKind.AUTHORIZATION_RECORDED,
        EventKind.AUTHORIZATION_CONSUMED,
        EventKind.ACTION_EXECUTED,
        EventKind.ACTION_OBSERVED,
    }
)


@dataclass(frozen=True, slots=True)
class _RuntimeState:
    provider: str
    active_run_id: str | None
    pending_provider_request_id: str | None


class SqliteSessionEventStore:
    """Authoritative local M3 chronology and authorization-consumption ledger."""

    def __init__(self, database_path: str | Path) -> None:
        raw = Path(database_path).expanduser()
        if raw.exists() and raw.is_symlink():
            raise SessionEventIntegrityError("M3 database path must not be a symlink")
        self._path = raw.absolute()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(self._path.parent, 0o700)
            except OSError:
                pass
        self._initialize()
        if os.name != "nt":
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._path),
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
        except sqlite3.DatabaseError:
            pass
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    head_digest TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_event_digest TEXT,
                    event_digest TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence),
                    UNIQUE (session_id, event_digest),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_events_session_kind
                    ON events(session_id, kind, sequence);

                CREATE TABLE IF NOT EXISTS consumed_authorizations (
                    receipt_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    proposal_digest TEXT NOT NULL,
                    consumed_event_id TEXT NOT NULL UNIQUE,
                    consumed_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY (consumed_event_id) REFERENCES events(event_id)
                );
                """
            )

    def start_session(
        self,
        *,
        session_id: str,
        payload: Mapping[str, Any],
    ) -> SessionEventReceipt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                raise SessionEventStateError("Persistent session already exists")
            event = SessionEventReceipt.create(
                session_id=session_id,
                sequence=0,
                kind=EventKind.SESSION_STARTED,
                payload=payload,
                previous_event_digest=None,
            )
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, created_at, last_sequence, head_digest, completed
                ) VALUES (?, ?, ?, ?, 0)
                """,
                (session_id, event.created_at, event.sequence, event.event_digest),
            )
            self._insert_event(connection, event)
            return event

    def append(
        self,
        session_id: str,
        kind: EventKind | str,
        payload: Mapping[str, Any],
    ) -> SessionEventReceipt:
        try:
            normalized_kind = EventKind(kind)
        except (TypeError, ValueError) as exc:
            raise SessionEventIntegrityError("Unknown M3 event kind") from exc
        if normalized_kind is EventKind.SESSION_STARTED:
            raise SessionEventStateError("Use start_session() for session_started")
        if normalized_kind in _AUTHORITY_EVENT_KINDS:
            raise SessionEventStateError(
                "Authority chronology must use the exact record/consume execution APIs"
            )
        normalized_payload = validate_event_payload(normalized_kind, payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_runtime_transition(
                connection,
                session_id=session_id,
                kind=normalized_kind,
                payload=normalized_payload,
            )
            if normalized_kind is EventKind.SESSION_COMPLETED:
                self._validate_session_consumption_integrity(connection, session_id)
                self._require_terminal_authority_for_completion(connection, session_id)
            return self._append_in_transaction(
                connection,
                session_id=session_id,
                kind=normalized_kind,
                payload=normalized_payload,
            )

    def record_proposal(
        self,
        session_id: str,
        proposal: ActionProposal,
    ) -> SessionEventReceipt:
        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be an ActionProposal")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_open_session(connection, session_id)
            if self._proposal_occurrences(connection, session_id, proposal.proposal_id):
                raise SessionEventStateError("Action proposal id was already recorded")
            return self._append_in_transaction(
                connection,
                session_id=session_id,
                kind=EventKind.ACTION_PROPOSED,
                payload={"proposal": proposal.to_dict()},
            )

    def record_authorization(
        self,
        session_id: str,
        receipt: AuthorizationReceipt,
    ) -> SessionEventReceipt:
        if not isinstance(receipt, AuthorizationReceipt):
            raise TypeError("receipt must be an AuthorizationReceipt")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_open_session(connection, session_id)
            proposal = self._find_recorded_proposal(
                connection,
                session_id=session_id,
                proposal_id=receipt.proposal_id,
                proposal_digest=receipt.proposal_digest,
            )
            if proposal.proposal_digest != receipt.proposal_digest:
                raise SessionEventStateError("Authorization is bound to another proposal")
            if self._receipt_occurrences(connection, session_id, receipt.receipt_id):
                raise SessionEventStateError("Authorization receipt id was already recorded")
            if self._authorization_for_proposal_exists(
                connection,
                session_id=session_id,
                proposal_id=proposal.proposal_id,
            ):
                raise SessionEventStateError("Action already has a durable authorization")
            return self._append_in_transaction(
                connection,
                session_id=session_id,
                kind=EventKind.AUTHORIZATION_RECORDED,
                payload={"receipt": receipt.to_dict()},
            )

    def consume_recorded_authorization(
        self,
        *,
        session_id: str,
        receipt_id: str,
        receipt_digest: str,
        proposal_id: str,
        proposal_digest: str,
    ) -> SessionEventReceipt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_open_session(connection, session_id)
            self._validate_session_consumption_integrity(connection, session_id)
            existing = connection.execute(
                """
                SELECT receipt_id, session_id, receipt_digest, proposal_id,
                       proposal_digest, consumed_event_id, consumed_at
                FROM consumed_authorizations
                WHERE receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
            consumption_event = self._find_consumption_event_for_receipt(
                connection,
                receipt_id,
            )
            if existing is not None:
                if consumption_event is None:
                    raise SessionEventIntegrityError(
                        "Consumed registry row exists without its chronology event"
                    )
                self._validate_consumed_row_event(existing, consumption_event)
                raise AuthorizationConsumedError(
                    f"Authorization receipt already consumed: {receipt_id}"
                )
            if consumption_event is not None:
                raise SessionEventIntegrityError(
                    "Consumption event exists without durable one-shot registry row"
                )

            proposal = self._find_recorded_proposal(
                connection,
                session_id=session_id,
                proposal_id=proposal_id,
                proposal_digest=proposal_digest,
            )
            receipt = self._find_recorded_receipt(
                connection,
                session_id=session_id,
                receipt_id=receipt_id,
            )
            if (
                receipt.receipt_digest != receipt_digest
                or receipt.proposal_id != proposal.proposal_id
                or receipt.proposal_digest != proposal.proposal_digest
            ):
                raise SessionEventStateError(
                    "Durable authorization does not match the exact runtime receipt/proposal"
                )
            if receipt.decision is not AuthorizationDecision.ALLOW:
                raise SessionEventStateError("A durable denial receipt cannot be consumed")

            event = self._append_in_transaction(
                connection,
                session_id=session_id,
                kind=EventKind.AUTHORIZATION_CONSUMED,
                payload={
                    "receipt_id": receipt.receipt_id,
                    "receipt_digest": receipt.receipt_digest,
                    "proposal_id": proposal.proposal_id,
                    "proposal_digest": proposal.proposal_digest,
                },
            )
            try:
                connection.execute(
                    """
                    INSERT INTO consumed_authorizations (
                        receipt_id,
                        session_id,
                        receipt_digest,
                        proposal_id,
                        proposal_digest,
                        consumed_event_id,
                        consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receipt_id,
                        session_id,
                        receipt.receipt_digest,
                        proposal.proposal_id,
                        proposal.proposal_digest,
                        event.event_id,
                        event.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthorizationConsumedError(
                    f"Authorization receipt already consumed: {receipt_id}"
                ) from exc
            return event

    def record_execution(
        self,
        session_id: str,
        *,
        proposal: ActionProposal,
        receipt: AuthorizationReceipt,
        execution_id: str,
    ) -> SessionEventReceipt:
        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be an ActionProposal")
        if not isinstance(receipt, AuthorizationReceipt):
            raise TypeError("receipt must be an AuthorizationReceipt")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_open_session(connection, session_id)
            self._validate_session_consumption_integrity(connection, session_id)
            recorded_proposal = self._find_recorded_proposal(
                connection,
                session_id=session_id,
                proposal_id=proposal.proposal_id,
                proposal_digest=proposal.proposal_digest,
            )
            recorded_receipt = self._find_recorded_receipt(
                connection,
                session_id=session_id,
                receipt_id=receipt.receipt_id,
            )
            if (
                recorded_proposal.to_dict() != proposal.to_dict()
                or recorded_receipt.to_dict() != receipt.to_dict()
            ):
                raise SessionEventStateError(
                    "Execution binding differs from durable proposal/receipt"
                )
            consumed = connection.execute(
                """
                SELECT receipt_id, session_id, receipt_digest, proposal_id,
                       proposal_digest, consumed_event_id, consumed_at
                FROM consumed_authorizations
                WHERE receipt_id = ? AND session_id = ?
                """,
                (receipt.receipt_id, session_id),
            ).fetchone()
            if consumed is None:
                raise SessionEventStateError(
                    "Execution requires prior durable authorization consumption"
                )
            if (
                str(consumed["receipt_digest"]) != receipt.receipt_digest
                or str(consumed["proposal_id"]) != proposal.proposal_id
                or str(consumed["proposal_digest"]) != proposal.proposal_digest
            ):
                raise SessionEventIntegrityError(
                    "Consumed registry binding drifted before execution"
                )
            consumption_event = self._find_consumption_event_for_receipt(
                connection,
                receipt.receipt_id,
            )
            if consumption_event is None:
                raise SessionEventIntegrityError(
                    "Execution cannot trust a consumed row without its chronology event"
                )
            self._validate_consumed_row_event(consumed, consumption_event)
            if consumption_event.session_id != session_id:
                raise SessionEventIntegrityError(
                    "Execution consumption event belongs to another session"
                )
            if self._execution_for_proposal_exists(
                connection,
                session_id,
                proposal.proposal_id,
            ):
                raise SessionEventStateError("Action execution was already recorded")
            return self._append_in_transaction(
                connection,
                session_id=session_id,
                kind=EventKind.ACTION_EXECUTED,
                payload={
                    "proposal_id": proposal.proposal_id,
                    "proposal_digest": proposal.proposal_digest,
                    "receipt_id": receipt.receipt_id,
                    "receipt_digest": receipt.receipt_digest,
                    "execution_id": execution_id,
                },
            )

    def record_observation(
        self,
        session_id: str,
        *,
        proposal: ActionProposal,
        execution_id: str,
        observation_id: str,
    ) -> SessionEventReceipt:
        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be an ActionProposal")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_open_session(connection, session_id)
            self._validate_session_consumption_integrity(connection, session_id)
            recorded_proposal = self._find_recorded_proposal(
                connection,
                session_id=session_id,
                proposal_id=proposal.proposal_id,
                proposal_digest=proposal.proposal_digest,
            )
            if recorded_proposal.to_dict() != proposal.to_dict():
                raise SessionEventStateError(
                    "Observation proposal differs from durable proposal"
                )
            execution = self._find_execution(
                connection,
                session_id=session_id,
                proposal_id=proposal.proposal_id,
            )
            if (
                execution["proposal_digest"] != proposal.proposal_digest
                or execution["execution_id"] != execution_id
            ):
                raise SessionEventStateError(
                    "Observation is not bound to the exact execution"
                )
            if self._observation_for_proposal_exists(
                connection,
                session_id,
                proposal.proposal_id,
            ):
                raise SessionEventStateError("Action observation was already recorded")
            return self._append_in_transaction(
                connection,
                session_id=session_id,
                kind=EventKind.ACTION_OBSERVED,
                payload={
                    "proposal_id": proposal.proposal_id,
                    "proposal_digest": proposal.proposal_digest,
                    "execution_id": execution_id,
                    "observation_id": observation_id,
                },
            )

    def is_authorization_consumed(self, receipt_id: str) -> bool:
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise ValueError("receipt_id must be a non-empty string")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT receipt_id, session_id, receipt_digest, proposal_id,
                       proposal_digest, consumed_event_id, consumed_at
                FROM consumed_authorizations
                WHERE receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
            event = self._find_consumption_event_for_receipt(connection, receipt_id)
            if row is None:
                if event is not None:
                    raise SessionEventIntegrityError(
                        "Consumption event exists without durable one-shot registry row"
                    )
                return False
            if event is None:
                raise SessionEventIntegrityError(
                    "Consumed registry row exists without its chronology event"
                )
            self._validate_consumed_row_event(row, event)
            return True

    def consumed_authorizations(self, session_id: str) -> dict[str, dict[str, str]]:
        events = self.load_events(session_id)
        consumed_event_ids = {
            event.event_id: event
            for event in events
            if event.kind is EventKind.AUTHORIZATION_CONSUMED
        }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT receipt_id, receipt_digest, proposal_id, proposal_digest,
                       consumed_event_id, consumed_at
                FROM consumed_authorizations
                WHERE session_id = ?
                ORDER BY consumed_at, receipt_id
                """,
                (session_id,),
            ).fetchall()
        result = {
            str(row["receipt_id"]): {
                "receipt_digest": str(row["receipt_digest"]),
                "proposal_id": str(row["proposal_id"]),
                "proposal_digest": str(row["proposal_digest"]),
                "consumed_event_id": str(row["consumed_event_id"]),
                "consumed_at": str(row["consumed_at"]),
            }
            for row in rows
        }
        if {item["consumed_event_id"] for item in result.values()} != set(consumed_event_ids):
            raise SessionEventIntegrityError(
                "Consumed registry and consumption chronology are not one-to-one"
            )
        return result

    def load_events(self, session_id: str) -> tuple[SessionEventReceipt, ...]:
        with self._connect() as connection:
            session = self._require_session_row(connection, session_id)
            rows = connection.execute(
                """
                SELECT session_id, sequence, event_id, created_at, kind,
                       payload_json, previous_event_digest, event_digest
                FROM events
                WHERE session_id = ?
                ORDER BY sequence ASC
                """,
                (session_id,),
            ).fetchall()
            consumed_rows = connection.execute(
                """
                SELECT receipt_id, receipt_digest, proposal_id, proposal_digest,
                       consumed_event_id
                FROM consumed_authorizations
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchall()

        events: list[SessionEventReceipt] = []
        previous: SessionEventReceipt | None = None
        for row in rows:
            payload = _strict_json_object(str(row["payload_json"]))
            try:
                kind = EventKind(str(row["kind"]))
            except ValueError as exc:
                raise SessionEventIntegrityError("Unknown durable event kind") from exc
            event = SessionEventReceipt(
                schema_version=1,
                event_id=str(row["event_id"]),
                session_id=str(row["session_id"]),
                sequence=int(row["sequence"]),
                created_at=str(row["created_at"]),
                kind=kind,
                payload=payload,
                previous_event_digest=(
                    None
                    if row["previous_event_digest"] is None
                    else str(row["previous_event_digest"])
                ),
                event_digest=str(row["event_digest"]),
            )
            if event.sequence != len(events):
                raise SessionEventIntegrityError(
                    "M3 event sequence contains a gap or reorder"
                )
            if previous is not None and event.previous_event_digest != previous.event_digest:
                raise SessionEventIntegrityError("M3 event hash chain is broken")
            events.append(event)
            previous = event

        if not events:
            raise SessionEventIntegrityError(
                "Persistent session has no session_started event"
            )
        if int(session["last_sequence"]) != events[-1].sequence:
            raise SessionEventIntegrityError(
                "Session head sequence does not match event ledger"
            )
        if str(session["head_digest"]) != events[-1].event_digest:
            raise SessionEventIntegrityError(
                "Session head digest does not match event ledger"
            )
        if bool(session["completed"]) != (
            events[-1].kind is EventKind.SESSION_COMPLETED
        ):
            raise SessionEventIntegrityError(
                "Session completion head metadata is inconsistent"
            )
        self._validate_consumption_bijection(events, consumed_rows)
        return tuple(events)

    def recover(self, session_id: str):
        from codexia_manual_agent.session_events.recovery import recover_session

        return recover_session(
            self.load_events(session_id),
            consumed_authorizations=self.consumed_authorizations(session_id),
        )

    def _append_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        kind: EventKind,
        payload: Mapping[str, Any],
    ) -> SessionEventReceipt:
        session = self._require_open_session(connection, session_id)
        head = connection.execute(
            """
            SELECT sequence, event_digest
            FROM events
            WHERE session_id = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if head is None:
            raise SessionEventIntegrityError(
                "Session row exists without event chronology"
            )
        if (
            int(head["sequence"]) != int(session["last_sequence"])
            or str(head["event_digest"]) != str(session["head_digest"])
        ):
            raise SessionEventIntegrityError(
                "Session head metadata drifted from event ledger"
            )
        event = SessionEventReceipt.create(
            session_id=session_id,
            sequence=int(head["sequence"]) + 1,
            kind=kind,
            payload=payload,
            previous_event_digest=str(head["event_digest"]),
        )
        self._insert_event(connection, event)
        connection.execute(
            """
            UPDATE sessions
            SET last_sequence = ?, head_digest = ?, completed = ?
            WHERE session_id = ?
            """,
            (
                event.sequence,
                event.event_digest,
                1 if kind is EventKind.SESSION_COMPLETED else 0,
                session_id,
            ),
        )
        return event

    def _validate_runtime_transition(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        kind: EventKind,
        payload: Mapping[str, Any],
    ) -> None:
        self._require_open_session(connection, session_id)
        state = self._runtime_state(connection, session_id)

        if kind is EventKind.RUN_STARTED:
            if state.active_run_id is not None:
                raise SessionEventStateError(
                    "Persistent session already has an active run"
                )
            if state.pending_provider_request_id is not None:
                raise SessionEventStateError(
                    "Persistent session has an unresolved provider request"
                )
            return

        if kind is EventKind.MODEL_REQUEST_STARTED:
            if str(payload["provider"]) != state.provider:
                raise SessionEventStateError(
                    "Model request changed the persistent provider identity"
                )
            if state.active_run_id != str(payload["run_id"]):
                raise SessionEventStateError(
                    "Model request does not belong to the active persistent run"
                )
            if state.pending_provider_request_id is not None:
                raise SessionEventStateError(
                    "Persistent run already has an unresolved provider request"
                )
            return

        if kind is EventKind.MODEL_RESPONSE_RECORDED:
            if str(payload["provider"]) != state.provider:
                raise SessionEventStateError(
                    "Model response changed the persistent provider identity"
                )
            if state.active_run_id != str(payload["run_id"]):
                raise SessionEventStateError(
                    "Model response does not belong to the active persistent run"
                )
            if state.pending_provider_request_id != str(payload["request_id"]):
                raise SessionEventStateError(
                    "Model response does not match the unresolved provider request"
                )
            return

        if kind is EventKind.TOOL_OBSERVATION_RECORDED:
            if state.active_run_id != str(payload["run_id"]):
                raise SessionEventStateError(
                    "Tool observation does not belong to the active persistent run"
                )
            if state.pending_provider_request_id is not None:
                raise SessionEventStateError(
                    "Tool observation cannot precede the pending model response"
                )
            return

        if kind is EventKind.RUN_COMPLETED:
            if state.active_run_id != str(payload["run_id"]):
                raise SessionEventStateError(
                    "run_completed does not match the active persistent run"
                )
            if state.pending_provider_request_id is not None:
                raise SessionEventStateError(
                    "run_completed cannot hide an unresolved provider request"
                )
            return

        if kind is EventKind.RUN_INTERRUPTED:
            if state.active_run_id != str(payload["run_id"]):
                raise SessionEventStateError(
                    "run_interrupted does not match the active persistent run"
                )
            request_id = payload["request_id"]
            if state.pending_provider_request_id is None:
                if request_id is not None:
                    raise SessionEventStateError(
                        "run_interrupted names a provider request that is not unresolved"
                    )
            elif request_id != state.pending_provider_request_id:
                raise SessionEventStateError(
                    "run_interrupted must name the exact unresolved provider request"
                )
            return

        if kind is EventKind.SESSION_COMPLETED:
            if state.active_run_id is not None:
                raise SessionEventStateError(
                    "session_completed cannot hide an active run"
                )
            if state.pending_provider_request_id is not None:
                raise SessionEventStateError(
                    "session_completed cannot hide an unresolved provider request"
                )
            return

        raise SessionEventStateError(f"Unsupported runtime event kind: {kind.value}")

    def _runtime_state(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> _RuntimeState:
        rows = connection.execute(
            """
            SELECT kind, payload_json
            FROM events
            WHERE session_id = ?
            ORDER BY sequence ASC
            """,
            (session_id,),
        ).fetchall()
        if not rows:
            raise SessionEventIntegrityError("Persistent session has no chronology")
        provider: str | None = None
        active_run_id: str | None = None
        pending_request_id: str | None = None

        for index, row in enumerate(rows):
            try:
                kind = EventKind(str(row["kind"]))
            except ValueError as exc:
                raise SessionEventIntegrityError("Unknown durable event kind") from exc
            payload = _strict_json_object(str(row["payload_json"]))
            validate_event_payload(kind, payload)

            if kind is EventKind.SESSION_STARTED:
                if index != 0 or provider is not None:
                    raise SessionEventIntegrityError(
                        "session_started chronology is invalid"
                    )
                provider = str(payload["provider"])
                continue
            if kind in _AUTHORITY_EVENT_KINDS:
                continue
            if provider is None:
                raise SessionEventIntegrityError(
                    "Runtime chronology precedes session_started"
                )

            if kind is EventKind.RUN_STARTED:
                if active_run_id is not None or pending_request_id is not None:
                    raise SessionEventIntegrityError(
                        "Persistent chronology contains overlapping runs"
                    )
                active_run_id = str(payload["run_id"])
                continue
            if kind is EventKind.MODEL_REQUEST_STARTED:
                if str(payload["provider"]) != provider:
                    raise SessionEventIntegrityError(
                        "Persistent chronology changed provider identity"
                    )
                if active_run_id != str(payload["run_id"]):
                    raise SessionEventIntegrityError(
                        "Provider request belongs to a non-active run"
                    )
                if pending_request_id is not None:
                    raise SessionEventIntegrityError(
                        "Persistent chronology has two unresolved provider requests"
                    )
                pending_request_id = str(payload["request_id"])
                continue
            if kind is EventKind.MODEL_RESPONSE_RECORDED:
                if str(payload["provider"]) != provider:
                    raise SessionEventIntegrityError(
                        "Persistent chronology changed provider identity"
                    )
                if active_run_id != str(payload["run_id"]):
                    raise SessionEventIntegrityError(
                        "Provider response belongs to a non-active run"
                    )
                if pending_request_id != str(payload["request_id"]):
                    raise SessionEventIntegrityError(
                        "Provider response does not match the pending request"
                    )
                pending_request_id = None
                continue
            if kind is EventKind.TOOL_OBSERVATION_RECORDED:
                if active_run_id != str(payload["run_id"]):
                    raise SessionEventIntegrityError(
                        "Tool observation belongs to a non-active run"
                    )
                if pending_request_id is not None:
                    raise SessionEventIntegrityError(
                        "Tool observation precedes a pending model response"
                    )
                continue
            if kind is EventKind.RUN_COMPLETED:
                if active_run_id != str(payload["run_id"]):
                    raise SessionEventIntegrityError(
                        "run_completed does not match the active run"
                    )
                if pending_request_id is not None:
                    raise SessionEventIntegrityError(
                        "run_completed hides an unresolved provider request"
                    )
                active_run_id = None
                continue
            if kind is EventKind.RUN_INTERRUPTED:
                if active_run_id != str(payload["run_id"]):
                    raise SessionEventIntegrityError(
                        "run_interrupted does not match the active run"
                    )
                request_id = payload["request_id"]
                if pending_request_id is None:
                    if request_id is not None:
                        raise SessionEventIntegrityError(
                            "run_interrupted names a non-pending request"
                        )
                elif request_id != pending_request_id:
                    raise SessionEventIntegrityError(
                        "run_interrupted changed the pending request identity"
                    )
                active_run_id = None
                continue
            if kind is EventKind.SESSION_COMPLETED:
                if active_run_id is not None or pending_request_id is not None:
                    raise SessionEventIntegrityError(
                        "session_completed hides unfinished runtime state"
                    )
                continue

        if provider is None:
            raise SessionEventIntegrityError(
                "Persistent chronology lacks session_started provider identity"
            )
        return _RuntimeState(
            provider=provider,
            active_run_id=active_run_id,
            pending_provider_request_id=pending_request_id,
        )

    def _validate_session_consumption_integrity(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> None:
        event_rows = connection.execute(
            """
            SELECT session_id, sequence, event_id, created_at, kind, payload_json,
                   previous_event_digest, event_digest
            FROM events
            WHERE session_id = ? AND kind = ?
            ORDER BY sequence ASC
            """,
            (session_id, EventKind.AUTHORIZATION_CONSUMED.value),
        ).fetchall()
        events: list[SessionEventReceipt] = []
        for row in event_rows:
            payload = _strict_json_object(str(row["payload_json"]))
            events.append(
                SessionEventReceipt(
                    schema_version=1,
                    event_id=str(row["event_id"]),
                    session_id=str(row["session_id"]),
                    sequence=int(row["sequence"]),
                    created_at=str(row["created_at"]),
                    kind=EventKind.AUTHORIZATION_CONSUMED,
                    payload=payload,
                    previous_event_digest=(
                        None
                        if row["previous_event_digest"] is None
                        else str(row["previous_event_digest"])
                    ),
                    event_digest=str(row["event_digest"]),
                )
            )
        consumed_rows = connection.execute(
            """
            SELECT receipt_id, session_id, receipt_digest, proposal_id,
                   proposal_digest, consumed_event_id, consumed_at
            FROM consumed_authorizations
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchall()
        self._validate_consumption_bijection(events, consumed_rows)

    def _require_terminal_authority_for_completion(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> None:
        proposals = connection.execute(
            "SELECT payload_json FROM events WHERE session_id = ? AND kind = ?",
            (session_id, EventKind.ACTION_PROPOSED.value),
        ).fetchall()
        receipts = connection.execute(
            "SELECT payload_json FROM events WHERE session_id = ? AND kind = ?",
            (session_id, EventKind.AUTHORIZATION_RECORDED.value),
        ).fetchall()
        observations = connection.execute(
            "SELECT payload_json FROM events WHERE session_id = ? AND kind = ?",
            (session_id, EventKind.ACTION_OBSERVED.value),
        ).fetchall()
        receipt_by_proposal = {
            receipt.proposal_id: receipt
            for receipt in (
                receipt_from_event_payload(_strict_json_object(str(row["payload_json"])))
                for row in receipts
            )
        }
        observed_proposals = {
            str(_strict_json_object(str(row["payload_json"]))["proposal_id"])
            for row in observations
        }
        for row in proposals:
            proposal = proposal_from_event_payload(
                _strict_json_object(str(row["payload_json"]))
            )
            receipt = receipt_by_proposal.get(proposal.proposal_id)
            if receipt is None:
                raise SessionEventStateError(
                    "session_completed cannot hide a proposal awaiting authorization"
                )
            if receipt.decision is AuthorizationDecision.ALLOW:
                if proposal.proposal_id not in observed_proposals:
                    raise SessionEventStateError(
                        "session_completed cannot hide an unfinished authorized action"
                    )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        event: SessionEventReceipt,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (
                session_id, sequence, event_id, created_at, kind,
                payload_json, previous_event_digest, event_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.session_id,
                event.sequence,
                event.event_id,
                event.created_at,
                event.kind.value,
                canonical_json(event.payload),
                event.previous_event_digest,
                event.event_digest,
            ),
        )

    @staticmethod
    def _require_session_row(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise UnknownSessionError(f"Persistent session not found: {session_id}")
        return row

    @classmethod
    def _require_open_session(
        cls,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> sqlite3.Row:
        row = cls._require_session_row(connection, session_id)
        if int(row["completed"]) != 0:
            raise SessionEventStateError("Completed persistent session is immutable")
        return row

    @staticmethod
    def _proposal_occurrences(
        connection: sqlite3.Connection,
        session_id: str,
        proposal_id: str,
    ) -> list[ActionProposal]:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE session_id = ? AND kind = ?",
            (session_id, EventKind.ACTION_PROPOSED.value),
        ).fetchall()
        result: list[ActionProposal] = []
        for row in rows:
            proposal = proposal_from_event_payload(
                _strict_json_object(str(row["payload_json"]))
            )
            if proposal.proposal_id == proposal_id:
                result.append(proposal)
        return result

    def _find_recorded_proposal(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        proposal_id: str,
        proposal_digest: str,
    ) -> ActionProposal:
        matches = self._proposal_occurrences(connection, session_id, proposal_id)
        if len(matches) != 1 or matches[0].proposal_digest != proposal_digest:
            raise SessionEventStateError(
                "Exact durable action proposal was not recorded once"
            )
        return matches[0]

    @staticmethod
    def _receipt_occurrences(
        connection: sqlite3.Connection,
        session_id: str,
        receipt_id: str,
    ) -> list[AuthorizationReceipt]:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE session_id = ? AND kind = ?",
            (session_id, EventKind.AUTHORIZATION_RECORDED.value),
        ).fetchall()
        result: list[AuthorizationReceipt] = []
        for row in rows:
            receipt = receipt_from_event_payload(
                _strict_json_object(str(row["payload_json"]))
            )
            if receipt.receipt_id == receipt_id:
                result.append(receipt)
        return result

    def _find_recorded_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        receipt_id: str,
    ) -> AuthorizationReceipt:
        matches = self._receipt_occurrences(connection, session_id, receipt_id)
        if len(matches) != 1:
            raise SessionEventStateError(
                "Exact durable authorization receipt was not recorded once"
            )
        return matches[0]

    @staticmethod
    def _authorization_for_proposal_exists(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        proposal_id: str,
    ) -> bool:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE session_id = ? AND kind = ?",
            (session_id, EventKind.AUTHORIZATION_RECORDED.value),
        ).fetchall()
        return any(
            receipt_from_event_payload(
                _strict_json_object(str(row["payload_json"]))
            ).proposal_id
            == proposal_id
            for row in rows
        )

    @staticmethod
    def _execution_for_proposal_exists(
        connection: sqlite3.Connection,
        session_id: str,
        proposal_id: str,
    ) -> bool:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE session_id = ? AND kind = ?",
            (session_id, EventKind.ACTION_EXECUTED.value),
        ).fetchall()
        return any(
            _strict_json_object(str(row["payload_json"])).get("proposal_id")
            == proposal_id
            for row in rows
        )

    @staticmethod
    def _observation_for_proposal_exists(
        connection: sqlite3.Connection,
        session_id: str,
        proposal_id: str,
    ) -> bool:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE session_id = ? AND kind = ?",
            (session_id, EventKind.ACTION_OBSERVED.value),
        ).fetchall()
        return any(
            _strict_json_object(str(row["payload_json"])).get("proposal_id")
            == proposal_id
            for row in rows
        )

    @staticmethod
    def _find_execution(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        proposal_id: str,
    ) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT payload_json FROM events WHERE session_id = ? AND kind = ?",
            (session_id, EventKind.ACTION_EXECUTED.value),
        ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            payload = _strict_json_object(str(row["payload_json"]))
            if payload.get("proposal_id") == proposal_id:
                matches.append(payload)
        if len(matches) != 1:
            raise SessionEventStateError(
                "Exact durable execution was not recorded once"
            )
        return matches[0]

    @staticmethod
    def _find_consumption_event_for_receipt(
        connection: sqlite3.Connection,
        receipt_id: str,
    ) -> SessionEventReceipt | None:
        rows = connection.execute(
            """
            SELECT session_id, sequence, event_id, created_at, kind, payload_json,
                   previous_event_digest, event_digest
            FROM events
            WHERE kind = ?
            ORDER BY session_id, sequence
            """,
            (EventKind.AUTHORIZATION_CONSUMED.value,),
        ).fetchall()
        matches: list[SessionEventReceipt] = []
        for row in rows:
            payload = _strict_json_object(str(row["payload_json"]))
            if payload.get("receipt_id") != receipt_id:
                continue
            matches.append(
                SessionEventReceipt(
                    schema_version=1,
                    event_id=str(row["event_id"]),
                    session_id=str(row["session_id"]),
                    sequence=int(row["sequence"]),
                    created_at=str(row["created_at"]),
                    kind=EventKind.AUTHORIZATION_CONSUMED,
                    payload=payload,
                    previous_event_digest=(
                        None
                        if row["previous_event_digest"] is None
                        else str(row["previous_event_digest"])
                    ),
                    event_digest=str(row["event_digest"]),
                )
            )
        if len(matches) > 1:
            raise SessionEventIntegrityError(
                "Authorization receipt has multiple durable consumption events"
            )
        return matches[0] if matches else None

    @staticmethod
    def _validate_consumed_row_event(
        row: sqlite3.Row,
        event: SessionEventReceipt,
    ) -> None:
        if str(row["consumed_event_id"]) != event.event_id:
            raise SessionEventIntegrityError(
                "Consumed registry points to the wrong chronology event"
            )
        if str(row["session_id"]) != event.session_id:
            raise SessionEventIntegrityError(
                "Consumed registry session differs from chronology event"
            )
        expected = {
            "receipt_id": str(row["receipt_id"]),
            "receipt_digest": str(row["receipt_digest"]),
            "proposal_id": str(row["proposal_id"]),
            "proposal_digest": str(row["proposal_digest"]),
        }
        if dict(event.payload) != expected:
            raise SessionEventIntegrityError(
                "Consumed registry binding disagrees with exact chronology event"
            )

    @staticmethod
    def _validate_consumption_bijection(
        events: list[SessionEventReceipt],
        consumed_rows: list[sqlite3.Row],
    ) -> None:
        event_map: dict[str, SessionEventReceipt] = {}
        for event in events:
            if event.kind is not EventKind.AUTHORIZATION_CONSUMED:
                continue
            receipt_id = str(event.payload["receipt_id"])
            if receipt_id in event_map:
                raise SessionEventIntegrityError(
                    "Receipt has duplicate consumption events"
                )
            event_map[receipt_id] = event
        row_map = {str(row["receipt_id"]): row for row in consumed_rows}
        if set(event_map) != set(row_map):
            raise SessionEventIntegrityError(
                "Consumed registry and consumption chronology are not one-to-one"
            )
        for receipt_id, event in event_map.items():
            row = row_map[receipt_id]
            if (
                str(row["receipt_digest"]) != str(event.payload["receipt_digest"])
                or str(row["proposal_id"]) != str(event.payload["proposal_id"])
                or str(row["proposal_digest"]) != str(event.payload["proposal_digest"])
                or str(row["consumed_event_id"]) != event.event_id
            ):
                raise SessionEventIntegrityError(
                    "Consumed registry binding disagrees with exact chronology event"
                )


def _reject_constant(value: str) -> None:
    raise SessionEventIntegrityError(
        f"Invalid JSON constant in durable event: {value}"
    )


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SessionEventIntegrityError(
                f"Duplicate JSON key in durable event: {key}"
            )
        result[key] = value
    return result


def _strict_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise SessionEventIntegrityError(
            "Durable event payload is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise SessionEventIntegrityError(
            "Durable event payload must be a JSON object"
        )
    return value
