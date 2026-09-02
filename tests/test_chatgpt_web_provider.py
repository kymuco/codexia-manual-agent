from __future__ import annotations

import unittest
from types import SimpleNamespace

from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import ProviderConversation, ProviderRequest
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider


class FakeMetrics:
    def to_dict(self):
        return {"total": 1.25}


class FakeClient:
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

    def send(self, prompt, **kwargs):
        self.calls.append(("send", prompt, kwargs))
        return self.response

    def send_to_conversation(self, conversation_id, prompt, **kwargs):
        self.calls.append(("continue", conversation_id, prompt, kwargs))
        return self.response


class ChatGPTWebProviderTests(unittest.TestCase):
    def test_new_conversation_uses_stable_send(self) -> None:
        client = FakeClient()
        provider = ChatGPTWebProvider(
            client=client,
            model="thinking",
            reasoning_effort="high",
        )
        response = provider.send(ProviderRequest(prompt="task", system="system"))

        call = client.calls[0]
        self.assertEqual(call[0], "send")
        self.assertEqual(call[1], "task")
        self.assertEqual(call[2]["system"], "system")
        self.assertFalse(call[2]["web_search"])
        self.assertFalse(call[2]["temporary"])
        self.assertEqual(response.conversation.conversation_id, "c1")
        self.assertEqual(response.model, "gpt-test")
        self.assertEqual(response.reasoning_effort, "extended")
        self.assertEqual(response.metrics["total"], 1.25)

    def test_existing_conversation_uses_send_to_conversation(self) -> None:
        client = FakeClient()
        provider = ChatGPTWebProvider(client=client)
        provider.send(
            ProviderRequest(
                prompt="continue",
                conversation=ProviderConversation(conversation_id="existing"),
            )
        )
        call = client.calls[0]
        self.assertEqual(call[:3], ("continue", "existing", "continue"))
        self.assertTrue(call[3]["preserve_model"])

    def test_explicit_model_disables_preserve_model(self) -> None:
        client = FakeClient()
        provider = ChatGPTWebProvider(client=client, model="gpt-explicit")
        provider.send(
            ProviderRequest(
                prompt="continue",
                conversation=ProviderConversation(conversation_id="existing"),
            )
        )
        self.assertFalse(client.calls[0][3]["preserve_model"])
        self.assertEqual(client.calls[0][3]["model"], "gpt-explicit")

    def test_transport_exception_is_wrapped(self) -> None:
        class BrokenClient(FakeClient):
            def send(self, prompt, **kwargs):
                raise RuntimeError("backend changed")

        provider = ChatGPTWebProvider(client=BrokenClient())
        with self.assertRaisesRegex(ProviderError, "backend changed"):
            provider.send(ProviderRequest(prompt="task"))

    def test_missing_response_text_is_rejected(self) -> None:
        client = FakeClient()
        client.response = SimpleNamespace(text=None)
        provider = ChatGPTWebProvider(client=client)
        with self.assertRaisesRegex(ProviderError, "did not contain text"):
            provider.send(ProviderRequest(prompt="task"))


if __name__ == "__main__":
    unittest.main()
