"""One continuation turn only, with no replay after unknown transport outcome."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from tools.dw_codexia_project_bootstrap_v1 import DW5ProjectRegistry
from tools.dw_codexia_controller_continue_v1 import (
    ControllerTurnStore, continue_controller,
)
from test_dw5_project_bootstrap_v1 import FakeCWA, request, CONTROLLER, TEXT

NEW_ID = "1546bd5d-70a9-4227-b739-2375c61056f8"


class ContinuingCWA(FakeCWA):
    def __init__(self, fail=False):
        super().__init__()
        self.fail_continuation = fail

    def send(self, prompt, *, model, reasoning_effort, temporary, conversation=None):
        if conversation is None:
            return super().send(prompt, model=model,
                                reasoning_effort=reasoning_effort, temporary=temporary)
        assert conversation == CONTROLLER
        assert temporary is False and model == "gpt-6" and reasoning_effort == "high"
        assert "not execution authority" in prompt.lower()
        self.sends += 1
        if self.fail_continuation:
            raise TimeoutError("unknown remote outcome")
        output = json.dumps({
            "schema_version": 1,
            "source_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
            "decision": "commands",
            "steps": [{"step": 1, "shell": "powershell", "command": "Write-Output 'SECOND_TURN'", "cwd": ".", "purpose": "Smoke"}],
            "reason": "Explicit second interpreted turn",
        })
        self.controller = SimpleNamespace(message_id=NEW_ID, text=output, role="assistant")
        return SimpleNamespace(conversation=SimpleNamespace(conversation_id=CONTROLLER, message_id=NEW_ID), text=output)


def test_same_controller_new_message_and_no_duplicate(tmp_path):
    registry = DW5ProjectRegistry(tmp_path / "store.sqlite3")
    client = ContinuingCWA()
    request(registry, client, tmp_path)
    outcome = continue_controller(registry=registry, client=client,
                                  project="player-intelligence", reason="Read-Host runtime is now verified")
    assert outcome["status"] == "CONTROLLER_ADVANCED_NO_LOCAL_EFFECT"
    assert outcome["controller_conversation"] == CONTROLLER
    assert outcome["controller_message_id"] == NEW_ID
    assert outcome["steps"] == 1 and client.sends == 2
    with pytest.raises(ValueError, match="already interpreted"):
        continue_controller(registry=registry, client=client,
                            project="player-intelligence", reason="Duplicate attempt")
    assert client.sends == 2
    assert ControllerTurnStore(registry).latest("player-intelligence")["phase"] == "ready"


def test_ambiguous_model_send_is_fenced_before_call(tmp_path):
    registry = DW5ProjectRegistry(tmp_path / "store.sqlite3")
    client = ContinuingCWA(fail=True)
    request(registry, client, tmp_path)
    with pytest.raises(TimeoutError):
        continue_controller(registry=registry, client=client,
                            project="player-intelligence", reason="Read-Host verified")
    assert ControllerTurnStore(registry).latest("player-intelligence")["phase"] == "send_admitted"
    with pytest.raises(ValueError, match="do NOT retry"):
        continue_controller(registry=registry, client=client,
                            project="player-intelligence", reason="Again")
    assert client.sends == 2


def test_controller_frontier_cannot_move_outside_registry(tmp_path):
    registry = DW5ProjectRegistry(tmp_path / "store.sqlite3")
    client = ContinuingCWA()
    request(registry, client, tmp_path)
    client.controller.message_id = NEW_ID
    with pytest.raises(ValueError, match="frontier differs"):
        continue_controller(registry=registry, client=client,
                            project="player-intelligence", reason="Read-Host verified")
    assert ControllerTurnStore(registry).latest("player-intelligence") is None
    assert client.sends == 1
