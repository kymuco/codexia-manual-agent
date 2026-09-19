from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Protocol

from codexia_manual_agent.work_core.models import (
    WORK_CANCELLED_EVENT,
    WORK_COMPLETED_EVENT,
    InvalidWorkCoreRecord,
    Work,
    WorkEvent,
    WorkIngressBinding,
    WorkSnapshot,
    WorkState,
)


class WorkStoreError(RuntimeError):
    """Base class for Gen2 work-store failures."""


class WorkNotFoundError(WorkStoreError):
    """Requested Work does not exist."""


class WorkIngressConflictError(WorkStoreError):
    """One ingress identity was reused for different semantic input."""


class WorkIdentityConflictError(WorkStoreError):
    """A work/event identity was reused for different exact content."""


class WorkConcurrencyError(WorkStoreError):
    """Candidate transition was derived from a stale Work revision."""


class WorkStateError(WorkStoreError):
    """Requested transition is invalid for the current Work lifecycle."""


class WorkPersistenceIntegrityError(WorkStoreError):
    """Durable Work bytes or chronology fail exact integrity validation."""


class WorkStore(Protocol):
    def create(self, work: Work) -> WorkSnapshot: ...

    def snapshot(self, work_id: str) -> WorkSnapshot: ...

    def events(self, work_id: str) -> tuple[WorkEvent, ...]: ...

    def append(
        self,
        work_id: str,
        *,
        expected_revision: int,
        event: WorkEvent,
    ) -> WorkSnapshot: ...


