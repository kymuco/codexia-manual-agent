"""Bounded DW5 second-pre-send recovery; never resend or erase history."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from tools import dw_codexia_presend_recovery_v1 as v1
from tools import dw_codexia_presend_recovery_v2 as v2
from test_dw5_presend_recovery_v1 import (
    CONTROLLER_ID,
    CONTROLLER_TEXT,
    PROJECT,
    WORKER_ID,
    WORKER_TEXT,
    init_db,
)


def approval(prompt):
    return prompt.split("Type ", 1)[1].split(": ", 1)[0]


@pytest.fixture
def scenario(tmp_path):
    path = tmp_path / "store.sqlite3"
    init_db(path)
    messages = {
        "worker-conversation": SimpleNamespace(message_id=WORKER_ID, text=WORKER_TEXT),
        "controller-conversation": SimpleNamespace(
            message_id=CONTROLLER_ID, text=CONTROLLER_TEXT
        ),
    }

    def latest(conversation_id):
        return messages[conversation_id]

    args = dict(
        registry=path,
        project=PROJECT,
        expected_worker=WORKER_ID,
        expected_controller=CONTROLLER_ID,
        expected_controller_sha256=v1._sha(CONTROLLER_TEXT),
        latest=latest,
    )
    v1.reconcile_fence(**args, error_kind=v1.FAILURE_KIND, confirm=approval)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO dw5_controller_turn_v1 VALUES (?,?,?,?,?,?)",
            (PROJECT, CONTROLLER_ID, WORKER_ID, v1._sha(WORKER_TEXT),
             None, "send_admitted"),
        )
    return path, messages, args


def active(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute(
            "SELECT predecessor_message_id, phase FROM dw5_controller_turn_v1 "
            "WHERE project=? ORDER BY rowid DESC LIMIT 1", (PROJECT,)
        ).fetchone()


def test_inspect_requires_exact_previous_archive_and_is_readonly(scenario):
    path, _, args = scenario
    before = path.read_bytes()
    with pytest.raises(ValueError, match="already been reconciled once"):
        v1.inspect_fence(**args)
    result = v2.inspect_fence(**args)
    assert result["status"] == "SECOND_PRESEND_CANDIDATE_READ_ONLY"
    assert result["recovery_limit"] == 2
    assert result["effects"] == "NONE"
    assert len(result["second_recovery_digest"]) == 64
    assert path.read_bytes() == before


def test_second_recovery_archived_without_changing_first(scenario):
    path, _, args = scenario
    with sqlite3.connect(path) as db:
        first_before = db.execute(
            "SELECT * FROM dw5_controller_presend_recovery_v1"
        ).fetchone()
    result = v2.reconcile_fence(
        **args, error_kind=v2.FAILURE_KIND, confirm=approval
    )
    assert result["status"] == "SECOND_PRESEND_FENCE_ARCHIVED_NO_SEND"
    assert result["third_recovery_blocked"] is True
    assert active(path) == ("older-controller", "ready")
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT * FROM dw5_controller_presend_recovery_v1"
        ).fetchone() == first_before
        record = db.execute(
            "SELECT original_phase, failure_kind, previous_reconciliation_digest, "
            "second_reconciliation_digest FROM dw5_controller_presend_recovery_v2"
        ).fetchone()
    assert record[0] == "send_admitted"
    assert record[1] == "sentinel_prepare_rejected"
    with sqlite3.connect(path) as db:
        first_digest = db.execute(
            "SELECT reconciliation_digest FROM dw5_controller_presend_recovery_v1"
        ).fetchone()[0]
    assert record[2] == first_digest
    assert record[3] == result["second_recovery_digest"]


def test_third_same_predecessor_cannot_reconcile(scenario):
    path, _, args = scenario
    v2.reconcile_fence(**args, error_kind=v2.FAILURE_KIND, confirm=approval)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO dw5_controller_turn_v1 VALUES (?,?,?,?,?,?)",
            (PROJECT, CONTROLLER_ID, WORKER_ID, v1._sha(WORKER_TEXT),
             None, "send_admitted"),
        )
    with pytest.raises(ValueError, match="further retries blocked"):
        v2.inspect_fence(**args)
    assert active(path) == (CONTROLLER_ID, "send_admitted")


@pytest.mark.parametrize("kind", ["timeout", "unknown", "sentinel_finalize"])
def test_wrong_error_kind_never_clears_fence(scenario, kind):
    path, _, args = scenario
    with pytest.raises(ValueError, match="only Sentinel prepare"):
        v2.reconcile_fence(**args, error_kind=kind, confirm=approval)
    assert active(path) == (CONTROLLER_ID, "send_admitted")


def test_human_rejection_keeps_active_fence(scenario):
    path, _, args = scenario
    with pytest.raises(ValueError, match="human denied"):
        v2.reconcile_fence(
            **args, error_kind=v2.FAILURE_KIND, confirm=lambda _: "NO"
        )
    assert active(path) == (CONTROLLER_ID, "send_admitted")


def test_worker_drift_blocks_recovery(scenario):
    path, messages, args = scenario
    messages["worker-conversation"].text = "changed"
    with pytest.raises(ValueError, match="Worker canonical"):
        v2.inspect_fence(**args)
    assert active(path) == (CONTROLLER_ID, "send_admitted")


def test_controller_drift_after_confirmation_blocks_recovery(scenario):
    path, messages, args = scenario

    def drift(prompt):
        messages["controller-conversation"].text = "changed"
        return approval(prompt)

    with pytest.raises(ValueError, match="Controller canonical"):
        v2.reconcile_fence(
            **args, error_kind=v2.FAILURE_KIND, confirm=drift
        )
    assert active(path) == (CONTROLLER_ID, "send_admitted")


def test_missing_prior_v1_archive_blocks_recovery(scenario):
    path, _, args = scenario
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM dw5_controller_presend_recovery_v1")
    with pytest.raises(ValueError, match="first recovery"):
        v2.inspect_fence(**args)
    assert active(path) == (CONTROLLER_ID, "send_admitted")


def test_tampered_prior_archive_blocks_recovery(scenario):
    path, _, args = scenario
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE dw5_controller_presend_recovery_v1 "
            "SET reconciliation_digest='tampered'"
        )
    with pytest.raises(ValueError, match="audit digest differs"):
        v2.inspect_fence(**args)
    assert active(path) == (CONTROLLER_ID, "send_admitted")


def test_second_archive_failure_rolls_back_fence(scenario):
    path, _, args = scenario
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE dw5_controller_presend_recovery_v2 "
            "(project TEXT, predecessor_message_id TEXT)"
        )
    with pytest.raises(sqlite3.OperationalError):
        v2.reconcile_fence(
            **args, error_kind=v2.FAILURE_KIND, confirm=approval
        )
    assert active(path) == (CONTROLLER_ID, "send_admitted")
