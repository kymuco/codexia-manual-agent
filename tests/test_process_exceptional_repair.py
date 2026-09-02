from __future__ import annotations

import errno
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.application.execute_process import ExecuteProcessService
from codexia_manual_agent.execution import ProcessLimits, ProcessTerminationReason
from codexia_manual_agent.execution import process_contained as contained


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux containment regressions")
class LinuxExceptionalExecutionRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_collector_start_failure_cannot_leave_sandbox_running(self) -> None:
        sentinel = self.root / "collector-failure-leaked.txt"
        code = (
            "import pathlib, time; "
            "time.sleep(0.5); "
            f"pathlib.Path({str(sentinel)!r}).write_text('leaked', encoding='utf-8')"
        )

        with patch.object(
            contained.threading.Thread,
            "start",
            side_effect=RuntimeError("synthetic thread exhaustion"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic thread exhaustion"):
                ExecuteProcessService().run(
                    workspace=self.root,
                    argv=[sys.executable, "-c", code],
                    approved=True,
                    limits=ProcessLimits(timeout_seconds=2.0),
                )

        time.sleep(0.8)
        self.assertFalse(
            sentinel.exists(),
            "Linux sandbox survived an exception after process launch",
        )

    def test_pipe_setup_failure_after_consumption_is_observed(self) -> None:
        error = OSError(errno.EMFILE, "Too many open files")
        with patch.object(contained.os, "pipe", side_effect=error):
            result = ExecuteProcessService().run(
                workspace=self.root,
                argv=[sys.executable, "-V"],
                approved=True,
            )

        self.assertEqual(
            result.observation.termination_reason,
            ProcessTerminationReason.SPAWN_ERROR,
        )
        self.assertFalse(result.observation.started)
        self.assertIsNone(result.observation.pid)
        self.assertIsNone(result.observation.exit_code)
        self.assertIn("Too many open files", result.observation.error or "")

    def test_startup_deadline_reason_is_reported_as_timeout(self) -> None:
        with patch.object(
            contained,
            "_await_linux_target_exec",
            return_value=(None, None, ProcessTerminationReason.TIMEOUT),
        ):
            result = ExecuteProcessService().run(
                workspace=self.root,
                argv=[sys.executable, "-c", "import time; time.sleep(2)"],
                approved=True,
                limits=ProcessLimits(timeout_seconds=2.0),
            )

        self.assertEqual(
            result.observation.termination_reason,
            ProcessTerminationReason.TIMEOUT,
        )
        self.assertFalse(result.observation.started)
        self.assertIsNone(result.observation.pid)
        self.assertIsNone(result.observation.exit_code)


if __name__ == "__main__":
    unittest.main()
