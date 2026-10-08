"""Offline-only SQLite experiment for Issue #88 (not runtime admission tests)."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from threading import Event, Thread
from uuid import uuid4

import pytest

from tools.issue88_sqlite_contract_spike import (
    Candidate,
    OfflineSqliteContractSpike,
    ProviderResultClaimed,
    RecoveryRefused,
    SPIKE_OUTCOME_KIND,
    StaleWorkHead,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "issue88-contract.sqlite3"
    with sqlite3.connect(path) as cx:
        cx.execute("PRAGMA foreign_keys=ON")
        cx.execute("""
            CREATE TABLE g2_work_v1 (
                work_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                objective TEXT NOT NULL, source_namespace TEXT NOT NULL,
                source_id TEXT NOT NULL, ingress_payload_digest TEXT NOT NULL,
                ingress_binding_digest TEXT NOT NULL, work_digest TEXT NOT NULL,
                UNIQUE(source_namespace,source_id)
            )
        """)
        cx.execute("""
            CREATE TABLE g2_work_event_v1 (
                event_id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_event_digest TEXT,
                event_digest TEXT NOT NULL,
                UNIQUE(work_id,sequence),
                FOREIGN KEY(work_id) REFERENCES g2_work_v1(work_id)
            )
        """)
        for i in range(3):
            cx.execute(
                "INSERT INTO g2_work_v1 VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"work-{i}", "2026-10-08T00:00:00+00:00", "contract spike",
                    "fixture", str(i), "1" * 64, "2" * 64, "3" * 64,
                ),
            )
    spike = OfflineSqliteContractSpike(path)
    spike.install_on_disposable_db()
    return spike


def candidate(work_id="work-0", adapter_version="v1", **changes):
    base = Candidate(
        work_id=work_id,
        request_id="request-1",
        handoff_id="handoff-1",
        provider_service="provider-x",
        provider_namespace="account-x",
        execution_id="execution-x",
        response_id="response-x",
        response_digest="a" * 64,
        expected_revision=0,
        expected_head_digest=None,
        authorized_epoch=1,
        authorized_coverage_digest="c" * 64,
        adapter_version=adapter_version,
    )
    return replace(base, **changes)


def enable(db):
    # Synthetic coverage assertion only: no provider history was inspected.
    db.fixture_enable("provider-x", "account-x", epoch=1, coverage_digest="c" * 64)


def test_disabled_by_default_does_not_claim_or_append(db):
    with pytest.raises(RecoveryRefused, match="unauthorized"):
        db.admit_fixture(candidate())
    assert db.counts() == (0, 0, 0)


def test_atomic_claim_and_work_append_and_retry(db):
    enable(db)
    item = candidate()
    assert db.admit_fixture(item) == "ADMITTED"
    assert db.work_head(item.work_id)[0] == 1
    assert db.counts() == (1, 1, 0)
    assert db.admit_fixture(item) == "ALREADY_ADMITTED"
    assert db.counts() == (1, 1, 0)
    with sqlite3.connect(db.path) as cx:
        row = cx.execute(
            "SELECT event_id,event_digest FROM g2_work_event_v1 WHERE kind=?",
            (SPIKE_OUTCOME_KIND,),
        ).fetchone()
        assert row[0] == item.outcome_event_id
        assert len(row[1]) == 64


def test_cross_work_double_claim_fails_even_after_adapter_upgrade(db):
    enable(db)
    a = candidate()
    assert db.admit_fixture(a) == "ADMITTED"
    b = candidate(
        work_id="work-1", adapter_version="v99", handoff_id="handoff-B"
    )
    assert a.result_key == b.result_key
    with pytest.raises(ProviderResultClaimed, match="assigned elsewhere"):
        db.admit_fixture(b)
    assert db.counts() == (1, 1, 0)


def test_readonly_replay_survives_revocation_after_committed_outcome(db):
    enable(db)
    item = candidate()
    assert db.admit_fixture(item) == "ADMITTED"
    db.revoke("provider-x", "account-x", epoch=2)
    assert db.admit_fixture(item) == "ALREADY_ADMITTED"
    assert db.counts() == (1, 1, 0)
    with pytest.raises(ProviderResultClaimed, match="assigned elsewhere"):
        db.admit_fixture(candidate(work_id="work-1", handoff_id="different"))


def test_authority_is_scoped_by_provider_service_and_account(db):
    enable(db)
    other_service = candidate(
        work_id="work-1", provider_service="provider-y",
        execution_id="execution-y", response_id="response-y",
    )
    with pytest.raises(RecoveryRefused, match="unauthorized"):
        db.admit_fixture(other_service)
    db.fixture_enable("provider-y", "account-x", epoch=1, coverage_digest="c" * 64)
    assert db.admit_fixture(other_service) == "ADMITTED"
    db.revoke("provider-x", "account-x", epoch=2)
    # Revoking service X cannot revoke the independent service Y.
    next_result = candidate(
        work_id="work-2", provider_service="provider-y",
        execution_id="execution-z", response_id="response-z",
    )
    assert db.admit_fixture(next_result) == "ADMITTED"
    assert db.counts() == (2, 2, 0)


def test_claim_index_read_failure_blocks_outcome(db):
    enable(db)
    with sqlite3.connect(db.path) as cx:
        cx.execute("DROP TABLE issue88_spike_claim")
    with pytest.raises(sqlite3.DatabaseError):
        db.admit_fixture(candidate())
    assert db.work_head("work-0") == (0, None)


def test_missing_source_identity_or_bad_digest_rejected():
    with pytest.raises(ValueError):
        candidate(response_id="")
    with pytest.raises(ValueError):
        candidate(response_digest="not-a-digest")
    with pytest.raises(ValueError):
        candidate(authorized_epoch=True)


def test_stale_expected_work_head_fails_without_claim(db):
    enable(db)
    with sqlite3.connect(db.path) as cx:
        cx.execute("""
            INSERT INTO g2_work_event_v1 VALUES (?,?,?,?,?,?,?,?)
        """, (
            str(uuid4()), "work-0", 1, "2026-10-08T00:00:00+00:00",
            "work.note", "{}", None, "b" * 64,
        ))
    with pytest.raises(StaleWorkHead):
        db.admit_fixture(candidate())
    assert db.counts() == (0, 0, 0)


def test_epoch_revocation_after_preflight_rejects(db):
    enable(db)
    item = candidate()
    # A read outside the append transaction is not an admission authority.
    with sqlite3.connect(db.path) as cx:
        assert cx.execute(
            "SELECT enabled FROM issue88_spike_authority"
        ).fetchone()[0] == 1
    db.revoke("provider-x", "account-x", epoch=2)
    with pytest.raises(RecoveryRefused, match="unauthorized"):
        db.admit_fixture(item)
    assert db.counts() == (0, 0, 0)


def test_coverage_revocation_after_preflight_rejects(db):
    enable(db)
    db.revoke("provider-x", "account-x", epoch=1, coverage_digest="d" * 64)
    with pytest.raises(RecoveryRefused, match="unauthorized"):
        db.admit_fixture(candidate())
    assert db.counts() == (0, 0, 0)


def test_legacy_direct_role_append_blocked_at_storage_boundary(db):
    enable(db)
    for kind in ("role.completed", "role.failed", "role.outcome-unknown"):
        with pytest.raises(sqlite3.IntegrityError, match="LEGACY_WRITER_FENCED"):
            with sqlite3.connect(db.path) as cx:
                cx.execute("""
                    INSERT INTO g2_work_event_v1 VALUES (?,?,?,?,?,?,?,?)
                """, (
                    str(uuid4()), "work-0", 1,
                    "2026-10-08T00:00:00+00:00", kind, "{}", None, "f" * 64,
                ))
    assert db.counts() == (0, 0, 0)


def test_legacy_writer_fenced_after_database_reopen(db):
    enable(db)
    reopened = OfflineSqliteContractSpike(db.path)
    with pytest.raises(sqlite3.IntegrityError, match="LEGACY_WRITER_FENCED"):
        with sqlite3.connect(reopened.path) as cx:
            cx.execute("""
                INSERT INTO g2_work_event_v1 VALUES (?,?,?,?,?,?,?,?)
            """, (
                str(uuid4()), "work-0", 1, "2026-10-08T00:00:00+00:00",
                "role.completed", "{}", None, "f" * 64,
            ))


def test_d19_final_authority_read_to_append_race_is_serialized(db):
    enable(db)
    item = candidate()
    inside_write_transaction = Event()
    release_writer = Event()
    revoker_started = Event()
    revoker_finished = Event()
    results = []
    failures = []

    def after_final_read():
        inside_write_transaction.set()
        if not release_writer.wait(6):
            raise AssertionError("writer hook release timed out")

    def writer():
        try:
            results.append(db.admit_fixture(
                item, after_final_authority_read=after_final_read
            ))
        except BaseException as exc:
            failures.append(exc)

    def revoker():
        revoker_started.set()
        try:
            db.revoke("provider-x", "account-x", epoch=2)
        except BaseException as exc:
            failures.append(exc)
        finally:
            revoker_finished.set()

    t1 = Thread(target=writer)
    t2 = Thread(target=revoker)
    t1.start()
    try:
        assert inside_write_transaction.wait(6)
        t2.start()
        assert revoker_started.wait(6)
        # A separate connection cannot commit revocation while the writer
        # holds the BEGIN IMMEDIATE transaction spanning the authority read.
        assert not revoker_finished.wait(0.15)
    finally:
        release_writer.set()
        t1.join(timeout=10)
        if t2.ident is not None:
            t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()
    assert not failures
    assert results == ["ADMITTED"]
    assert revoker_finished.is_set()
    assert db.counts() == (1, 1, 0)
    with pytest.raises(RecoveryRefused, match="unauthorized"):
        db.admit_fixture(candidate(work_id="work-1", handoff_id="other"))


def test_d19_revocation_commits_first_and_stale_append_rejects(db):
    enable(db)
    db.revoke("provider-x", "account-x", epoch=2)
    with pytest.raises(RecoveryRefused, match="unauthorized"):
        db.admit_fixture(candidate())
    assert db.counts() == (0, 0, 0)


def test_native_sqlite_workstore_rejects_legacy_role_event(tmp_path):
    core = pytest.importorskip("codexia_manual_agent.work_core")
    db_path = tmp_path / "native.sqlite3"
    store = core.SqliteWorkStore(db_path)
    ingress = core.WorkIngressBinding.create(
        source_namespace="issue88.spike",
        source_id="native-legacy",
        payload_digest="3" * 64,
    )
    work = core.Work.create(objective="Issue 88 offline gate", ingress=ingress)
    initial = store.create(work)
    spike = OfflineSqliteContractSpike(db_path)
    spike.install_on_disposable_db()
    spike.fixture_enable("provider-x", "account-x", epoch=1, coverage_digest="c" * 64)
    event = initial.next_event(kind="role.completed", payload={})
    with pytest.raises(sqlite3.IntegrityError, match="LEGACY_WRITER_FENCED"):
        store.append(work.work_id, expected_revision=0, event=event)
    assert store.events(work.work_id) == ()
    assert store.snapshot(work.work_id).revision == 0
    # The synthetic path writes a digest-valid WorkEvent to the real schema,
    # but deliberately does not claim a valid RoleRun or admission policy.
    item = candidate(work_id=work.work_id)
    assert spike.admit_fixture(item) == "ADMITTED"
    assert store.events(work.work_id)[0].kind == SPIKE_OUTCOME_KIND
    assert store.snapshot(work.work_id).revision == 1
