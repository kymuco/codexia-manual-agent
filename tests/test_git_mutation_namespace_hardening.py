from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.domain.errors import GitRepositoryBoundaryError
from codexia_manual_agent.git_mutation.windows_namespace import WindowsGitNamespacePin


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.name", "Codexia Test")
    _git(root, "config", "user.email", "codexia@example.invalid")
    (root / "tracked.txt").write_bytes(b"base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "base")


def _pin_dirs(root: Path) -> tuple[Path, ...]:
    git_dir = root / ".git"
    return (
        git_dir,
        git_dir / "objects",
        git_dir / "objects" / "pack",
        git_dir / "refs" / "heads",
    )


@unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Windows Git is required")
class GitNamespaceHardeningTests(unittest.TestCase):
    def test_commit_pin_automatically_locks_live_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            git_dir = root / ".git"
            pin = WindowsGitNamespacePin.acquire(
                _pin_dirs(root),
                locked_files=(git_dir / "config",),
            )
            try:
                with self.assertRaises(OSError):
                    with (git_dir / "index").open("r+b") as handle:
                        handle.write(b"X")
                        handle.flush()
                        os.fsync(handle.fileno())
            finally:
                self.assertIsNone(pin.close())

    def test_locked_config_rejects_include_sections(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            git_dir = root / ".git"
            with (git_dir / "config").open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("\n[include]\n\tpath = C:/external/gitconfig\n")
            with self.assertRaisesRegex(GitRepositoryBoundaryError, "include/includeIf"):
                WindowsGitNamespacePin.acquire(
                    _pin_dirs(root),
                    locked_files=(git_dir / "config",),
                )

    def test_namespace_admission_rejects_external_object_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            git_dir = root / ".git"
            alternates = git_dir / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text("C:/external/objects\n", encoding="utf-8")
            with self.assertRaisesRegex(GitRepositoryBoundaryError, "external Git object/ancestry"):
                WindowsGitNamespacePin.acquire(
                    _pin_dirs(root),
                    locked_files=(git_dir / "config",),
                )


if __name__ == "__main__":
    unittest.main()
