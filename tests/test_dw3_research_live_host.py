from __future__ import annotations

import json
from types import SimpleNamespace

from codexia_manual_agent.domain.models import ProviderRequest
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
    first = provider.send(ProviderRequest(prompt="first"))
    second = provider.send(ProviderRequest(prompt="second"))

    assert "--conversation" not in commands[0]
    continuation_index = commands[1].index("--conversation")
    assert commands[1][continuation_index + 1] == "conversation-1"
    assert first.metrics["pilot_continuation"] is False
    assert second.metrics["pilot_continuation"] is True

    restarted = _ExternalCwaProvider(
        executable=str(executable),
        auth_file="auth.json",
        profile="DEEP",
        timeout=420,
    )
    restarted.send(ProviderRequest(prompt="after restart"))
    assert "--conversation" not in commands[2]
