from __future__ import annotations

from types import SimpleNamespace

import pytest

from codexia_manual_agent.domain.errors import ProviderError, ProviderUnavailableError
from codexia_manual_agent.domain.models import ProviderConversation, ProviderRequest
from codexia_manual_agent.providers.chatgpt_http import ChatGPTHttpProvider
from codexia_manual_agent.providers.model_provider_cognition import (
    ModelProviderCognitionPort,
)


class FakeHTTPClient:
    def __init__(self) -> None:
        self.calls = []
        self.fail = False
        self.invalid_identity = False
        self.missing_ids = False
        self.auth_refresh_calls = []
        self.auth_refresh_error = None
        self.auth_refresh_receipt = SimpleNamespace(
            status_code=200, access_token_present=True, persisted=False
        )

    def refresh_auth(self, *, persist):
        self.auth_refresh_calls.append(persist)
        if self.auth_refresh_error is not None:
            raise self.auth_refresh_error
        return self.auth_refresh_receipt

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



def test_http_preflight_auth_get_only_keeps_credentials_in_memory() -> None:
    client = FakeHTTPClient()
    provider = ChatGPTHttpProvider(client=client)
    provider.preflight_read_only()
    assert client.auth_refresh_calls == [False]
    assert client.calls == []


@pytest.mark.parametrize(
    "receipt",
    [
        SimpleNamespace(status_code=401, access_token_present=False, persisted=False),
        SimpleNamespace(status_code=200, access_token_present=False, persisted=False),
        SimpleNamespace(status_code=200, access_token_present=True, persisted=True),
        None,
    ],
)
def test_http_preflight_rejects_unproved_or_persistent_auth(receipt) -> None:
    client = FakeHTTPClient()
    client.auth_refresh_receipt = receipt
    provider = ChatGPTHttpProvider(client=client)
    with pytest.raises(ProviderUnavailableError, match="HTTP_PREFLIGHT_FAILED"):
        provider.preflight_read_only()
    assert client.auth_refresh_calls == [False]
    assert client.calls == []


def test_http_preflight_auth_error_is_redacted_and_never_sends() -> None:
    client = FakeHTTPClient()
    client.auth_refresh_error = RuntimeError("401 token=PRIVATE_SECRET")
    provider = ChatGPTHttpProvider(client=client)
    with pytest.raises(ProviderUnavailableError, match="HTTP_PREFLIGHT_FAILED") as error:
        provider.preflight_read_only()
    assert "PRIVATE_SECRET" not in str(error.value)
    assert client.calls == []


def test_http_preflight_rejects_missing_refresh_capability() -> None:
    class NoAuthGet:
        def send(self, *_args, **_kwargs):
            raise AssertionError("conversation POST is forbidden in preflight")

    provider = ChatGPTHttpProvider(client=NoAuthGet())
    with pytest.raises(ProviderUnavailableError, match="HTTP_PREFLIGHT_FAILED"):
        provider.preflight_read_only()


@pytest.mark.parametrize(
    "node_version,ready",
    [("v23.8.0", False), ("v24.0.0", True), ("v25.3.1", True), ("invalid", False)],
)
def test_http_preflight_checks_node_runtime_before_session_get(
    monkeypatch, node_version, ready
) -> None:
    client = FakeHTTPClient()
    client.node_bin = "node"
    provider = ChatGPTHttpProvider(client=client)
    checked = []

    def fake_run(args, **kwargs):
        checked.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=node_version + "\n")

    monkeypatch.setattr(
        "codexia_manual_agent.providers.chatgpt_http.subprocess.run", fake_run
    )
    if ready:
        provider.preflight_read_only()
        assert client.auth_refresh_calls == [False]
    else:
        with pytest.raises(ProviderUnavailableError, match="Node 24"):
            provider.preflight_read_only()
        assert client.auth_refresh_calls == []
    assert checked[0][0] == ["node", "--version"]
    assert client.calls == []


def test_http_preflight_fails_before_auth_get_if_curl_missing() -> None:
    client = FakeHTTPClient()
    client.curl_bin = ""
    with pytest.raises(ProviderUnavailableError, match="curl unavailable"):
        ChatGPTHttpProvider(client=client).preflight_read_only()
    assert client.auth_refresh_calls == []
    assert client.calls == []


def test_http_send_preserves_system_as_visible_text_not_hidden_authority() -> None:
    client = FakeHTTPClient()
    provider = ChatGPTHttpProvider(client=client)

    result = provider.send(
        ProviderRequest("Inspect this.", system="Act as a reviewer.")
    )

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
        reasoning_effort="high",
    )

    provider.send(ProviderRequest(prompt="Hi"))
    assert client.calls[0][1]["model"] == "gpt-6-instant"
    assert client.calls[0][1]["reasoning_effort"] == "high"


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
    assert len(arguments) == 1
    assert (
        arguments[0]["auth_file"].replace(chr(92), "/") == "C:/private/auth_data.json"
    )
    assert arguments[0]["timeout"] == 120


def test_http_missing_or_conflicting_client_fails_before_write() -> None:
    client = FakeHTTPClient()
    with pytest.raises(ValueError, match="client or client_factory"):
        ChatGPTHttpProvider(client=client, client_factory=lambda **kw: client)
    with pytest.raises(ValueError, match="model"):
        ChatGPTHttpProvider(client=client, model=" ")
    with pytest.raises(ValueError, match="timeout"):
        ChatGPTHttpProvider(client=client, timeout=0)
    with pytest.raises(ValueError, match="reasoning_effort"):
        ChatGPTHttpProvider(client=client, reasoning_effort="low")
    assert client.calls == []


def test_falsy_injected_http_client_never_activates_browser_factory() -> None:
    class FalsyHTTP(FakeHTTPClient):
        def __bool__(self):
            return False

    client = FalsyHTTP()
    provider = ChatGPTHttpProvider(client=client)
    assert provider.send(ProviderRequest(prompt="direct only")).text == "HTTP response"
    assert len(client.calls) == 1


def test_http_invalid_continuation_is_rejected_without_a_new_chat() -> None:
    client = FakeHTTPClient()
    provider = ChatGPTHttpProvider(client=client)
    with pytest.raises(ValueError, match="exact nonempty conversation_id"):
        provider.send(
            ProviderRequest(
                prompt="This must not start another chat",
                conversation=ProviderConversation(conversation_id=None),
            )
        )
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
