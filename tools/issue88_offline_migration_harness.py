"""Issue #88: clone-only offline migration preparation and claim contract fixture.

Never migrates the source WorkStore. Never installs a writer fence or enables
reconciliation. Use a NEW output directory and explicitly disposable DBs for
fixture claim operations. This is not a product WorkStore admission API.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal

from codexia_manual_agent.work_core import SqliteWorkStore

SCHEMA_VERSION = 1
STATE = "PREPARED_DISABLED"
_TABLES = {
    "g2_work_v1": (
        "work_id", "created_at", "objective", "source_namespace",
        "source_id", "ingress_payload_digest", "ingress_binding_digest",
        "work_digest",
    ),
    "g2_work_event_v1": (
        "event_id", "work_id", "sequence", "created_at", "kind",
        "payload_json", "previous_event_digest", "event_digest",
    ),
}


class OfflinePreparationRefused(RuntimeError):
    """Source/backup/clone evidence does not support preparation."""


class OfflineClaimConflict(OfflinePreparationRefused):
    """One provider result is claimed by different semantic input."""


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _text(value: str, field: str) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field} must be canonical nonempty text")


def _sha(value: str, field: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{field} must be lowercase SHA-256")


@contextmanager
def _connect(
    path: Path, *, readonly: bool = False
) -> Iterator[sqlite3.Connection]:
    if readonly:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path, timeout=10)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        with connection:
            yield connection
    finally:
        connection.close()


def _unique_columns(connection: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    found = set()
    for row in connection.execute(f"PRAGMA index_list({table})"):
        if row[2]:
            escaped = row[1].replace('"', '""')
            columns = tuple(
                item[2] for item in connection.execute(
                    f'PRAGMA index_info("{escaped}")'
                )
            )
            found.add(columns)
    return found


def _verify_core(connection: sqlite3.Connection) -> tuple[str, int, int]:
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise OfflinePreparationRefused("SQLite integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise OfflinePreparationRefused("SQLite foreign key integrity failed")
    expected_indexes = {
        "g2_work_v1": {("work_id",), ("source_namespace", "source_id")},
        "g2_work_event_v1": {("event_id",), ("work_id", "sequence")},
    }
    for name, columns in _TABLES.items():
        table = connection.execute(
            "SELECT type FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()
        actual = tuple(
            row[1] for row in connection.execute(f"PRAGMA table_info({name})")
        )
        if table != ("table",) or actual != columns:
            raise OfflinePreparationRefused(f"unexpected Gen2 table shape: {name}")
        if not expected_indexes[name].issubset(_unique_columns(connection, name)):
            raise OfflinePreparationRefused(f"missing Gen2 unique indexes: {name}")
    foreign_keys = {
        (row[2], row[3], row[4])
        for row in connection.execute("PRAGMA foreign_key_list(g2_work_event_v1)")
    }
    if ("g2_work_v1", "work_id", "work_id") not in foreign_keys:
        raise OfflinePreparationRefused("missing Gen2 event Work foreign key")
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name LIKE 'issue88_offline_%'"
    ).fetchone():
        raise OfflinePreparationRefused("Issue88 migration objects already exist")
    return _core_rows(connection)


def _core_rows(connection: sqlite3.Connection) -> tuple[str, int, int]:
    works = connection.execute(
        "SELECT * FROM g2_work_v1 ORDER BY work_id"
    ).fetchall()
    events = connection.execute(
        "SELECT * FROM g2_work_event_v1 ORDER BY work_id,sequence"
    ).fetchall()
    digest = _hash([works, events])
    return digest, len(works), len(events)


def _verify_projection(path: Path) -> None:
    store = SqliteWorkStore(path)
    with _connect(path, readonly=True) as connection:
        work_ids = [
            row[0] for row in connection.execute(
                "SELECT work_id FROM g2_work_v1 ORDER BY work_id"
            )
        ]
    for work_id in work_ids:
        store.snapshot(work_id)
        store.events(work_id)


@dataclass(frozen=True)
class Preparation:
    output_dir: Path
    core_digest: str
    work_count: int
    event_count: int
    state: str = STATE


def prepare_clone(
    source: str | Path,
    output_dir: str | Path,
    *,
    fault_at: Literal["backup", "restore", "schema"] | None = None,
) -> Preparation:
    """Create backup, verify independent restore, then prepare a disabled clone.

    Output directory must not exist. Partial failures leave quarantined files
    without a ready manifest; they are never applied to source.
    """
    source = Path(source)
    output = Path(output_dir)
    if not source.is_file() or source.is_symlink():
        raise OfflinePreparationRefused("source must be an existing regular file")
    if output.exists() or output.is_symlink():
        raise OfflinePreparationRefused("output directory must not already exist")
    if not output.parent.is_dir():
        raise OfflinePreparationRefused("output parent directory must exist")

    with _connect(source, readonly=True) as original:
        before = _verify_core(original)

    output.mkdir(exist_ok=False)
    backup_path = output / "backup.sqlite3"
    prepared_path = output / "prepared.sqlite3"
    manifest_path = output / "manifest.json"

    with _connect(source, readonly=True) as original:
        with _connect(backup_path) as backup:
            original.backup(backup)
    if fault_at == "backup":
        raise OfflinePreparationRefused("injected fault after backup")

    with _connect(backup_path, readonly=True) as backup:
        backed = _verify_core(backup)
    if backed != before:
        raise OfflinePreparationRefused("source changed across backup snapshot")
    _verify_projection(backup_path)

    with _connect(backup_path, readonly=True) as backup:
        with _connect(prepared_path) as restored:
            backup.backup(restored)
    if fault_at == "restore":
        raise OfflinePreparationRefused("injected fault after restore")

    with _connect(prepared_path) as prepared:
        after_restore = _verify_core(prepared)
    if after_restore != backed:
        raise OfflinePreparationRefused("restore does not match backup")
    _verify_projection(prepared_path)

    with _connect(prepared_path) as prepared:
        prepared.execute("BEGIN IMMEDIATE")
        try:
            prepared.execute("""
                CREATE TABLE issue88_offline_meta (
                    schema_version INTEGER PRIMARY KEY CHECK(schema_version=1),
                    state TEXT NOT NULL CHECK(state='PREPARED_DISABLED'),
                    core_digest TEXT NOT NULL
                )
            """)
            prepared.execute("""
                CREATE TABLE issue88_offline_claim (
                    result_key TEXT PRIMARY KEY,
                    provider_service TEXT NOT NULL,
                    provider_namespace TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    response_id TEXT NOT NULL,
                    work_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    handoff_id TEXT NOT NULL,
                    response_digest TEXT NOT NULL,
                    first_path TEXT NOT NULL CHECK(first_path IN ('live','recovery'))
                )
            """)
            prepared.execute(
                "INSERT INTO issue88_offline_meta VALUES (?,?,?)",
                (SCHEMA_VERSION, STATE, backed[0]),
            )
            prepared.commit()
        except BaseException:
            prepared.rollback()
            raise
    if fault_at == "schema":
        raise OfflinePreparationRefused("injected fault after schema preparation")

    with _connect(prepared_path, readonly=True) as prepared:
        if _core_rows(prepared) != backed:
            raise OfflinePreparationRefused("preparation mutated Work chronology")
        metadata = prepared.execute(
            "SELECT schema_version,state,core_digest FROM issue88_offline_meta"
        ).fetchone()
        if metadata != (SCHEMA_VERSION, STATE, backed[0]):
            raise OfflinePreparationRefused("prepared metadata is not canonical")
    with _connect(source, readonly=True) as original:
        if _verify_core(original) != before:
            raise OfflinePreparationRefused("source moved during preparation")

    record = {
        "schema_version": SCHEMA_VERSION,
        "state": STATE,
        "core_digest": backed[0],
        "work_count": backed[1],
        "event_count": backed[2],
        "backup": "backup.sqlite3",
        "prepared": "prepared.sqlite3",
    }
    with manifest_path.open("x", encoding="utf-8") as writer:
        json.dump(record, writer, sort_keys=True, indent=2)
        writer.write("\n")
    return Preparation(output, backed[0], backed[1], backed[2])


def inspect_prepared(output_dir: str | Path) -> Preparation:
    """Read-only validation of a complete, still-disabled clone."""
    output = Path(output_dir)
    record = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if record.get("schema_version") != SCHEMA_VERSION or record.get("state") != STATE:
        raise OfflinePreparationRefused("missing valid disabled manifest")
    with _connect(output / "backup.sqlite3", readonly=True) as backup:
        backed = _verify_core(backup)
    with _connect(output / "prepared.sqlite3", readonly=True) as clone:
        if _core_rows(clone) != backed:
            raise OfflinePreparationRefused("prepared Work chronology changed")
        state = clone.execute(
            "SELECT schema_version,state,core_digest FROM issue88_offline_meta"
        ).fetchone()
        if state != (SCHEMA_VERSION, STATE, backed[0]):
            raise OfflinePreparationRefused("prepared clone is not disabled")
        installed = clone.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'issue88_%'"
        ).fetchone()
        if installed:
            raise OfflinePreparationRefused("unexpected writer trigger")
    if (
        record.get("core_digest") != backed[0]
        or record.get("work_count") != backed[1]
        or record.get("event_count") != backed[2]
    ):
        raise OfflinePreparationRefused("manifest and backup differ")
    return Preparation(output, backed[0], backed[1], backed[2])


@dataclass(frozen=True)
class FixtureClaim:
    provider_service: str
    provider_namespace: str
    execution_id: str
    response_id: str
    work_id: str
    request_id: str
    handoff_id: str
    response_digest: str
    adapter_version: str = "v1"

    def __post_init__(self) -> None:
        for field in (
            "provider_service", "provider_namespace", "execution_id",
            "response_id", "work_id", "request_id", "handoff_id",
            "adapter_version",
        ):
            _text(getattr(self, field), field)
        _sha(self.response_digest, "response_digest")

    @property
    def result_key(self) -> str:
        return _hash([
            "issue88.provider-result.v0", self.provider_service,
            self.provider_namespace, self.execution_id, self.response_id,
        ])


def claim_fixture(
    output_dir: str | Path,
    claim: FixtureClaim,
    *,
    path: Literal["live", "recovery"],
    after_begin: Callable[[], None] | None = None,
) -> str:
    """Atomic index-only claim shared by *simulated* live/recovery codepaths.

    Deliberately cannot append a Work event or enable recovery. Neither
    source provenance nor an authorized CognitionOutcome is established.
    """
    if type(claim) is not FixtureClaim:
        raise TypeError("claim must be FixtureClaim")
    if path not in ("live", "recovery"):
        raise ValueError("path must be live or recovery")
    inspect_prepared(output_dir)
    db = Path(output_dir) / "prepared.sqlite3"
    with _connect(db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if after_begin is not None:
            after_begin()
        state = connection.execute(
            "SELECT schema_version,state FROM issue88_offline_meta"
        ).fetchone()
        if state != (SCHEMA_VERSION, STATE):
            raise OfflinePreparationRefused("recovery must remain disabled")
        existing = connection.execute(
            "SELECT provider_service,provider_namespace,execution_id,"
            "response_id,work_id,request_id,handoff_id,response_digest "
            "FROM issue88_offline_claim WHERE result_key=?",
            (claim.result_key,),
        ).fetchone()
        desired = (
            claim.provider_service, claim.provider_namespace,
            claim.execution_id, claim.response_id, claim.work_id,
            claim.request_id, claim.handoff_id, claim.response_digest,
        )
        if existing is not None:
            if existing != desired:
                raise OfflineClaimConflict("provider result already claimed elsewhere")
            return "ALREADY_CLAIMED"
        connection.execute(
            "INSERT INTO issue88_offline_claim VALUES (?,?,?,?,?,?,?,?,?,?)",
            (claim.result_key, *desired, path),
        )
        return "CLAIMED"
