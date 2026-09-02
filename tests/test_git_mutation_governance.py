from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    ApprovalMode,
    ApprovalRequirement,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    AuthorizationMismatchError,
    GitMutationPreconditionChangedError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation import (
    GIT_COMMIT_ACTION,
    GIT_PUSH_ACTION,
    GitCommitPreparation,
    GitMutationOutcome,
    GitPushPreparation,
    execute_git_commit,
    execute_git_push,
    prepare_git_commit_proposal,
    prepare_git_push_proposal,
)
from codexia_manual_agent.git_mutation.windows_namespace import WindowsGitNamespacePin


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


def _stage_change(root: Path, payload: bytes = b"staged\n") -> None:
    (root / "tracked.txt").write_bytes(payload)
    _git(root, "add", "tracked.txt")


def _direct_commit(root: Path, payload: bytes = b"local\n") -> str:
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


@unittest.skipUnless(shutil.which("git"), "Git executable is required")
class GitCommitGovernanceTests(unittest.TestCase):
    def test_commit_policy_is_independent_human_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            _stage_change(root)
            prepared = prepare_git_commit_proposal(workspace=root, message="governed")
            self.assertIs(prepared.proposal.capability, Capability.GIT_COMMIT)
            self.assertEqual(prepared.proposal.action, GIT_COMMIT_ACTION)
            authority = LocalApprovalAuthority()
            for mode, expected in (
                (ApprovalMode.ALWAYS, ApprovalRequirement.REQUIRE_HUMAN),
                (ApprovalMode.RISKY, ApprovalRequirement.REQUIRE_HUMAN),
                (ApprovalMode.NEVER, ApprovalRequirement.DENY),
            ):
                with self.subTest(mode=mode.value):
                    self.assertIs(authority.requirement(prepared.proposal, mode), expected)

    def test_host_git_control_environment_cannot_redirect_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as other_raw:
            root, other = Path(raw), Path(other_raw)
            _init_repo(root)
            _init_repo(other)
            _stage_change(root)
            with patch.dict(
                os.environ,
                {
                    "GIT_DIR": str(other / ".git"),
                    "GIT_WORK_TREE": str(other),
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "user.name",
                    "GIT_CONFIG_VALUE_0": "Injected Name",
                },
                clear=False,
            ):
                prepared = prepare_git_commit_proposal(workspace=root, message="env safe")
            self.assertEqual(prepared.proposal.workspace_root, str(root.resolve()))
            self.assertEqual(prepared.approval_preview.head_oid, _oid(root))
            self.assertEqual(prepared.approval_preview.author_name, "Codexia Test")

    def test_commit_preview_binds_exact_index_diff_object_pack_message_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            _stage_change(root)
            prepared = prepare_git_commit_proposal(workspace=root, message="exact message")
            preview = prepared.approval_preview
            self.assertEqual(preview.head_oid, _oid(root))
            self.assertEqual(preview.head_ref, "refs/heads/main")
            self.assertIn("tracked.txt", preview.staged_diff)
            self.assertEqual(preview.message, "exact message")
            self.assertTrue(preview.commit_timestamp.endswith("+00:00"))
            self.assertTrue(preview.expected_tree_oid)
            self.assertTrue(preview.expected_commit_oid)
            self.assertEqual(preview.pack_size_bytes, len(prepared.pack_bytes))
            self.assertTrue(preview.pack_sha256)
            self.assertTrue(preview.requires_human)

    def test_commit_without_staged_changes_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            with self.assertRaisesRegex(InvalidGitMutationError, "no staged changes"):
                prepare_git_commit_proposal(workspace=root, message="nothing")

    def test_tampered_commit_review_surface_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            _stage_change(root)
            prepared = prepare_git_commit_proposal(workspace=root, message="real")
            forged = GitCommitPreparation(
                prepared.proposal,
                replace(prepared.approval_preview, staged_diff="harmless\n"),
                prepared.pack_bytes,
            )
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            with self.assertRaisesRegex(InvalidGitMutationError, "Displayed Git commit preview"):
                execute_git_commit(forged, lifecycle=lifecycle, authority=authority)
            self.assertFalse(authority.is_consumed(receipt))
            self.assertIs(lifecycle.phase, ActionPhase.AUTHORIZED)

    def test_index_drift_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            _stage_change(root, b"first\n")
            prepared = prepare_git_commit_proposal(workspace=root, message="first")
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            _stage_change(root, b"second\n")
            with self.assertRaisesRegex(GitMutationPreconditionChangedError, "staged index changed"):
                execute_git_commit(prepared, lifecycle=lifecycle, authority=authority)
            self.assertFalse(authority.is_consumed(receipt))
            self.assertIs(lifecycle.phase, ActionPhase.AUTHORIZED)

    def test_head_drift_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            _stage_change(root, b"governed\n")
            prepared = prepare_git_commit_proposal(workspace=root, message="governed")
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            (root / "other.txt").write_bytes(b"other\n")
            _git(root, "add", "other.txt")
            _git(root, "commit", "--no-gpg-sign", "-m", "racing commit")
            with self.assertRaisesRegex(GitMutationPreconditionChangedError, "HEAD/ref"):
                execute_git_commit(prepared, lifecycle=lifecycle, authority=authority)
            self.assertFalse(authority.is_consumed(receipt))

    @unittest.skipUnless(os.name == "nt", "M2.5 execution boundary is Windows TxF")
    def test_commit_uses_frozen_pack_not_later_worktree_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            _stage_change(root, b"approved staged bytes\n")
            prepared = prepare_git_commit_proposal(workspace=root, message="frozen index")
            authority, lifecycle, _ = _authorize(prepared.proposal)
            (root / "tracked.txt").write_bytes(b"later unstaged bytes\n")
            observation = execute_git_commit(prepared, lifecycle=lifecycle, authority=authority)
            self.assertIs(observation.outcome, GitMutationOutcome.APPLIED)
            self.assertIs(lifecycle.phase, ActionPhase.OBSERVED)
            self.assertEqual(observation.observed_head_oid, prepared.approval_preview.expected_commit_oid)
            self.assertEqual(_git(root, "show", "HEAD:tracked.txt").stdout, b"approved staged bytes\n")
            self.assertEqual((root / "tracked.txt").read_bytes(), b"later unstaged bytes\n")
            self.assertEqual(
                _git(root, "show", "-s", "--format=%B", "HEAD").stdout.decode().strip(),
                "frozen index",
            )

    def test_commit_receipt_cannot_authorize_push_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as remote_raw:
            root, remote = Path(raw), Path(remote_raw)
            _init_repo(root)
            _git(remote, "init", "--bare")
            _git(root, "remote", "add", "origin", remote.as_uri())
            _git(root, "push", "-u", "origin", "main")
            _direct_commit(root, b"ahead\n")
            _stage_change(root, b"staged after commit\n")
            commit = prepare_git_commit_proposal(workspace=root, message="commit")
            authority = LocalApprovalAuthority()
            commit_receipt = authority.decide(
                commit.proposal,
                mode=ApprovalMode.ALWAYS,
                approved=True,
                actor="test-human",
            )
            push = prepare_git_push_proposal(
                workspace=root,
                remote="origin",
                destination_ref="refs/heads/main",
            )
            push_lifecycle = ActionLifecycle(push.proposal, ApprovalMode.ALWAYS)
            with self.assertRaises(AuthorizationMismatchError):
                push_lifecycle.apply_receipt(commit_receipt, authority=authority)


