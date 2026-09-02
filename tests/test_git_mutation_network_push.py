from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import GitMutationPreconditionChangedError
from codexia_manual_agent.git_mutation.models import GitMutationOutcome
from codexia_manual_agent.git_mutation.network_push import (
    GIT_SSH_NETWORK_PUSH_BACKEND,
    execute_network_git_push,
    prepare_network_git_push_proposal,
)
from codexia_manual_agent.git_mutation.ssh_execution import close_ssh_execution_plan


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


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.name", "Codexia Test")
    _git(root, "config", "user.email", "codexia@example.invalid")
    (root / "tracked.txt").write_bytes(b"base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "base")


def _direct_commit(root: Path, payload: bytes) -> str:
    (root / "tracked.txt").write_bytes(payload)
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "local")
    return _oid(root)


def _ssh_string(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def _host_key_line(host: str) -> bytes:
    blob = _ssh_string(b"ssh-ed25519") + _ssh_string(b"h" * 32)
    encoded = base64.b64encode(blob).decode("ascii")
    return f"{host} ssh-ed25519 {encoded}\n".encode("ascii")


def _authorize(proposal: ActionProposal):
    authority = LocalApprovalAuthority()
    lifecycle = ActionLifecycle(proposal=proposal, mode=ApprovalMode.ALWAYS)
    receipt = authority.decide(
        proposal,
        mode=ApprovalMode.ALWAYS,
        approved=True,
        actor="test-human",
    )
    lifecycle.apply_receipt(receipt, authority=authority)
    return authority, lifecycle, receipt


class _NoopPin:
    def close(self):
        return None


def _completed(returncode: int, *, stdout: bytes = b"", stderr: bytes = b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _prepare(root: Path, trust: Path):
    base = _oid(root)
    _git(root, "config", "remote.origin.url", "git@example.com:team/repo.git")
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    local = _direct_commit(root, b"ahead\n")
    identity = trust / "identity"
    identity.write_bytes(b"synthetic-private-key-material\n")
    host_key = trust / "known_hosts"
    host_key.write_bytes(_host_key_line("example.com"))
    records = [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("203.0.113.23", 22),
        )
    ]
    with mock.patch(
        "codexia_manual_agent.git_mutation.ssh_execution.socket.getaddrinfo",
        return_value=records,
    ):
        prepared = prepare_network_git_push_proposal(
            workspace=root,
            remote="origin",
            destination_ref="refs/heads/main",
            identity_file=identity.resolve(),
            host_key_file=host_key.resolve(),
        )
    return prepared, base, local


@unittest.skipUnless(shutil.which("git") and shutil.which("ssh"), "Git and OpenSSH are required")
class NetworkGitPushGovernanceTests(unittest.TestCase):
    def test_preparation_binds_exact_oid_lease_route_host_key_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, base, local = _prepare(root, trust)
            self.addCleanup(close_ssh_execution_plan, prepared.ssh_plan)

            preview = prepared.approval_preview
            self.assertEqual(preview.local_oid, local)
            self.assertEqual(preview.expected_remote_oid, base)
            self.assertEqual(preview.destination_ref, "refs/heads/main")
            self.assertEqual(preview.tracking_ref, "refs/remotes/origin/main")
            self.assertEqual(preview.review_destination, "ssh://git@example.com:22/~/team/repo.git")
            self.assertEqual(preview.route_address, "203.0.113.23")
            self.assertEqual(preview.backend, GIT_SSH_NETWORK_PUSH_BACKEND)
            self.assertEqual(prepared.proposal.capability.value, "git_push")
            self.assertTrue(preview.host_key_fingerprint_sha256.startswith("SHA256:"))
            self.assertTrue(preview.identity_source_sha256)
            self.assertFalse(Path(prepared.ssh_plan.bundle_identity_path).exists())
            self.assertFalse(Path(prepared.ssh_plan.bundle_known_hosts_path).exists())

    def test_tracking_ref_drift_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, _, local = _prepare(root, trust)
            self.addCleanup(close_ssh_execution_plan, prepared.ssh_plan)
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            _git(root, "update-ref", "refs/remotes/origin/main", local)

            with self.assertRaises(GitMutationPreconditionChangedError):
                execute_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )
            self.assertFalse(authority.is_consumed(receipt))
            self.assertIs(lifecycle.phase, ActionPhase.AUTHORIZED)

    def test_materialization_failure_does_not_consume_receipt_or_open_network(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, _, _ = _prepare(root, trust)
            authority, lifecycle, receipt = _authorize(prepared.proposal)

            with mock.patch(
                "codexia_manual_agent.git_mutation.network_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.network_push.materialize_ssh_execution_plan",
                side_effect=GitMutationPreconditionChangedError("synthetic materialization failure"),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.network_push.run_git",
                side_effect=AssertionError("network Git must not run before receipt consumption"),
            ) as network_git:
                with self.assertRaisesRegex(
                    GitMutationPreconditionChangedError,
                    "synthetic materialization failure",
                ):
                    execute_network_git_push(
                        prepared,
                        lifecycle=lifecycle,
                        authority=authority,
                    )

            self.assertFalse(authority.is_consumed(receipt))
            self.assertIs(lifecycle.phase, ActionPhase.AUTHORIZED)
            network_git.assert_not_called()

    def test_exact_push_argv_uses_approved_oid_refspec_and_explicit_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, base, local = _prepare(root, trust)
            authority, lifecycle, _ = _authorize(prepared.proposal)
            calls: list[list[str]] = []

            def fake_run_git(_git_identity, _root, args, **kwargs):
                calls.append(list(args))
                if "ls-remote" in args:
                    return _completed(0, stdout=f"{local}\trefs/heads/main\n".encode("ascii"))
                return _completed(0, stdout=b"ok\n")

            with mock.patch(
                "codexia_manual_agent.git_mutation.network_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.network_push.run_git",
                side_effect=fake_run_git,
            ):
                observation = execute_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )

            self.assertIs(observation.outcome, GitMutationOutcome.APPLIED)
            self.assertIs(lifecycle.phase, ActionPhase.OBSERVED)
            push_args = next(args for args in calls if "push" in args)
            self.assertIn("--no-verify", push_args)
            self.assertIn("--no-signed", push_args)
            self.assertIn("--no-follow-tags", push_args)
            self.assertIn("--no-recurse-submodules", push_args)
            self.assertIn("--no-force-if-includes", push_args)
            self.assertIn(f"--force-with-lease=refs/heads/main:{base}", push_args)
            self.assertIn(f"{local}:refs/heads/main", push_args)
            self.assertNotIn("HEAD:refs/heads/main", push_args)

    def test_nonzero_push_with_exact_target_observed_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, _, local = _prepare(root, trust)
            authority, lifecycle, _ = _authorize(prepared.proposal)
            results = iter(
                [
                    _completed(1, stderr=b"lease race\n"),
                    _completed(0, stdout=f"{local}\trefs/heads/main\n".encode("ascii")),
                ]
            )
            with mock.patch(
                "codexia_manual_agent.git_mutation.network_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.network_push.run_git",
                side_effect=lambda *args, **kwargs: next(results),
            ):
                observation = execute_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )
            self.assertIs(observation.outcome, GitMutationOutcome.APPLIED)
            self.assertEqual(observation.observed_remote_oid, local)

    def test_nonzero_lease_with_competing_oid_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, base, _ = _prepare(root, trust)
            authority, lifecycle, _ = _authorize(prepared.proposal)
            results = iter(
                [
                    _completed(1, stderr=b"stale info\n"),
                    _completed(0, stdout=f"{base}\trefs/heads/main\n".encode("ascii")),
                ]
            )
            with mock.patch(
                "codexia_manual_agent.git_mutation.network_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.network_push.run_git",
                side_effect=lambda *args, **kwargs: next(results),
            ):
                observation = execute_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )
            self.assertIs(observation.outcome, GitMutationOutcome.REJECTED)
            self.assertEqual(observation.observed_remote_oid, base)
            self.assertTrue(observation.remote_observation_complete)

    def test_success_with_different_terminal_oid_is_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, base, _ = _prepare(root, trust)
            authority, lifecycle, _ = _authorize(prepared.proposal)
            results = iter(
                [
                    _completed(0),
                    _completed(0, stdout=f"{base}\trefs/heads/main\n".encode("ascii")),
                ]
            )
            with mock.patch(
                "codexia_manual_agent.git_mutation.network_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.network_push.run_git",
                side_effect=lambda *args, **kwargs: next(results),
            ):
                observation = execute_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )
            self.assertIs(observation.outcome, GitMutationOutcome.MISMATCH)

    def test_unobservable_remote_after_consumption_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, _, _ = _prepare(root, trust)
            authority, lifecycle, _ = _authorize(prepared.proposal)
            results = iter([_completed(1, stderr=b"network failure\n"), _completed(1)])
            with mock.patch(
                "codexia_manual_agent.git_mutation.network_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.network_push.run_git",
                side_effect=lambda *args, **kwargs: next(results),
            ):
                observation = execute_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )
            self.assertIs(observation.outcome, GitMutationOutcome.INCOMPLETE)
            self.assertFalse(observation.remote_observation_complete)
            self.assertIs(lifecycle.phase, ActionPhase.OBSERVED)


if __name__ == "__main__":
    unittest.main()
