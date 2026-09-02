from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import (
    GitRepositoryBoundaryError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation import (
    GitMutationOutcome,
    execute_git_commit,
    prepare_git_commit_proposal,
    prepare_git_push_proposal,
)
from codexia_manual_agent.git_mutation.repository import run_git, snapshot_repository


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode != 0:
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


def _direct_commit(root: Path, payload: bytes = b"ahead\n") -> str:
    (root / "tracked.txt").write_bytes(payload)
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "ahead")
    return _oid(root)


def _authorize(proposal):
    authority = LocalApprovalAuthority()
    lifecycle = ActionLifecycle(proposal=proposal, mode=ApprovalMode.ALWAYS)
    receipt = authority.decide(
        proposal,
        mode=ApprovalMode.ALWAYS,
        approved=True,
        actor="test-human",
    )
    lifecycle.apply_receipt(receipt, authority=authority)
    return authority, lifecycle


@unittest.skipUnless(shutil.which("git"), "Git executable is required")
class GitTransportBoundaryTests(unittest.TestCase):
    def test_global_url_rewrite_is_ignored_during_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw, tempfile.TemporaryDirectory() as other_raw, tempfile.TemporaryDirectory() as config_raw:
            root, bare, other = Path(raw), Path(bare_raw), Path(other_raw)
            _init_repo(root)
            _git(bare, "init", "--bare")
            _git(root, "remote", "add", "origin", bare.as_uri())
            _git(root, "push", "-u", "origin", "main")
            _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
            _direct_commit(root)

            global_config = Path(config_raw) / "gitconfig"
            global_config.write_text(
                f'[url "{other.as_uri()}"]\n\tinsteadOf = {bare.as_uri()}\n',
                encoding="utf-8",
                newline="\n",
            )
            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config)}):
                prepared = prepare_git_push_proposal(
                    workspace=root,
                    remote="origin",
                    destination_ref="refs/heads/main",
                )
            self.assertEqual(prepared.approval_preview.remote_url, bare.as_uri())
            self.assertEqual(prepared.approval_preview.backend, "file-pack-update-ref.v1")

    def test_https_remote_is_rejected_before_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            _git(root, "remote", "add", "origin", "https://example.invalid/repo.git")
            with self.assertRaises(InvalidGitMutationError):
                prepare_git_push_proposal(
                    workspace=root,
                    remote="origin",
                    destination_ref="refs/heads/main",
                )

    def test_ssh_network_push_is_deferred_to_m2_5_1(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            _git(root, "remote", "add", "origin", "git@example.invalid:repo.git")
            _direct_commit(root)
            with self.assertRaisesRegex(InvalidGitMutationError, "M2.5.1"):
                prepare_git_push_proposal(
                    workspace=root,
                    remote="origin",
                    destination_ref="refs/heads/main",
                )

    def test_file_push_proposal_binds_exact_pack_and_backend(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            _init_repo(root)
            _git(bare, "init", "--bare")
            _git(root, "remote", "add", "origin", bare.as_uri())
            _git(root, "push", "-u", "origin", "main")
            _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
            _direct_commit(root)

            prepared = prepare_git_push_proposal(
                workspace=root,
                remote="origin",
                destination_ref="refs/heads/main",
            )
            preview = prepared.approval_preview
            self.assertEqual(preview.backend, "file-pack-update-ref.v1")
            self.assertEqual(preview.pack_size_bytes, len(prepared.pack_bytes))
            self.assertEqual(len(preview.pack_sha256), 64)
            self.assertEqual(preview.remote_path, str(bare.resolve()))

    def test_governed_git_runner_suppresses_hooks_and_lazy_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            snapshot = snapshot_repository(root)
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b"",
                stderr=b"",
            )
            with patch.dict(os.environ, {"GIT_NO_LAZY_FETCH": "0"}, clear=False):
                with patch(
                    "codexia_manual_agent.git_mutation.repository.subprocess.run",
                    return_value=completed,
                ) as mocked_run:
                    run_git(snapshot.git, root, ["update-ref", "--stdin"])

            command = mocked_run.call_args.args[0]
            child_env = mocked_run.call_args.kwargs["env"]
            self.assertIn(f"core.hooksPath={os.devnull}", command)
            self.assertIn("hook.reference-transaction.enabled=false", command)
            self.assertEqual(child_env["GIT_NO_LAZY_FETCH"], "1")

    def test_bound_repository_snapshot_ignores_host_path_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as fake_raw:
            root = Path(raw)
            fake_dir = Path(fake_raw)
            _init_repo(root)
            snapshot = snapshot_repository(root)

            fake_git = fake_dir / ("git.exe" if os.name == "nt" else "git")
            fake_git.write_bytes(b"not the approved git executable\n")
            try:
                fake_git.chmod(0o755)
            except OSError:
                pass
            hostile_path = str(fake_dir) + os.pathsep + os.environ.get("PATH", "")
            with patch.dict(os.environ, {"PATH": hostile_path}, clear=False):
                rebound = snapshot_repository(root, git=snapshot.git)

            self.assertEqual(rebound.git, snapshot.git)
            self.assertEqual(rebound.head_oid, snapshot.head_oid)
            self.assertEqual(rebound.head_ref, snapshot.head_ref)

    def test_local_external_object_semantics_are_rejected_during_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            alternates = root / ".git" / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text("C:/unapproved/object-store\n", encoding="utf-8")

            with self.assertRaisesRegex(
                GitRepositoryBoundaryError,
                "external Git object/ancestry",
            ):
                snapshot_repository(root)


@unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Windows Git + TxF required")
class GitCommitPostConsumptionConfigTests(unittest.TestCase):
    def test_post_consumption_commit_config_rewrite_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            (root / "tracked.txt").write_bytes("approved Ω\n".encode("utf-8"))
            _git(root, "add", "tracked.txt")
            prepared = prepare_git_commit_proposal(workspace=root, message="utf8 Ω")
            authority, lifecycle = _authorize(prepared.proposal)
            original_consume = ActionLifecycle.consume_authorization
            blocked: list[bool] = []

            def consume_then_reconfigure(self, *, authority):
                original_consume(self, authority=authority)
                try:
                    with (root / ".git" / "config").open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write("\n[i18n]\n\tcommitEncoding = ISO-8859-1\n")
                except OSError:
                    blocked.append(True)

            with patch.object(ActionLifecycle, "consume_authorization", consume_then_reconfigure):
                observation = execute_git_commit(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )

            self.assertEqual(blocked, [True])
            self.assertIs(observation.outcome, GitMutationOutcome.APPLIED)
            self.assertIs(lifecycle.phase, ActionPhase.OBSERVED)
            self.assertEqual(observation.observed_head_oid, prepared.approval_preview.expected_commit_oid)
            raw_commit = _git(root, "cat-file", "commit", "HEAD").stdout
            self.assertNotIn(b"encoding ", raw_commit)
            self.assertTrue(raw_commit.endswith("utf8 Ω".encode("utf-8")))

    def test_competing_commit_after_consumption_is_cas_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            old_oid = _oid(root)
            base_tree = _oid(root, "HEAD^{tree}")
            competing_oid = _git(
                root,
                "commit-tree",
                base_tree,
                "-p",
                old_oid,
                "-m",
                "competing ref move",
            ).stdout.decode().strip()

            (root / "tracked.txt").write_bytes(b"governed staged bytes\n")
            _git(root, "add", "tracked.txt")
            prepared = prepare_git_commit_proposal(workspace=root, message="governed commit")
            authority, lifecycle = _authorize(prepared.proposal)
            original_consume = ActionLifecycle.consume_authorization

            def consume_then_race(self, *, authority):
                original_consume(self, authority=authority)
                _git(
                    root,
                    "update-ref",
                    prepared.approval_preview.head_ref,
                    competing_oid,
                    old_oid,
                )

            with patch.object(ActionLifecycle, "consume_authorization", consume_then_race):
                observation = execute_git_commit(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )

            self.assertIs(observation.outcome, GitMutationOutcome.REJECTED)
            self.assertEqual(observation.observed_head_oid, competing_oid)
            self.assertEqual(_oid(root, prepared.approval_preview.head_ref), competing_oid)
            self.assertNotEqual(observation.observed_head_oid, prepared.approval_preview.expected_commit_oid)
            self.assertIs(lifecycle.phase, ActionPhase.OBSERVED)


if __name__ == "__main__":
    unittest.main()
