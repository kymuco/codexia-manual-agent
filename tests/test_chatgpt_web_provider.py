from __future__ import annotations

import unittest
from types import SimpleNamespace

from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import ProviderConversation, ProviderRequest
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider


class FakeMetrics:
    def to_dict(self):
        return {"total": 1.25}


class FakeRuntime:
    def __init__(self) -> None:
        self.calls = []
        self.response = SimpleNamespace(
            text='{"type":"final","text":"ok"}',
            conversation=SimpleNamespace(
                conversation_id="c1",
                message_id="m1",
                parent_message_id="m1",
                finish_reason="stop",
            ),
            request=SimpleNamespace(
                observed_model="gpt-test",
                sent_model="gpt-fallback",
                observed_reasoning_effort="extended",
                sent_reasoning_effort="standard",
            ),
            metrics=FakeMetrics(),
        )
        self.messages = [
            SimpleNamespace(
                node_id="n-user",
                message_id="m-user",
                role="user",
                text="hello",
                create_time=1.0,
                recipient=None,
                model=None,
                finish_reason=None,
            ),
            SimpleNamespace(
                node_id="n-assistant",
                message_id="m-assistant",
                role="assistant",
                text="hi",
                create_time=2.0,
                recipient="all",
                model="gpt-test",
                finish_reason="stop",
            ),
        ]

    def send_text_observed(self, text, **kwargs):
        self.calls.append(("send_text_observed", text, kwargs))
        return SimpleNamespace(transport="browser-owned", response=self.response)

    def get_messages(self, conversation_id, **kwargs):
        self.calls.append(("history", conversation_id, kwargs))
        return self.messages


class FakeLegacyClient:
    def __init__(self) -> None:
        self.calls = []
        self.response = FakeRuntime().response

    def send(self, prompt, **kwargs):
        self.calls.append(("send", prompt, kwargs))
        return self.response

    def send_to_conversation(self, conversation_id, prompt, **kwargs):
        self.calls.append(("continue", conversation_id, prompt, kwargs))
        return self.response

    def get_messages(self, conversation_id, **kwargs):
        self.calls.append(("history", conversation_id, kwargs))
        return FakeRuntime().messages


