from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.adapters.filesystem_workspace import FilesystemWorkspace
from codexia_manual_agent.agent.loop import ReadOnlyAgentLoop
from codexia_manual_agent.application.inspect_workspace import InspectWorkspaceService
from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import (
    AgentBudgets,
    AgentRunStatus,
    ProviderConversation,
    ProviderResponse,
)


class FakeProvider:
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


class AgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "README.md").write_bytes(b"# Demo\nvalue\n")
        self.inspector = InspectWorkspaceService(FilesystemWorkspace(self.root))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_tool_observation_then_final(self) -> None:
        conversation = ProviderConversation(conversation_id="c1", message_id="m1")
        provider = FakeProvider(
            [
                ProviderResponse(
                    text=(
                        '{"type":"tool_request","request_id":"read-1",'
                        '"tool":"read_file","arguments":{"path":"README.md"}}'
                    ),
                    conversation=conversation,
                ),
                ProviderResponse(
                    text='{"type":"final","text":"README contains Demo."}',
                    conversation=ProviderConversation(
                        conversation_id="c1",
                        message_id="m2",
                    ),
                ),
            ]
        )
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
        ).run("Inspect the README")

        self.assertEqual(result.status, AgentRunStatus.COMPLETED)
        self.assertEqual(result.final_text, "README contains Demo.")
        self.assertEqual(result.turns, 2)
        self.assertEqual(result.tool_calls, 1)
        self.assertIsNotNone(provider.requests[0].system)
        self.assertIsNone(provider.requests[1].system)
        self.assertIn("TOOL_OBSERVATION", provider.requests[1].prompt)
        self.assertIn("# Demo", provider.requests[1].prompt)

    def test_existing_conversation_does_not_resend_system(self) -> None:
        provider = FakeProvider(
            [ProviderResponse(text='{"type":"final","text":"continued"}')]
        )
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
        ).run(
            "Continue",
            conversation=ProviderConversation(conversation_id="existing"),
        )
        self.assertTrue(result.completed)
        self.assertIsNone(provider.requests[0].system)

    def test_duplicate_request_id_is_protocol_error(self) -> None:
        request = (
            '{"type":"tool_request","request_id":"same",'
            '"tool":"git_status","arguments":{}}'
        )
        provider = FakeProvider(
            [ProviderResponse(text=request), ProviderResponse(text=request)]
        )
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
        ).run("Check git")
        self.assertEqual(result.status, AgentRunStatus.PROTOCOL_ERROR)
        self.assertIn("Duplicate", result.error or "")

    def test_invalid_tool_arguments_return_failed_observation(self) -> None:
        provider = FakeProvider(
            [
                ProviderResponse(
                    text=(
                        '{"type":"tool_request","request_id":"read-1",'
                        '"tool":"read_file","arguments":{"path":"README.md",'
                        '"max_bytes":999999}}'
                    )
                ),
                ProviderResponse(text='{"type":"final","text":"limit rejected"}'),
            ]
        )
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
        ).run("Read too much")
        self.assertTrue(result.completed)
        self.assertIn('"success":false', provider.requests[1].prompt)
        self.assertIn("exceeds 65536", provider.requests[1].prompt)

    def test_sensitive_model_path_returns_failed_observation(self) -> None:
        (self.root / "auth_data.json").write_text(
            '{"accessToken":"do-not-expose"}',
            encoding="utf-8",
        )
        provider = FakeProvider(
            [
                ProviderResponse(
                    text=(
                        '{"type":"tool_request","request_id":"secret-1",'
                        '"tool":"read_file","arguments":{"path":"auth_data.json"}}'
                    )
                ),
                ProviderResponse(
                    text='{"type":"final","text":"Sensitive file was unavailable."}'
                ),
            ]
        )
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
        ).run("Read auth data")
        self.assertTrue(result.completed)
        self.assertIn('"success":false', provider.requests[1].prompt)
        self.assertIn("Sensitive path is not available", provider.requests[1].prompt)
        self.assertNotIn("do-not-expose", provider.requests[1].prompt)

    def test_turn_budget_stops_loop(self) -> None:
        provider = FakeProvider(
            [
                ProviderResponse(
                    text=(
                        '{"type":"tool_request","request_id":"one",'
                        '"tool":"read_file","arguments":{"path":"README.md"}}'
                    )
                )
            ]
        )
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
            budgets=AgentBudgets(max_turns=1),
        ).run("Inspect")
        self.assertEqual(result.status, AgentRunStatus.BUDGET_EXHAUSTED)
        self.assertEqual(result.turns, 1)

    def test_provider_failure_is_calibrated(self) -> None:
        provider = FakeProvider([ProviderError("transport down")])
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
        ).run("Inspect")
        self.assertEqual(result.status, AgentRunStatus.PROVIDER_ERROR)
        self.assertIn("transport down", result.error or "")

    def test_model_character_budget_stops_run(self) -> None:
        provider = FakeProvider(
            [ProviderResponse(text='{"type":"final","text":"long enough"}')]
        )
        result = ReadOnlyAgentLoop(
            provider=provider,
            inspector=self.inspector,
            budgets=AgentBudgets(max_total_model_chars=10),
        ).run("Inspect")
        self.assertEqual(result.status, AgentRunStatus.BUDGET_EXHAUSTED)


if __name__ == "__main__":
    unittest.main()
