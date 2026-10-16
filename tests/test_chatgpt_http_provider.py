from __future__ import annotations

from types import SimpleNamespace

import pytest

from codexia_manual_agent.domain.errors import ProviderError, ProviderUnavailableError
from codexia_manual_agent.domain.models import ProviderConversation, ProviderRequest
from codexia_manual_agent.providers.chatgpt_http import ChatGPTHttpProvider
from codexia_manual_agent.providers.model_provider_cognition import ModelProviderCognitionPort


class FakeHTTPClient:
    def __init__(self) -> None:
        self.calls = []
        self.fail = False
        self.invalid_identity = False
        self.missing_ids = False

    def send(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.fail:
            raise TimeoutError("sensitive details must not appear in logs")
        conversation_id = (
            "other-chat"
            if self.invalid_identity
            else kwargs.get("conversation") or "chat-1"
        )
        return SimpleNamespace(
            text="HTTP response",
            conversation=SimpleNamespace(
                conversation_id=None if self.missing_ids else conversation_id,
                message_id=None if self.missing_ids else "msg-1",
                parent_message_id="msg-1",
                finish_reason="stop",
            ),
            request=SimpleNamespace(
                observed_model="gpt-6",
                sent_model=kwargs["model"],
                observed_reasoning_effort=None,
                sent_reasoning_effort=None,
            ),
            metrics=SimpleNamespace(to_dict=lambda: {"total": 1.0}),
        )

    def get_status(self, conversation_id):
        return SimpleNamespace(
            status="completed",
            message_id="msg-1",
            finish_reason="stop",
        )

    def get_messages(self, conversation_id, **kwargs):
        return [
            SimpleNamespace(
                node_id="node-1",
                message_id="msg-1",
                role="assistant",
                text="HTTP response",
                create_time=None,
                recipient="all",
                model="gpt-6",
                finish_reason="stop",
            ),
        ]


def test_http_send_preserves_system_as_visible_text_not_hidden_authority() -> None:
    client = FakeHTTPClient()
    provider = ChatGPTHttpProvider(client=client)

    result = provider.send(ProviderRequest("Inspect this.", system="Act as a reviewer."))

    assert provider.provider_id == "chatgpt-http"
    assert ModelProviderCognitionPort(provider).port_id == "model-provider:chatgpt-http"
    assert client.calls[0][0] == (
        "[Codexia product-runtime system context]\n"
        "Act as a reviewer.\n\n"
        "[Codexia product-runtime request]\n"
        "Inspect this."
    )
    assert client.calls[0][1]["temporary"] is False
    assert client.calls[0][1]["model"] == "gpt-6-instant"
    assert result.text == "HTTP response"
    assert result.model == "gpt-6"
    assert result.conversation.conversation_id == "chat-1"
    assert result.conversation.message_id == "msg-1"


def test_http_continuation_and_canonical_read_only_api() -> None:
    client = FakeHTTPClient()
    provider = ChatGPTHttpProvider(client=client)

    result = provider.send(
        ProviderRequest(
            prompt="Next step.",
            conversation=ProviderConversation(
                conversation_id="existing-chat",
                message_id="previous",
            ),
        )
    )
    assert result.conversation.conversation_id == "existing-chat"
    assert client.calls[0][1]["conversation"] == "existing-chat"
    assert provider.read_status("existing-chat").message_id == "msg-1"
    assert provider.read_messages("existing-chat")[0].text == "HTTP response"


def test_http_model_slug_and_reasoning_forwarded_without_profile_translation() -> None:
    client = FakeHTTPClient()
    provider = ChatGPTHttpProvider(
        client=client,
        model="gpt-6-instant",
        reasoning_effort="low",
    )

    provider.send(ProviderRequest(prompt="Hi"))
    assert client.calls[0][1]["model"] == "gpt-6-instant"
    assert client.calls[0][1]["reasoning_effort"] == "low"


def test_http_ambiguous_error_has_no_fallback_retry_or_secret_leak() -> None:
    client = FakeHTTPClient()
    client.fail = True
    provider = ChatGPTHttpProvider(client=client)

    with pytest.raises(ProviderError, match="do not retry") as raised:
        provider.send(ProviderRequest(prompt="One attempt"))
    assert len(client.calls) == 1
    assert "sensitive details" not in str(raised.value)


def test_http_continuation_identity_drift_fails_closed_without_resend() -> None:
    client = FakeHTTPClient()
    client.invalid_identity = True
    provider = ChatGPTHttpProvider(client=client)

    with pytest.raises(ProviderError, match="switched conversation identity"):
        provider.send(
            ProviderRequest(
                prompt="Continue",
                conversation=ProviderConversation(conversation_id="existing"),
            )
        )
    assert len(client.calls) == 1


def test_http_missing_ids_fail_closed() -> None:
    client = FakeHTTPClient()
    client.missing_ids = True
    provider = ChatGPTHttpProvider(client=client)

    with pytest.raises(ProviderError, match="canonical IDs"):
        provider.send(ProviderRequest(prompt="Hello"))
    assert len(client.calls) == 1


def test_http_explicit_factory_cannot_assemble_browser_runtime() -> None:
    client = FakeHTTPClient()
    arguments = []

    def factory(**kwargs):
        arguments.append(kwargs)
        return client

    provider = ChatGPTHttpProvider(
        client_factory=factory,
        auth_file="C:/private/auth_data.json",
    )
    assert provider.provider_id == "chatgpt-http"
    assert arguments == [
        {"auth_file": "C:/private/auth_data.json", "timeout": 120}
    ]


def test_http_missing_or_conflicting_client_fails_before_write() -> None:
    client = FakeHTTPClient()
    with pytest.raises(ValueError, match="client or client_factory"):
        ChatGPTHttpProvider(client=client, client_factory=lambda **kw: client)
    with pytest.raises(ValueError, match="model"):
        ChatGPTHttpProvider(client=client, model=" ")
    with pytest.raises(ValueError, match="timeout"):
        ChatGPTHttpProvider(client=client, timeout=0)
    assert client.calls == []


def test_http_temporary_turn_and_unsupported_browser_features() -> None:
    client = FakeHTTPClient()
    provider = ChatGPTHttpProvider(client=client)
    response = provider.send_temporary("Hello temporary")
    assert response.text == "HTTP response"
    assert client.calls[-1][1]["temporary"] is True
    with pytest.raises(ProviderUnavailableError, match="Temporary lifecycle"):
        provider.end_temporary_chat()
    with pytest.raises(ProviderUnavailableError, match="not implemented"):
        provider.handoff_generated_artifact(
            "chat-id",
            filename="artifact.py",
            destination="out.py",
        )