class ChatGPTWebProviderTests(unittest.TestCase):
    def test_product_runtime_new_conversation_uses_browser_owned_observed_send(self) -> None:
        runtime = FakeRuntime()
        provider = ChatGPTWebProvider(runtime=runtime, reasoning_effort="high")

        response = provider.send(ProviderRequest(prompt="task"))

        call = runtime.calls[0]
        self.assertEqual(call[0], "send_text_observed")
        self.assertEqual(call[1], "task")
        self.assertIsNone(call[2]["conversation"])
        self.assertEqual(call[2]["model_profile"], "DEEP")
        self.assertEqual(response.conversation.conversation_id, "c1")
        self.assertEqual(response.model, "gpt-test")
        self.assertEqual(response.reasoning_effort, "extended")
        self.assertEqual(response.metrics["total"], 1.25)

    def test_product_runtime_existing_conversation_preserves_identity(self) -> None:
        runtime = FakeRuntime()
        provider = ChatGPTWebProvider(runtime=runtime)

        provider.send(
            ProviderRequest(
                prompt="continue",
                conversation=ProviderConversation(conversation_id="existing"),
            )
        )

        call = runtime.calls[0]
        self.assertEqual(call[0], "send_text_observed")
        self.assertEqual(call[1], "continue")
        self.assertEqual(call[2]["conversation"], "existing")

    def test_product_runtime_system_contract_is_visible_not_silently_dropped(self) -> None:
        runtime = FakeRuntime()
        provider = ChatGPTWebProvider(runtime=runtime)

        provider.send(ProviderRequest(prompt="task", system="system contract"))

        sent = runtime.calls[0][1]
        self.assertEqual(
            sent,
            "[Codexia product-runtime system context]\n"
            "system contract\n\n"
            "[Codexia product-runtime request]\n"
            "task",
        )

    def test_semantic_model_profile_is_forwarded(self) -> None:
        runtime = FakeRuntime()
        provider = ChatGPTWebProvider(runtime=runtime, model="balanced")

        provider.send(ProviderRequest(prompt="task"))

        self.assertEqual(runtime.calls[0][2]["model_profile"], "BALANCED")

    def test_raw_model_slug_fails_closed_before_product_write(self) -> None:
        runtime = FakeRuntime()
        provider = ChatGPTWebProvider(runtime=runtime, model="gpt-5-6-thinking")

        with self.assertRaisesRegex(ProviderError, "does not accept raw model slugs"):
            provider.send(ProviderRequest(prompt="task"))
        self.assertEqual(runtime.calls, [])

    def test_conflicting_profile_and_reasoning_fail_closed(self) -> None:
        runtime = FakeRuntime()
        provider = ChatGPTWebProvider(
            runtime=runtime,
            model="FAST",
            reasoning_effort="high",
        )

        with self.assertRaisesRegex(ProviderError, "conflicting product modes"):
            provider.send(ProviderRequest(prompt="task"))
        self.assertEqual(runtime.calls, [])

    def test_runtime_factory_receives_exact_product_transport(self) -> None:
        runtime = FakeRuntime()
        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return runtime

        ChatGPTWebProvider(
            auth_file="auth.json",
            timeout=91.9,
            runtime_factory=factory,
        )

        self.assertEqual(captured["transport"], "browser-owned")
        self.assertEqual(captured["auth_file"], "auth.json")
        self.assertEqual(captured["client_timeout"], 91)

    def test_history_read_uses_complete_canonical_current_branch_surface(self) -> None:
        runtime = FakeRuntime()
        provider = ChatGPTWebProvider(runtime=runtime)

        messages = provider.read_messages("existing")

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].node_id, "n-user")
        self.assertEqual(messages[0].message_id, "m-user")
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].text, "hello")
        self.assertEqual(messages[1].role, "assistant")
        call = runtime.calls[0]
        self.assertEqual(call[:2], ("history", "existing"))
        self.assertEqual(call[2]["roles"], ("user", "assistant"))
        self.assertFalse(call[2]["include_empty"])
        self.assertIsNone(call[2]["limit"])

    def test_history_requires_exact_visible_message_identity(self) -> None:
        runtime = FakeRuntime()
        runtime.messages[0].node_id = None
        provider = ChatGPTWebProvider(runtime=runtime)
        with self.assertRaisesRegex(ProviderError, "missing node_id"):
            provider.read_messages("existing")

    def test_history_rejects_duplicate_provider_identity(self) -> None:
        runtime = FakeRuntime()
        runtime.messages[1].node_id = "n-user"
        provider = ChatGPTWebProvider(runtime=runtime)
        with self.assertRaisesRegex(ProviderError, "duplicate message identity"):
            provider.read_messages("existing")

    def test_product_runtime_transport_identity_must_be_browser_owned(self) -> None:
        class WrongTransportRuntime(FakeRuntime):
            def send_text_observed(self, text, **kwargs):
                self.calls.append(("send_text_observed", text, kwargs))
                return SimpleNamespace(
                    transport="browserless-request",
                    response=self.response,
                )

        provider = ChatGPTWebProvider(runtime=WrongTransportRuntime())
        with self.assertRaisesRegex(ProviderError, "unexpected transport identity"):
            provider.send(ProviderRequest(prompt="task"))

    def test_product_runtime_exception_is_wrapped(self) -> None:
        class BrokenRuntime(FakeRuntime):
            def send_text_observed(self, text, **kwargs):
                raise RuntimeError("bridge changed")

        provider = ChatGPTWebProvider(runtime=BrokenRuntime())
        with self.assertRaisesRegex(ProviderError, "bridge changed"):
            provider.send(ProviderRequest(prompt="task"))

    def test_history_exception_is_wrapped(self) -> None:
        class BrokenRuntime(FakeRuntime):
            def get_messages(self, conversation_id, **kwargs):
                raise RuntimeError("history backend changed")

        provider = ChatGPTWebProvider(runtime=BrokenRuntime())
        with self.assertRaisesRegex(ProviderError, "history backend changed"):
            provider.read_messages("existing")

    def test_missing_response_text_is_rejected(self) -> None:
        runtime = FakeRuntime()
        runtime.response = SimpleNamespace(text=None)
        provider = ChatGPTWebProvider(runtime=runtime)
        with self.assertRaisesRegex(ProviderError, "did not contain text"):
            provider.send(ProviderRequest(prompt="task"))

    def test_explicit_legacy_client_injection_retains_historical_test_seam(self) -> None:
        client = FakeLegacyClient()
        provider = ChatGPTWebProvider(
            client=client,
            model="gpt-explicit",
            reasoning_effort="extended",
        )

        provider.send(
            ProviderRequest(
                prompt="continue",
                system="legacy-system",
                conversation=ProviderConversation(conversation_id="existing"),
            )
        )

        call = client.calls[0]
        self.assertEqual(call[:3], ("continue", "existing", "continue"))
        self.assertFalse(call[3]["preserve_model"])
        self.assertEqual(call[3]["model"], "gpt-explicit")
        self.assertEqual(call[3]["system"], "legacy-system")
        self.assertEqual(call[3]["reasoning_effort"], "extended")


if __name__ == "__main__":
    unittest.main()
