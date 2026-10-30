"""DW5 pre-send recovery is human attested, fail closed, and audit preserving."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from tools import dw_codexia_presend_recovery_v1 as recovery

PROJECT = "player-intelligence"
WORKER_ID = "a57787c5-91b3-4fa8-814d-c81fda54a8e4"
CONTROLLER_ID = "938a6213-2a6a-4657-b886-3fee5aba2bea"
WORKER_TEXT = "Worker replied after post"
CONTROLLER_TEXT = "Previous Controller plan"


def init_db(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE dw5_project_binding_v1 (
                project TEXT PRIMARY KEY, phase TEXT, worker_conversation TEXT,
                controller_conversation TEXT
            );
            CREATE TABLE dw5_controller_turn_v1 (
                project TEXT, predecessor_message_id TEXT,
                worker_message_id TEXT, worker_sha256 TEXT,
                controller_message_id TEXT, phase TEXT,
                PRIMARY KEY (project, predecessor_message_id)
            );
        """)
        db.execute("INSERT INTO dw5_project_binding_v1 VALUES (?,?,?,?)",
                   (PROJECT, "ready", "worker-conversation", "controller-conversation"))
        db.execute("INSERT INTO dw5_controller_turn_v1 VALUES (?,?,?,?,?,?)",
                   (PROJECT, "older-controller", "older-worker", "old-worker-sha", CONTROLLER_ID, "ready"))
        db.execute("INSERT INTO dw5_controller_turn_v1 VALUES (?,?,?,?,?,?)",
                   (PROJECT, CONTROLLER_ID, WORKER_ID, recovery._sha(WORKER_TEXT), None,
                    "send_admitted"))


@pytest.fixture
def scenario(tmp_path):
    db = tmp_path / "registry.sqlite3"
    init_db(db)
    texts = {
        "worker-conversation": SimpleNamespace(message_id=WORKER_ID, text=WORKER_TEXT),
        "controller-conversation": SimpleNamespace(message_id=CONTROLLER_ID, text=CONTROLLER_TEXT),
    }
    def latest(conversation_id):
        return texts[conversation_id]
    arguments = dict(registry=db, project=PROJECT, expected_worker=WORKER_ID,
                     expected_controller=CONTROLLER_ID,
                     expected_controller_sha256=recovery._sha(CONTROLLER_TEXT),
                     latest=latest)
    return db, texts, arguments


def rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT * FROM dw5_controller_turn_v1 ORDER BY rowid").fetchall()


def approval(prompt):
    return prompt.split("Type ", 1)[1].split(": ", 1)[0]


def test_inspect_strictly_readonly(scenario):
    db, _, args = scenario
    before = db.read_bytes()
    outcome = recovery.inspect_fence(**args)
    assert outcome["status"] == "PRE_SEND_FENCE_CANDIDATE_READ_ONLY"
    assert outcome["fence_phase"] == "send_admitted"
    assert outcome["effects"] == "NONE"
    assert db.read_bytes() == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='dw5_controller_presend_recovery_v1'").fetchone() is None


def test_reconciliation_archives_exact_row_and_unblocks(scenario):
    db, _, args = scenario
    original = rows(db)[-1]
    result = recovery.reconcile_fence(**args, error_kind=recovery.FAILURE_KIND,
                                       confirm=approval)
    assert result["status"] == "PRESEND_FENCE_ARCHIVED_NO_SEND"
    assert result["next_action_expected"] == "interpret"
    assert rows(db) == [rows(db)[0]]
    with sqlite3.connect(db) as conn:
        record = conn.execute("SELECT project, predecessor_message_id, worker_message_id, "
                              "worker_sha256, controller_message_id, original_phase, failure_kind "
                              "FROM dw5_controller_presend_recovery_v1").fetchone()
    assert record == original[:6] + (recovery.FAILURE_KIND,)


def test_wrong_failure_kind_never_clears_fence(scenario):
    db, _, args = scenario
    for kind in ("timeout", "http_500", "sentinel_finalize", "unknown"):
        with pytest.raises(ValueError, match="only exact Sentinel prepare"):
            recovery.reconcile_fence(**args, error_kind=kind, confirm=approval)
    assert len(rows(db)) == 2


def test_user_rejection_never_mutates(scenario):
    db, _, args = scenario
    with pytest.raises(ValueError, match="human did not authorize"):
        recovery.reconcile_fence(**args, error_kind=recovery.FAILURE_KIND,
                                 confirm=lambda _: "NO")
    assert len(rows(db)) == 2


def test_changed_worker_blocks_recovery(scenario):
    db, texts, args = scenario
    texts["worker-conversation"].text = "different"
    with pytest.raises(ValueError, match="Worker latest ID or text differs"):
        recovery.inspect_fence(**args)
    assert len(rows(db)) == 2


def test_changed_controller_blocks_recovery(scenario):
    db, texts, args = scenario
    texts["controller-conversation"].message_id = "unexpected"
    with pytest.raises(ValueError, match="Controller latest ID or text differs"):
        recovery.inspect_fence(**args)
    assert len(rows(db)) == 2


def test_controller_text_mismatch_blocks_recovery(scenario):
    db, texts, args = scenario
    texts["controller-conversation"].text = "different"
    with pytest.raises(ValueError, match="Controller latest ID or text differs"):
        recovery.inspect_fence(**args)
    assert len(rows(db)) == 2


def test_second_recovery_same_predecessor_forbidden(scenario):
    db, _, args = scenario
    recovery.reconcile_fence(**args, error_kind=recovery.FAILURE_KIND,
                             confirm=approval)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO dw5_controller_turn_v1 VALUES (?,?,?,?,?,?)",
                     (PROJECT, CONTROLLER_ID, WORKER_ID,
                      recovery._sha(WORKER_TEXT), None, "send_admitted"))
    with pytest.raises(ValueError, match="already been reconciled once"):
        recovery.inspect_fence(**args)
    assert len(rows(db)) == 2


def test_failure_after_prompt_does_not_mutate(scenario):
    db, texts, args = scenario
    def change_after_confirmation(prompt):
        texts["controller-conversation"].text = "changed during confirmation"
        return approval(prompt)
    with pytest.raises(ValueError, match="Controller latest ID or text differs"):
        recovery.reconcile_fence(**args, error_kind=recovery.FAILURE_KIND,
                                 confirm=change_after_confirmation)
    assert len(rows(db)) == 2


def test_requires_previous_verified_controller_turn(scenario):
    db, _, args = scenario
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM dw5_controller_turn_v1 WHERE phase='ready'")
    with pytest.raises(ValueError, match="previous verified"):
        recovery.inspect_fence(**args)
    assert len(rows(db)) == 1


def test_archival_failure_rolls_back_active_fence(scenario):
    db, _, args = scenario
    # Force an archive insert failure inside the transaction.
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE dw5_controller_presend_recovery_v1 "
                     "(project TEXT, predecessor_message_id TEXT, failure_kind TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        recovery.reconcile_fence(**args, error_kind=recovery.FAILURE_KIND,
                                 confirm=approval)
    assert len(rows(db)) == 2


def test_extra_newer_turn_is_blocked(scenario):
    db, _, args = scenario
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO dw5_controller_turn_v1 VALUES (?,?,?,?,?,?)",
                     (PROJECT, "new", "worker3", "sha", "controller3", "ready"))
    with pytest.raises(ValueError, match="not an unresolved send_admitted"):
        recovery.inspect_fence(**args)
    assert len(rows(db)) == 3
