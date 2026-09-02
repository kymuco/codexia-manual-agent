from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from codexia_manual_agent.adapters.filesystem_workspace import FilesystemWorkspace
from codexia_manual_agent.agent.loop import ReadOnlyAgentLoop
from codexia_manual_agent.application.inspect_workspace import InspectWorkspaceService
from codexia_manual_agent.domain.models import (
    AgentRunStatus,
    ProviderConversation,
    ProviderResponse,
    SessionManifest,
)
from codexia_manual_agent.session_events import (
    EventKind,
    RecoveryDisposition,
    SqliteAgentEventRecorder,
    SqliteSessionEventStore,
)
from codexia_manual_agent.session_events.models import MAX_EVENT_TEXT_CHARS


class _OversizedProvider:
    provider_id = "oversized-test"

    def __init__(self, response: ProviderResponse) -> None:
        self.response = response
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return self.response


class OversizedProviderResponseTests(unittest.TestCase):
    def test_known_oversized_provider_response_closes_request_with_digest_only_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            manifest = SessionManifest.create(
                workspace=workspace,
                prompt_version="v0.3",
                provider="oversized-test",
            )
            store = SqliteSessionEventStore(root / "events.sqlite3")
            recorder = SqliteAgentEventRecorder.start_from_manifest(
                store,
                manifest,
                provider_id="oversized-test",
            )
            inspector = InspectWorkspaceService(FilesystemWorkspace(workspace))
            response_text = "x" * (MAX_EVENT_TEXT_CHARS + 1)
            conversation = ProviderConversation(
                conversation_id="conversation-large",
                message_id="message-large",
                finish_reason="stop",
            )
            provider = _OversizedProvider(
                ProviderResponse(
                    text=response_text,
                    conversation=conversation,
                    model="large-test-model",
                    reasoning_effort="high",
                )
            )

            result = ReadOnlyAgentLoop(
                provider=provider,
                inspector=inspector,
                event_recorder=recorder,
            ).run("Return a deliberately oversized response")

            self.assertIs(result.status, AgentRunStatus.BUDGET_EXHAUSTED)
            self.assertEqual(result.turns, 1)
            self.assertEqual(result.tool_calls, 0)
            self.assertEqual(result.model_chars, len(response_text))
            self.assertEqual(result.conversation, conversation)

            events = store.load_events(manifest.session_id)
            self.assertEqual(
                [event.kind for event in events],
                [
                    EventKind.SESSION_STARTED,
                    EventKind.RUN_STARTED,
                    EventKind.MODEL_REQUEST_STARTED,
                    EventKind.MODEL_RESPONSE_RECORDED,
                    EventKind.RUN_COMPLETED,
                ],
            )
            response_event = events[3]
            self.assertNotIn("response_text", response_event.payload)
            self.assertEqual(response_event.payload["response_storage"], "digest_only")
            self.assertEqual(response_event.payload["response_chars"], len(response_text))
            self.assertEqual(
                response_event.payload["response_bytes"],
                len(response_text.encode("utf-8")),
            )
            self.assertEqual(
                response_event.payload["response_digest"],
                sha256(response_text.encode("utf-8")).hexdigest(),
            )

            recovery = store.recover(manifest.session_id)
            self.assertIs(recovery.disposition, RecoveryDisposition.RESUMABLE)
            self.assertEqual(recovery.open_provider_request_ids, ())
            self.assertEqual(recovery.turns, 1)
            self.assertEqual(recovery.tool_calls, 0)
            self.assertEqual(recovery.model_chars, len(response_text))
            self.assertEqual(recovery.latest_conversation, conversation)


if __name__ == "__main__":
    unittest.main()
