from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.authority import ActionLifecycle, ApprovalMode, LocalApprovalAuthority
from codexia_manual_agent.git_mutation import (
    GitMutationOutcome,
    execute_git_commit,
    execute_git_push,
    prepare_git_commit_proposal,
    prepare_git_push_proposal,
)
from codexia_manual_agent.git_mutation import commit as commit_module
from codexia_manual_agent.git_mutation import push as push_module


class _NoopPin:
    def close(self) -> None:
        return None


def _git(
    root: Path,
    *args: str,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_bytes,
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
class GitMutationReadyReviewRegressions(unittest.TestCase):
    def test_push_accepts_existing_branch_stored_only_in_packed_refs(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            _init_repo(root)
            _git(bare, "init", "--bare")
            _git(root, "remote", "add", "origin", bare.as_uri())
            _git(root, "push", "origin", "main")
            old_remote_oid = _oid(bare, "refs/heads/main")
            _git(bare, "pack-refs", "--all", "--prune")
            self.assertFalse((bare / "refs" / "heads" / "main").exists())
            self.assertTrue((bare / "packed-refs").is_file())

            local_oid = _direct_commit(root, b"ahead\n")
            prepared = prepare_git_push_proposal(
                workspace=root,
                remote="origin",
                destination_ref="refs/heads/main",
            )

            self.assertEqual(prepared.approval_preview.expected_remote_oid, old_remote_oid)
            self.assertEqual(prepared.approval_preview.local_oid, local_oid)

    def test_commit_nonzero_cas_with_exact_target_observed_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _init_repo(root)
            (root / "tracked.txt").write_bytes(b"approved\n")
            _git(root, "add", "tracked.txt")
            prepared = prepare_git_commit_proposal(workspace=root, message="target race")
            params = prepared.proposal.to_dict()["parameters"]
            _git(root, "index-pack", "--stdin", input_bytes=prepared.pack_bytes)
            authority, lifecycle = _authorize(prepared.proposal)
            original_consume = ActionLifecycle.consume_authorization

            def consume_then_apply_target(self, *, authority):
                original_consume(self, authority=authority)
                _git(
                    root,
                    "update-ref",
                    params["head_ref"],
                    params["expected_commit_oid"],
                    params["head_oid"],
                )

            with (
                patch.object(ActionLifecycle, "consume_authorization", consume_then_apply_target),
                patch.object(
                    commit_module.WindowsGitNamespacePin,
                    "acquire",
                    return_value=_NoopPin(),
                ),
            ):
                observation = execute_git_commit(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )

            self.assertIs(observation.outcome, GitMutationOutcome.APPLIED)
            self.assertEqual(observation.observed_head_oid, params["expected_commit_oid"])
            self.assertEqual(_oid(root, params["head_ref"]), params["expected_commit_oid"])

    def test_push_nonzero_cas_with_exact_target_observed_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as bare_raw:
            root, bare = Path(raw), Path(bare_raw)
            _init_repo(root)
            _git(bare, "init", "--bare")
            _git(root, "remote", "add", "origin", bare.as_uri())
            _git(root, "push", "origin", "main")
            local_oid = _direct_commit(root, b"ahead\n")
            prepared = prepare_git_push_proposal(
                workspace=root,
                remote="origin",
                destination_ref="refs/heads/main",
            )
            params = prepared.proposal.to_dict()["parameters"]
            _git(bare, "index-pack", "--stdin", input_bytes=prepared.pack_bytes)
            authority, lifecycle = _authorize(prepared.proposal)
            original_consume = ActionLifecycle.consume_authorization

            def consume_then_apply_target(self, *, authority):
                original_consume(self, authority=authority)
                _git(
                    bare,
                    "update-ref",
                    params["destination_ref"],
                    params["local_oid"],
                    params["expected_remote_oid"],
                )

            with (
                patch.object(ActionLifecycle, "consume_authorization", consume_then_apply_target),
                patch.object(
                    push_module.WindowsGitNamespacePin,
                    "acquire",
                    return_value=_NoopPin(),
                ),
            ):
                observation = execute_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )

            self.assertIs(observation.outcome, GitMutationOutcome.APPLIED)
            self.assertEqual(observation.observed_remote_oid, local_oid)
            self.assertEqual(_oid(bare, params["destination_ref"]), local_oid)


if __name__ == "__main__":
    unittest.main()
