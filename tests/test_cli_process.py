from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.cli import main


class ProcessCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _invoke(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_exec_requires_approve_flag(self) -> None:
        code, _stdout, stderr = self._invoke(
            [
                "exec",
                "--workspace",
                str(self.root),
                "--",
                sys.executable,
                "-V",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("requires explicit local approval", stderr)

    def test_exec_runs_structured_argv_and_returns_exact_observation(self) -> None:
        code, stdout, stderr = self._invoke(
            [
                "exec",
                "--workspace",
                str(self.root),
                "--approve",
                "--json",
                "--",
                sys.executable,
                "-c",
                "print('cli-ok')",
            ]
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        observation = payload["observation"]
        self.assertEqual(observation["termination_reason"], "exited")
        self.assertEqual(observation["exit_code"], 0)
        self.assertEqual(
            observation["stdout"]["text_utf8"],
            f"cli-ok{os.linesep}",
        )
        self.assertEqual(payload["authorization"]["actor"], "local-cli")

    def test_never_mode_cannot_be_overridden_by_approve(self) -> None:
        code, _stdout, stderr = self._invoke(
            [
                "exec",
                "--workspace",
                str(self.root),
                "--approval-mode",
                "never",
                "--approve",
                "--",
                sys.executable,
                "-V",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("forbids side effects", stderr)


if __name__ == "__main__":
    unittest.main()
