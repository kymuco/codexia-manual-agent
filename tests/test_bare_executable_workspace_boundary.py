from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.domain.errors import ProcessExecutableNotFoundError
from codexia_manual_agent.execution import prepare_process_proposal
from codexia_manual_agent.execution import process as base_process
from codexia_manual_agent.execution import process_contained


class BareExecutableWorkspaceBoundaryTests(unittest.TestCase):
    def test_contained_resolver_rejects_workspace_result_from_which(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve(strict=True)
            fake = workspace / ("tool.exe" if os.name == "nt" else "tool")
            fake.write_bytes(b"workspace controlled executable")
            if os.name != "nt":
                fake.chmod(0o755)

            with patch.object(process_contained.shutil, "which", return_value=str(fake)):
                with self.assertRaises(ProcessExecutableNotFoundError):
                    prepare_process_proposal(workspace=workspace, argv=["tool"])

    def test_base_resolver_rejects_workspace_result_from_which(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve(strict=True)
            fake = workspace / ("tool.exe" if os.name == "nt" else "tool")
            fake.write_bytes(b"workspace controlled executable")
            if os.name != "nt":
                fake.chmod(0o755)

            with patch.object(base_process.shutil, "which", return_value=str(fake)):
                with self.assertRaises(ProcessExecutableNotFoundError):
                    base_process._resolve_executable("tool", workspace)

    def test_explicit_workspace_executable_remains_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve(strict=True)
            explicit = workspace / ("tool.exe" if os.name == "nt" else "tool")
            explicit.write_bytes(b"explicitly selected workspace executable")
            if os.name != "nt":
                explicit.chmod(0o755)

            proposal = prepare_process_proposal(
                workspace=workspace,
                argv=[str(explicit)],
            )
            resolved = Path(proposal.to_dict()["parameters"]["resolved_executable"])
            self.assertEqual(resolved.resolve(strict=True), explicit)

    @unittest.skipUnless(os.name == "nt", "Windows current-directory lookup regression")
    def test_windows_cwd_cannot_bind_workspace_git_for_bare_name(self) -> None:
        found = shutil.which("git")
        if found is None:
            self.skipTest("git is unavailable on this test host")
        real_git = Path(found).resolve(strict=True)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve(strict=True)
            fake_git = workspace / "git.exe"
            shutil.copy2(real_git, fake_git)

            previous_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                with patch.dict(
                    os.environ,
                    {"PATH": str(real_git.parent)},
                    clear=False,
                ):
                    try:
                        proposal = prepare_process_proposal(
                            workspace=workspace,
                            argv=["git", "--version"],
                        )
                    except ProcessExecutableNotFoundError:
                        # Fail-closed rejection is acceptable when Windows inserts cwd
                        # ahead of the supplied filtered PATH.
                        return
            finally:
                os.chdir(previous_cwd)

            resolved = Path(
                proposal.to_dict()["parameters"]["resolved_executable"]
            ).resolve(strict=True)
            self.assertNotEqual(resolved, fake_git)
            self.assertFalse(resolved.is_relative_to(workspace))


if __name__ == "__main__":
    unittest.main()
