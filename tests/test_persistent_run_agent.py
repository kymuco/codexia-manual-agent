from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.adapters.filesystem_workspace import FilesystemWorkspace
from codexia_manual_agent.adapters.json_session_store import JsonSessionStore
from codexia_manual_agent.agent.loop import ReadOnlyAgentLoop
from codexia_manual_agent.application.inspect_workspace import InspectWorkspaceService
from codexia_manual_agent.application.persistent_run_agent import PersistentRunAgentService
from codexia_manual_agent.application.run_session import RunSessionService
from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderResponse,
)
from codexia_manual_agent.session_events import (
    RecoveryDisposition,
    SessionEventStateError,
    SqliteAgentEventRecorder,
    SqliteSessionEventStore,
)


class _Provider:
    provider_id = "persistent-provider"

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _OtherProvider(_Provider):
    provider_id = "other-provider"


class _FailingStartEventStore(SqliteSessionEventStore):
    def start_session(self, *, session_id, payload):
        raise RuntimeError("injected durable session-start failure")


class PersistentRunAgentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("# Demo\n", encoding="utf-8")
        self.manifest_store = JsonSessionStore(self.root / "sessions")
        self.event_store = SqliteSessionEventStore(self.root / "events.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_ledger_start_failure_never_exposes_manifest(self) -> None:
        provider = _Provider(
            [ProviderResponse(text='{"type":"final","text":"must not run"}')]
        )
        service = PersistentRunAgentService(
            provider=provider,
            manifest_store=self.manifest_store,
            event_store=_FailingStartEventStore(self.root / "failing-events.sqlite3"),
        )

        with self.assertRaisesRegex(RuntimeError, "session-start failure"):
            service.start_and_run(workspace=self.workspace, task="Do not expose")

        self.assertEqual(self.manifest_store.list(), ())
        self.assertEqual(provider.requests, [])

    def test_start_and_resume_use_ledger_conversation_and_cumulative_counters(self) -> None:
        first_provider = _Provider(
            [
                ProviderResponse(
                    text='{"type":"final","text":"first"}',
                    conversation=ProviderConversation(
                        conversation_id="conversation-1",
                        message_id="message-1",
                    ),
                )
            ]
        )
        first_service = PersistentRunAgentService(
            provider=first_provider,
            manifest_store=self.manifest_store,
            event_store=self.event_store,
        )
        first_result, first_manifest = first_service.start_and_run(
            workspace=self.workspace,
            task="First",
        )
        self.assertTrue(first_result.completed)
        self.assertEqual(first_manifest.turn_count, 1)

        second_provider = _Provider(
            [
                ProviderResponse(
                    text='{"type":"final","text":"second"}',
                    conversation=ProviderConversation(
                        conversation_id="conversation-1",
                        message_id="message-2",
                        parent_message_id="message-1",
                    ),
                )
            ]
        )
        second_service = PersistentRunAgentService(
            provider=second_provider,
            manifest_store=self.manifest_store,
            event_store=SqliteSessionEventStore(self.event_store.path),
        )
        second_result, second_manifest, recovery = second_service.resume_and_run(
            session_id=first_manifest.session_id,
            task="Second",
        )
        self.assertTrue(second_result.completed)
        self.assertIs(recovery.disposition, RecoveryDisposition.RESUMABLE)
        self.assertEqual(recovery.turns, 1)
        self.assertIsNotNone(second_provider.requests[0].conversation)
        self.assertEqual(second_provider.requests[0].conversation.message_id, "message-1")
        self.assertIsNone(second_provider.requests[0].system)
        self.assertEqual(second_manifest.turn_count, 2)
        self.assertEqual(second_manifest.conversation.message_id, "message-2")
        self.assertEqual(
            self.manifest_store.load(first_manifest.session_id),
            second_manifest,
        )

    def test_resume_repairs_manifest_that_lagged_after_durable_completed_run(self) -> None:
        manifest = RunSessionService(self.manifest_store).start(
            workspace=self.workspace,
            provider="persistent-provider",
        )
        recorder = SqliteAgentEventRecorder.start_from_manifest(
            self.event_store,
            manifest,
            provider_id="persistent-provider",
        )
        crash_window_provider = _Provider(
            [
                ProviderResponse(
                    text='{"type":"final","text":"durable-before-manifest"}',
                    conversation=ProviderConversation(
                        conversation_id="conversation-1",
                        message_id="message-1",
                    ),
                )
            ]
        )
        result = ReadOnlyAgentLoop(
            provider=crash_window_provider,
            inspector=InspectWorkspaceService(FilesystemWorkspace(self.workspace)),
            event_recorder=recorder,
        ).run("Durable run")
        self.assertTrue(result.completed)

        stale = self.manifest_store.load(manifest.session_id)
        self.assertEqual(stale.turn_count, 0)
        self.assertIsNone(stale.conversation)

        continuation_provider = _Provider(
            [
                ProviderResponse(
                    text='{"type":"final","text":"continued"}',
                    conversation=ProviderConversation(
                        conversation_id="conversation-1",
                        message_id="message-2",
                        parent_message_id="message-1",
                    ),
                )
            ]
        )
        service = PersistentRunAgentService(
            provider=continuation_provider,
            manifest_store=self.manifest_store,
            event_store=SqliteSessionEventStore(self.event_store.path),
        )
        _, updated, recovery = service.resume_and_run(
            session_id=manifest.session_id,
            task="Continue",
        )
        self.assertEqual(recovery.turns, 1)
        self.assertEqual(recovery.latest_conversation.message_id, "message-1")
        self.assertEqual(continuation_provider.requests[0].conversation.message_id, "message-1")
        self.assertEqual(updated.turn_count, 2)

    def test_unknown_provider_outcome_is_inspectable_but_resume_contacts_no_provider(self) -> None:
        manifest = RunSessionService(self.manifest_store).start(
            workspace=self.workspace,
            provider="persistent-provider",
        )
        recorder = SqliteAgentEventRecorder.start_from_manifest(
            self.event_store,
            manifest,
            provider_id="persistent-provider",
        )
        failed_provider = _Provider([ProviderError("unknown remote outcome")])
        result = ReadOnlyAgentLoop(
            provider=failed_provider,
            inspector=InspectWorkspaceService(FilesystemWorkspace(self.workspace)),
            event_recorder=recorder,
        ).run("Fail")
        self.assertEqual(result.status.value, "provider_error")

        unused_provider = _Provider(
            [ProviderResponse(text='{"type":"final","text":"must not run"}')]
        )
        service = PersistentRunAgentService(
            provider=unused_provider,
            manifest_store=self.manifest_store,
            event_store=SqliteSessionEventStore(self.event_store.path),
        )
        recovery = service.recover(manifest.session_id)
        self.assertIs(
            recovery.disposition,
            RecoveryDisposition.UNKNOWN_PROVIDER_OUTCOME,
        )
        self.assertEqual(unused_provider.requests, [])
        with self.assertRaises(SessionEventStateError):
            service.resume_and_run(session_id=manifest.session_id, task="Retry")
        self.assertEqual(unused_provider.requests, [])

    def test_different_provider_can_inspect_but_cannot_resume_persisted_session(self) -> None:
        first_provider = _Provider(
            [
                ProviderResponse(
                    text='{"type":"final","text":"done"}',
                    conversation=ProviderConversation(
                        conversation_id="conversation-1",
                        message_id="message-1",
                    ),
                )
            ]
        )
        first_service = PersistentRunAgentService(
            provider=first_provider,
            manifest_store=self.manifest_store,
            event_store=self.event_store,
        )
        _, manifest = first_service.start_and_run(
            workspace=self.workspace,
            task="Persist",
        )

        other_provider = _OtherProvider(
            [ProviderResponse(text='{"type":"final","text":"must not run"}')]
        )
        other_service = PersistentRunAgentService(
            provider=other_provider,
            manifest_store=self.manifest_store,
            event_store=SqliteSessionEventStore(self.event_store.path),
        )
        recovery = other_service.recover(manifest.session_id)
        self.assertIs(recovery.disposition, RecoveryDisposition.RESUMABLE)
        self.assertEqual(other_provider.requests, [])
        with self.assertRaisesRegex(SessionEventStateError, "provider differs"):
            other_service.resume_and_run(session_id=manifest.session_id, task="Wrong provider")
        self.assertEqual(other_provider.requests, [])


if __name__ == "__main__":
    unittest.main()
