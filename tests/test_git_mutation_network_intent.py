from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.domain.errors import InvalidGitMutationError
from codexia_manual_agent.git_mutation import network_intent
from codexia_manual_agent.git_mutation.network_intent import build_network_push_intent
from codexia_manual_agent.git_mutation.repository import snapshot_repository


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
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
    return result


def _oid(root: Path, ref: str = "HEAD") -> str:
    return _git(root, "rev-parse", "--verify", ref).stdout.decode().strip()


def _init_repo(root: Path) -> str:
    _git(root, "init")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.name", "Codexia Test")
    _git(root, "config", "user.email", "codexia@example.invalid")
    (root / "tracked.txt").write_bytes(b"base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "base")
    return _oid(root)


def _commit(root: Path, payload: bytes) -> str:
    (root / "tracked.txt").write_bytes(payload)
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "next")
    return _oid(root)


def _network_repo(root: Path, url: str = "git@example.com:team/repo.git") -> tuple[str, str]:
    base_oid = _init_repo(root)
    _git(root, "remote", "add", "origin", url)
    _git(root, "update-ref", "refs/remotes/origin/main", base_oid)
    local_oid = _commit(root, b"ahead\n")
    return base_oid, local_oid


@unittest.skipUnless(shutil.which("git"), "Git executable is required")
class GitNetworkPushIntentTests(unittest.TestCase):
    def test_builds_exact_intent_from_local_tracking_state_without_network_commands(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            expected_oid, local_oid = _network_repo(root)
            snapshot = snapshot_repository(root)
            original_run_git = network_intent.run_git
            observed_commands: list[tuple[str, ...]] = []

            def recording_run_git(git, workspace, args, **kwargs):
                observed_commands.append(tuple(args))
                return original_run_git(git, workspace, args, **kwargs)

            with patch.object(network_intent, "run_git", recording_run_git):
                intent = build_network_push_intent(
                    snapshot,
                    remote="origin",
                    destination_ref="refs/heads/main",
                )

            self.assertEqual(intent.local_oid, local_oid)
            self.assertEqual(intent.expected_remote_oid, expected_oid)
            self.assertEqual(intent.tracking_ref, "refs/remotes/origin/main")
            self.assertEqual(intent.endpoint.host, "example.com")
            self.assertEqual(intent.endpoint.ssh_user, "git")
            forbidden = {"ls-remote", "push", "fetch"}
            self.assertFalse(
                any(command and command[0] in forbidden for command in observed_commands),
                observed_commands,
            )

    def test_https_intent_is_constructed_without_credentials_or_network_contact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            expected_oid, local_oid = _network_repo(
                root,
                "https://example.com/team/repo.git",
            )
            intent = build_network_push_intent(
                snapshot_repository(root),
                remote="origin",
                destination_ref="refs/heads/main",
            )
            self.assertEqual(intent.expected_remote_oid, expected_oid)
            self.assertEqual(intent.local_oid, local_oid)
            self.assertEqual(intent.endpoint.review_destination, "https://example.com:443/team/repo.git")

    def test_missing_remote_tracking_commit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            _git(root, "remote", "add", "origin", "git@example.com:team/repo.git")
            _commit(root, b"ahead\n")
            with self.assertRaisesRegex(InvalidGitMutationError, "remote-tracking commit"):
                build_network_push_intent(
                    snapshot_repository(root),
                    remote="origin",
                    destination_ref="refs/heads/main",
                )

    def test_locally_provable_non_fast_forward_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base_oid = _init_repo(root)
            _git(root, "remote", "add", "origin", "git@example.com:team/repo.git")
            _git(root, "checkout", "-b", "remote-line")
            remote_oid = _commit(root, b"remote\n")
            _git(root, "checkout", "main")
            _git(root, "update-ref", "refs/remotes/origin/main", remote_oid)
            _commit(root, b"local\n")
            self.assertEqual(_git(root, "merge-base", "HEAD", remote_oid).stdout.decode().strip(), base_oid)
            with self.assertRaisesRegex(InvalidGitMutationError, "non-fast-forward"):
                build_network_push_intent(
                    snapshot_repository(root),
                    remote="origin",
                    destination_ref="refs/heads/main",
                )

    def test_multiple_push_destinations_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _network_repo(root)
            _git(root, "config", "--add", "remote.origin.pushurl", "git@example.com:one/repo.git")
            _git(root, "config", "--add", "remote.origin.pushurl", "git@example.com:two/repo.git")
            with self.assertRaisesRegex(InvalidGitMutationError, "exactly one"):
                build_network_push_intent(
                    snapshot_repository(root),
                    remote="origin",
                    destination_ref="refs/heads/main",
                )

    def test_destination_rewrite_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _network_repo(root)
            _git(root, "config", "url.ssh://git@other.example/.insteadOf", "git@example.com:")
            with self.assertRaisesRegex(InvalidGitMutationError, "URL rewriting"):
                build_network_push_intent(
                    snapshot_repository(root),
                    remote="origin",
                    destination_ref="refs/heads/main",
                )

    def test_receive_pack_mirror_and_push_option_overrides_fail_closed(self) -> None:
        cases = (
            ("remote.origin.receivepack", "/tmp/receive-pack"),
            ("remote.origin.mirror", "true"),
            ("push.pushOption", "server-side-option"),
        )
        for key, value in cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _network_repo(root)
                _git(root, "config", "--add", key, value)
                with self.assertRaises(InvalidGitMutationError):
                    build_network_push_intent(
                        snapshot_repository(root),
                        remote="origin",
                        destination_ref="refs/heads/main",
                    )


if __name__ == "__main__":
    unittest.main()
