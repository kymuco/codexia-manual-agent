from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from codexia_manual_agent.session_events.models import (
    EventKind,
    SessionEventIntegrityError,
    SessionEventReceipt,
    SessionEventStateError,
)
from codexia_manual_agent.session_events.recovery import (
    SessionRecovery,
    recover_session,
)
from codexia_manual_agent.session_events.sqlite_store import (
    SqliteSessionEventStore as _BaseSqliteSessionEventStore,
    _strict_json_object,
)


class _ClosingConnection:
    """Proxy one sqlite3 connection and always close it after a with-block.

    `sqlite3.Connection.__exit__` commits or rolls back a transaction but does not
    close the connection. POSIX can unlink an open SQLite file, which hid this
    lifetime bug; Windows correctly keeps the database/journal path busy. The
    store already scopes every internal connection with `with self._connect()`, so
    this proxy preserves the existing transaction semantics while adding the
    missing deterministic close.
    """

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._connection.__enter__()
        return self._connection

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool | None:
        try:
            return self._connection.__exit__(exc_type, exc, traceback)
        finally:
            self._connection.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class SqliteSessionEventStore(_BaseSqliteSessionEventStore):
    """Public M3.1 store with deterministic lifetime and replay-parity guards."""

    def _connect(self) -> _ClosingConnection:
        return _ClosingConnection(super()._connect())

    def load_events(self, session_id: str) -> tuple[SessionEventReceipt, ...]:
        events, _ = self._read_snapshot(session_id)
        return events

    def consumed_authorizations(self, session_id: str) -> dict[str, dict[str, str]]:
        _, consumed = self._read_snapshot(session_id)
        return consumed

    def recover(self, session_id: str) -> SessionRecovery:
        events, consumed = self._read_snapshot(session_id)
        return recover_session(events, consumed_authorizations=consumed)

    def is_authorization_consumed(self, receipt_id: str) -> bool:
        """Read registry/event consumption evidence from one SQLite snapshot."""

        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise ValueError("receipt_id must be a non-empty string")
        with self._connect() as connection:
            connection.execute("BEGIN")
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

    def _read_snapshot(
        self,
        session_id: str,
    ) -> tuple[tuple[SessionEventReceipt, ...], dict[str, dict[str, str]]]:
        """Read one coherent session/chronology/consumption SQLite snapshot."""

        with self._connect() as connection:
            # isolation_level=None puts the connection in SQLite autocommit mode.
            # An explicit read transaction is therefore required so session head,
            # events and consumption rows cannot come from different commits.
            connection.execute("BEGIN")
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
                SELECT receipt_id, session_id, receipt_digest, proposal_id,
                       proposal_digest, consumed_event_id, consumed_at
                FROM consumed_authorizations
                WHERE session_id = ?
                ORDER BY consumed_at, receipt_id
                """,
                (session_id,),
            ).fetchall()

            events = self._decode_snapshot_events(session, rows, consumed_rows)
            consumed = {
                str(row["receipt_id"]): {
                    "receipt_digest": str(row["receipt_digest"]),
                    "proposal_id": str(row["proposal_id"]),
                    "proposal_digest": str(row["proposal_digest"]),
                    "consumed_event_id": str(row["consumed_event_id"]),
                    "consumed_at": str(row["consumed_at"]),
                }
                for row in consumed_rows
            }
            return events, consumed

    def _decode_snapshot_events(
        self,
        session: sqlite3.Row,
        rows: list[sqlite3.Row],
        consumed_rows: list[sqlite3.Row],
    ) -> tuple[SessionEventReceipt, ...]:
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

    def _validate_runtime_transition(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        kind: EventKind,
        payload: Mapping[str, Any],
    ) -> None:
        # The base transition checker validates the full chronology first. Keep
        # historical identifier uniqueness here as an additional public-store
        # invariant so append() cannot create a chronology that pure recovery would
        # later reject. This executes inside the same BEGIN IMMEDIATE transaction as
        # publication, so concurrent writers cannot race the check.
        super()._validate_runtime_transition(
            connection,
            session_id=session_id,
            kind=kind,
            payload=payload,
        )
        known_run_ids, known_request_ids = self._historical_runtime_ids(
            connection,
            session_id,
        )
        if kind is EventKind.RUN_STARTED and str(payload["run_id"]) in known_run_ids:
            raise SessionEventStateError("run_id was reused in one persistent session")
        if (
            kind is EventKind.MODEL_REQUEST_STARTED
            and str(payload["request_id"]) in known_request_ids
        ):
            raise SessionEventStateError(
                "Provider request id was reused in one persistent session"
            )

    @staticmethod
    def _historical_runtime_ids(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> tuple[set[str], set[str]]:
        rows = connection.execute(
            """
            SELECT kind, payload_json
            FROM events
            WHERE session_id = ? AND kind IN (?, ?)
            ORDER BY sequence ASC
            """,
            (
                session_id,
                EventKind.RUN_STARTED.value,
                EventKind.MODEL_REQUEST_STARTED.value,
            ),
        ).fetchall()
        run_ids: set[str] = set()
        request_ids: set[str] = set()
        for row in rows:
            # The base transition replay above has already strict-parsed and
            # schema-validated every stored event in this transaction.
            event_kind = EventKind(str(row["kind"]))
            event_payload = json.loads(str(row["payload_json"]))
            if event_kind is EventKind.RUN_STARTED:
                run_id = str(event_payload["run_id"])
                if run_id in run_ids:
                    raise SessionEventIntegrityError(
                        "Persistent chronology reused a historical run_id"
                    )
                run_ids.add(run_id)
            else:
                request_id = str(event_payload["request_id"])
                if request_id in request_ids:
                    raise SessionEventIntegrityError(
                        "Persistent chronology reused a historical provider request id"
                    )
                request_ids.add(request_id)
        return run_ids, request_ids
