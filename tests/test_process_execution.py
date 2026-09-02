from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.application.execute_process import ExecuteProcessService
from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    ApprovalRequiredError,
    InvalidProcessSpecError,
    ProcessExecutableChangedError,
    ProcessWorkspaceBoundaryError,
)
from codexia_manual_agent.execution import (
    PROCESS_ACTION,
    ProcessExecutor,
    ProcessLimits,
    ProcessTerminationReason,
    prepare_process_proposal,
)


class ProcessExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "subdir").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _authorized(
        self,
        proposal: ActionProposal,
    ) -> tuple[ActionLifecycle, LocalApprovalAuthority]:
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="test-human",
        )
        lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
        lifecycle.apply_receipt(receipt, authority=authority)
        return lifecycle, authority

    def test_exact_stdout_stderr_and_exit_code(self) -> None:
        result = ExecuteProcessService().run(
            workspace=self.root,
            argv=[
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr)",
            ],
            approved=True,
        )
        observation = result.observation
        self.assertEqual(observation.termination_reason, ProcessTerminationReason.EXITED)
        self.assertEqual(observation.exit_code, 0)
        self.assertEqual(observation.stdout.text_utf8, f"out{os.linesep}")
        self.assertEqual(observation.stderr.text_utf8, f"err{os.linesep}")
        self.assertFalse(observation.stdout.truncated)
        self.assertFalse(observation.stderr.truncated)

    def test_nonzero_exit_is_observed_not_raised(self) -> None:
        result = ExecuteProcessService().run(
            workspace=self.root,
            argv=[sys.executable, "-c", "raise SystemExit(7)"],
            approved=True,
        )
        self.assertEqual(result.observation.exit_code, 7)
        self.assertEqual(
            result.observation.termination_reason,
            ProcessTerminationReason.EXITED,
        )

    def test_environment_does_not_inherit_arbitrary_secret(self) -> None:
        with patch.dict(os.environ, {"CODEXIA_TEST_SECRET": "must-not-leak"}):
            result = ExecuteProcessService().run(
                workspace=self.root,
                argv=[
                    sys.executable,
                    "-c",
                    "import os; print(os.getenv('CODEXIA_TEST_SECRET'))",
                ],
                approved=True,
            )
        self.assertEqual(result.observation.stdout.text_utf8, f"None{os.linesep}")

    def test_workspace_relative_cwd_is_used(self) -> None:
        result = ExecuteProcessService().run(
            workspace=self.root,
            cwd="subdir",
            argv=[sys.executable, "-c", "import os; print(os.getcwd())"],
            approved=True,
        )
        self.assertEqual(
            result.observation.stdout.text_utf8.strip(),
            str((self.root / "subdir").resolve()),
        )
        self.assertEqual(result.observation.cwd, "subdir")

    def test_cwd_escape_is_rejected(self) -> None:
        with self.assertRaises(ProcessWorkspaceBoundaryError):
            prepare_process_proposal(
                workspace=self.root,
                cwd="..",
                argv=[sys.executable, "-V"],
            )

    def test_common_shell_interpreters_are_rejected(self) -> None:
        if os.name == "nt":
            root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
            shell = Path(root) / "System32" / "cmd.exe" if root else None
        else:
            shell = Path("/bin/sh")
        if shell is None or not shell.is_file():
            self.skipTest("platform shell path unavailable")
        with self.assertRaises(InvalidProcessSpecError):
            prepare_process_proposal(
                workspace=self.root,
                argv=[str(shell), "-c", "echo blocked"],
            )

    def test_timeout_terminates_process(self) -> None:
        result = ExecuteProcessService().run(
            workspace=self.root,
            argv=[sys.executable, "-c", "import time; time.sleep(2)"],
            approved=True,
            limits=ProcessLimits(timeout_seconds=0.1),
        )
        self.assertEqual(
            result.observation.termination_reason,
            ProcessTerminationReason.TIMEOUT,
        )

    def test_output_limit_terminates_or_marks_completed_flood(self) -> None:
        result = ExecuteProcessService().run(
            workspace=self.root,
            argv=[sys.executable, "-c", "print('x' * 200000)"],
            approved=True,
            limits=ProcessLimits(
                timeout_seconds=5,
                max_stdout_bytes=1024,
                max_stderr_bytes=1024,
            ),
        )
        self.assertEqual(
            result.observation.termination_reason,
            ProcessTerminationReason.OUTPUT_LIMIT,
        )
        self.assertTrue(result.observation.stdout.truncated)
        self.assertLessEqual(
            len(result.observation.stdout.data_base64),
            4 * 1024 // 3 + 8,
        )
        self.assertGreater(result.observation.stdout.byte_count, 1024)

    def test_executable_change_blocks_before_receipt_consumption(self) -> None:
        suffix = ".exe" if os.name == "nt" else ""
        copied = self.root / f"python-copy{suffix}"
        shutil.copy2(sys.executable, copied)
        proposal = prepare_process_proposal(
            workspace=self.root,
            argv=[str(copied), "-V"],
        )
        lifecycle, authority = self._authorized(proposal)
        receipt = lifecycle.authorization
        assert receipt is not None
        with copied.open("ab") as handle:
            handle.write(b"tamper")

        with self.assertRaises(ProcessExecutableChangedError):
            ProcessExecutor().execute(lifecycle, authority=authority)
        self.assertFalse(authority.is_consumed(receipt))
        self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)

    def test_forged_environment_is_rejected_before_consumption(self) -> None:
        original = prepare_process_proposal(
            workspace=self.root,
            argv=[sys.executable, "-V"],
        )
        parameters = original.to_dict()["parameters"]
        parameters["environment"]["AWS_SECRET_ACCESS_KEY"] = "forged"
        forged = ActionProposal.create(
            capability=Capability.EXECUTE_PROCESS,
            action=PROCESS_ACTION,
            workspace_root=original.workspace_root,
            parameters=parameters,
            summary="forged environment",
        )
        lifecycle, authority = self._authorized(forged)
        receipt = lifecycle.authorization
        assert receipt is not None

        with self.assertRaises(InvalidProcessSpecError):
            ProcessExecutor().execute(lifecycle, authority=authority)
        self.assertFalse(authority.is_consumed(receipt))

    def test_spawn_error_consumes_receipt_and_becomes_observation(self) -> None:
        proposal = prepare_process_proposal(
            workspace=self.root,
            argv=[sys.executable, "-V"],
        )
        lifecycle, authority = self._authorized(proposal)
        receipt = lifecycle.authorization
        assert receipt is not None

        with patch(
            "codexia_manual_agent.execution.process.subprocess.Popen",
            side_effect=OSError("synthetic spawn failure"),
        ):
            observation = ProcessExecutor().execute(
                lifecycle,
                authority=authority,
            )
        self.assertTrue(authority.is_consumed(receipt))
        self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)
        self.assertFalse(observation.started)
        self.assertEqual(
            observation.termination_reason,
            ProcessTerminationReason.SPAWN_ERROR,
        )
        self.assertIn("synthetic spawn failure", observation.error or "")

    def test_service_requires_explicit_human_approval(self) -> None:
        with self.assertRaises(ApprovalRequiredError):
            ExecuteProcessService().run(
                workspace=self.root,
                argv=[sys.executable, "-V"],
            )


if __name__ == "__main__":
    unittest.main()
