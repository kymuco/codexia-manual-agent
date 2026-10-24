"""DW5 governed shell execution contract with exact host/work logs."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from codexia_manual_agent.application.execute_process import ExecuteProcessService
from codexia_manual_agent.capability_core import CapabilityOutcome
from codexia_manual_agent.domain.errors import InvalidProcessSpecError
from codexia_manual_agent.execution.process_contained import (
    POWERSHELL_PROFILE, prepare_process_proposal,
)
from codexia_manual_agent.standalone_work import StandaloneWorkSurface
from codexia_manual_agent.work_core import SqliteWorkStore
from tools import dw_codexia_shell_plan_v1 as shell


def _messages(index=1, commands=None):
    commands = commands or [
        "@'\nfrom pathlib import Path\nPath('pi-15.md').write_text('ok')\n'@ | python -",
        "Write-Output 'verification ok'",
    ]
    worker_conv = f"10000000-0000-4000-8000-{index:012d}"
    controller_conv = f"20000000-0000-4000-8000-{index:012d}"
    worker_msg = f"30000000-0000-4000-8000-{index:012d}"
    controller_msg = f"40000000-0000-4000-8000-{index:012d}"
    worker_text = (
        "Execute the here-string in PowerShell, then verify. "
        "Example only, do NOT execute: rm -rf build."
    )
    from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import _sha

    plan = {
        "schema_version": 1, "source_sha256": _sha(worker_text),
        "decision": "commands",
        "steps": [
            {
                "step": i + 1, "shell": "powershell",
                "command": command, "cwd": ".", "purpose": "Run requested check",
            }
            for i, command in enumerate(commands)
        ],
        "reason": "These are two ordered real commands; ignore the example.",
    }
    worker = SimpleNamespace(
        role="assistant", message_id=worker_msg, text=worker_text,
    )
    controller = SimpleNamespace(
        role="assistant", message_id=controller_msg,
        text="RUN_COMMANDS:\n" + json.dumps(plan, ensure_ascii=False),
    )
    return worker_conv, controller_conv, worker, controller


class _Client:
    def __init__(self, data):
        self.data = data
        self.sends = []
        self.reads = []

    def get_messages(self, conversation, *, limit):
        self.reads.append((conversation, limit))
        return [self.data[conversation]]

    def send(self, text, *, conversation, model):
        self.sends.append((text, conversation, model))
        result_id = "90000000-0000-4000-8000-000000000001"
        self.data[conversation] = SimpleNamespace(
            role="assistant", message_id=result_id, text="Output received."
        )
        return SimpleNamespace(
            text="Output received.",
            conversation=SimpleNamespace(
                conversation_id=conversation, message_id=result_id,
            ),
        )


class _Process:
    host_id = "standalone.local.powershell.v1"

    def __init__(self, *, fail_step=0):
        self.calls = []
        self.fail_step = fail_step

    def submit(self, request):
        index = len(self.calls) + 1
        self.calls.append(request)
        assert request.need.need.to_dict()["parameters"]["argv"][:4] == [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command"
        ]
        assert request.need.need.to_dict()["parameters"]["cwd"] == "."
        if self.fail_step == index:
            return CapabilityOutcome.failed(
                request.need, attempt_id=f"step-{index}",
                attempt_digest="d" * 64,
                error="test failure",
                observation={"execution": {"exit_code": 3, "stderr": {
                    "text_utf8_preview": "fail",
                }}},
            )
        return CapabilityOutcome.succeeded(
            request.need, attempt_id=f"step-{index}",
            attempt_digest="a" * 64,
            observation={"execution": {
                "exit_code": 0, "termination_reason": "exited",
                "stdout": {
                    "text_utf8_preview": f"STEP {index} PASS",
                    "sha256": "a" * 64, "byte_count": 11,
                }, "stderr": {
                    "text_utf8_preview": "", "sha256": "b" * 64,
                    "byte_count": 0,
                },
            }},
        )


def _setup(tmp_path, index=1, commands=None, *, store=None):
    worker_conv, controller_conv, worker, controller = _messages(index, commands)
    client = _Client({worker_conv: worker, controller_conv: controller})
    store = store or SqliteWorkStore(tmp_path / "work.sqlite")
    args = SimpleNamespace(
        project=f"project-{index}", repo=f"kymuco/project-{index}",
        workspace=str(tmp_path), worker_conversation=worker_conv,
        codexia_conversation=controller_conv,
        worker_message_id=worker.message_id,
        codexia_message_id=controller.message_id,
    )
    info = shell._capture(args, client, store)
    work_id = info["work_id"]
    shell._review(store, work_id)
    assert shell._status(store, work_id)["yield"]["kind"] == "attention"
    return store, work_id, client


def _approve(store, work_id, monkeypatch):
    p = shell._status(store, work_id)["plan_sha256"]
    monkeypatch.setattr("builtins.input", lambda prompt: "APPROVE " + p[:12])
    shell._answer(store, work_id, "approve")


def _run(store, work_id, client, monkeypatch, host):
    monkeypatch.setattr(
        shell, "StandalonePowerShellCapabilityPort", lambda **kwargs: host
    )
    p = shell._status(store, work_id)["plan_sha256"]
    monkeypatch.setattr("builtins.input", lambda prompt: "EXECUTE " + p[:12])
    return shell._run(store, work_id, client)


def test_governed_two_step_gen2_handoff_and_no_replay(tmp_path, monkeypatch, capsys):
    store, work_id, client = _setup(tmp_path)
    _approve(store, work_id, monkeypatch)
    fake = _Process()
    result = _run(store, work_id, client, monkeypatch, fake)
    assert result["status"] == "ALL_STEPS_SUCCEEDED"
    assert len(fake.calls) == 2
    assert len(result["work"]["results"]) == 2
    assert all(x["state"] == "succeeded" for x in result["work"]["results"])
    kinds = [e.kind for e in store.events(work_id)]
    assert kinds.count("capability.handoff-admitted") == 2
    assert kinds.count("capability.outcome-recorded") == 2
    assert kinds.count("attention.response-recorded") == 1
    assert "work.completed" not in kinds
    logs = capsys.readouterr()
    assert "COMMAND_SHA256=" in logs.err
    assert "RESULT STEP 1" in logs.out
    assert "RESULT STEP 2" in logs.out
    assert "pi-15.md" in logs.err
    history = store.events(work_id)
    with pytest.raises(ValueError, match="already dispatched"):
        _run(store, work_id, client, monkeypatch, fake)
    assert store.events(work_id) == history
    assert len(fake.calls) == 2


def test_failed_second_step_stops_and_no_duplicate(tmp_path, monkeypatch):
    store, work_id, client = _setup(tmp_path)
    _approve(store, work_id, monkeypatch)
    fake = _Process(fail_step=2)
    result = _run(store, work_id, client, monkeypatch, fake)
    assert result["status"] == "STOPPED_NON_SUCCESS_OR_UNKNOWN"
    assert [x["state"] for x in result["work"]["results"]] == [
        "succeeded", "failed"
    ]
    before = store.events(work_id)
    with pytest.raises(ValueError, match="non-success/unknown"):
        _run(store, work_id, client, monkeypatch, fake)
    assert len(fake.calls) == 2
    assert store.events(work_id) == before


def test_no_effect_before_review_or_rejected_review(tmp_path, monkeypatch):
    store, work_id, client = _setup(tmp_path)
    fake = _Process()
    monkeypatch.setattr(
        shell, "StandalonePowerShellCapabilityPort", lambda **kwargs: fake
    )
    with pytest.raises(ValueError, match="review"):
        shell._run(store, work_id, client)
    assert fake.calls == []
    p = shell._status(store, work_id)["plan_sha256"]
    monkeypatch.setattr("builtins.input", lambda prompt: "REJECT " + p[:12])
    shell._answer(store, work_id, "reject")
    with pytest.raises(ValueError, match="not approved"):
        shell._run(store, work_id, client)
    assert not shell._caps(store, work_id)


def test_post_is_bounded_same_worker_conversation_only_once(tmp_path, monkeypatch):
    store, work_id, client = _setup(tmp_path)
    _approve(store, work_id, monkeypatch)
    fake = _Process()
    _run(store, work_id, client, monkeypatch, fake)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: "POST " + prompt.strip().split()[2].rstrip(":")
    )
    result = shell._post(store, work_id, client, "gpt-6-instant")
    assert result["status"] == "SAME_WORKER_CHAT_READBACK_VERIFIED"
    assert len(client.sends) == 1
    assert client.sends[0][1] == shell._decision(store, work_id).source.conversation_id
    assert "STEP 1" in client.sends[0][0]
    with pytest.raises(ValueError, match="no resending"):
        shell._post(store, work_id, client, "gpt-6-instant")
    assert len(client.sends) == 1


def test_five_project_scope_isolation(tmp_path):
    store = SqliteWorkStore(tmp_path / "shared.sqlite")
    ids = []
    for index in range(1, 6):
        _, work_id, client = _setup(tmp_path, index, store=store)
        ids.append(work_id)
        status = shell._status(store, work_id)
        assert status["project"] == f"project-{index}"
        assert status["worker_conversation"] != status["controller_conversation"]
    assert len(set(ids)) == 5
    assert len({shell._status(store, w)["worker_conversation"] for w in ids}) == 5


def test_m2_1_default_shell_denial_unchanged(tmp_path):
    with pytest.raises(Exception) as caught:
        prepare_process_proposal(
            workspace=tmp_path,
            argv=["powershell.exe", "-NoProfile", "-NonInteractive",
                  "-Command", "Write-Output 'HELLO'"],
        )
    assert "Shell" in str(caught.value) or "Executable" in str(caught.value)


@pytest.mark.skipif(os.name != "nt", reason="real PowerShell process is Windows-only")
def test_explicit_host_profile_executes_real_powershell_with_logs(tmp_path):
    service = ExecuteProcessService()
    result = service.run(
        workspace=tmp_path,
        argv=[
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-Command", "Write-Output 'DW5_REAL_POWERSHELL_PASS'",
        ],
        approved=True, actor="ci-human-approval-fixture",
        shell_profile=POWERSHELL_PROFILE,
    )
    assert result.observation.started
    assert result.observation.exit_code == 0
    assert "DW5_REAL_POWERSHELL_PASS" in result.observation.stdout.text_utf8
    assert result.authorization.receipt_id
    assert result.observation.observation_digest


def test_unknown_shell_policy_or_noninteractive_shape_rejected(tmp_path):
    with pytest.raises(InvalidProcessSpecError, match="unknown shell"):
        prepare_process_proposal(
            workspace=tmp_path, argv=["python", "-V"],
            shell_profile="any-shell-v0",
        )
    with pytest.raises(InvalidProcessSpecError, match="noninteractive"):
        prepare_process_proposal(
            workspace=tmp_path, argv=[
                "powershell.exe", "-Command", "Write-Output 'BAD'"
            ], shell_profile=POWERSHELL_PROFILE,
        )
