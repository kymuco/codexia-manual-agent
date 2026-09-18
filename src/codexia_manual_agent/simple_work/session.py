from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class SimpleWorkStatus(StrEnum):
    READY = "ready"
    RECONCILE_REQUIRED = "reconcile_required"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"


class WorkerMode(StrEnum):
    NONE = "none"
    TEMPORARY = "temporary"
    PERSISTENT = "persistent"


@dataclass(frozen=True, slots=True)
class SimpleCodexiaSession:
    session_id: str
    conversation_id: str | None
    created_at: str
    updated_at: str

    @classmethod
    def create(cls) -> "SimpleCodexiaSession":
        now = _now()
        return cls(
            session_id=str(uuid4()),
            conversation_id=None,
            created_at=now,
            updated_at=now,
        )

    def updated(self, **changes: object) -> "SimpleCodexiaSession":
        return replace(self, updated_at=_now(), **changes)


@dataclass(frozen=True, slots=True)
class SimpleWorkSession:
    work_id: str
    user_request: str
    status: SimpleWorkStatus
    worker_mode: WorkerMode
    worker_conversation_id: str | None
    last_codexia_text: str | None
    last_worker_text: str | None
    next_worker_message: str | None
    pending_human_question: str | None
    final_text: str | None
    worker_turns: int
    created_at: str
    updated_at: str

    @classmethod
    def create(cls, user_request: str) -> "SimpleWorkSession":
        request = user_request.strip()
        if not request:
            raise ValueError("user_request must be non-empty")
        now = _now()
        return cls(
            work_id=str(uuid4()),
            user_request=request,
            status=SimpleWorkStatus.READY,
            worker_mode=WorkerMode.NONE,
            worker_conversation_id=None,
            last_codexia_text=None,
            last_worker_text=None,
            next_worker_message=None,
            pending_human_question=None,
            final_text=None,
            worker_turns=0,
            created_at=now,
            updated_at=now,
        )

    def updated(self, **changes: object) -> "SimpleWorkSession":
        return replace(self, updated_at=_now(), **changes)


@dataclass(frozen=True, slots=True)
class SimpleWorkArtifact:
    artifact_id: int
    work_id: str
    worker_turn: int
    source_filename: str
    local_path: str
    size_bytes: int
    sha256: str
    source_conversation_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class SimpleWorkEvent:
    event_id: int
    work_id: str
    actor: str
    text: str
    conversation_id: str | None
    worker_mode: WorkerMode | None
    created_at: str


