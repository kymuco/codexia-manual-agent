"""DW6.1 one-shot human-scoped cognition and no-replay regression tests."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import dw6_worker_continuation_v1 as wc


class Registry:
    def __init__(self, path):
        self.path = path
        self.binding = SimpleNamespace(
            project="player-intelligence", repo="kymuco/player-intelligence",
            workspace=str(path.parent), worker_conversation="worker-id",
            controller_conversation="controller-id",
        )

    @contextlib.contextmanager
    def _open(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            yield db

    def require_ready(self, project):
        assert project == self.binding.project
        return self.binding


def idle():
    return dict(project="player-intelligence", next_action="idle", work_id=None,
                controller_message_id="c1", worker_message_id="w1",
                worker_advanced=False, controller_phase="ready")


def setup(tmp_path):
    reg = Registry(tmp_path / "registry.sqlite")
    store = wc.WorkerContinuationStore(reg)
    return reg, store


def arm(store):
    store.arm(project="player-intelligence", controller_message_id="c1",
              controller_sha256=wc._sha("ctl"), worker_message_id="w1",
              worker_sha256=wc._sha("worker"), task="Continue R1d.1 audit only")


def test_goal_policy():
    for bad in ("", "ab", "a\nb", "a\rb", "a\x00b", "x" * 501):
        with pytest.raises(ValueError):
            wc._goal(bad)
    assert wc._goal("  Continue the audit  ") == "Continue the audit"


def test_store_arm_and_exact_key(tmp_path):
    reg, store = setup(tmp_path)
    arm(store)
    row = store.latest("player-intelligence")
    assert row["phase"] == "armed"
    assert row["task_sha256"] == wc._sha("Continue R1d.1 audit only")
    with pytest.raises(ValueError, match="active"):
        arm(store)
    assert store.latest("other-project") is None


def test_store_transitions_no_replay(tmp_path):
    reg, store = setup(tmp_path)
    arm(store)
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="armed", to_phase="send_admitted")
    with pytest.raises(ValueError, match="state changed"):
        store.transition(project="player-intelligence", controller_message_id="c1",
                         from_phase="armed", to_phase="send_admitted")
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="send_admitted", to_phase="worker_known", result_id="w2")
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="worker_known", to_phase="verified", result_id="w2")
    assert store.latest("player-intelligence")["result_worker_message_id"] == "w2"



def test_verified_transition_cannot_replace_admitted_worker_id(tmp_path):
    _, store = setup(tmp_path)
    arm(store)
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="armed", to_phase="send_admitted")
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="send_admitted", to_phase="worker_known", result_id="w2")

    with pytest.raises(ValueError, match="state changed"):
        store.transition(project="player-intelligence", controller_message_id="c1",
                         from_phase="worker_known", to_phase="verified",
                         result_id="another-worker")
    row = store.latest("player-intelligence")
    assert row["phase"] == "worker_known"
    assert row["result_worker_message_id"] == "w2"

    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="worker_known", to_phase="verified", result_id="w2")
    row = store.latest("player-intelligence")
    assert row["phase"] == "verified"
    assert row["result_worker_message_id"] == "w2"


@pytest.mark.parametrize("missing_id", [None, ""])
def test_worker_known_requires_nonempty_result_identity(tmp_path, missing_id):
    _, store = setup(tmp_path)
    arm(store)
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="armed", to_phase="send_admitted")
    with pytest.raises(ValueError, match="result identity"):
        store.transition(project="player-intelligence", controller_message_id="c1",
                         from_phase="send_admitted", to_phase="worker_known",
                         result_id=missing_id)
    assert store.latest("player-intelligence")["phase"] == "send_admitted"


def test_cancel_only_before_send(tmp_path):
    _, store = setup(tmp_path)
    arm(store)
    with pytest.raises(ValueError, match="result identity"):
        store.transition(project="player-intelligence", controller_message_id="c1",
                         from_phase="armed", to_phase="send_admitted", result_id="w2")
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="armed", to_phase="cancelled")
    assert store.latest("player-intelligence")["phase"] == "cancelled"


def test_overlay_idle_requires_exact_armed_grant(tmp_path):
    reg, store = setup(tmp_path)
    assert wc.continuation_frontier(idle(), reg) == {}
    arm(store)
    out = wc.continuation_frontier(idle(), reg)
    assert out["next_action"] == "continue_worker"
    assert out["worker_task_sha256"] == wc._sha("Continue R1d.1 audit only")
    state = {**idle(), "worker_message_id": "w2"}
    assert wc.continuation_frontier(state, reg)["next_action"] == "blocked_stale_worker_grant"
    assert wc.continuation_frontier({**idle(), "next_action": "interpret"}, reg) == {}


def test_overlay_ambiguous_and_verified_fail_closed(tmp_path):
    reg, store = setup(tmp_path)
    arm(store)
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="armed", to_phase="send_admitted")
    assert wc.continuation_frontier(idle(), reg)["next_action"] == "blocked_ambiguous_worker_send"
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="send_admitted", to_phase="worker_known", result_id="w2")
    assert wc.continuation_frontier(idle(), reg)["next_action"] == "blocked_ambiguous_worker_send"
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="worker_known", to_phase="verified", result_id="w2")
    assert wc.continuation_frontier(idle(), reg)["next_action"] == "blocked_worker_frontier_not_advanced"



def test_verified_historical_grant_does_not_block_new_controller(tmp_path):
    reg, store = setup(tmp_path)
    arm(store)
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="armed", to_phase="send_admitted")
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="send_admitted", to_phase="worker_known", result_id="w2")
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="worker_known", to_phase="verified", result_id="w2")

    # An ordinary verified Controller interpretation of w2 creates c2.
    # The earlier completed grant for c1 must not block the new idle frontier.
    later = {**idle(), "controller_message_id": "c2",
             "worker_message_id": "w2"}
    assert wc.continuation_frontier(later, reg) == {}
    assert store.latest("player-intelligence")["phase"] == "verified"

    # Reusing c1 remains blocked, even if a caller presents a later Worker ID.
    same_controller = {**later, "controller_message_id": "c1"}
    assert (wc.continuation_frontier(same_controller, reg)["next_action"]
            == "blocked_worker_frontier_not_advanced")


def test_historical_ambiguous_worker_grant_still_blocks_new_controller(tmp_path):
    reg, store = setup(tmp_path)
    arm(store)
    store.transition(project="player-intelligence", controller_message_id="c1",
                     from_phase="armed", to_phase="send_admitted")

    later = {**idle(), "controller_message_id": "c2",
             "worker_message_id": "w2"}
    assert (wc.continuation_frontier(later, reg)["next_action"]
            == "blocked_ambiguous_worker_send")


def test_arm_requires_human_confirmation(monkeypatch, tmp_path):
    reg, store = setup(tmp_path)
    binding = reg.binding
    worker = SimpleNamespace(message_id="w1", text="worker")
    controller = SimpleNamespace(message_id="c1", text="ctl")
    monkeypatch.setattr(wc, "_frontier", lambda **_: (binding, worker, controller))
    with pytest.raises(ValueError, match="did not authorize"):
        wc.arm_worker(state=idle(), registry=reg, client=object(),
                      task="Continue R1d.1 audit only", expected_worker="w1",
                      expected_controller="c1", confirm=lambda _: "NO")
    assert store.latest("player-intelligence") is None
    with pytest.raises(ValueError, match="expected message"):
        wc.arm_worker(state=idle(), registry=reg, client=object(),
                      task="Continue R1d.1 audit only", expected_worker="changed",
                      expected_controller="c1", confirm=lambda _: "YES")

    def yes(prompt):
        return "ARM " + prompt.split("ARM ", 1)[1].split(":", 1)[0]

    result = wc.arm_worker(state=idle(), registry=reg, client=object(),
                           task="Continue R1d.1 audit only", expected_worker="w1",
                           expected_controller="c1", confirm=yes)
    assert result["status"] == "WORKER_CONTINUATION_ARMED_NO_SEND"
    assert store.latest("player-intelligence")["phase"] == "armed"


class FakeClient:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.sent = []
        self.latest_worker = SimpleNamespace(message_id="w2", text="new answer")

    def send(self, prompt, *, conversation, model, reasoning_effort, temporary):
        self.sent.append((prompt, conversation, model, reasoning_effort, temporary))
        if self.mode == "timeout":
            raise TimeoutError("ambiguous send")
        return SimpleNamespace(
            conversation=SimpleNamespace(conversation_id=conversation, message_id="w2"),
            text="new answer",
        )


def fake_frontier(reg):
    return (reg.binding, SimpleNamespace(message_id="w1", text="worker"),
            SimpleNamespace(message_id="c1", text="ctl"))


def _inject_latest(monkeypatch, client):
    module = types.ModuleType("tools.dw_codexia_controller_command_v1")
    module._latest = lambda _, conversation: client.latest_worker
    monkeypatch.setitem(sys.modules, "tools.dw_codexia_controller_command_v1", module)


def test_single_authorized_worker_send(monkeypatch, tmp_path):
    reg, store = setup(tmp_path)
    arm(store)
    client = FakeClient()
    _inject_latest(monkeypatch, client)
    monkeypatch.setattr(wc, "_frontier", lambda **_: fake_frontier(reg))
    state = {**idle(), "next_action": "continue_worker"}
    result = wc.continue_worker(state=state, registry=reg, client=client)
    assert result["status"] == "WORKER_CONTINUED_READBACK_VERIFIED"
    assert result["worker_message_id"] == "w2"
    assert len(client.sent) == 1
    assert client.sent[0][1] == "worker-id"
    assert "not permission to execute" in client.sent[0][0].lower() or "NOT permission to execute" in client.sent[0][0]
    assert store.latest("player-intelligence")["phase"] == "verified"
    with pytest.raises(ValueError, match="stale"):
        wc.continue_worker(state=state, registry=reg, client=client)
    assert len(client.sent) == 1


def test_ambiguous_send_never_retried(monkeypatch, tmp_path):
    reg, store = setup(tmp_path)
    arm(store)
    client = FakeClient("timeout")
    monkeypatch.setattr(wc, "_frontier", lambda **_: fake_frontier(reg))
    state = {**idle(), "next_action": "continue_worker"}
    with pytest.raises(TimeoutError):
        wc.continue_worker(state=state, registry=reg, client=client)
    assert store.latest("player-intelligence")["phase"] == "send_admitted"
    assert wc.continuation_frontier(idle(), reg)["next_action"] == "blocked_ambiguous_worker_send"
    with pytest.raises(ValueError, match="stale"):
        wc.continue_worker(state=state, registry=reg, client=client)
    assert len(client.sent) == 1


def test_readback_mismatch_is_not_success(monkeypatch, tmp_path):
    reg, store = setup(tmp_path)
    arm(store)
    client = FakeClient()
    client.latest_worker = SimpleNamespace(message_id="other", text="wrong")
    _inject_latest(monkeypatch, client)
    monkeypatch.setattr(wc, "_frontier", lambda **_: fake_frontier(reg))
    with pytest.raises(ValueError, match="readback differs"):
        wc.continue_worker(state={**idle(), "next_action": "continue_worker"},
                           registry=reg, client=client)
    assert store.latest("player-intelligence")["phase"] == "worker_known"


def test_no_grant_means_no_send(monkeypatch, tmp_path):
    reg, store = setup(tmp_path)
    client = FakeClient()
    monkeypatch.setattr(wc, "_frontier", lambda **_: fake_frontier(reg))
    with pytest.raises(ValueError, match="missing or stale"):
        wc.continue_worker(state={**idle(), "next_action": "continue_worker"},
                           registry=reg, client=client)
    assert client.sent == []


def test_nonidle_actions_do_not_create_send_frontier(tmp_path):
    reg, store = setup(tmp_path)
    arm(store)
    for action in ("capture", "review", "await_post", "blocked_controller_frontier"):
        assert wc.continuation_frontier({**idle(), "next_action": action}, reg) == {}


def test_status_before_grant_does_not_create_sqlite_table(tmp_path):
    reg, store = setup(tmp_path)
    assert wc.continuation_frontier(idle(), reg) == {}
    with reg._open() as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='dw6_worker_continuation_v1'"
        ).fetchone() is None
