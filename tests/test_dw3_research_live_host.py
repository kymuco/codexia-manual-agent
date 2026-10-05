from __future__ import annotations

import json
from types import SimpleNamespace

from codexia_manual_agent.domain.models import ProviderConversation, ProviderRequest
from codexia_manual_agent.workflow_runtime.research_v1 import research_context_text
from examples.dw3_research_live_host import _ExternalCwaProvider


def _payload(*, text: str, message_id: str) -> str:
    return json.dumps(
        {
            "transport": "browser-owned",
            "ok": True,
            "text": text,
            "conversation_id": "conversation-1",
            "message_id": message_id,
            "finish_reason": "stop",
            "observed_model": "test-model",
            "backend_status": 200,
            "runtime_observation": {},
            "provenance": {},
        }
    )


def test_external_cwa_provider_reuses_conversation_only_within_instance(
    tmp_path,
    monkeypatch,
) -> None:
    executable = tmp_path / "cwa.exe"
    executable.write_bytes(b"test")
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        return SimpleNamespace(
            returncode=0,
            stdout=_payload(
                text=f"response-{len(commands)}",
                message_id=f"message-{len(commands)}",
            ),
            stderr="",
        )

    monkeypatch.setattr("examples.dw3_research_live_host.subprocess.run", fake_run)

    provider = _ExternalCwaProvider(
        executable=str(executable),
        auth_file="auth.json",
        profile="DEEP",
        timeout=420,
    )
    initial_context = research_context_text(
        objective="choose durable storage",
        stage="initial",
        prior_outputs=(),
        human_responses=(),
    )
    researcher_output = "SQLite is the strongest default for the control plane."
    critique_context = research_context_text(
        objective="choose durable storage",
        stage="critique",
        prior_outputs=(researcher_output,),
        human_responses=(),
    )

    first = provider.send(
        ProviderRequest(prompt=initial_context, system="researcher instructions")
    )
    second = provider.send(
        ProviderRequest(prompt=critique_context, system="critic instructions")
    )

    assert "--conversation" not in commands[0]
    assert "choose durable storage" in commands[0][2]
    assert '"prior_outputs":[]' in commands[0][2]

    continuation_index = commands[1].index("--conversation")
    assert commands[1][continuation_index + 1] == "conversation-1"
    assert "critic instructions" in commands[1][2]
    assert '"context_delivery":"hot-conversation-delta"' in commands[1][2]
    assert '"stage":"critique"' in commands[1][2]
    assert "prior_outputs" not in commands[1][2]
    assert researcher_output not in commands[1][2]
    assert "choose durable storage" not in commands[1][2]
    assert first.metrics["pilot_continuation"] is False
    assert first.metrics["pilot_context_mode"] == "full-rehydration"
    assert second.metrics["pilot_continuation"] is True
    assert second.metrics["pilot_context_mode"] == "hot-conversation-delta"

    restarted = _ExternalCwaProvider(
        executable=str(executable),
        auth_file="auth.json",
        profile="DEEP",
        timeout=420,
    )
    restarted_response = restarted.send(
        ProviderRequest(prompt=critique_context, system="critic instructions")
    )
    assert "--conversation" not in commands[2]
    assert "choose durable storage" in commands[2][2]
    assert researcher_output in commands[2][2]
    assert '"prior_outputs"' in commands[2][2]
    assert restarted_response.metrics["pilot_context_mode"] == "full-rehydration"

    externally_bound = _ExternalCwaProvider(
        executable=str(executable),
        auth_file="auth.json",
        profile="DEEP",
        timeout=420,
    )
    externally_bound_response = externally_bound.send(
        ProviderRequest(
            prompt=critique_context,
            system="critic instructions",
            conversation=ProviderConversation(conversation_id="conversation-1"),
        )
    )
    external_index = commands[3].index("--conversation")
    assert commands[3][external_index + 1] == "conversation-1"
    assert researcher_output in commands[3][2]
    assert '"prior_outputs"' in commands[3][2]
    assert externally_bound_response.metrics["pilot_context_mode"] == "full-rehydration"

