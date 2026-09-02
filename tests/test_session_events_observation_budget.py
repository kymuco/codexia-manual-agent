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
    SqliteAgentEventRecorder,
    SqliteSessionEventStore,
)


class _Provider:
    provider_id = "fake"

    def __init__(self) -> None:
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ProviderResponse(
            text=(
                '{"type":"tool_request","request_id":"read-large",'
                '"tool":"read_file","arguments":{"path":"README.md"}}'
            )
        )


class ObservationBudgetDurabilityTests(unittest.TestCase):
    def test_context_budget_exhaustion_keeps_exact_tool_event_and_recovery_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            exact_text = "durable-observation-" * 40
            (workspace / "README.md").write_text(exact_text, encoding="utf-8")

            manifest = SessionManifest.create(
                workspace=workspace,
                prompt_version="v0.3",
                provider="fake",
            )
            store = SqliteSessionEventStore(root / "events.sqlite3")
            recorder = SqliteAgentEventRecorder.start_from_manifest(
                store,
                manifest,
                provider_id="fake",
            )
            provider = _Provider()

            result = ReadOnlyAgentLoop(
                provider=provider,
                inspector=InspectWorkspaceService(FilesystemWorkspace(workspace)),
                budgets=AgentBudgets(max_observation_chars=64),
                event_recorder=recorder,
            ).run("Read the large README")

            self.assertIs(result.status, AgentRunStatus.BUDGET_EXHAUSTED)
            self.assertEqual(result.turns, 1)
            self.assertEqual(result.tool_calls, 1)
            self.assertIn("Tool observation exceeds limit", result.error or "")

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
            durable_observation = str(events[4].payload["observation_json"])
            self.assertGreater(len(durable_observation), 64)
            self.assertIn(exact_text, durable_observation)

            recovery = store.recover(manifest.session_id)
            self.assertIs(recovery.disposition, RecoveryDisposition.RESUMABLE)
            self.assertEqual(recovery.turns, 1)
            self.assertEqual(recovery.tool_calls, 1)
            self.assertEqual(recovery.tool_observation_json, (durable_observation,))


if __name__ == "__main__":
    unittest.main()