@unittest.skipUnless(shutil.which("git"), "Git executable is required")
class GitPushGovernanceTests(unittest.TestCase):
    def _repo_with_remote(self, root: Path, bare: Path) -> None:
        _init_repo(root)
        _git(bare, "init", "--bare")
        _git(root, "remote", "add", "origin", bare.as_uri())
        _git(root, "push", "-u", "origin", "main")
        _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")

    def test_push_policy_and_preview_bind_exact_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            self._repo_with_remote(root, bare)
            local_oid = _direct_commit(root)
            prepared = prepare_git_push_proposal(workspace=root, remote="origin", destination_ref="refs/heads/main")
            preview = prepared.approval_preview
            self.assertIs(prepared.proposal.capability, Capability.GIT_PUSH)
            self.assertEqual(prepared.proposal.action, GIT_PUSH_ACTION)
            self.assertEqual(preview.local_oid, local_oid)
            self.assertEqual(preview.remote_url, bare.as_uri())
            self.assertEqual(preview.destination_ref, "refs/heads/main")
            self.assertEqual(preview.pack_size_bytes, len(prepared.pack_bytes))
            authority = LocalApprovalAuthority()
            for mode, expected in (
                (ApprovalMode.ALWAYS, ApprovalRequirement.REQUIRE_HUMAN),
                (ApprovalMode.RISKY, ApprovalRequirement.REQUIRE_HUMAN),
                (ApprovalMode.NEVER, ApprovalRequirement.DENY),
            ):
                with self.subTest(mode=mode.value):
                    self.assertIs(authority.requirement(prepared.proposal, mode), expected)

    def test_push_rejects_non_branch_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            self._repo_with_remote(root, bare)
            _direct_commit(root)
            with self.assertRaises(InvalidGitMutationError):
                prepare_git_push_proposal(workspace=root, remote="origin", destination_ref="refs/tags/v1")

    def test_push_rejects_noop_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            self._repo_with_remote(root, bare)
            with self.assertRaisesRegex(InvalidGitMutationError, "no-op"):
                prepare_git_push_proposal(workspace=root, remote="origin", destination_ref="refs/heads/main")

    def test_tampered_push_review_surface_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            self._repo_with_remote(root, bare)
            _direct_commit(root)
            prepared = prepare_git_push_proposal(workspace=root, remote="origin", destination_ref="refs/heads/main")
            forged = GitPushPreparation(
                prepared.proposal,
                replace(prepared.approval_preview, remote_url="file:///tampered"),
                prepared.pack_bytes,
            )
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            with self.assertRaisesRegex(InvalidGitMutationError, "Displayed Git push preview"):
                execute_git_push(forged, lifecycle=lifecycle, authority=authority)
            self.assertFalse(authority.is_consumed(receipt))

    def test_push_head_drift_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            self._repo_with_remote(root, bare)
            _direct_commit(root, b"one\n")
            prepared = prepare_git_push_proposal(workspace=root, remote="origin", destination_ref="refs/heads/main")
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            _direct_commit(root, b"two\n")
            with self.assertRaisesRegex(GitMutationPreconditionChangedError, "local ref/object changed"):
                execute_git_push(prepared, lifecycle=lifecycle, authority=authority)
            self.assertFalse(authority.is_consumed(receipt))

    def test_push_remote_url_drift_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw, tempfile.TemporaryDirectory() as other_raw:
            root, bare, other = Path(raw), Path(bare_raw), Path(other_raw)
            self._repo_with_remote(root, bare)
            _git(other, "init", "--bare")
            _direct_commit(root)
            prepared = prepare_git_push_proposal(workspace=root, remote="origin", destination_ref="refs/heads/main")
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            _git(root, "remote", "set-url", "--push", "origin", other.as_uri())
            with self.assertRaises(GitMutationPreconditionChangedError):
                execute_git_push(prepared, lifecycle=lifecycle, authority=authority)
            self.assertFalse(authority.is_consumed(receipt))

    def test_push_other_local_config_drift_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            self._repo_with_remote(root, bare)
            _direct_commit(root)
            prepared = prepare_git_push_proposal(workspace=root, remote="origin", destination_ref="refs/heads/main")
            authority, lifecycle, receipt = _authorize(prepared.proposal)
            _git(root, "config", "codexia.race", "changed")
            with self.assertRaisesRegex(GitMutationPreconditionChangedError, "Local Git config changed"):
                execute_git_push(prepared, lifecycle=lifecycle, authority=authority)
            self.assertFalse(authority.is_consumed(receipt))

    @unittest.skipUnless(os.name == "nt", "M2.5 execution boundary is Windows TxF")
    def test_push_applies_exact_oid_to_local_bare_remote(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            self._repo_with_remote(root, bare)
            local_oid = _direct_commit(root)
            prepared = prepare_git_push_proposal(workspace=root, remote="origin", destination_ref="refs/heads/main")
            authority, lifecycle, _ = _authorize(prepared.proposal)
            observation = execute_git_push(prepared, lifecycle=lifecycle, authority=authority)
            self.assertIs(observation.outcome, GitMutationOutcome.APPLIED)
            self.assertEqual(observation.observed_remote_oid, local_oid)
            self.assertEqual(_oid(bare, "refs/heads/main"), local_oid)
            self.assertIs(lifecycle.phase, ActionPhase.OBSERVED)

    @unittest.skipUnless(os.name == "nt", "M2.5 execution boundary is Windows TxF")
    def test_remote_config_rewrite_after_consumption_is_physically_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            self._repo_with_remote(root, bare)
            local_oid = _direct_commit(root, b"approved\n")
            prepared = prepare_git_push_proposal(workspace=root, remote="origin", destination_ref="refs/heads/main")
            authority, lifecycle, _ = _authorize(prepared.proposal)
            original_consume = ActionLifecycle.consume_authorization
            blocked: list[bool] = []

            def consume_then_rewrite(self, *, authority):
                original_consume(self, authority=authority)
                try:
                    with (bare / "config").open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write("\n[codexia]\n\trace = changed\n")
                except OSError:
                    blocked.append(True)

            with patch.object(ActionLifecycle, "consume_authorization", consume_then_rewrite):
                observation = execute_git_push(prepared, lifecycle=lifecycle, authority=authority)

            self.assertEqual(blocked, [True])
            self.assertIs(observation.outcome, GitMutationOutcome.APPLIED)
            self.assertEqual(_oid(bare, "refs/heads/main"), local_oid)

    @unittest.skipUnless(os.name == "nt", "M2.5 execution boundary is Windows TxF")
    def test_remote_drift_after_consumption_is_cas_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw, tempfile.TemporaryDirectory() as racer_raw:
            root, bare, racer = Path(raw), Path(bare_raw), Path(racer_raw)
            self._repo_with_remote(root, bare)
            local_oid = _direct_commit(root, b"local ahead\n")
            prepared = prepare_git_push_proposal(workspace=root, remote="origin", destination_ref="refs/heads/main")
            authority, lifecycle, _ = _authorize(prepared.proposal)

            _git(racer.parent, "clone", bare.as_uri(), str(racer))
            _git(racer, "config", "user.name", "Racer")
            _git(racer, "config", "user.email", "racer@example.invalid")
            (racer / "racer.txt").write_bytes(b"racer\n")
            _git(racer, "add", "racer.txt")
            _git(racer, "commit", "--no-gpg-sign", "-m", "racer")
            original_consume = ActionLifecycle.consume_authorization
            competing_oid: list[str] = []

            def consume_then_race(self, *, authority):
                original_consume(self, authority=authority)
                _git(racer, "push", "origin", "main")
                competing_oid.append(_oid(bare, "refs/heads/main"))

            with patch.object(ActionLifecycle, "consume_authorization", consume_then_race):
                observation = execute_git_push(prepared, lifecycle=lifecycle, authority=authority)

            self.assertIs(observation.outcome, GitMutationOutcome.REJECTED)
            self.assertEqual(observation.observed_remote_oid, competing_oid[0])
            self.assertNotEqual(observation.observed_remote_oid, local_oid)
            self.assertEqual(_oid(bare, "refs/heads/main"), competing_oid[0])
            self.assertIs(lifecycle.phase, ActionPhase.OBSERVED)


@unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Windows Git + TxF required")
class GitNamespacePinTests(unittest.TestCase):
    def test_txf_namespace_pin_blocks_repository_directory_rename(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            git_dir = root / ".git"
            moved = root / ".git-moved"
            pin = WindowsGitNamespacePin.acquire((git_dir, git_dir / "objects"))
            try:
                with self.assertRaises(OSError):
                    os.replace(git_dir, moved)
            finally:
                cleanup = pin.close()
            self.assertIsNone(cleanup)
            self.assertTrue(git_dir.is_dir())
            self.assertFalse(moved.exists())


if __name__ == "__main__":
    unittest.main()
