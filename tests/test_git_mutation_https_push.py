from __future__ import annotations

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
from codexia_manual_agent.domain.errors import (
    GitMutationPreconditionChangedError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation.https_push import (
    GIT_HTTPS_NETWORK_PUSH_BACKEND,
    close_https_network_git_push_preparation,
    execute_https_network_git_push,
    prepare_https_network_git_push_proposal,
)
from codexia_manual_agent.git_mutation.models import GitMutationOutcome


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
    _git(root, "config", "remote.origin.url", "https://example.com/team/repo.git")
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    local = _direct_commit(root, b"ahead\n")
    ca = trust / "ca-bundle.pem"
    ca.write_text(
        "-----BEGIN CERTIFICATE-----\nsynthetic-test-ca\n-----END CERTIFICATE-----\n",
        encoding="ascii",
    )
    credential = trust / "credentials"
    credential.write_text(
        "https://codexia-user:exact-secret@example.com/team/repo.git\n",
        encoding="utf-8",
    )
    records = [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("203.0.113.41", 443),
        )
    ]
    with mock.patch(
        "codexia_manual_agent.git_mutation.https_transport.socket.getaddrinfo",
        return_value=records,
    ):
        prepared = prepare_https_network_git_push_proposal(
            workspace=root,
            remote="origin",
            destination_ref="refs/heads/main",
            credential_file=credential.resolve(),
            ca_bundle_file=ca.resolve(),
        )
    return prepared, base, local, ca, credential


