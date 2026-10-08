"""Issue #88 legacy-writer cutover probes on disposable native SQLite WorkStores.

These probes DO NOT install a migration on a real database or validate source
history/claim-index completeness. They only characterize SQLite interleavings.
"""

from __future__ import annotations

import json
import sqlite3
from threading import Event, Thread
from uuid import uuid4

import pytest

from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkIngressBinding,
)
from tools.issue88_sqlite_contract_spike import (
    OfflineSqliteContractSpike,
    RecoveryRefused,
)


@pytest.fixture
def native(tmp_path):
    db_path = tmp_path / "native-cutover.sqlite3"
    store = SqliteWorkStore(db_path)
    ingress = WorkIngressBinding.create(
        source_namespace="issue88.migration",
        source_id=str(uuid4()),
        payload_digest="a" * 64,
    )
    work = Work.create(objective="Issue 88 offline cutover", ingress=ingress)
    store.create(work)
    return store, work, OfflineSqliteContractSpike(db_path)


def _insert_native_event(connection, event):
    connection.execute(
        """
        INSERT INTO g2_work_event_v1 (
            event_id,work_id,sequence,created_at,kind,payload_json,
            previous_event_digest,event_digest
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            event.event_id,
            event.work_id,
            event.sequence,
            event.created_at,
            event.kind,
            json.dumps(
                event.to_dict()["payload"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            event.previous_event_digest,
            event.event_digest,
        ),
    )


def test_m01_m02_old_writer_commits_before_barrier_then_is_fenced(native):
    store, work, spike = native
    old_store = store  # Instantiated before migration; uses the old append API.
    prepared = store.snapshot(work.work_id).next_event(
        kind="role.completed", payload={"synthetic": True}
    )
    old_connection = sqlite3.connect(
        spike.path, isolation_level=None, timeout=10.0
    )
    old_connection.execute("BEGIN IMMEDIATE")
    started = Event()
    finished = Event()
    failures = []

    def installer():
        started.set()
        try:
            spike.install_on_disposable_db()
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = Thread(target=installer)
    thread.start()
    try:
        assert started.wait(5)
        # The installer cannot install its writer fence before this transaction
        # commits; it has to acquire the same DB write serialization barrier.
        assert not finished.wait(0.15)
        _insert_native_event(old_connection, prepared)
        old_connection.execute("COMMIT")
    finally:
        if old_connection.in_transaction:
            old_connection.execute("ROLLBACK")
        old_connection.close()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert finished.is_set()
    assert not failures
    assert store.events(work.work_id) == (prepared,)

    # This old Python object has no epoch logic; the storage trigger rejects
    # its next terminal cognition event even after reopening the connection.
    after = store.snapshot(work.work_id).next_event(
        kind="role.completed", payload={"after_barrier": True}
    )
    with pytest.raises(sqlite3.IntegrityError, match="LEGACY_WRITER_FENCED"):
        old_store.append(work.work_id, expected_revision=1, event=after)
    assert store.events(work.work_id) == (prepared,)


def test_m03_preopened_direct_sqlite_connection_obeys_new_fence(native):
    store, work, spike = native
    old_connection = sqlite3.connect(spike.path, timeout=5.0)
    try:
        # Opening an old handle before installation does not exempt a later
        # INSERT from the schema trigger applied by the cutover transaction.
        spike.install_on_disposable_db()
        event = store.snapshot(work.work_id).next_event(
            kind="role.outcome-unknown", payload={}
        )
        with pytest.raises(sqlite3.IntegrityError, match="LEGACY_WRITER_FENCED"):
            _insert_native_event(old_connection, event)
        old_connection.rollback()
    finally:
        old_connection.close()

    assert store.events(work.work_id) == ()
    with sqlite3.connect(spike.path) as reopened:
        trigger = reopened.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' AND name='issue88_spike_fence'"
        ).fetchone()
        assert trigger is not None
        assert "LEGACY_WRITER_FENCED" in trigger[0]


def test_m04_disabling_recovery_does_not_remove_writer_fence(native):
    store, work, spike = native
    spike.install_on_disposable_db()
    spike.fixture_enable(
        "provider-x", "account-x", epoch=1, coverage_digest="c" * 64
    )
    spike.revoke("provider-x", "account-x", epoch=2)

    event = store.snapshot(work.work_id).next_event(
        kind="role.failed", payload={}
    )
    with pytest.raises(sqlite3.IntegrityError, match="LEGACY_WRITER_FENCED"):
        store.append(work.work_id, expected_revision=0, event=event)
    assert store.snapshot(work.work_id).revision == 0

    # An unrelated nonterminal Work event can still be appended normally.
    note = store.snapshot(work.work_id).next_event(
        kind="work.note", payload={"observed": True}
    )
    store.append(work.work_id, expected_revision=0, event=note)
    assert store.events(work.work_id) == (note,)


def test_partial_m06_missing_core_table_aborts_scratch_install(tmp_path):
    path = tmp_path / "missing-schema.sqlite3"
    spike = OfflineSqliteContractSpike(path)
    with pytest.raises(RecoveryRefused, match="expected existing"):
        spike.install_on_disposable_db()
    with sqlite3.connect(path) as cx:
        tables = {
            row[0]
            for row in cx.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert not any(name.startswith("issue88_spike_") for name in tables)
