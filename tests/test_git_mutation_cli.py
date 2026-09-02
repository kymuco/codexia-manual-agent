from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from codexia_manual_agent.cli import main


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def _oid(root: Path, ref: str = "HEAD") -> str:
    return _git(root, "rev-parse", "--verify", ref).decode().strip()


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.name", "CLI Test")
    _git(root, "config", "user.email", "cli@example.invalid")
    (root / "tracked.txt").write_bytes(b"base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "base")


@unittest.skipUnless(shutil.which("git"), "Git executable is required")
class GitMutationCliTests(unittest.TestCase):
    def test_git_commit_without_approve_is_preview_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            before = _oid(root)
            (root / "tracked.txt").write_bytes(b"staged\n")
            _git(root, "add", "tracked.txt")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "git",
                        "commit",
                        "--workspace",
                        str(root),
                        "--message",
                        "needs approval",
                        "--json",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertEqual(_oid(root), before)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "approval_required")
            self.assertEqual(payload["approval_preview"]["message"], "needs approval")
            self.assertTrue(payload["approval_preview"]["expected_commit_oid"])
            self.assertNotIn("authorization", payload)
            self.assertNotIn("observation", payload)

    @unittest.skipUnless(os.name == "nt", "M2.5 execution boundary is Windows TxF")
    def test_git_commit_approve_displays_preview_then_requires_yes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            before = _oid(root)
            (root / "tracked.txt").write_bytes(b"staged\n")
            _git(root, "add", "tracked.txt")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sys.stdin", io.StringIO("YES\n")), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "git",
                        "commit",
                        "--workspace",
                        str(root),
                        "--message",
                        "cli governed",
                        "--approve",
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertNotEqual(_oid(root), before)
            self.assertIn("exact Git mutation approval preview", stderr.getvalue())
            self.assertIn("Type YES", stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["observation"]["outcome"], "applied")
            self.assertEqual(payload["approval_preview"]["message"], "cli governed")
            self.assertEqual(payload["authorization"]["source"], "human")

    def test_git_commit_non_yes_denies_without_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            before = _oid(root)
            (root / "tracked.txt").write_bytes(b"staged\n")
            _git(root, "add", "tracked.txt")
            stderr = io.StringIO()
            with patch("sys.stdin", io.StringIO("NO\n")), redirect_stderr(stderr):
                code = main(
                    [
                        "git",
                        "commit",
                        "--workspace",
                        str(root),
                        "--message",
                        "denied",
                        "--approve",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertEqual(_oid(root), before)
            self.assertIn("denied", stderr.getvalue().lower())

    @unittest.skipUnless(os.name == "nt", "M2.5 execution boundary is Windows TxF")
    def test_git_push_default_transport_preserves_file_backend_and_applies(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            _init_repo(root)
            _git(bare, "init", "--bare")
            _git(root, "remote", "add", "origin", bare.as_uri())
            _git(root, "push", "-u", "origin", "main")
            _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
            (root / "tracked.txt").write_bytes(b"ahead\n")
            _git(root, "add", "tracked.txt")
            _git(root, "commit", "--no-gpg-sign", "-m", "ahead")
            local_oid = _oid(root)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("sys.stdin", io.StringIO("YES\n")), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "git",
                        "push",
                        "--workspace",
                        str(root),
                        "--remote",
                        "origin",
                        "--destination-ref",
                        "refs/heads/main",
                        "--approve",
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(_oid(bare, "refs/heads/main"), local_oid)
            self.assertIn("Type YES", stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["observation"]["outcome"], "applied")
            self.assertEqual(payload["approval_preview"]["destination_ref"], "refs/heads/main")
            self.assertEqual(payload["approval_preview"]["backend"], "file-pack-update-ref.v1")

    def test_network_push_preview_routes_explicit_ssh_inputs_and_cleans_preparation(self) -> None:
        preparation = Mock()
        preparation.proposal.to_dict.return_value = {
            "action": "git.push.v1",
            "capability": "git_push",
        }
        preparation.approval_preview.to_dict.return_value = {
            "backend": "network-ssh-direct.v1",
            "review_destination": "ssh://git@example.com:22~/team/repo.git",
        }
        stdout = io.StringIO()
        with patch(
            "codexia_manual_agent.cli.prepare_governed_git_push_proposal",
            return_value=preparation,
        ) as prepare, patch(
            "codexia_manual_agent.cli.close_governed_git_push_preparation"
        ) as close, redirect_stdout(stdout):
            code = main(
                [
                    "git",
                    "push",
                    "--workspace",
                    "workspace",
                    "--remote",
                    "origin",
                    "--destination-ref",
                    "refs/heads/main",
                    "--transport",
                    "ssh",
                    "--ssh-identity-file",
                    "C:/trust/id_ed25519",
                    "--ssh-host-key-file",
                    "C:/trust/known_hosts",
                    "--json",
                ]
            )

        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "approval_required")
        self.assertEqual(payload["approval_preview"]["backend"], "network-ssh-direct.v1")
        prepare.assert_called_once_with(
            workspace="workspace",
            remote="origin",
            destination_ref="refs/heads/main",
            transport="ssh",
            ssh_identity_file="C:/trust/id_ed25519",
            ssh_host_key_file="C:/trust/known_hosts",
            https_credential_file=None,
            https_ca_bundle=None,
        )
        close.assert_called_once_with(preparation)

    def test_incomplete_ssh_trust_inputs_fail_before_backend_or_network(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "git",
                    "push",
                    "--workspace",
                    "does-not-need-to-exist",
                    "--remote",
                    "origin",
                    "--destination-ref",
                    "refs/heads/main",
                    "--transport",
                    "ssh",
                    "--ssh-identity-file",
                    "C:/trust/id_ed25519",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("requires both", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