@unittest.skipUnless(shutil.which("git"), "Git is required")
class HttpsNetworkGitPushGovernanceTests(unittest.TestCase):
    def test_preparation_binds_oid_lease_route_tls_ca_and_read_only_credential_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, base, local, ca, credential = _prepare(root, trust)
            self.addCleanup(close_https_network_git_push_preparation, prepared)

            preview = prepared.approval_preview
            self.assertEqual(preview.local_oid, local)
            self.assertEqual(preview.expected_remote_oid, base)
            self.assertEqual(preview.destination_ref, "refs/heads/main")
            self.assertEqual(preview.tracking_ref, "refs/remotes/origin/main")
            self.assertEqual(preview.review_destination, "https://example.com:443/team/repo.git")
            self.assertEqual(preview.route_address, "203.0.113.41")
            self.assertEqual(preview.tls_backend, prepared.https_binding.tls_backend)
            self.assertEqual(Path(preview.ca_bundle_path), ca.resolve())
            self.assertEqual(Path(preview.credential_source_path), credential.resolve())
            self.assertEqual(preview.credential_username, "codexia-user")
            self.assertEqual(preview.credential_mode, "frozen-shell-response.v1")
            self.assertEqual(preview.credential_shell_path, prepared.https_binding.git_shell.path)
            self.assertEqual(preview.backend, GIT_HTTPS_NETWORK_PUSH_BACKEND)
            self.assertEqual(prepared.proposal.capability.value, "git_push")
            self.assertTrue(preview.ca_bundle_sha256)
            self.assertTrue(preview.credential_shell_sha256)
            self.assertEqual(list(Path(prepared.https_binding.credential_bundle_root).iterdir()), [])

            rendered = repr(prepared.proposal.to_dict())
            self.assertNotIn("exact-secret", rendered)
            self.assertNotIn(prepared.https_binding.credential_source.secret_sha256, rendered)
            self.assertNotIn("credential_store_helper", rendered)

    def test_tracking_ref_drift_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, _, local, _, _ = _prepare(root, trust)
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            _git(root, "update-ref", "refs/remotes/origin/main", local)

            with self.assertRaises(GitMutationPreconditionChangedError):
                execute_https_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )
            self.assertFalse(authority.is_consumed(receipt))
            self.assertIs(lifecycle.phase, ActionPhase.AUTHORIZED)

    def test_ca_or_credential_drift_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, _, _, ca, _credential = _prepare(root, trust)
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            ca.write_bytes(ca.read_bytes() + b"tamper")
            with self.assertRaises(GitMutationPreconditionChangedError):
                execute_https_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )
            self.assertFalse(authority.is_consumed(receipt))
            self.assertIs(lifecycle.phase, ActionPhase.AUTHORIZED)

            # Return to the original base commit before constructing a second
            # independent preparation. Reusing the already-ahead fixture would
            # otherwise try to commit the same bytes twice and fail before the
            # credential-drift assertion is reached.
            _git(root, "reset", "--hard", "HEAD~1")
            prepared, _, _, _ca, credential = _prepare(root, trust)
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            credential.write_bytes(credential.read_bytes() + b"tamper")
            with self.assertRaises(GitMutationPreconditionChangedError):
                execute_https_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )
            self.assertFalse(authority.is_consumed(receipt))
            self.assertIs(lifecycle.phase, ActionPhase.AUTHORIZED)

    def test_credential_materialization_failure_does_not_consume_receipt_or_open_network(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, _, _, _, _ = _prepare(root, trust)
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            with mock.patch(
                "codexia_manual_agent.git_mutation.https_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.https_push.materialize_https_credentials",
                side_effect=InvalidGitMutationError("synthetic materialization failure"),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.https_push.run_git"
            ) as network_git:
                with self.assertRaises(InvalidGitMutationError):
                    execute_https_network_git_push(
                        prepared,
                        lifecycle=lifecycle,
                        authority=authority,
                    )
            self.assertFalse(authority.is_consumed(receipt))
            self.assertIs(lifecycle.phase, ActionPhase.AUTHORIZED)
            network_git.assert_not_called()

    def test_exact_push_argv_uses_bound_http_config_oid_refspec_and_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, base, local, _, _ = _prepare(root, trust)
            authority, lifecycle, _ = _authorize(prepared.proposal)
            calls: list[list[str]] = []

            def fake_run_git(_git_identity, _root, args, **kwargs):
                calls.append(list(args))
                if "ls-remote" in args:
                    return _completed(0, stdout=f"{local}\trefs/heads/main\n".encode("ascii"))
                return _completed(0, stdout=b"ok\n")

            with mock.patch(
                "codexia_manual_agent.git_mutation.https_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.https_push.run_git",
                side_effect=fake_run_git,
            ):
                observation = execute_https_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )

            self.assertIs(observation.outcome, GitMutationOutcome.APPLIED)
            self.assertIs(lifecycle.phase, ActionPhase.OBSERVED)
            push_args = next(args for args in calls if "push" in args)
            rendered = "\n".join(push_args)
            self.assertIn("http.proxy=", rendered)
            self.assertIn("http.sslVerify=true", rendered)
            self.assertFalse(any(arg.startswith("http.sslBackend=") for arg in push_args))
            self.assertIn("http.followRedirects=false", rendered)
            self.assertIn("credential.helper=", rendered)
            self.assertIn("credential.useHttpPath=true", rendered)
            self.assertIn("http.curloptResolve=example.com:443:203.0.113.41", rendered)
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
            prepared, _, local, _, _ = _prepare(root, trust)
            authority, lifecycle, _ = _authorize(prepared.proposal)
            results = iter(
                [
                    _completed(1, stderr=b"lease race\n"),
                    _completed(0, stdout=f"{local}\trefs/heads/main\n".encode("ascii")),
                ]
            )
            with mock.patch(
                "codexia_manual_agent.git_mutation.https_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.https_push.run_git",
                side_effect=lambda *args, **kwargs: next(results),
            ):
                observation = execute_https_network_git_push(
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
            prepared, base, _, _, _ = _prepare(root, trust)
            authority, lifecycle, _ = _authorize(prepared.proposal)
            results = iter(
                [
                    _completed(1, stderr=b"stale info\n"),
                    _completed(0, stdout=f"{base}\trefs/heads/main\n".encode("ascii")),
                ]
            )
            with mock.patch(
                "codexia_manual_agent.git_mutation.https_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.https_push.run_git",
                side_effect=lambda *args, **kwargs: next(results),
            ):
                observation = execute_https_network_git_push(
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
            prepared, base, _, _, _ = _prepare(root, trust)
            authority, lifecycle, _ = _authorize(prepared.proposal)
            results = iter(
                [
                    _completed(0),
                    _completed(0, stdout=f"{base}\trefs/heads/main\n".encode("ascii")),
                ]
            )
            with mock.patch(
                "codexia_manual_agent.git_mutation.https_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.https_push.run_git",
                side_effect=lambda *args, **kwargs: next(results),
            ):
                observation = execute_https_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )
            self.assertIs(observation.outcome, GitMutationOutcome.MISMATCH)

    def test_unobservable_remote_after_consumption_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            prepared, _, _, _, _ = _prepare(root, trust)
            authority, lifecycle, _ = _authorize(prepared.proposal)
            results = iter([_completed(1, stderr=b"network failure\n"), _completed(1)])
            with mock.patch(
                "codexia_manual_agent.git_mutation.https_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ), mock.patch(
                "codexia_manual_agent.git_mutation.https_push.run_git",
                side_effect=lambda *args, **kwargs: next(results),
            ):
                observation = execute_https_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )
            self.assertIs(observation.outcome, GitMutationOutcome.INCOMPLETE)
            self.assertFalse(observation.remote_observation_complete)
            self.assertIs(lifecycle.phase, ActionPhase.OBSERVED)


if __name__ == "__main__":
    unittest.main()
