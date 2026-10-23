"""Offline Gen2 command bridge proof. Never spawns PowerShell or calls CWA."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from codexia_manual_agent.capability_core import CapabilityOutcome
from codexia_manual_agent.work_core import SqliteWorkStore
from tools import dw_chat_command_v0 as bridge


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _assistant(workspace: Path, script: Path, *, message_id: str):
    tick = chr(96) * 3
    text = (
        f"{tick}powershell\n"
        f'& "{script}" '
        + chr(96) + "\n    -CwaTransport browserless-http\n"
        f"{tick}"
    )
    return SimpleNamespace(role="assistant", message_id=message_id, text=text)


class _Client:
    def __init__(self, message):
        self.message = message
        self.calls = []

    def get_messages(self, conversation_id, *, limit):
        self.calls.append((conversation_id, limit))
        return [self.message]


class _Args:
    pass


def _capture(store, client, workspace, *, index=1):
    args = _Args()
    args.conversation = f"00000000-0000-4000-8000-{index:012d}"
    args.project = f"project-{index}"
    args.repo = f"user/repo-{index}"
    args.workspace = str(workspace)
    args.message_id = client.message.message_id
    return bridge._capture(args, store, client)


class _Host:
    host_id = "standalone.local"

    def __init__(self):
        self.calls = 0

    def submit(self, request):
        self.calls += 1
        return CapabilityOutcome.succeeded(
            request.need,
            attempt_id="fake-local-process",
            attempt_digest=_hash("fake-local-process"),
            observation={
                "execution": {
                    "exit_code": 0, "stdout": {"text_utf8_preview": "ALL PASS"},
                    "stderr": {"text_utf8_preview": ""},
                }
            },
        )


def test_one_real_gen2_work_dispatch_and_no_replay(tmp_path, monkeypatch) -> None:
    store = SqliteWorkStore(tmp_path / "commands.sqlite")
    workspace = tmp_path / "project"
    workspace.mkdir()
    script = workspace / "check.ps1"
    script.write_text("Write-Host 'PASS'\n", encoding="utf-8")
    client = _Client(_assistant(
        workspace, script, message_id="11111111-1111-4111-8111-111111111111"
    ))
    captured = _capture(store, client, workspace)
    work_id = captured["work_id"]
    assert captured["status"] == "CAPTURED_NOT_EXECUTED"
    assert captured["argv"][-2:] == ["-CwaTransport", "browserless-http"]
    assert client.calls == [(captured["conversation_id"], 1)]
    assert len(store.events(work_id)) == 2
    assert bridge._cap(store, work_id) is None

    host = _Host()
    monkeypatch.setattr(
        bridge, "StandaloneProcessCapabilityPort", lambda **kwargs: host
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: "EXECUTE " + bridge._sha(
            bridge._json(bridge._spec(store.snapshot(work_id).work.objective))
        )[:12],
    )
    args = _Args()
    args.work_id = work_id
    result = bridge._execute(args, store)
    assert result["status"] == "succeeded"
    assert result["outcome"]["observation"]["execution"]["exit_code"] == 0
    assert host.calls == 1
    kinds = [e.kind for e in store.events(work_id)]
    assert kinds.count("capability.handoff-admitted") == 1
    assert kinds.count("capability.outcome-recorded") == 1
    assert "work.completed" not in kinds
    before = store.events(work_id)
    with pytest.raises(ValueError, match="already handed off"):
        bridge._execute(args, store)
    assert host.calls == 1
    assert store.events(work_id) == before


def test_five_projects_and_conversations_do_not_mix(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "shared.sqlite")
    ids = []
    for index in range(1, 6):
        workspace = tmp_path / f"project-{index}"
        workspace.mkdir()
        script = workspace / "check.ps1"
        script.write_text(f"Write-Output {index}\n", encoding="utf-8")
        message_id = f"11111111-1111-4111-8111-{index:012d}"
        client = _Client(_assistant(workspace, script, message_id=message_id))
        result = _capture(store, client, workspace, index=index)
        ids.append(result["work_id"])
        assert result["project"] == f"project-{index}"
    assert len(set(ids)) == 5
    views = [bridge._status(store, item) for item in ids]
    assert len({item["conversation_id"] for item in views}) == 5
    assert len({item["repo"] for item in views}) == 5
    assert all(item["revision"] == 2 for item in views)
    assert all(item["outcome"] is None for item in views)


def test_stale_script_and_rejected_approval_make_no_handoff(tmp_path, monkeypatch) -> None:
    store = SqliteWorkStore(tmp_path / "rejected.sqlite")
    workspace = tmp_path / "project"
    workspace.mkdir()
    script = workspace / "test.ps1"
    script.write_text("Write-Output ok\n", encoding="utf-8")
    client = _Client(_assistant(
        workspace, script, message_id="22222222-2222-4222-8222-222222222222"
    ))
    work_id = _capture(store, client, workspace)["work_id"]
    args = _Args()
    args.work_id = work_id
    script.write_text("Write-Output changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="script bytes changed"):
        bridge._execute(args, store)
    assert len(store.events(work_id)) == 2
    script.write_text("Write-Output ok\n", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt: "NO")
    with pytest.raises(ValueError, match="declined"):
        bridge._execute(args, store)
    assert len(store.events(work_id)) == 2


def test_chat_code_must_be_single_safe_literal_script(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    script = root / "do.ps1"
    script.write_text("Write-Output ok\n", encoding="utf-8")
    tick = chr(96) * 3
    good = f'{tick}powershell\n& "{script}" -Mode safe\n{tick}'
    assert bridge._command(good, root)[0][-2:] == ["-Mode", "safe"]
    for code in [
        f'{tick}powershell\n& "{script}"; Remove-Item C:\\foo\n{tick}',
        f'{tick}powershell\n& "{script}" | Out-File bad.txt\n{tick}',
        f'{tick}powershell\n& "{script}" -Mode $env:SECRET\n{tick}',
        f'{tick}powershell\n& "{script}" -Mode safe\n{tick}\n{tick}ps1\nx\n{tick}',
    ]:
        with pytest.raises(ValueError):
            bridge._command(code, root)
    outside = tmp_path / "outside.ps1"
    outside.write_text("Write-Output outside\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        bridge._command(f'{tick}powershell\n& "{outside}"\n{tick}', root)


def test_not_latest_assistant_message_fails(tmp_path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    script = workspace / "check.ps1"
    script.write_text("Write-Output 1\n", encoding="utf-8")
    client = _Client(_assistant(
        workspace, script, message_id="11111111-1111-4111-8111-111111111111"
    ))
    args = _Args()
    args.project = "project"
    args.repo = "user/repo"
    args.workspace = str(workspace)
    args.conversation = "00000000-0000-4000-8000-000000000001"
    args.message_id = "22222222-2222-4222-8222-222222222222"
    store = SqliteWorkStore(tmp_path / "none.sqlite")
    with pytest.raises(ValueError, match="differs"):
        bridge._capture(args, store, client)


def test_same_chat_result_post_readback_and_no_replay(tmp_path, monkeypatch) -> None:
    store = SqliteWorkStore(tmp_path / "post.sqlite")
    root = tmp_path / "project"
    root.mkdir()
    script = root / "check.ps1"
    script.write_text("Write-Output 'PASS'\n", encoding="utf-8")
    original = _assistant(
        root, script, message_id="33333333-3333-4333-8333-333333333333"
    )
    client = _Client(original)
    info = _capture(store, client, root)
    work_id = info["work_id"]
    fake = _Host()
    monkeypatch.setattr(bridge, "StandaloneProcessCapabilityPort", lambda **k: fake)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: "EXECUTE " + bridge._sha(
            bridge._json(bridge._spec(store.snapshot(work_id).work.objective))
        )[:12],
    )
    args = _Args()
    args.work_id = work_id
    assert bridge._execute(args, store)["status"] == "succeeded"

    class _PostClient(_Client):
        def __init__(self, initial):
            super().__init__(initial)
            self.sends = []
            self.response_id = "44444444-4444-4444-8444-444444444444"

        def send(self, body, *, conversation, model):
            self.sends.append((body, conversation, model))
            self.message = SimpleNamespace(
                role="assistant", message_id=self.response_id, text="Acknowledged PASS"
            )
            return SimpleNamespace(
                text="Acknowledged PASS",
                conversation=SimpleNamespace(
                    conversation_id=conversation, message_id=self.response_id
                ),
            )

    sender = _PostClient(original)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: "POST " + prompt.strip().split()[2].rstrip(":"),
    )
    # Match the digest exactly, not the proposed text of the model.
    # The prompted token includes the exact first 12 SHA-256 hex characters.
    args.model = "gpt-6-instant"
    result = bridge._post(args, store, sender)
    assert result["status"] == "SAME_CHAT_READBACK_VERIFIED"
    assert result["conversation_id"] == info["conversation_id"]
    assert len(sender.sends) == 1
    body, conv, model = sender.sends[0]
    assert conv == info["conversation_id"]
    assert model == "gpt-6-instant"
    assert "ALL PASS" in body
    assert "project-1" in body
    assert bridge._status(store, work_id)["post_handoff"]
    assert bridge._status(store, work_id)["post_readback"]

    before = store.events(work_id)
    with pytest.raises(ValueError, match="never POST twice"):
        bridge._post(args, store, sender)
    assert len(sender.sends) == 1
    assert store.events(work_id) == before


def test_post_chat_frontier_change_is_fail_closed(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "frontier.sqlite")
    root = tmp_path / "project"
    root.mkdir()
    script = root / "check.ps1"
    script.write_text("Write-Output 'PASS'\n", encoding="utf-8")
    original = _assistant(
        root, script, message_id="55555555-5555-4555-8555-555555555555"
    )
    info = _capture(store, _Client(original), root)
    work_id = info["work_id"]
    # No outcome is available yet: POST must fail even with a valid conversation.
    args = _Args()
    args.work_id = work_id
    args.model = "gpt-6-instant"
    with pytest.raises(ValueError, match="no terminal"):
        bridge._post(args, store, _Client(original))
    assert not bridge._status(store, work_id)["post_handoff"]


def test_one_command_run_with_two_approvals(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    script = root / "test.ps1"
    script.write_text("Write-Output ok\n", encoding="utf-8")
    source = _assistant(
        root, script, message_id="66666666-6666-4666-8666-666666666666"
    )
    class Sender(_Client):
        def __init__(self, original):
            super().__init__(original)
            self.posts = []

        def send(self, text, *, conversation, model):
            self.posts.append((text, conversation, model))
            self.message = SimpleNamespace(
                role="assistant",
                message_id="77777777-7777-4777-8777-777777777777",
                text="Result accepted",
            )
            return SimpleNamespace(
                text="Result accepted",
                conversation=SimpleNamespace(
                    conversation_id=conversation,
                    message_id=self.message.message_id,
                ),
            )
    client = Sender(source)
    host = _Host()
    monkeypatch.setattr(bridge, "_client", lambda auth: client)
    monkeypatch.setattr(
        bridge, "StandaloneProcessCapabilityPort", lambda **kwargs: host
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: " ".join(prompt.strip().split()[1:3])
        if prompt.startswith("Type EXECUTE ")
        else "POST " + prompt.strip().split()[2].rstrip(":"),
    )
    args = [
        "--store", str(tmp_path / "pilot.sqlite"), "run",
        "--conversation", "00000000-0000-4000-8000-000000000001",
        "--project", "project-1", "--repo", "user/repo-1",
        "--workspace", str(root), "--auth-file", "unused-test-auth.json",
    ]
    assert bridge.main(args) == 0
    assert len(client.posts) == 1
    assert host.calls == 1
    output = capsys.readouterr().out
    assert "CAPTURED_NOT_EXECUTED" in output
    assert "SAME_CHAT_READBACK_VERIFIED" in output