class SqliteWorkStore:
    """SQLite proof implementation for the Gen2 Work truth boundary.

    Canonical properties:
    - external ingress is idempotent by exact source identity + payload binding;
    - chronology is append-only and hash chained;
    - every transition is compare-and-append against an exact revision;
    - an exact event retry is idempotent after an ambiguous caller acknowledgement;
    - recovery derives lifecycle only from durable records and never performs work.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS g2_work_v1 (
                    work_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    source_namespace TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    ingress_payload_digest TEXT NOT NULL,
                    ingress_binding_digest TEXT NOT NULL,
                    work_digest TEXT NOT NULL,
                    UNIQUE(source_namespace, source_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS g2_work_event_v1 (
                    event_id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_event_digest TEXT,
                    event_digest TEXT NOT NULL,
                    UNIQUE(work_id, sequence),
                    FOREIGN KEY(work_id) REFERENCES g2_work_v1(work_id)
                )
                """
            )

    def create(self, work: Work) -> WorkSnapshot:
        if not isinstance(work, Work):
            raise TypeError("work must be Work")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            ingress_row = connection.execute(
                """
                SELECT * FROM g2_work_v1
                WHERE source_namespace = ? AND source_id = ?
                """,
                (work.ingress.source_namespace, work.ingress.source_id),
            ).fetchone()
            if ingress_row is not None:
                existing = self._work_from_row(ingress_row)
                if not existing.same_ingress_semantics(work):
                    raise WorkIngressConflictError(
                        "Ingress identity is already bound to different exact Work input"
                    )
                snapshot = self._snapshot_in_connection(connection, existing.work_id)
                connection.commit()
                return snapshot

            identity_row = connection.execute(
                "SELECT * FROM g2_work_v1 WHERE work_id = ?",
                (work.work_id,),
            ).fetchone()
            if identity_row is not None:
                existing = self._work_from_row(identity_row)
                if existing.to_dict() != work.to_dict():
                    raise WorkIdentityConflictError(
                        "work_id is already bound to different exact Work bytes"
                    )
                snapshot = self._snapshot_in_connection(connection, existing.work_id)
                connection.commit()
                return snapshot

            connection.execute(
                """
                INSERT INTO g2_work_v1 (
                    work_id,
                    created_at,
                    objective,
                    source_namespace,
                    source_id,
                    ingress_payload_digest,
                    ingress_binding_digest,
                    work_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    work.work_id,
                    work.created_at,
                    work.objective,
                    work.ingress.source_namespace,
                    work.ingress.source_id,
                    work.ingress.payload_digest,
                    work.ingress.binding_digest,
                    work.work_digest,
                ),
            )
            snapshot = WorkSnapshot(
                work=work,
                state=WorkState.ACTIVE,
                revision=0,
                last_event_digest=None,
                terminal_event_id=None,
            )
            connection.commit()
            return snapshot
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def snapshot(self, work_id: str) -> WorkSnapshot:
        with self._connect() as connection:
            return self._snapshot_in_connection(connection, work_id)

    def events(self, work_id: str) -> tuple[WorkEvent, ...]:
        with self._connect() as connection:
            self._require_work_row(connection, work_id)
            return self._events_in_connection(connection, work_id)

    def append(
        self,
        work_id: str,
        *,
        expected_revision: int,
        event: WorkEvent,
    ) -> WorkSnapshot:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if not isinstance(event, WorkEvent):
            raise TypeError("event must be WorkEvent")
        if event.work_id != work_id:
            raise WorkIdentityConflictError("WorkEvent is bound to another work_id")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_work_row(connection, work_id)

            event_id_row = connection.execute(
                "SELECT * FROM g2_work_event_v1 WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if event_id_row is not None:
                existing = self._event_from_row(event_id_row)
                if existing.to_dict() != event.to_dict():
                    raise WorkIdentityConflictError(
                        "event_id is already bound to different exact WorkEvent bytes"
                    )
                snapshot = self._snapshot_in_connection(connection, work_id)
                connection.commit()
                return snapshot

            sequence_row = connection.execute(
                """
                SELECT * FROM g2_work_event_v1
                WHERE work_id = ? AND sequence = ?
                """,
                (work_id, event.sequence),
            ).fetchone()
            if sequence_row is not None:
                existing = self._event_from_row(sequence_row)
                if existing.to_dict() == event.to_dict():
                    snapshot = self._snapshot_in_connection(connection, work_id)
                    connection.commit()
                    return snapshot
                raise WorkConcurrencyError(
                    "Candidate sequence is already occupied by another WorkEvent"
                )

            snapshot = self._snapshot_in_connection(connection, work_id)
            if snapshot.revision != expected_revision:
                raise WorkConcurrencyError(
                    f"Stale Work revision: expected={expected_revision} "
                    f"actual={snapshot.revision}"
                )
            if snapshot.state is not WorkState.ACTIVE:
                raise WorkStateError(
                    f"Work is {snapshot.state.value}; terminal Work cannot append events"
                )
            if event.sequence != expected_revision + 1:
                raise WorkConcurrencyError(
                    "WorkEvent sequence does not bind the expected revision"
                )
            if event.previous_event_digest != snapshot.last_event_digest:
                raise WorkConcurrencyError(
                    "WorkEvent previous digest does not bind the exact snapshot"
                )

            connection.execute(
                """
                INSERT INTO g2_work_event_v1 (
                    event_id,
                    work_id,
                    sequence,
                    created_at,
                    kind,
                    payload_json,
                    previous_event_digest,
                    event_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.work_id,
                    event.sequence,
                    event.created_at,
                    event.kind,
                    json.dumps(
                        event.to_dict()["payload"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    event.previous_event_digest,
                    event.event_digest,
                ),
            )
            result = self._snapshot_in_connection(connection, work_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _snapshot_in_connection(
        self,
        connection: sqlite3.Connection,
        work_id: str,
    ) -> WorkSnapshot:
        work = self._work_from_row(self._require_work_row(connection, work_id))
        events = self._events_in_connection(connection, work_id)

        state = WorkState.ACTIVE
        terminal_event_id: str | None = None
        for event in events:
            if state is not WorkState.ACTIVE:
                raise WorkPersistenceIntegrityError(
                    "Persisted chronology contains an event after terminal Work state"
                )
            if event.kind == WORK_COMPLETED_EVENT:
                state = WorkState.COMPLETED
                terminal_event_id = event.event_id
            elif event.kind == WORK_CANCELLED_EVENT:
                state = WorkState.CANCELLED
                terminal_event_id = event.event_id

        return WorkSnapshot(
            work=work,
            state=state,
            revision=len(events),
            last_event_digest=(events[-1].event_digest if events else None),
            terminal_event_id=terminal_event_id,
        )

    def _events_in_connection(
        self,
        connection: sqlite3.Connection,
        work_id: str,
    ) -> tuple[WorkEvent, ...]:
        rows = connection.execute(
            """
            SELECT * FROM g2_work_event_v1
            WHERE work_id = ?
            ORDER BY sequence ASC
            """,
            (work_id,),
        ).fetchall()

        events: list[WorkEvent] = []
        previous: WorkEvent | None = None
        for index, row in enumerate(rows, start=1):
            event = self._event_from_row(row)
            if event.work_id != work_id:
                raise WorkPersistenceIntegrityError(
                    "Persisted event crosses Work identity"
                )
            if event.sequence != index:
                raise WorkPersistenceIntegrityError(
                    "Persisted Work chronology has a sequence gap/reorder"
                )
            expected_previous = None if previous is None else previous.event_digest
            if event.previous_event_digest != expected_previous:
                raise WorkPersistenceIntegrityError(
                    "Persisted Work chronology has a broken hash chain"
                )
            events.append(event)
            previous = event
        return tuple(events)

    @staticmethod
    def _require_work_row(
        connection: sqlite3.Connection,
        work_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM g2_work_v1 WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        if row is None:
            raise WorkNotFoundError(f"Unknown work_id: {work_id}")
        return row

    @staticmethod
    def _work_from_row(row: sqlite3.Row) -> Work:
        try:
            ingress = WorkIngressBinding(
                schema_version=1,
                source_namespace=row["source_namespace"],
                source_id=row["source_id"],
                payload_digest=row["ingress_payload_digest"],
                binding_digest=row["ingress_binding_digest"],
            )
            return Work(
                schema_version=1,
                work_id=row["work_id"],
                created_at=row["created_at"],
                objective=row["objective"],
                ingress=ingress,
                work_digest=row["work_digest"],
            )
        except (InvalidWorkCoreRecord, KeyError, TypeError, ValueError) as exc:
            raise WorkPersistenceIntegrityError(
                "Persisted Work row fails integrity validation"
            ) from exc

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> WorkEvent:
        try:
            payload = json.loads(row["payload_json"])
            return WorkEvent(
                schema_version=1,
                event_id=row["event_id"],
                work_id=row["work_id"],
                sequence=row["sequence"],
                created_at=row["created_at"],
                kind=row["kind"],
                payload=payload,
                previous_event_digest=row["previous_event_digest"],
                event_digest=row["event_digest"],
            )
        except (
            InvalidWorkCoreRecord,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise WorkPersistenceIntegrityError(
                "Persisted WorkEvent row fails integrity validation"
            ) from exc
