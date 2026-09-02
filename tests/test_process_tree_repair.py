from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.application.execute_process import ExecuteProcessService
from codexia_manual_agent.authority import (
    ActionLifecycle,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import AuthorizationConsumedError
from codexia_manual_agent.execution import (
    ProcessExecutor,
    ProcessLimits,
    ProcessTerminationReason,
    prepare_process_proposal,
)
from codexia_manual_agent.execution import process_contained as contained
from codexia_manual_agent.execution.windows_job import WindowsJobObject


class ProcessTreeRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / "subdir").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _authorized(self):
        proposal = prepare_process_proposal(
            workspace=self.root,
            argv=[sys.executable, "-V"],
        )
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="test-human",
        )
        lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
        lifecycle.apply_receipt(receipt, authority=authority)
        return proposal, lifecycle, authority, receipt

    def test_descendant_cannot_survive_parent_exit(self) -> None:
        sentinel = self.root / "descendant-leaked.txt"
        child_code = (
            "import pathlib, time; "
            "print('child-start', flush=True); "
            "time.sleep(2.0); "
            f"pathlib.Path({str(sentinel)!r}).write_text('leaked', encoding='utf-8')"
        )
        parent_code = (
            "import subprocess, sys; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "print('parent-exit', flush=True)"
        )

        result = ExecuteProcessService().run(
            workspace=self.root,
            argv=[sys.executable, "-c", parent_code],
            approved=True,
            limits=ProcessLimits(timeout_seconds=1.0),
        )

        text = result.observation.stdout.text_utf8 or ""
        self.assertIn("parent-exit", text)
        if os.name == "nt":
            self.assertEqual(
                result.observation.termination_reason,
                ProcessTerminationReason.TIMEOUT,
            )
        else:
            self.assertEqual(
                result.observation.termination_reason,
                ProcessTerminationReason.EXITED,
            )

        time.sleep(1.2)
        self.assertFalse(
            sentinel.exists(),
            "descendant survived after the approved root exited",
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux bubblewrap regression")
    def test_detached_descendant_cannot_escape_timeout(self) -> None:
        sentinel = self.root / "detached-descendant-leaked.txt"
        child_code = (
            "import pathlib, time; "
            "time.sleep(2.0); "
            f"pathlib.Path({str(sentinel)!r}).write_text('leaked', encoding='utf-8')"
        )
        parent_code = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}], start_new_session=True); "
            "print('root-alive', flush=True); "
            "time.sleep(2.0)"
        )

        result = ExecuteProcessService().run(
            workspace=self.root,
            argv=[sys.executable, "-c", parent_code],
            approved=True,
            limits=ProcessLimits(timeout_seconds=0.8),
        )

        self.assertEqual(
            result.observation.termination_reason,
            ProcessTerminationReason.TIMEOUT,
        )
        time.sleep(1.4)
        self.assertFalse(
            sentinel.exists(),
            "start_new_session descendant escaped bubblewrap PID containment",
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux bubblewrap regression")
    def test_short_detached_descendant_is_removed_when_root_exits(self) -> None:
        sentinel = self.root / "short-detached.txt"
        child_code = (
            "import pathlib, time; time.sleep(0.5); "
            f"pathlib.Path({str(sentinel)!r}).write_text('late', encoding='utf-8')"
        )
        parent_code = (
            "import subprocess, sys; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}], start_new_session=True); "
            "print('root-exit', flush=True)"
        )

        result = ExecuteProcessService().run(
            workspace=self.root,
            argv=[sys.executable, "-c", parent_code],
            approved=True,
            limits=ProcessLimits(timeout_seconds=2.0),
        )

        self.assertEqual(
            result.observation.termination_reason,
            ProcessTerminationReason.EXITED,
        )
        self.assertLess(result.observation.duration_ms, 1500)
        time.sleep(0.8)
        self.assertFalse(sentinel.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux bubblewrap regression")
    def test_target_cannot_kill_namespace_init_and_escape_timeout(self) -> None:
        sentinel = self.root / "killed-init-leaked.txt"
        kill_attempt = (
            "try:\n"
            " os.kill(os.getppid(), signal.SIGKILL)\n"
            "except (PermissionError, ProcessLookupError):\n"
            " pass\n"
        )
        code = (
            "import os, pathlib, signal, time; "
            "print(f'parent={os.getppid()}', flush=True); "
            f"exec({kill_attempt!r}); "
            "time.sleep(2.0); "
            f"pathlib.Path({str(sentinel)!r}).write_text('leaked', encoding='utf-8')"
        )
        result = ExecuteProcessService().run(
            workspace=self.root,
            argv=[sys.executable, "-c", code],
            approved=True,
            limits=ProcessLimits(timeout_seconds=0.8),
        )
        self.assertEqual(
            result.observation.termination_reason,
            ProcessTerminationReason.TIMEOUT,
        )
        time.sleep(1.4)
        self.assertFalse(sentinel.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux argv regression")
    def test_large_accepted_argv_is_passed_as_argv_not_one_env_value(self) -> None:
        executable = shutil.which("true")
        self.assertIsNotNone(executable)
        large = "x" * 32_700
        argv = [executable, large, large, large, large]
        result = ExecuteProcessService().run(
            workspace=self.root,
            argv=argv,
            approved=True,
            limits=ProcessLimits(timeout_seconds=2.0),
        )
        self.assertEqual(result.observation.termination_reason, ProcessTerminationReason.EXITED)
        self.assertTrue(result.observation.started)
        self.assertIsNotNone(result.observation.pid)
        self.assertEqual(result.observation.exit_code, 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux exec handshake regression")
    def test_non_executable_regular_file_is_spawn_error_not_started(self) -> None:
        target = self.root / "not-executable"
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o644)

        result = ExecuteProcessService().run(
            workspace=self.root,
            argv=[str(target)],
            approved=True,
            limits=ProcessLimits(timeout_seconds=2.0),
        )

        self.assertEqual(
            result.observation.termination_reason,
            ProcessTerminationReason.SPAWN_ERROR,
        )
        self.assertFalse(result.observation.started)
        self.assertIsNone(result.observation.pid)
        self.assertIsNone(result.observation.exit_code)
        self.assertIn("exec failed", result.observation.error or "")

    @unittest.skipUnless(os.name == "nt", "Windows ownership regression")
    def test_windows_root_is_owned_before_first_instruction(self) -> None:
        sentinel = self.root / "suspended-root-ran.txt"
        code = (
            "import pathlib; "
            f"pathlib.Path({str(sentinel)!r}).write_text('ran', encoding='utf-8')"
        )
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        )

        process = subprocess.Popen(
            [sys.executable, "-c", code],
            creationflags=flags,
        )
        job = WindowsJobObject()
        try:
            time.sleep(0.1)
            self.assertFalse(
                sentinel.exists(),
                "CREATE_SUSPENDED root executed before Job Object assignment",
            )
            job.assign_and_resume(int(getattr(process, "_handle")), process.pid)
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertTrue(sentinel.exists())
        finally:
            if process.poll() is None:
                job.terminate()
                process.wait(timeout=5)
            job.close()

    @unittest.skipUnless(os.name == "nt", "Windows handle cleanup regression")
    def test_windows_job_closes_when_authorization_consumption_fails(self) -> None:
        proposal, lifecycle, authority, receipt = self._authorized()
        authority.consume(proposal, receipt, mode=ApprovalMode.RISKY)
        created = []

        class FakeJob:
            def __init__(self) -> None:
                self.closed = False
                created.append(self)

            def close(self) -> None:
                self.closed = True

        with patch.object(contained, "WindowsJobObject", FakeJob):
            with self.assertRaises(AuthorizationConsumedError):
                ProcessExecutor().execute(lifecycle, authority=authority)

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)

    @unittest.skipUnless(os.name == "nt", "Windows assignment regression")
    def test_failed_windows_job_assignment_kills_unowned_suspended_root(self) -> None:
        sentinel = self.root / "unowned-root-leaked.txt"
        code = (
            "import pathlib, time; time.sleep(0.5); "
            f"pathlib.Path({str(sentinel)!r}).write_text('leaked', encoding='utf-8')"
        )
        created = []

        class RejectingJob:
            def __init__(self) -> None:
                self.closed = False
                created.append(self)

            def assign(self, process_handle: int) -> None:
                raise OSError("synthetic assignment failure")

            def resume(self, process_id: int) -> None:  # pragma: no cover
                raise AssertionError("unassigned process must never be resumed")

            def close(self) -> None:
                self.closed = True

        with patch.object(contained, "WindowsJobObject", RejectingJob):
            result = ExecuteProcessService().run(
                workspace=self.root,
                argv=[sys.executable, "-c", code],
                approved=True,
            )

        self.assertEqual(result.observation.termination_reason, ProcessTerminationReason.SPAWN_ERROR)
        time.sleep(0.8)
        self.assertFalse(sentinel.exists())
        self.assertTrue(created[0].closed)

    def test_relative_executable_resolves_from_requested_cwd(self) -> None:
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
            self.assertIsNotNone(system_root)
            source = Path(system_root) / "System32" / "whoami.exe"
            copied = self.root / "subdir" / "cwd-tool.exe"
        else:
            resolved = shutil.which("true")
            self.assertIsNotNone(resolved)
            source = Path(resolved)
            copied = self.root / "subdir" / "cwd-tool"

        self.assertTrue(source.is_file())
        shutil.copy2(source, copied)
        relative_command = f".{os.sep}{copied.name}"

        proposal = prepare_process_proposal(
            workspace=self.root,
            cwd="subdir",
            argv=[relative_command],
        )
        parameters = proposal.to_dict()["parameters"]
        self.assertEqual(
            Path(parameters["resolved_executable"]).resolve(),
            copied.resolve(),
        )
        self.assertEqual(parameters["argv"][0], relative_command)

        result = ExecuteProcessService().run(
            workspace=self.root,
            cwd="subdir",
            argv=[relative_command],
            approved=True,
        )
        self.assertEqual(result.observation.exit_code, 0)
        self.assertEqual(
            Path(result.observation.resolved_executable).resolve(),
            copied.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
