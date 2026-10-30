"""DW6 boundary tests: no automatically inferred approval or replay."""

from __future__ import annotations

import sqlite3

import pytest

from tools import dw6_governed_work_loop_v1 as dw6

DIGEST = "a" * 64


def work(*, review=False, results=(), yield_kind="none", handoff=False, receipt=False):
    return {
        "plan_sha256": DIGEST,
        "plan": {"decision": "commands", "steps": [{"step": 1}]},
        "review_recorded": review,
        "results": list(results),
        "yield": {"kind": yield_kind},
        "post_handoff": handoff,
        "post_receipt": receipt,
    }


def choice(obj=None, *, worker=False, decision="commands", phase="ready",
           matches=True, review=None):
    return dw6.choose_action(
        controller_phase=phase, controller_matches=matches,
        worker_advanced=worker, controller_decision=decision,
        work=obj, review_response=review,
    )


def test_new_controller_captures_once_and_requires_same_worker():
    assert choice() == "capture"
    assert choice(worker=True) == "blocked_uncaptured_worker_moved"
    assert choice(phase="send_admitted") == "blocked_controller_frontier"
    assert choice(matches=False) == "blocked_controller_frontier"


def test_no_commands_wait_for_new_worker():
    assert choice(decision="no_commands") == "idle"
    assert choice(decision="no_commands", worker=True) == "interpret"
    assert choice(decision="uncertain") == "blocked_controller_decision"


def test_review_must_stop_for_human():
    assert choice(work()) == "review"
    assert choice(work(yield_kind="attention")) == "await_approval"
    assert choice(work(results=[{"state": "succeeded"}])) == "blocked_unapproved_execution"


def test_rejection_never_grants_execution():
    assert choice(work(review=True), review=f"REJECT {DIGEST}") == "rejected"
    assert choice(work(review=True), review="APPROVE wrong") == "blocked_review_inconsistent"
    assert choice(work(review=True), review=None) == "blocked_review_inconsistent"


def test_approved_plan_needs_separate_execution_authority():
    assert choice(work(review=True), review=f"APPROVE {DIGEST}") == "await_execution"
    assert choice(work(review=True, results=[{"state": "failed"}]),
                  review=f"APPROVE {DIGEST}") == "blocked_non_success"
    assert choice(work(review=True, results=[{"state": "unknown"}]),
                  review=f"APPROVE {DIGEST}") == "blocked_non_success"


def test_success_must_stop_before_post():
    obj = work(review=True, results=[{"state": "succeeded"}])
    assert choice(obj, review=f"APPROVE {DIGEST}") == "await_post"
    assert choice(obj, review=f"APPROVE {DIGEST}", worker=True) == "blocked_worker_frontier_moved"
    assert choice(obj | {"post_handoff": True}, review=f"APPROVE {DIGEST}") == "blocked_ambiguous_post"


def test_verified_post_advances_only_when_worker_moves():
    obj = work(review=True, results=[{"state": "succeeded"}],
               handoff=True, receipt=True)
    assert choice(obj, review=f"APPROVE {DIGEST}") == "idle"
    assert choice(obj, review=f"APPROVE {DIGEST}", worker=True) == "interpret"


def test_bad_status_is_fail_closed():
    assert choice(work() | {"plan_sha256": "nope"}) == "blocked_invalid_work"
    assert choice(work() | {"results": [None]}) == "blocked_unapproved_execution"
    assert choice(work() | {"results": [{"state": "succeeded"}, {"state": "succeeded"}]}) == "blocked_invalid_work"


def test_find_work_uses_exact_source_identity_and_read_only_db(tmp_path):
    db = tmp_path / "work.sqlite3"
    assert dw6._work_for_controller(str(db), "c", "m") is None
    assert not db.exists()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE g2_work_v1 (work_id TEXT, source_namespace TEXT, source_id TEXT)")
        conn.execute("INSERT INTO g2_work_v1 VALUES ('work-1', ?, ?)",
                     ("codexia.standalone.work", "shell:controller-1:message-1"))
    assert dw6._work_for_controller(str(db), "controller-1", "message-1") == "work-1"
    assert dw6._work_for_controller(str(db), "controller-1", "message-2") is None
    assert dw6._work_for_controller(str(db), "controller-2", "message-1") is None


def test_no_approval_when_frontier_is_different():
    state = {"next_action": "await_approval", "work_id": "w", "project": "p"}
    with pytest.raises(ValueError, match="does not match verified frontier"):
        dw6.advance(state=state, registry=None, client=None, store_path="x",
                    requested="execute", model="gpt-6", reasoning_effort="high", reason=None)


def test_user_action_needed_without_flag():
    for action in ("await_approval", "await_execution", "await_post", "interpret"):
        state = {"next_action": action, "work_id": "w", "project": "p"}
        result = dw6.advance(state=state, registry=None, client=None, store_path="x",
                             requested=None, model="gpt-6", reasoning_effort="high", reason=None)
        assert result["status"] == "WAITING_FOR_EXPLICIT_HUMAN_ACTION"


def test_blocked_state_never_sends_or_executes():
    for action in ("blocked_ambiguous_post", "blocked_controller_frontier", "rejected"):
        with pytest.raises(ValueError, match="inspect durable history"):
            dw6.advance(state={"next_action": action, "work_id": "w", "project": "p"},
                        registry=None, client=None, store_path="x", requested=None,
                        model="gpt-6", reasoning_effort="high", reason=None)
