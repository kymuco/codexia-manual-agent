from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from codexia_manual_agent import __version__
from codexia_manual_agent.cli import main
from codexia_manual_agent.domain.models import ProviderConversation, ProviderResponse


class FakeCliProvider:
    provider_id = "chatgpt-web"

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def send(self, request):
        return ProviderResponse(
            text='{"type":"final","text":"done"}',
            conversation=ProviderConversation(
                conversation_id="conversation-cli",
                message_id="message-cli",
            ),
        )


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "hello.txt").write_bytes(b"hello\n")
        self.state = self.root / "state"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _invoke(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_run_is_truthful_about_provider(self) -> None:
        code, stdout, stderr = self._invoke(
            [
                "run",
                "--workspace",
                str(self.root),
                "--state-dir",
                str(self.state),
                "--json",
            ]
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["provider"], "unconfigured")
        self.assertIn("No model provider", payload["message"])

    def test_sessions_lists_created_manifest(self) -> None:
        self._invoke(
            [
                "run",
                "--workspace",
                str(self.root),
                "--state-dir",
                str(self.state),
                "--json",
            ]
        )
        code, stdout, stderr = self._invoke(
            ["sessions", "--state-dir", str(self.state), "--json"]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(json.loads(stdout)), 1)

    def test_inspect_read_returns_observation(self) -> None:
        code, stdout, stderr = self._invoke(
            [
                "inspect",
                "--workspace",
                str(self.root),
                "--json",
                "read",
                "hello.txt",
            ]
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["text"], "hello\n")

    def test_live_run_persists_provider_conversation(self) -> None:
        with patch(
            "codexia_manual_agent.cli.ChatGPTWebProvider",
            FakeCliProvider,
        ):
            code, stdout, stderr = self._invoke(
                [
                    "run",
                    "Inspect the workspace",
                    "--workspace",
                    str(self.root),
                    "--state-dir",
                    str(self.state),
                    "--json",
                ]
            )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["result"]["status"], "completed")
        self.assertEqual(
            payload["session"]["conversation"]["conversation_id"],
            "conversation-cli",
        )
        self.assertEqual(payload["session"]["provider"], "chatgpt-web")

    def test_resume_without_task_does_not_create_provider(self) -> None:
        code, stdout, stderr = self._invoke(
            [
                "run",
                "--workspace",
                str(self.root),
                "--state-dir",
                str(self.state),
                "--json",
            ]
        )
        self.assertEqual(code, 0, stderr)
        session_id = json.loads(stdout)["session_id"]
        with patch(
            "codexia_manual_agent.cli.ChatGPTWebProvider",
            side_effect=AssertionError("provider must not be constructed"),
        ):
            code, stdout, stderr = self._invoke(
                [
                    "resume",
                    session_id,
                    "--state-dir",
                    str(self.state),
                    "--json",
                ]
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["session_id"], session_id)

    def test_workspace_escape_returns_nonzero(self) -> None:
        code, _stdout, stderr = self._invoke(
            [
                "inspect",
                "--workspace",
                str(self.root),
                "read",
                "../outside.txt",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("escapes workspace boundary", stderr)


if __name__ == "__main__":
    unittest.main()
