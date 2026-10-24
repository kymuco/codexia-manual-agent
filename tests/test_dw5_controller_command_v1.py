"""Controller makes command decision in its already-saved ChatGPT conversation.

Offline Gen2 Work/Attention proof. There are ZERO CognitionPort calls, shell
effects or outgoing CWA writes in this milestone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import (
    ChatCommandSource,
    _sha,
)
from codexia_manual_agent.workflow_runtime.controller_command_v1 import (
    ControllerCommandDecision,
)
from tools import dw_codexia_controller_command_v1 as tool


def _make(index, *, source_text=None, decision="commands"):
    source_text = source_text or (
        "Run the following in PowerShell:\n"
        "@'\nfrom pathlib import Path\n"
        "Path('pi-15.md').write_text('ok')\n"
        "'@ | python -\n"
        "Do not run illustrative example: rm -rf build."
    )
    worker_conv = f"00000000-0000-4000-8000-{index:012d}"
    controller_conv = f"11111111-1111-4111-8111-{index:012d}"
    worker_msg = f"22222222-2222-4222-8222-{index:012d}"
    controller_msg = f"33333333-3333-4333-8333-{index:012d}"
    plan = {
        "schema_version": 1,
        "source_sha256": _sha(source_text),
        "decision": decision,
        "steps": [
            {
                "step": 1,
                "shell": "powershell",
                "command": "@'\nfrom pathlib import Path\n"
                "Path('pi-15.md').write_text('ok')\n'@ | python -",
                "cwd": ".",
                "purpose": "Export the chat context in project cwd",
            }
        ] if decision == "commands" else [],
        "reason": "Run one instructed here-string, not the illustrative rm command.",
    }
    worker = SimpleNamespace(role="assistant", message_id=worker_msg, text=source_text)
    controller = SimpleNamespace(
        role="assistant", message_id=controller_msg,
        text="RUN_COMMANDS:\n" + json.dumps(plan, ensure_ascii=False),
    )
    return (worker_conv, controller_conv, worker, controller)


class _Client:
    def __init__(self, pairs):
        self.pairs = dict(pairs)
        self.reads = []

    def get_messages(self, conversation, *, limit):
        self.reads.append((conversation, limit))
        return [self.pairs[conversation]]


def _args(index):
    w, c, worker, controller = _make(index)
    return SimpleNamespace(
        project=f"project-{index}", repo=f"user/project-{index}",
        workspace="W:\\dev\\projects\\test", worker_conversation=w,
        codexia_conversation=c, worker_message_id=worker.message_id,
        codexia_message_id=controller.message_id,
    ), _Client(((w, worker), (c, controller)))


def test_controller_existing_reply_bound_without_extra_model_call(tmp_path, monkeypatch):
    store = SqliteWorkStore(tmp_path / "controller.sqlite")
    args, client = _args(1)
    result = tool._capture(args, client, store)
    work_id = result["work_id"]
    assert result["status"] == "CODEXIA_DECISION_CAPTURED_NO_MODEL_CALL"
    assert result["effects"] == "NONE"
    assert len(client.reads) == 2
    assert all(limit == 1 for _, limit in client.reads)
    assert len(store.events(work_id)) == 2
    before = store.events(work_id)
    repeated = tool._capture(args, client, store)
    assert repeated["work_id"] == work_id
    assert store.events(work_id) == before

    # One Gen2 AttentionNeed. Still no cognition or capabilities.
    review = tool._review(store, work_id)
    assert review["status"]["yield"]["kind"] == "attention"
    question = review["status"]["yield"]["attention"]["question"]
    assert "Path('pi-15.md')" in question
    assert "rm -rf build" not in question
    digest = review["status"]["plan_digest"]
    assert digest in question

    restarted = SqliteWorkStore(tmp_path / "controller.sqlite")
    recovered = tool._review(restarted, work_id)
    assert recovered["status"]["yield"]["kind"] == "attention"
    assert sum(
        event.kind == "attention.need-declared"
        for event in restarted.events(work_id)
    ) == 1

    monkeypatch.setattr(
        "builtins.input", lambda prompt: "APPROVE " + digest[:12]
    )
    done = tool._answer(restarted, work_id, "approve")
    assert done["status"]["yield"]["kind"] == "none"
    assert sum(
        event.kind == "attention.response-recorded"
        for event in restarted.events(work_id)
    ) == 1
    assert not any(
        event.kind.startswith("capability.") or event.kind == "work.completed"
        for event in restarted.events(work_id)
    )


def test_five_independent_project_work_items(tmp_path):
    store = SqliteWorkStore(tmp_path / "five.sqlite")
    ids = []
    for index in range(1, 6):
        args, client = _args(index)
        result = tool._capture(args, client, store)
        ids.append(result["work_id"])
        tool._review(store, result["work_id"])
        status = tool._status(store, result["work_id"])
        assert status["project"] == f"project-{index}"
        assert status["repo"] == f"user/project-{index}"
        assert status["worker_conversation"] == args.worker_conversation
        assert status["controller_conversation"] == args.codexia_conversation
    assert len(set(ids)) == 5


def test_changed_worker_or_controller_head_fails_closed(tmp_path):
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    args, client = _args(1)
    client.pairs[args.worker_conversation] = SimpleNamespace(
        role="assistant",
        message_id=args.worker_message_id,
        text="A completely different reply",
    )
    with pytest.raises(ValueError, match="different assistant message"):
        tool._capture(args, client, store)

    args, client = _args(1)
    client.pairs[args.codexia_conversation] = SimpleNamespace(
        role="assistant",
        message_id=args.codexia_message_id,
        text='{"schema_version":1,"source_sha256":"' + "0" * 64 + '",'
        '"decision":"no_commands","steps":[],"reason":"No"}',
    )
    with pytest.raises(ValueError, match="different assistant message"):
        tool._capture(args, client, store)


def test_forged_source_digest_is_refused(tmp_path):
    store = SqliteWorkStore(tmp_path / "digest.sqlite")
    args, client = _args(1)
    controller = client.pairs[args.codexia_conversation]
    payload = json.loads(controller.text.removeprefix("RUN_COMMANDS:\n"))
    payload["source_sha256"] = "f" * 64
    client.pairs[args.codexia_conversation] = SimpleNamespace(
        role="assistant", message_id=controller.message_id,
        text="RUN_COMMANDS:\n" + json.dumps(payload),
    )
    with pytest.raises(ValueError, match="different assistant message"):
        tool._capture(args, client, store)


def test_exact_controller_decision_provenance_is_revalidated():
    w, c, worker, controller = _make(1)
    source = ChatCommandSource(
        project="project-1", repo="user/project-1",
        conversation_id=w, source_message_id=worker.message_id,
        source_text=worker.text, source_sha256=_sha(worker.text),
        workspace="W:\\dev\\projects\\test",
    )
    decision = ControllerCommandDecision.from_messages(
        source=source, controller_conversation_id=c,
        controller_message_id=controller.message_id, controller_text=controller.text,
    )
    assert ControllerCommandDecision.parse(decision.to_json()) == decision
    assert len(decision.digest) == 64
    bad = decision.to_dict()
    bad["controller_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="digest changed"):
        ControllerCommandDecision.parse(json.dumps(bad))
    bad = decision.to_dict()
    bad["plan"]["steps"][0]["command"] = "Remove-Item /"
    with pytest.raises(ValueError, match="recorded controller plan changed"):
        ControllerCommandDecision.parse(json.dumps(bad))


@pytest.mark.parametrize("decision", ["no_commands", "uncertain"])
def test_non_executable_codexia_decisions_do_not_request_attention(tmp_path, decision):
    w, c, worker, controller = _make(7, decision=decision)
    args, _ = _args(7)
    client = _Client(((w, worker), (c, controller)))
    store = SqliteWorkStore(tmp_path / (decision + ".sqlite"))
    captured = tool._capture(args, client, store)
    reviewed = tool._review(store, captured["work_id"])
    assert reviewed["status"]["yield"]["kind"] == "none"
    assert not any(
        event.kind.startswith("attention.") or event.kind.startswith("capability.")
        for event in store.events(captured["work_id"])
    )