class SimpleWorkStore:
    """Minimal local continuity and transcript store for Simple Work.

    v1 tables intentionally coexist with the older v0 experiment tables so an
    existing pilot database can be reused without rewriting historical rows.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS simple_codexia_v1 (
                    slot INTEGER PRIMARY KEY CHECK(slot = 1),
                    session_id TEXT NOT NULL,
                    conversation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS simple_work_v1 (
                    work_id TEXT PRIMARY KEY,
                    user_request TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_mode TEXT NOT NULL,
                    worker_conversation_id TEXT,
                    last_codexia_text TEXT,
                    last_worker_text TEXT,
                    next_worker_message TEXT,
                    pending_human_question TEXT,
                    final_text TEXT,
                    worker_turns INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS simple_work_events_v1 (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    text TEXT NOT NULL,
                    conversation_id TEXT,
                    worker_mode TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS simple_work_artifacts_v1 (
                    artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id TEXT NOT NULL,
                    worker_turn INTEGER NOT NULL,
                    source_filename TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    source_conversation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(work_id, worker_turn, source_filename)
                )
                """
            )

    def codexia(self) -> SimpleCodexiaSession:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM simple_codexia_v1 WHERE slot = 1"
            ).fetchone()
            if row is None:
                session = SimpleCodexiaSession.create()
                connection.execute(
                    """
                    INSERT INTO simple_codexia_v1 (
                        slot, session_id, conversation_id, created_at, updated_at
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (
                        session.session_id,
                        session.conversation_id,
                        session.created_at,
                        session.updated_at,
                    ),
                )
                return session
        return SimpleCodexiaSession(
            session_id=row["session_id"],
            conversation_id=row["conversation_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def save_codexia(self, session: SimpleCodexiaSession) -> SimpleCodexiaSession:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO simple_codexia_v1 (
                    slot, session_id, conversation_id, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    session_id=excluded.session_id,
                    conversation_id=excluded.conversation_id,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at
                """,
                (
                    session.session_id,
                    session.conversation_id,
                    session.created_at,
                    session.updated_at,
                ),
            )
        return session

    def save(self, session: SimpleWorkSession) -> SimpleWorkSession:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO simple_work_v1 (
                    work_id, user_request, status, worker_mode,
                    worker_conversation_id, last_codexia_text, last_worker_text,
                    next_worker_message, pending_human_question, final_text,
                    worker_turns, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_id) DO UPDATE SET
                    user_request=excluded.user_request,
                    status=excluded.status,
                    worker_mode=excluded.worker_mode,
                    worker_conversation_id=excluded.worker_conversation_id,
                    last_codexia_text=excluded.last_codexia_text,
                    last_worker_text=excluded.last_worker_text,
                    next_worker_message=excluded.next_worker_message,
                    pending_human_question=excluded.pending_human_question,
                    final_text=excluded.final_text,
                    worker_turns=excluded.worker_turns,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at
                """,
                (
                    session.work_id,
                    session.user_request,
                    session.status.value,
                    session.worker_mode.value,
                    session.worker_conversation_id,
                    session.last_codexia_text,
                    session.last_worker_text,
                    session.next_worker_message,
                    session.pending_human_question,
                    session.final_text,
                    session.worker_turns,
                    session.created_at,
                    session.updated_at,
                ),
            )
        return session

    def load(self, work_id: str) -> SimpleWorkSession:
        candidate = work_id.strip()
        if not candidate:
            raise ValueError("work_id must be non-empty")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM simple_work_v1 WHERE work_id = ?",
                (candidate,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown simple work_id: {candidate}")
        return SimpleWorkSession(
            work_id=row["work_id"],
            user_request=row["user_request"],
            status=SimpleWorkStatus(row["status"]),
            worker_mode=WorkerMode(row["worker_mode"]),
            worker_conversation_id=row["worker_conversation_id"],
            last_codexia_text=row["last_codexia_text"],
            last_worker_text=row["last_worker_text"],
            next_worker_message=row["next_worker_message"],
            pending_human_question=row["pending_human_question"],
            final_text=row["final_text"],
            worker_turns=int(row["worker_turns"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def append_event(
        self,
        *,
        work_id: str,
        actor: str,
        text: str,
        conversation_id: str | None = None,
        worker_mode: WorkerMode | None = None,
    ) -> None:
        value = text.strip()
        if not value:
            raise ValueError("event text must be non-empty")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO simple_work_events_v1 (
                    work_id, actor, text, conversation_id, worker_mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    work_id,
                    actor,
                    value,
                    conversation_id,
                    None if worker_mode is None else worker_mode.value,
                    _now(),
                ),
            )

    def save_artifact(
        self,
        *,
        work_id: str,
        worker_turn: int,
        source_filename: str,
        local_path: str,
        size_bytes: int,
        sha256: str,
        source_conversation_id: str,
    ) -> SimpleWorkArtifact:
        if worker_turn <= 0:
            raise ValueError("worker_turn must be positive")
        if size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        created_at = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO simple_work_artifacts_v1 (
                    work_id, worker_turn, source_filename, local_path,
                    size_bytes, sha256, source_conversation_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_id, worker_turn, source_filename) DO NOTHING
                """,
                (
                    work_id,
                    worker_turn,
                    source_filename,
                    local_path,
                    size_bytes,
                    sha256,
                    source_conversation_id,
                    created_at,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM simple_work_artifacts_v1
                WHERE work_id = ? AND worker_turn = ? AND source_filename = ?
                """,
                (work_id, worker_turn, source_filename),
            ).fetchone()
        if row is None:
            raise RuntimeError("artifact row was not persisted")
        return SimpleWorkArtifact(
            artifact_id=int(row["artifact_id"]),
            work_id=row["work_id"],
            worker_turn=int(row["worker_turn"]),
            source_filename=row["source_filename"],
            local_path=row["local_path"],
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            source_conversation_id=row["source_conversation_id"],
            created_at=row["created_at"],
        )

    def artifacts(self, work_id: str) -> tuple[SimpleWorkArtifact, ...]:
        self.load(work_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM simple_work_artifacts_v1
                WHERE work_id = ?
                ORDER BY worker_turn ASC, artifact_id ASC
                """,
                (work_id,),
            ).fetchall()
        return tuple(
            SimpleWorkArtifact(
                artifact_id=int(row["artifact_id"]),
                work_id=row["work_id"],
                worker_turn=int(row["worker_turn"]),
                source_filename=row["source_filename"],
                local_path=row["local_path"],
                size_bytes=int(row["size_bytes"]),
                sha256=row["sha256"],
                source_conversation_id=row["source_conversation_id"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def history(self, work_id: str) -> tuple[SimpleWorkEvent, ...]:
        self.load(work_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM simple_work_events_v1
                WHERE work_id = ?
                ORDER BY event_id ASC
                """,
                (work_id,),
            ).fetchall()
        return tuple(
            SimpleWorkEvent(
                event_id=int(row["event_id"]),
                work_id=row["work_id"],
                actor=row["actor"],
                text=row["text"],
                conversation_id=row["conversation_id"],
                worker_mode=(
                    None
                    if row["worker_mode"] is None
                    else WorkerMode(row["worker_mode"])
                ),
                created_at=row["created_at"],
            )
            for row in rows
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
