from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codexia_manual_agent.adapters.filesystem_workspace import FilesystemWorkspace
from codexia_manual_agent.agent.loop import ReadOnlyAgentLoop
from codexia_manual_agent.application.inspect_workspace import InspectWorkspaceService
from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import (
    AgentRunStatus,
    ProviderConversation,
    ProviderResponse,
    SessionManifest,
)
from codexia_manual_agent.session_events import (
    EventKind,
    RecoveryDisposition,
    SessionEventStateError,
    SqliteAgentEventRecorder,
    SqliteSessionEventStore,
)


class _FakeProvider:
    provider_id = "fake"

    def __init__(self, responses: list[ProviderResponse | Exception]) -> None:
        self.responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PersistentAgentIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("# Demo\nexact output\n", encoding="utf-8")
        self.manifest = SessionManifest.create(
            workspace=self.workspace,
            prompt_version="v0.3",
            provider="fake",
        )
        self.store = SqliteSessionEventStore(self.root / "state" / "events.sqlite3")
        self.recorder = SqliteAgentEventRecorder.start_from_manifest(
            self.store,
            self.manifest,
            provider_id="fake",
        )
        self.inspector = InspectWorkspaceService(FilesystemWorkspace(self.workspace))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_read_only_chronology_recovers_and_resumes_with_recovered_conversation(self) -> None:
        provider = _FakeProvider(
            [
                ProviderResponse(
                    text=(
                        '{"type":"tool_request","request_id":"read-1",'
                        '"tool":"read_file","arguments":{"path":"README.md"}}'
                    ),
                    conversation=ProviderConversation(
                        conversation_id="conversation-1",
                        message_id="message-1",
                    ),
                    model="test-model",
                    reasoning_effort="high",
                ),
                ProviderResponse(
                    text='{"type":"final","text":"README inspected."}',
                    conversation=ProviderConversation(
                        conversation_id="conversation-1",
                        message_id="message-2",
                        parent_message_id="message-1",
                        finish_reason="stop",
                    ),
                    model="test-model",
                    reasoning_effort="high",
                ),
            ]
        )
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
            event_recorder=self.recorder,
        ).run("Inspect README")
        self.assertEqual(result.status, AgentRunStatus.COMPLETED)

        events = self.store.load_events(self.manifest.session_id)
        self.assertEqual(
            [event.kind for event in events],
            [
                EventKind.SESSION_STARTED,
                EventKind.RUN_STARTED,
                EventKind.MODEL_REQUEST_STARTED,
                EventKind.MODEL_RESPONSE_RECORDED,
                EventKind.TOOL_OBSERVATION_RECORDED,
                EventKind.MODEL_REQUEST_STARTED,
                EventKind.MODEL_RESPONSE_RECORDED,
                EventKind.RUN_COMPLETED,
            ],
        )
        observation = events[4].payload["observation_json"]
        self.assertIn("# Demo", observation)
        self.assertIn("exact output", observation)

        resumed_recorder, recovered = SqliteAgentEventRecorder.resume_from_manifest(
            SqliteSessionEventStore(self.store.path),
            self.manifest,
            provider_id="fake",
        )
        self.assertIs(recovered.disposition, RecoveryDisposition.RESUMABLE)
        self.assertEqual(recovered.turns, 2)
        self.assertEqual(recovered.tool_calls, 1)
        self.assertIsNotNone(recovered.latest_conversation)
        self.assertEqual(recovered.latest_conversation.message_id, "message-2")

        continuation_provider = _FakeProvider(
            [
                ProviderResponse(
                    text='{"type":"final","text":"continued"}',
                    conversation=ProviderConversation(
                        conversation_id="conversation-1",
                        message_id="message-3",
                        parent_message_id="message-2",
                        finish_reason="stop",
                    ),
                )
            ]
        )
        continuation = ReadOnlyAgentLoop(
            provider=continuation_provider,
            inspector=self.inspector,
            event_recorder=resumed_recorder,
        ).run("Continue", conversation=recovered.latest_conversation)
        self.assertTrue(continuation.completed)
        self.assertIsNone(continuation_provider.requests[0].system)
        self.assertEqual(
            continuation_provider.requests[0].conversation.message_id,
            "message-2",
        )
        recovered_again = self.store.recover(self.manifest.session_id)
        self.assertEqual(recovered_again.turns, 3)
        self.assertEqual(recovered_again.tool_calls, 1)
        self.assertEqual(recovered_again.latest_conversation.message_id, "message-3")

    def test_response_without_conversation_does_not_erase_existing_identity(self) -> None:
        existing = ProviderConversation(
            conversation_id="existing",
            message_id="old-message",
        )
        provider = _FakeProvider(
            [ProviderResponse(text='{"type":"final","text":"continued"}')]
        )
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
            event_recorder=self.recorder,
        ).run("Continue", conversation=existing)
        self.assertTrue(result.completed)
        recovered = self.store.recover(self.manifest.session_id)
        self.assertEqual(recovered.latest_conversation, existing)

    def test_provider_failure_is_durable_unknown_outcome_and_cannot_auto_resume(self) -> None:
        provider = _FakeProvider([ProviderError("transport down")])
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
            event_recorder=self.recorder,
        ).run("Inspect")
        self.assertEqual(result.status, AgentRunStatus.PROVIDER_ERROR)

        events = self.store.load_events(self.manifest.session_id)
        self.assertEqual(
            [event.kind for event in events],
            [
                EventKind.SESSION_STARTED,
                EventKind.RUN_STARTED,
                EventKind.MODEL_REQUEST_STARTED,
                EventKind.RUN_INTERRUPTED,
            ],
        )
        request_id = events[2].payload["request_id"]
        self.assertEqual(events[3].payload["request_id"], request_id)

        recovered = self.store.recover(self.manifest.session_id)
        self.assertIs(
            recovered.disposition,
            RecoveryDisposition.UNKNOWN_PROVIDER_OUTCOME,
        )
        self.assertEqual(recovered.open_provider_request_ids, (request_id,))
        self.assertFalse(recovered.can_resume_provider)
        with self.assertRaises(SessionEventStateError):
            SqliteAgentEventRecorder.resume_from_manifest(
                self.store,
                self.manifest,
                provider_id="fake",
            )

    def test_resume_rejects_manifest_identity_or_provider_substitution(self) -> None:
        provider = _FakeProvider(
            [ProviderResponse(text='{"type":"final","text":"done"}')]
        )
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
            event_recorder=self.recorder,
        ).run("Inspect")
        self.assertTrue(result.completed)

        different_workspace = self.root / "other"
        different_workspace.mkdir()
        wrong_manifest = replace(self.manifest, workspace=str(different_workspace.resolve()))
        with self.assertRaises(SessionEventStateError):
            SqliteAgentEventRecorder.resume_from_manifest(
                self.store,
                wrong_manifest,
                provider_id="fake",
            )
        with self.assertRaises(SessionEventStateError):
            SqliteAgentEventRecorder.resume_from_manifest(
                self.store,
                self.manifest,
                provider_id="other-provider",
            )


if __name__ == "__main__":
    unittest.main()
