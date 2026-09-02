from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.adapters.filesystem_workspace import FilesystemWorkspace
from codexia_manual_agent.domain.errors import (
    BinaryFileError,
    FileTooLargeError,
    GitStatusError,
    WorkspaceBoundaryError,
)


class FilesystemWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(
            "print('hello')\nVALUE = 7\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text(
            "# Demo\nhello world\n",
            encoding="utf-8",
        )
        self.workspace = FilesystemWorkspace(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reads_utf8_file(self) -> None:
        self.assertIn("VALUE = 7", self.workspace.read_file("src/app.py"))

    def test_read_preserves_crlf_line_endings(self) -> None:
        (self.root / "crlf.txt").write_bytes(b"alpha\r\nbeta\r\n")
        self.assertEqual(
            self.workspace.read_file("crlf.txt"),
            "alpha\r\nbeta\r\n",
        )

    def test_rejects_parent_escape(self) -> None:
        with self.assertRaises(WorkspaceBoundaryError):
            self.workspace.read_file("../outside.txt")

    def test_rejects_absolute_path(self) -> None:
        with self.assertRaises(WorkspaceBoundaryError):
            self.workspace.read_file(str((self.root / "README.md").resolve()))

    def test_enforces_file_size_limit(self) -> None:
        with self.assertRaises(FileTooLargeError):
            self.workspace.read_file("README.md", max_bytes=2)

    def test_rejects_binary_file(self) -> None:
        (self.root / "binary.bin").write_bytes(b"abc\x00def")
        with self.assertRaises(BinaryFileError):
            self.workspace.read_file("binary.bin")

    def test_lists_entries_with_relative_paths(self) -> None:
        entries = self.workspace.list_files(".")
        paths = {entry.path for entry in entries}
        self.assertEqual(paths, {"README.md", "src"})

    def test_recursive_list_skips_git_directory(self) -> None:
        (self.root / ".git").mkdir()
        (self.root / ".git" / "secret").write_text("x", encoding="utf-8")
        paths = {entry.path for entry in self.workspace.list_files(".", recursive=True)}
        self.assertNotIn(".git", paths)
        self.assertNotIn(".git/secret", paths)

    def test_search_is_case_insensitive_and_bounded(self) -> None:
        matches = self.workspace.search_text("HELLO", max_matches=1)
        self.assertEqual(len(matches), 1)
        self.assertIn(matches[0].path, {"README.md", "src/app.py"})

    def test_sensitive_files_are_hidden_from_listing_and_search(self) -> None:
        (self.root / "auth_data.json").write_text(
            '{"accessToken":"do-not-expose"}',
            encoding="utf-8",
        )
        (self.root / ".env").write_text(
            "SECRET=do-not-expose\n",
            encoding="utf-8",
        )
        listed = {
            entry.path
            for entry in self.workspace.list_files(".", recursive=True)
        }
        self.assertNotIn("auth_data.json", listed)
        self.assertNotIn(".env", listed)
        self.assertEqual(self.workspace.search_text("do-not-expose"), ())

    def test_template_env_file_remains_visible_and_searchable(self) -> None:
        (self.root / ".env.example").write_text(
            "EXAMPLE_SETTING=visible\n",
            encoding="utf-8",
        )
        listed = {entry.path for entry in self.workspace.list_files(".")}
        self.assertIn(".env.example", listed)
        matches = self.workspace.search_text("EXAMPLE_SETTING")
        self.assertEqual([match.path for match in matches], [".env.example"])

    def test_recursive_search_does_not_follow_file_symlink_outside_workspace(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside-search.txt"
        outside.write_text("outside-search-secret", encoding="utf-8")
        link = self.root / "outside-link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        try:
            self.assertEqual(
                self.workspace.search_text("outside-search-secret"),
                (),
            )
        finally:
            outside.unlink(missing_ok=True)

    def test_symlink_escape_is_rejected(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        try:
            with self.assertRaises(WorkspaceBoundaryError):
                self.workspace.read_file("link.txt")
        finally:
            outside.unlink(missing_ok=True)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_status_uses_fixed_read_only_command(self) -> None:
        subprocess.run(
            ["git", "init", "-q", str(self.root)],
            check=True,
            capture_output=True,
        )
        status = self.workspace.git_status()
        self.assertIsNotNone(status.branch_line)
        self.assertTrue(any("README.md" in entry for entry in status.entries))

    def test_git_status_reports_non_repository(self) -> None:
        if not shutil.which("git"):
            self.skipTest("git not installed")
        with self.assertRaises(GitStatusError):
            self.workspace.git_status()


if __name__ == "__main__":
    unittest.main()
