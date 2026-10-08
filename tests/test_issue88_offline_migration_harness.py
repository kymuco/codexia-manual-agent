"""Offline Issue #88 backup/restore and two-path claim experiments."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from threading import Event, Thread
from uuid import uuid4

import pytest

from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkIngressBinding,
)
from tools.issue88_offline_migration_harness import (
    FixtureClaim,
    OfflineClaimConflict,
    OfflinePreparationRefused,
    claim_fixture,
    inspect_prepared,
    prepare_clone,
)


@pytest.fixture
def original(tmp_path):
    source = tmp_path / "source.sqlite3"
    store = SqliteWorkStore(source)
    ingress = WorkIngressBinding.create(
        source_namespace="issue88.offline",
        source_id=str(uuid4()),
        payload_digest="c" * 64,
    )
    work = Work.create(objective="offline migration backup", ingress=ingress)
    store.create(work)
    note = store.snapshot(work.work_id).next_event(
        kind="work.note", payload={"unicode": "готово"}
    )
    store.append(work.work_id, expected_revision=0, event=note)
    return source, work, note


def _claim(**changes):
    return replace(FixtureClaim(
        provider_service="provider-x",
        provider_namespace="account-x",
        execution_id="turn-1",
        response_id="message-1",
        work_id="work-a",
        request_id="request-a",
        handoff_id="handoff-a",
        response_digest="a" * 64,
    ), **changes)


def test_backup_restore_preserves_real_work_without_touching_source(original, tmp_path):
    source, work, note = original
    with sqlite3.connect(source) as cx:
        before_objects = cx.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    result = prepare_clone(source, tmp_path / "prepared")
    assert result.state == "PREPARED_DISABLED"
    assert result.work_count == 1 and result.event_count == 1
    assert inspect_prepared(result.output_dir) == result
    with sqlite3.connect(source) as cx:
        after_objects = cx.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    assert before_objects == after_objects
    assert not any("issue88_offline" in row[1] for row in after_objects)

    for name in ("backup.sqlite3", "prepared.sqlite3"):
        copy = SqliteWorkStore(result.output_dir / name)
        assert copy.events(work.work_id) == (note,)
        assert copy.snapshot(work.work_id).revision == 1
    with sqlite3.connect(result.output_dir / "prepared.sqlite3") as cx:
        meta = cx.execute(
            "SELECT schema_version,state,core_digest FROM issue88_offline_meta"
        ).fetchone()
        assert meta == (1, "PREPARED_DISABLED", result.core_digest)
        assert cx.execute(
            "SELECT COUNT(*) FROM issue88_offline_claim"
        ).fetchone() == (0,)
        assert cx.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='trigger' AND name LIKE 'issue88_%'"
        ).fetchone() == (0,)


def test_wal_source_is_backed_up_with_committed_event(original, tmp_path):
    source, work, note = original
    with sqlite3.connect(source) as cx:
        assert cx.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    another = SqliteWorkStore(source)
    event = another.snapshot(work.work_id).next_event(
        kind="work.note", payload={"wal": "included"}
    )
    another.append(work.work_id, expected_revision=1, event=event)
    snapshot = prepare_clone(source, tmp_path / "wal-snapshot")
    assert snapshot.event_count == 2
    restored = SqliteWorkStore(snapshot.output_dir / "prepared.sqlite3")
    assert restored.events(work.work_id) == (note, event)


@pytest.mark.parametrize("fault_at", ["backup", "restore", "schema"])
def test_injected_failure_leaves_no_ready_manifest(original, tmp_path, fault_at):
    source, _, _ = original
    out = tmp_path / fault_at
    with pytest.raises(OfflinePreparationRefused, match="injected fault"):
        prepare_clone(source, out, fault_at=fault_at)
    assert not (out / "manifest.json").exists()
    with pytest.raises(FileNotFoundError):
        inspect_prepared(out)
    assert source.is_file()
    with sqlite3.connect(source) as connection:
        assert not any(
            "issue88_offline" in row[0]
            for row in connection.execute("SELECT name FROM sqlite_master")
        )


def test_missing_core_table_fails_before_creating_output(tmp_path):
    source = tmp_path / "invalid.sqlite3"
    sqlite3.connect(source).close()
    dest = tmp_path / "output"
    with pytest.raises(OfflinePreparationRefused, match="unexpected Gen2 table"):
        prepare_clone(source, dest)
    assert not dest.exists()


def test_missing_unique_indexes_are_not_accepted(tmp_path):
    source = tmp_path / "no-indexes.sqlite3"
    with sqlite3.connect(source) as cx:
        cx.execute("""
            CREATE TABLE g2_work_v1 (
                work_id TEXT, created_at TEXT, objective TEXT,
                source_namespace TEXT, source_id TEXT,
                ingress_payload_digest TEXT, ingress_binding_digest TEXT,
                work_digest TEXT
            )
        """)
        cx.execute("""
            CREATE TABLE g2_work_event_v1 (
                event_id TEXT, work_id TEXT, sequence INTEGER,
                created_at TEXT, kind TEXT, payload_json TEXT,
                previous_event_digest TEXT, event_digest TEXT,
                FOREIGN KEY(work_id) REFERENCES g2_work_v1(work_id)
            )
        """)
    with pytest.raises(OfflinePreparationRefused, match="unique indexes"):
        prepare_clone(source, tmp_path / "do-not-clone")


def test_partial_unique_indexes_do_not_meet_core_schema(tmp_path):
    source = tmp_path / "partial-unique.sqlite3"
    with sqlite3.connect(source) as cx:
        cx.execute("""
            CREATE TABLE g2_work_v1 (
                work_id TEXT PRIMARY KEY, created_at TEXT, objective TEXT,
                source_namespace TEXT, source_id TEXT,
                ingress_payload_digest TEXT, ingress_binding_digest TEXT,
                work_digest TEXT
            )
        """)
        cx.execute("""
            CREATE UNIQUE INDEX subset_work_ingress ON g2_work_v1
            (source_namespace, source_id) WHERE source_id <> 'hidden'
        """)
        cx.execute("""
            CREATE TABLE g2_work_event_v1 (
                event_id TEXT PRIMARY KEY,
                work_id TEXT, sequence INTEGER, created_at TEXT, kind TEXT,
                payload_json TEXT, previous_event_digest TEXT,
                event_digest TEXT,
                FOREIGN KEY(work_id) REFERENCES g2_work_v1(work_id)
            )
        """)
        cx.execute("""
            CREATE UNIQUE INDEX subset_event_sequence ON g2_work_event_v1
            (work_id,sequence) WHERE sequence > 0
        """)
    with pytest.raises(OfflinePreparationRefused, match="unique indexes"):
        prepare_clone(source, tmp_path / "reject-partial")


def test_prepared_core_schema_tamper_is_rejected(original, tmp_path):
    source, _, _ = original
    out = tmp_path / "tamper-schema"
    prepare_clone(source, out)
    with sqlite3.connect(out / "prepared.sqlite3") as cx:
        cx.execute("""
            CREATE TABLE issue88_bad_core AS
            SELECT * FROM g2_work_event_v1
        """)
        cx.execute("DROP TABLE g2_work_event_v1")
        cx.execute("ALTER TABLE issue88_bad_core RENAME TO g2_work_event_v1")
    with pytest.raises(OfflinePreparationRefused, match="unique indexes"):
        inspect_prepared(out)
    with pytest.raises(OfflinePreparationRefused, match="unique indexes"):
        claim_fixture(out, _claim(), path="recovery")


def test_non_fresh_destination_is_refused(original, tmp_path):
    source, _, _ = original
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(OfflinePreparationRefused, match="must not already exist"):
        prepare_clone(source, target)


def test_existing_migration_objects_are_refused(original, tmp_path):
    source, _, _ = original
    with sqlite3.connect(source) as cx:
        cx.execute("CREATE TABLE issue88_offline_unknown (x INTEGER)")
    with pytest.raises(OfflinePreparationRefused, match="already exist"):
        prepare_clone(source, tmp_path / "bad")


def test_modified_backup_fails_readonly_validation(original, tmp_path):
    source, _, _ = original
    out = tmp_path / "safe"
    prepare_clone(source, out)
    with sqlite3.connect(out / "backup.sqlite3") as cx:
        cx.execute("UPDATE g2_work_v1 SET objective='changed'")
    with pytest.raises(OfflinePreparationRefused, match="chronology changed"):
        inspect_prepared(out)


def test_two_paths_share_one_atomic_claim_even_after_adapter_upgrade(
    original, tmp_path
):
    source, _, _ = original
    out = tmp_path / "claims"
    prepare_clone(source, out)
    first = _claim()
    assert claim_fixture(out, first, path="live") == "CLAIMED"
    assert claim_fixture(
        out, replace(first, adapter_version="v77"), path="recovery"
    ) == "ALREADY_CLAIMED"
    with pytest.raises(OfflineClaimConflict, match="already claimed"):
        claim_fixture(out, replace(first, work_id="work-b"), path="recovery")
    with pytest.raises(OfflineClaimConflict, match="already claimed"):
        claim_fixture(
            out, replace(first, response_digest="b" * 64), path="live"
        )
    assert inspect_prepared(out).state == "PREPARED_DISABLED"
    with sqlite3.connect(out / "prepared.sqlite3") as cx:
        assert cx.execute(
            "SELECT first_path,COUNT(*) FROM issue88_offline_claim"
        ).fetchone() == ("live", 1)
        assert cx.execute(
            "SELECT COUNT(*) FROM g2_work_event_v1"
        ).fetchone() == (1,)


def test_recovery_first_then_live_claim_and_service_isolation(original, tmp_path):
    source, _, _ = original
    out = tmp_path / "two-paths"
    prepare_clone(source, out)
    first = _claim()
    assert claim_fixture(out, first, path="recovery") == "CLAIMED"
    assert claim_fixture(out, first, path="live") == "ALREADY_CLAIMED"
    second = replace(first, provider_service="provider-y", work_id="work-b")
    assert second.result_key != first.result_key
    assert claim_fixture(out, second, path="live") == "CLAIMED"


def test_claims_survive_database_reopen(original, tmp_path):
    source, _, _ = original
    out = tmp_path / "reopen"
    prepare_clone(source, out)
    first = _claim()
    assert claim_fixture(out, first, path="live") == "CLAIMED"
    # Separate connections are used for each operation.
    assert claim_fixture(out, first, path="recovery") == "ALREADY_CLAIMED"


def test_claim_hook_rollback_never_persists_partial_claim(original, tmp_path):
    source, _, _ = original
    out = tmp_path / "rollback"
    prepare_clone(source, out)

    def fail():
        raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        claim_fixture(out, _claim(), path="live", after_begin=fail)
    with sqlite3.connect(out / "prepared.sqlite3") as cx:
        assert cx.execute(
            "SELECT COUNT(*) FROM issue88_offline_claim"
        ).fetchone() == (0,)
    assert claim_fixture(out, _claim(), path="recovery") == "CLAIMED"


def test_two_writers_cannot_claim_same_result_for_different_work(original, tmp_path):
    source, _, _ = original
    out = tmp_path / "concurrent"
    prepare_clone(source, out)
    entered = Event()
    release = Event()
    outcomes = []
    failures = []

    def pause_after_begin():
        entered.set()
        if not release.wait(5):
            raise RuntimeError("fixture hook timed out")

    def live_writer():
        try:
            outcomes.append(claim_fixture(
                out, _claim(), path="live", after_begin=pause_after_begin
            ))
        except BaseException as exc:
            failures.append(exc)

    worker = Thread(target=live_writer)
    worker.start()
    try:
        assert entered.wait(5)
        # The first writer already holds BEGIN IMMEDIATE. Releasing it
        # allows the second path to observe the durable claim and conflict.
        release.set()
        with pytest.raises(OfflineClaimConflict):
            claim_fixture(
                out, replace(_claim(), work_id="work-b"), path="recovery"
            )
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert not failures
    assert outcomes == ["CLAIMED"]
