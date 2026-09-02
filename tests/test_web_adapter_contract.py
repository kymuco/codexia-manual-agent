from __future__ import annotations

import importlib.metadata
import importlib.util
import inspect
import unittest


_EXPECTED_ADAPTER_VERSION = "0.1.5"
_REQUIRED_SEND_PARAMETERS = {
    "prompt",
    "model",
    "system",
    "reasoning_effort",
    "conversation",
}


@unittest.skipUnless(
    importlib.util.find_spec("chatgpt_web_adapter") is not None,
    "chatgpt-web-adapter optional dependency is not installed",
)
class WebAdapterContractTests(unittest.TestCase):
    def test_pinned_adapter_version_is_installed(self) -> None:
        self.assertEqual(
            importlib.metadata.version("chatgpt-web-adapter"),
            _EXPECTED_ADAPTER_VERSION,
        )

    def test_stable_send_surface_is_available(self) -> None:
        from chatgpt_web_adapter import ChatGPTWebClient

        send = ChatGPTWebClient.send
        self.assertTrue(callable(send))

        parameters = inspect.signature(send).parameters
        if _REQUIRED_SEND_PARAMETERS.issubset(parameters):
            return

        # chatgpt-web-adapter 0.1.5 wraps send() for metrics collection.
        # The public wrapper intentionally exposes (self, *args, **kwargs),
        # while provider unit tests verify the exact named arguments Codexia
        # sends through that wrapper.
        supports_keyword_forwarding = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        self.assertTrue(
            supports_keyword_forwarding,
            "ChatGPTWebClient.send must expose the documented named parameters "
            "or a public **kwargs forwarding wrapper",
        )

    def test_stable_continue_surface_is_available(self) -> None:
        from chatgpt_web_adapter import ChatGPTWebClient

        self.assertTrue(hasattr(ChatGPTWebClient, "send_to_conversation"))
        parameters = inspect.signature(
            ChatGPTWebClient.send_to_conversation
        ).parameters
        self.assertIn("url_or_id", parameters)
        self.assertIn("prompt", parameters)
        self.assertIn("preserve_model", parameters)
        self.assertIn("system", parameters)
        self.assertIn("reasoning_effort", parameters)


if __name__ == "__main__":
    unittest.main()
