"""Issue #88 offline SQLite contract experiment (NOT an admission runtime API).

Only use with a disposable SQLite database. The trigger is intentionally
incompatible with existing live role callbacks. No provider, WorkStore
migration, production registration, or historical coverage verifier exists.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import NAMESPACE_URL, uuid5

SPIKE_OUTCOME_KIND = "issue88.spike-outcome"
_FIXED_TIMESTAMP = "2026-10-08T00:00:00+00:00"


class RecoveryRefused(RuntimeError):
    """Storage evidence or admission authority is missing or changed."""


class ProviderResultClaimed(RecoveryRefused):
    """The provider result is assigned to another handoff or digest."""


class StaleWorkHead(RecoveryRefused):
    """The target Work chronology changed after authorizing the candidate."""


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _nonempty(value: str, label: str) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} must be nonempty canonical text")


def _sha(value: str, label: str) -> None:
    if type(value) is not str or len(value) != 64 or any(
        c not in "0123456789abcdef" for c in value
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class Candidate:
    work_id: str
    request_id: str
    handoff_id: str
    provider_service: str
    provider_namespace: str
    execution_id: str
    response_id: str
    response_digest: str
    expected_revision: int
    expected_head_digest: str | None
    authorized_epoch: int
    authorized_coverage_digest: str
    adapter_version: str = "v1"  # Audit only; never part of uniqueness key.

    def __post_init__(self) -> None:
        for name in (
            "work_id",
            "request_id",
            "handoff_id",
            "provider_service",
            "provider_namespace",
            "execution_id",
            "response_id",
            "adapter_version",
        ):
            _nonempty(getattr(self, name), name)
        for name in ("response_digest", "authorized_coverage_digest"):
            _sha(getattr(self, name), name)
        if self.expected_head_digest is not None:
            _sha(self.expected_head_digest, "expected_head_digest")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError("expected_revision must be >= 0")
        if type(self.authorized_epoch) is not int or self.authorized_epoch < 1:
            raise ValueError("authorized_epoch must be >= 1")

    @property
    def result_key(self) -> str:
        return _digest([
            "issue88.result-claim.v0",
            self.provider_service,
            self.provider_namespace,
            self.execution_id,
            self.response_id,
        ])

    @property
    def outcome_event_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, "issue88.spike-outcome:" + _digest([
            self.work_id,
            self.request_id,
            self.handoff_id,
            self.result_key,
            self.response_digest,
        ])))


class OfflineSqliteContractSpike:
    """Scratch DB-only hypothesis for an eventual storage-side claim/CAS gate.

    Reads the real Gen2 SQLite table shape but uses a synthetic Work event.
    The caller is responsible for proving provider observations and historical
    coverage; fixture_enable deliberately cannot certify those facts.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def install_on_disposable_db(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='g2_work_event_v1'"
                ).fetchone()
                if exists is None:
                    raise RecoveryRefused("expected existing Gen2 event table")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS issue88_spike_authority (
                        provider_namespace TEXT PRIMARY KEY,
                        epoch INTEGER NOT NULL,
                        coverage_digest TEXT NOT NULL,
                        enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS issue88_spike_claim (
                        result_key TEXT PRIMARY KEY,
                        provider_namespace TEXT NOT NULL,
                        work_id TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        handoff_id TEXT NOT NULL,
                        response_digest TEXT NOT NULL,
                        event_id TEXT NOT NULL UNIQUE
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS issue88_spike_intent (
                        event_id TEXT PRIMARY KEY,
                        result_key TEXT NOT NULL,
                        provider_namespace TEXT NOT NULL,
                        work_id TEXT NOT NULL,
                        epoch INTEGER NOT NULL,
                        coverage_digest TEXT NOT NULL
                    )
                """)
                connection.execute("""
                    CREATE TRIGGER IF NOT EXISTS issue88_spike_fence
                    BEFORE INSERT ON g2_work_event_v1
                    WHEN NEW.kind IN (
                        'role.completed', 'role.failed',
                        'role.outcome-unknown', 'issue88.spike-outcome'
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'ISSUE88_SPIKE_LEGACY_WRITER_FENCED')
                        WHERE NOT EXISTS (
                            SELECT 1 FROM issue88_spike_intent i
                            JOIN issue88_spike_claim c ON c.result_key=i.result_key
                            JOIN issue88_spike_authority a
                              ON a.provider_namespace=i.provider_namespace
                            WHERE i.event_id=NEW.event_id
                              AND i.work_id=NEW.work_id
                              AND c.event_id=NEW.event_id
                              AND c.work_id=NEW.work_id
                              AND c.provider_namespace=i.provider_namespace
                              AND a.enabled=1 AND a.epoch=i.epoch
                              AND a.coverage_digest=i.coverage_digest
                        );
                    END
                """)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def fixture_enable(
        self, namespace: str, *, epoch: int, coverage_digest: str
    ) -> None:
        """Fixture-only seed; does not attest to live-writer/historical coverage."""
        _nonempty(namespace, "namespace")
        _sha(coverage_digest, "coverage_digest")
        if type(epoch) is not int or epoch < 1:
            raise ValueError("epoch must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                old = connection.execute(
                    "SELECT epoch FROM issue88_spike_authority "
                    "WHERE provider_namespace=?",
                    (namespace,),
                ).fetchone()
                if old is not None and epoch <= old[0]:
                    raise RecoveryRefused("epoch must advance")
                connection.execute("""
                    INSERT INTO issue88_spike_authority VALUES (?, ?, ?, 1)
                    ON CONFLICT(provider_namespace) DO UPDATE SET
                        epoch=excluded.epoch,
                        coverage_digest=excluded.coverage_digest, enabled=1
                """, (namespace, epoch, coverage_digest))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def revoke(
        self, namespace: str, *, epoch: int, coverage_digest: str | None = None
    ) -> None:
        _nonempty(namespace, "namespace")
        if type(epoch) is not int or epoch < 1:
            raise ValueError("epoch must be positive")
        if coverage_digest is not None:
            _sha(coverage_digest, "coverage_digest")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                old = connection.execute(
                    "SELECT epoch, coverage_digest FROM issue88_spike_authority "
                    "WHERE provider_namespace=?",
                    (namespace,),
                ).fetchone()
                if old is None or epoch < old[0]:
                    raise RecoveryRefused("unknown or stale authority")
                connection.execute("""
                    UPDATE issue88_spike_authority
                    SET epoch=?, coverage_digest=?, enabled=0
                    WHERE provider_namespace=?
                """, (epoch, coverage_digest or old[1], namespace))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def work_head(self, work_id: str) -> tuple[int, str | None]:
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM g2_work_v1 WHERE work_id=?", (work_id,)
            ).fetchone() is None:
                raise RecoveryRefused("unknown Work")
            row = connection.execute("""
                SELECT sequence,event_digest FROM g2_work_event_v1
                WHERE work_id=? ORDER BY sequence DESC LIMIT 1
            """, (work_id,)).fetchone()
            return (row[0], row[1]) if row else (0, None)

    def admit_fixture(
        self,
        candidate: Candidate,
        *,
        after_final_authority_read: Callable[[], None] | None = None,
    ) -> str:
        """Atomic synthetic event append; not RoleAdmission or WorkCompletion."""
        if not isinstance(candidate, Candidate):
            raise TypeError("candidate must be Candidate")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("""
                SELECT epoch,coverage_digest,enabled FROM issue88_spike_authority
                WHERE provider_namespace=?
            """, (candidate.provider_namespace,)).fetchone()
            if (
                current is None
                or current[2] != 1
                or current[0] != candidate.authorized_epoch
                or current[1] != candidate.authorized_coverage_digest
            ):
                raise RecoveryRefused("current recovery epoch or coverage unauthorized")
            if after_final_authority_read is not None:
                after_final_authority_read()

            old_claim = connection.execute("""
                SELECT provider_namespace,work_id,request_id,handoff_id,
                       response_digest,event_id FROM issue88_spike_claim
                WHERE result_key=?
            """, (candidate.result_key,)).fetchone()
            desired = (
                candidate.provider_namespace,
                candidate.work_id,
                candidate.request_id,
                candidate.handoff_id,
                candidate.response_digest,
                candidate.outcome_event_id,
            )
            if old_claim is not None:
                if tuple(old_claim) != desired:
                    raise ProviderResultClaimed("result was already assigned elsewhere")
                event = connection.execute(
                    "SELECT 1 FROM g2_work_event_v1 WHERE event_id=?",
                    (candidate.outcome_event_id,),
                ).fetchone()
                if event is None:
                    raise RecoveryRefused("claim exists without its outcome event")
                connection.execute("COMMIT")
                return "ALREADY_ADMITTED"

            if connection.execute(
                "SELECT 1 FROM g2_work_v1 WHERE work_id=?", (candidate.work_id,)
            ).fetchone() is None:
                raise RecoveryRefused("unknown Work")
            head = connection.execute("""
                SELECT sequence,event_digest,kind FROM g2_work_event_v1
                WHERE work_id=? ORDER BY sequence DESC LIMIT 1
            """, (candidate.work_id,)).fetchone()
            revision, digest = (head[0], head[1]) if head else (0, None)
            if (
                revision != candidate.expected_revision
                or digest != candidate.expected_head_digest
            ):
                raise StaleWorkHead("stale expected revision or digest")
            if head and head[2] in ("work.completed", "work.cancelled"):
                raise RecoveryRefused("terminal Work cannot append")

            event_id = candidate.outcome_event_id
            seq = revision + 1
            payload = {
                "schema_version": 1,
                "event_id": event_id,
                "work_id": candidate.work_id,
                "sequence": seq,
                "created_at": _FIXED_TIMESTAMP,
                "kind": SPIKE_OUTCOME_KIND,
                "payload": {
                    "claim_key": candidate.result_key,
                    "handoff_id": candidate.handoff_id,
                    "request_id": candidate.request_id,
                    "response_digest": candidate.response_digest,
                },
                "previous_event_digest": digest,
            }
            event_digest = _digest(payload)
            connection.execute("""
                INSERT INTO issue88_spike_claim VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (candidate.result_key, *desired))
            connection.execute("""
                INSERT INTO issue88_spike_intent VALUES (?, ?, ?, ?, ?, ?)
            """, (
                event_id, candidate.result_key, candidate.provider_namespace,
                candidate.work_id, candidate.authorized_epoch,
                candidate.authorized_coverage_digest,
            ))
            connection.execute("""
                INSERT INTO g2_work_event_v1 (
                    event_id,work_id,sequence,created_at,kind,payload_json,
                    previous_event_digest,event_digest
                ) VALUES (?,?,?,?,?,?,?,?)
            """, (
                event_id, candidate.work_id, seq, _FIXED_TIMESTAMP,
                SPIKE_OUTCOME_KIND, json.dumps(
                    payload["payload"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ), digest, event_digest,
            ))
            connection.execute(
                "DELETE FROM issue88_spike_intent WHERE event_id=?", (event_id,)
            )
            connection.execute("COMMIT")
            return "ADMITTED"
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def counts(self) -> tuple[int, int, int]:
        with self._connect() as connection:
            claim = connection.execute(
                "SELECT COUNT(*) FROM issue88_spike_claim"
            ).fetchone()[0]
            events = connection.execute(
                "SELECT COUNT(*) FROM g2_work_event_v1 WHERE kind=?",
                (SPIKE_OUTCOME_KIND,),
            ).fetchone()[0]
            intents = connection.execute(
                "SELECT COUNT(*) FROM issue88_spike_intent"
            ).fetchone()[0]
            return claim, events, intents
