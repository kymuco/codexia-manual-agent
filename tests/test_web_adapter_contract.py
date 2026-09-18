from __future__ import annotations

import importlib.metadata
import importlib.util
import inspect
import json
import unittest


_EXPECTED_ADAPTER_VERSION = "0.3.0"
_EXPECTED_ADAPTER_COMMIT = "5418acdd11266195787b3059685a5a17c6cbf838"


@unittest.skipUnless(
    importlib.util.find_spec("chatgpt_web_adapter") is not None,
    "chatgpt-web-adapter optional dependency is not installed",
)
class WebAdapterContractTests(unittest.TestCase):
    def test_exact_source_revision_is_installed(self) -> None:
        distribution = importlib.metadata.distribution("chatgpt-web-adapter")
        self.assertEqual(distribution.version, _EXPECTED_ADAPTER_VERSION)

        direct_url_text = distribution.read_text("direct_url.json")
        self.assertIsNotNone(
            direct_url_text,
            "CWA must be installed from the exact VCS source dependency",
        )
        direct_url = json.loads(direct_url_text)
        vcs_info = direct_url.get("vcs_info") or {}
        self.assertEqual(vcs_info.get("vcs"), "git")
        self.assertEqual(vcs_info.get("commit_id"), _EXPECTED_ADAPTER_COMMIT)
        requested = vcs_info.get("requested_revision")
        if requested is not None:
            self.assertEqual(requested, _EXPECTED_ADAPTER_COMMIT)

    def test_product_runtime_primary_surface_is_available(self) -> None:
        from chatgpt_web_adapter import ChatGPTProductRuntime, assemble_product_runtime

        self.assertTrue(callable(assemble_product_runtime))
        assembly = inspect.signature(assemble_product_runtime).parameters
        self.assertIn("transport", assembly)
        self.assertIn("auth_file", assembly)
        self.assertIn("client_timeout", assembly)

        observed_send = inspect.signature(
            ChatGPTProductRuntime.send_text_observed
        ).parameters
        self.assertIn("text", observed_send)
        self.assertIn("conversation", observed_send)
        self.assertIn("timeout", observed_send)
        self.assertIn("model_profile", observed_send)

        self.assertTrue(callable(ChatGPTProductRuntime.get_messages))
        self.assertTrue(callable(ChatGPTProductRuntime.attach_conversation))
        self.assertTrue(callable(ChatGPTProductRuntime.health))

    def test_canonical_message_identity_surface_is_available(self) -> None:
        from chatgpt_web_adapter import ChatMessage

        message = ChatMessage(
            node_id="node",
            message_id="message",
            role="assistant",
            text="ok",
        )
        self.assertEqual(message.node_id, "node")
        self.assertEqual(message.message_id, "message")
        self.assertEqual(message.role, "assistant")
        self.assertEqual(message.text, "ok")


if __name__ == "__main__":
    unittest.main()
