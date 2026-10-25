"""No live CWA calls. Exercises persistent project binding and no-replay fences."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from codexia_manual_agent.simple_work.session import SimpleWorkStore
from tools.dw_codexia_project_bootstrap_v1 import DW5ProjectRegistry, bootstrap

WORKER = "6adb4dd7-9ea0-83ed-bb9f-285ce4706b06"
CONTROLLER = "128bf496-358f-477d-a5a0-460df99160d8"
WORKER_MSG = "ab0f5ef0-d823-40d4-a795-75747ed6bebd"
CONTROLLER_MSG = "2f1bce44-a778-4f2b-9083-91b32eb9fe85"
TEXT = "Run now: powershell command Write-Output 'DW5_SAFE_BOOTSTRAP'."


class FakeCWA:
    def __init__(self, *, fail_send=False, invalid_plan=False):
        self.fail_send = fail_send
        self.invalid_plan = invalid_plan
        self.sends = 0
        self.worker = SimpleNamespace(message_id=WORKER_MSG, text=TEXT, role="assistant")
        self.controller = None

    def get_messages(self, conversation, limit=1):
        assert limit == 1
        if conversation == WORKER:
            return [self.worker]
        if conversation == CONTROLLER and self.controller is not None:
            return [self.controller]
        return []

    def send(self, prompt, *, model, reasoning_effort, temporary):
        assert model == "gpt-6" and reasoning_effort == "high"
        assert temporary is False
        assert "6adb4dd7" in prompt
        assert "source_sha256" in prompt
        self.sends += 1
        if self.fail_send:
            raise TimeoutError("unknown remote outcome")
        text = (
            "not-json" if self.invalid_plan else
            json.dumps({
                "schema_version": 1,
                "source_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
                "decision": "commands",
                "steps": [{
                    "step": 1, "shell": "powershell",
                    "command": "Write-Output 'DW5_SAFE_BOOTSTRAP'",
                    "cwd": ".", "purpose": "Smoke test",
                }],
                "reason": "The worker explicitly requests one local command",
            })
        )
        self.controller = SimpleNamespace(
            message_id=CONTROLLER_MSG, text=text, role="assistant"
        )
        return SimpleNamespace(
            conversation=SimpleNamespace(
                conversation_id=CONTROLLER, message_id=CONTROLLER_MSG,
            ), text=text,
        )


def request(registry, client, workspace):
    return bootstrap(
        registry=registry, client=client, project="player-intelligence",
        repo="kymuco/player-intelligence", workspace=str(workspace),
        worker_conversation=WORKER,
    )


def test_first_bootstrap_creates_one_saved_controller_and_reuses(tmp_path):
    registry = DW5ProjectRegistry(tmp_path / "simple_work.sqlite3")
    client = FakeCWA()
    first = request(registry, client, tmp_path)
    assert first["status"] == "CONTROLLER_CREATED_AND_BOUND_NO_LOCAL_EFFECT"
    assert first["phase"] == "ready"
    assert first["steps"] == 1
    assert client.sends == 1
    assert registry.require_ready("player-intelligence").controller_conversation == CONTROLLER
    assert SimpleWorkStore(registry.database).codexia("player-intelligence").conversation_id == CONTROLLER
    second = request(registry, client, tmp_path)
    assert second["status"] == "EXISTING_PROJECT_REUSED_NO_SEND"
    assert client.sends == 1


def test_unknown_network_send_is_not_retried(tmp_path):
    registry = DW5ProjectRegistry(tmp_path / "simple_work.sqlite3")
    client = FakeCWA(fail_send=True)
    with pytest.raises(TimeoutError):
        request(registry, client, tmp_path)
    assert registry.get("player-intelligence").phase == "send_admitted"
    with pytest.raises(ValueError, match="do NOT resend"):
        request(registry, client, tmp_path)
    assert client.sends == 1


def test_invalid_controller_result_retains_conversation_without_resend(tmp_path):
    registry = DW5ProjectRegistry(tmp_path / "simple_work.sqlite3")
    client = FakeCWA(invalid_plan=True)
    with pytest.raises(ValueError):
        request(registry, client, tmp_path)
    binding = registry.get("player-intelligence")
    assert binding.phase == "controller_known"
    assert binding.controller_conversation == CONTROLLER
    with pytest.raises(ValueError, match="do NOT resend"):
        request(registry, client, tmp_path)
    assert client.sends == 1


def test_binding_conflicts_fail_without_new_send(tmp_path):
    registry = DW5ProjectRegistry(tmp_path / "simple_work.sqlite3")
    client = FakeCWA()
    request(registry, client, tmp_path)
    with pytest.raises(ValueError, match="different identity"):
        bootstrap(registry=registry, client=client, project="player-intelligence",
                  repo="different/repo", workspace=str(tmp_path),
                  worker_conversation=WORKER)
    assert client.sends == 1


def test_capture_binding_uses_exact_stored_scope(tmp_path):
    from tools.dw_codexia_project_bootstrap_v1 import resolve_capture_binding
    registry = DW5ProjectRegistry(tmp_path / "simple_work.sqlite3")
    request(registry, FakeCWA(), tmp_path)
    args = SimpleNamespace(
        registry=str(registry.database), project="player-intelligence",
        repo="kymuco/player-intelligence", workspace=str(tmp_path),
        worker_conversation=None, codexia_conversation=None,
    )
    resolve_capture_binding(args)
    assert args.worker_conversation == WORKER
    assert args.codexia_conversation == CONTROLLER
    args.repo = "wrong/repo"
    with pytest.raises(ValueError, match="differs from binding"):
        resolve_capture_binding(args)


def test_worker_advanced_reuses_controller_without_stale_plan(tmp_path):
    registry = DW5ProjectRegistry(tmp_path / "simple_work.sqlite3")
    client = FakeCWA()
    request(registry, client, tmp_path)
    client.worker = SimpleNamespace(
        message_id="3c3dcde9-7319-411e-b982-e7b90c690139",
        text="Next new Worker reply", role="assistant",
    )
    result = request(registry, client, tmp_path)
    assert result["status"] == "EXISTING_PROJECT_WORKER_ADVANCED_NO_SEND"
    assert client.sends == 1


def test_reserved_alias_collision_fails_before_send(tmp_path):
    from uuid import uuid4
    from datetime import datetime, timezone
    registry = DW5ProjectRegistry(tmp_path / "simple_work.sqlite3")
    with registry._open() as db:
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO simple_codexia_chats_v1 "
            "(alias,session_id,conversation_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?)",
            ("player-intelligence", str(uuid4()), str(uuid4()), now, now),
        )
    client = FakeCWA()
    with pytest.raises(ValueError, match="alias already registered"):
        request(registry, client, tmp_path)
    assert client.sends == 0
