from __future__ import annotations

import json
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


class _SearchProvider:
    provider_id = "oversized-tool-test"

    def __init__(self) -> None:
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ProviderResponse(
            text=(
                '{"type":"tool_request","request_id":"search-large",'
                '"tool":"search_text","arguments":{"query":"needle",'
                '"path":".","max_matches":1}}'
            )
        )


class OversizedToolObservationTests(unittest.TestCase):
    def test_known_oversized_tool_observation_is_durable_digest_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            line = "needle" + ("x" * (MAX_EVENT_TEXT_CHARS - len("needle")))
            (workspace / "large.txt").write_text(line, encoding="utf-8")

            manifest = SessionManifest.create(
                workspace=workspace,
                prompt_version="v0.3",
                provider="oversized-tool-test",
            )
            store = SqliteSessionEventStore(root / "events.sqlite3")
            recorder = SqliteAgentEventRecorder.start_from_manifest(
                store,
                manifest,
                provider_id="oversized-tool-test",
            )
            provider = _SearchProvider()

            result = ReadOnlyAgentLoop(
                provider=provider,
                inspector=InspectWorkspaceService(FilesystemWorkspace(workspace)),
                budgets=AgentBudgets(max_observation_chars=64),
                event_recorder=recorder,
            ).run("Search the large file")

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
            marker = json.loads(durable_observation)
            self.assertEqual(marker["observation_storage"], "digest_only")
            self.assertGreater(marker["observation_chars"], MAX_EVENT_TEXT_CHARS)
            self.assertGreater(marker["observation_bytes"], MAX_EVENT_TEXT_CHARS)
            self.assertEqual(len(marker["observation_digest"]), 64)
            self.assertLess(len(durable_observation), 512)

            recovery = store.recover(manifest.session_id)
            self.assertIs(recovery.disposition, RecoveryDisposition.RESUMABLE)
            self.assertEqual(recovery.open_provider_request_ids, ())
            self.assertEqual(recovery.turns, 1)
            self.assertEqual(recovery.tool_calls, 1)
            self.assertEqual(recovery.tool_observation_json, (durable_observation,))

    def test_malformed_observation_does_not_use_digest_only_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            manifest = SessionManifest.create(
                workspace=workspace,
                prompt_version="v0.3",
                provider="oversized-tool-test",
            )
            store = SqliteSessionEventStore(root / "events.sqlite3")
            recorder = SqliteAgentEventRecorder.start_from_manifest(
                store,
                manifest,
                provider_id="oversized-tool-test",
            )
            run_id = recorder.start_run(task="Malformed observation", budgets=AgentBudgets())

            with self.assertRaises(SessionEventIntegrityError):
                recorder.tool_observation_recorded(
                    run_id=run_id,
                    request_id="tool-request",
                    tool="search_text",
                    observation_json="bad\x00observation",
                )

            events = store.load_events(manifest.session_id)
            self.assertEqual(
                [event.kind for event in events],
                [EventKind.SESSION_STARTED, EventKind.RUN_STARTED],
            )


if __name__ == "__main__":
    unittest.main()
