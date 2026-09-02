from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.adapters.filesystem_workspace import FilesystemWorkspace
from codexia_manual_agent.agent.loop import ReadOnlyAgentLoop
from codexia_manual_agent.application.inspect_workspace import InspectWorkspaceService
from codexia_manual_agent.domain.models import (
    AgentBudgets,
    AgentRunStatus,
    ProviderResponse,
    SessionManifest,
)
from codexia_manual_agent.session_events import (
    EventKind,
    RecoveryDisposition,
    SessionEventIntegrityError,
    SqliteAgentEventRecorder,
    SqliteSessionEventStore,
)
from codexia_manual_agent.session_events.models import MAX_EVENT_TEXT_CHARS


class _NeverCalledProvider:
    provider_id = "request-budget-test"

    def __init__(self) -> None:
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ProviderResponse(text='{"type":"final","text":"must not run"}')


class _OneToolProvider:
    provider_id = "request-budget-test"

    def __init__(self) -> None:
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if len(self.requests) > 1:
            raise AssertionError("second provider request must be rejected by preflight")
        return ProviderResponse(
            text=(
                '{"type":"tool_request","request_id":"search-large",'
                '"tool":"search_text","arguments":{"query":"needle",'
                '"path":".","max_matches":1}}'
            )
        )


class ProviderRequestBudgetTests(unittest.TestCase):
    def _persistent_loop(self, root: Path, provider, *, budgets: AgentBudgets | None = None):
        workspace = root / "workspace"
        workspace.mkdir()
        manifest = SessionManifest.create(
            workspace=workspace,
            prompt_version="v0.3",
            provider=provider.provider_id,
        )
        store = SqliteSessionEventStore(root / "events.sqlite3")
        recorder = SqliteAgentEventRecorder.start_from_manifest(
            store,
            manifest,
            provider_id=provider.provider_id,
        )
        loop = ReadOnlyAgentLoop(
            provider=provider,
            inspector=InspectWorkspaceService(FilesystemWorkspace(workspace)),
            budgets=budgets,
            event_recorder=recorder,
        )
        return workspace, manifest, store, loop

    def test_near_limit_initial_task_is_rejected_before_run_started(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            provider = _NeverCalledProvider()
            _, manifest, store, loop = self._persistent_loop(root, provider)
            task = "x" * MAX_EVENT_TEXT_CHARS

            result = loop.run(task)

            self.assertIs(result.status, AgentRunStatus.BUDGET_EXHAUSTED)
            self.assertEqual(result.turns, 0)
            self.assertEqual(result.tool_calls, 0)
            self.assertEqual(provider.requests, [])
            self.assertIn("Persistent model request exceeds event budget", result.error or "")
            events = store.load_events(manifest.session_id)
            self.assertEqual([event.kind for event in events], [EventKind.SESSION_STARTED])
            recovery = store.recover(manifest.session_id)
            self.assertIs(recovery.disposition, RecoveryDisposition.RESUMABLE)
            self.assertEqual(recovery.turns, 0)
            self.assertEqual(recovery.tool_calls, 0)

    def test_malformed_initial_request_stays_fail_closed_before_run_started(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            provider = _NeverCalledProvider()
            _, manifest, store, loop = self._persistent_loop(root, provider)

            with self.assertRaises(SessionEventIntegrityError):
                loop.run("bad\x00task")

            self.assertEqual(provider.requests, [])
            events = store.load_events(manifest.session_id)
            self.assertEqual([event.kind for event in events], [EventKind.SESSION_STARTED])

    def test_oversized_later_request_closes_run_before_second_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            provider = _OneToolProvider()
            budgets = AgentBudgets(max_observation_chars=MAX_EVENT_TEXT_CHARS * 2)
            workspace, manifest, store, loop = self._persistent_loop(
                root,
                provider,
                budgets=budgets,
            )
            line = "needle" + ("x" * (MAX_EVENT_TEXT_CHARS - len("needle")))
            (workspace / "large.txt").write_text(line, encoding="utf-8")

            result = loop.run("Search the large file")

            self.assertIs(result.status, AgentRunStatus.BUDGET_EXHAUSTED)
            self.assertEqual(result.turns, 1)
            self.assertEqual(result.tool_calls, 1)
            self.assertEqual(len(provider.requests), 1)
            self.assertIn("Persistent model request exceeds event budget", result.error or "")
            events = store.load_events(manifest.session_id)
            self.assertEqual(
                [event.kind for event in events],
                [
                    EventKind.SESSION_STARTED,
                    EventKind.RUN_STARTED,
                    EventKind.MODEL_REQUEST_STARTED,
                    EventKind.MODEL_RESPONSE_RECORDED,
                    EventKind.TOOL_OBSERVATION_RECORDED,
                    EventKind.RUN_COMPLETED,
                ],
            )
            recovery = store.recover(manifest.session_id)
            self.assertIs(recovery.disposition, RecoveryDisposition.RESUMABLE)
            self.assertEqual(recovery.open_provider_request_ids, ())
            self.assertEqual(recovery.turns, 1)
            self.assertEqual(recovery.tool_calls, 1)


if __name__ == "__main__":
    unittest.main()
