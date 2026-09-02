from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.adapters.json_session_store import JsonSessionStore
from codexia_manual_agent.application.run_agent import RunAgentService
from codexia_manual_agent.application.run_session import RunSessionService
from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderResponse,
)


class FinalProvider:
    provider_id = "fake-provider"

    def send(self, request):
        return ProviderResponse(
            text='{"type":"final","text":"done"}',
            conversation=ProviderConversation(
                conversation_id="conversation-1",
                message_id="message-1",
            ),
            model="observed-model",
            reasoning_effort="extended",
        )


class InterruptedProvider:
    provider_id = "fake-provider"

    def __init__(self) -> None:
        self.turn = 0

    def send(self, request):
        self.turn += 1
        if self.turn == 1:
            return ProviderResponse(
                text=(
                    '{"type":"tool_request","request_id":"read-1",'
                    '"tool":"read_file","arguments":{"path":"README.md"}}'
                ),
                conversation=ProviderConversation(
                    conversation_id="recoverable-conversation",
                    message_id="message-1",
                ),
                model="observed-model",
            )
        raise ProviderError("transport interrupted")


class RunAgentServiceTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_bytes(b"# Demo\n")
        return workspace

    def test_run_persists_transport_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self._workspace(root)
            store = JsonSessionStore(root / "sessions")
            manifest = RunSessionService(store).start(workspace=workspace)

            result, updated = RunAgentService(
                provider=FinalProvider(),
                store=store,
                model="model-x",
                reasoning_effort="high",
            ).run(manifest=manifest, task="Answer")

            self.assertTrue(result.completed)
            self.assertEqual(updated.provider, "fake-provider")
            self.assertEqual(updated.conversation.conversation_id, "conversation-1")
            self.assertEqual(updated.turn_count, 1)
            self.assertEqual(updated.tool_call_count, 0)
            self.assertEqual(updated.last_status, "completed")
            self.assertEqual(updated.model, "observed-model")
            self.assertEqual(updated.reasoning_effort, "extended")
            self.assertEqual(store.load(updated.session_id), updated)

    def test_provider_interruption_persists_last_conversation_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = self._workspace(root)
            store = JsonSessionStore(root / "sessions")
            manifest = RunSessionService(store).start(workspace=workspace)

            result, updated = RunAgentService(
                provider=InterruptedProvider(),
                store=store,
            ).run(manifest=manifest, task="Inspect README")

            self.assertEqual(result.status.value, "provider_error")
            self.assertEqual(result.turns, 1)
            self.assertEqual(result.tool_calls, 1)
            self.assertIn("interrupted", result.error or "")
            self.assertEqual(
                updated.conversation.conversation_id,
                "recoverable-conversation",
            )
            self.assertEqual(updated.last_status, "provider_error")
            self.assertEqual(store.load(updated.session_id), updated)


if __name__ == "__main__":
    unittest.main()
