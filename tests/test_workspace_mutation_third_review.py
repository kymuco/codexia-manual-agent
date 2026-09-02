from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.application.mutate_workspace import MutateWorkspaceService
from codexia_manual_agent.mutation import (
    MutationOperation,
    MutationTerminationReason,
    PreimageSnapshot,
    WorkspaceMutationExecutor,
    prepare_create_proposal,
)
from codexia_manual_agent.mutation.parent_anchor import (
    PinnedMutationTarget,
    _StagedFile,
    _linux_replace_from_fd,
)


class ThirdReviewWorkspaceMutationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows CRT binary-mode regression")
    def test_windows_staging_preserves_newline_bytes_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            payload = b"alpha\nbeta\n"
            result = MutateWorkspaceService().run(
                workspace=raw,
                operation=MutationOperation.CREATE,
                target="newlines.txt",
                content=payload,
                approved=True,
            )

            self.assertEqual(Path(raw, "newlines.txt").read_bytes(), payload)
            self.assertTrue(result.observation.applied)
            self.assertEqual(
                result.observation.termination_reason,
                MutationTerminationReason.APPLIED,
            )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux commit helper regression")
    def test_linux_replace_has_no_post_success_commit_link_cleanup(self) -> None:
        staged = _StagedFile(
            fd=17,
            token=None,
            device=11,
            inode=22,
            size_bytes=3,
            sha256="0" * 64,
        )
        identity = SimpleNamespace(st_dev=11, st_ino=22)

        with (
            mock.patch(
                "codexia_manual_agent.mutation.parent_anchor._linux_link_fd"
            ) as link_fd,
            mock.patch(
                "codexia_manual_agent.mutation.parent_anchor.os.stat",
                return_value=identity,
            ),
            mock.patch(
                "codexia_manual_agent.mutation.parent_anchor.os.fstat",
                return_value=identity,
            ),
            mock.patch(
                "codexia_manual_agent.mutation.parent_anchor.os.replace"
            ) as replace,
            mock.patch(
                "codexia_manual_agent.mutation.parent_anchor.os.unlink",
                side_effect=AssertionError("must not cleanup after successful replace"),
            ) as unlink,
        ):
            _linux_replace_from_fd(staged, 9, "target.txt")

        link_fd.assert_called_once()
        replace.assert_called_once()
        unlink.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "M2.3 execution is currently Windows-only")
    def test_precommit_abort_records_staging_cleanup_failure_before_observed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_create_proposal(
                workspace=root,
                target="file.txt",
                content=b"approved",
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(
                proposal,
                mode=ApprovalMode.RISKY,
                approved=True,
            )
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)

            original_capture = PinnedMutationTarget.capture_preimage
            original_discard = PinnedMutationTarget.discard_staged
            capture_calls = 0

            def capture(anchor: PinnedMutationTarget, *, max_bytes: int):
                nonlocal capture_calls
                capture_calls += 1
                if capture_calls == 3:
                    return PreimageSnapshot.present(
                        size_bytes=1,
                        digest="0" * 64,
                        mode=0o644,
                    )
                return original_capture(anchor, max_bytes=max_bytes)

            def discard_then_raise(anchor: PinnedMutationTarget, staged: _StagedFile) -> None:
                original_discard(anchor, staged)
                raise OSError("cleanup blocked")

            with (
                mock.patch.object(PinnedMutationTarget, "capture_preimage", new=capture),
                mock.patch.object(
                    PinnedMutationTarget,
                    "discard_staged",
                    new=discard_then_raise,
                ),
            ):
                observation = WorkspaceMutationExecutor().execute(
                    lifecycle,
                    authority=authority,
                )

            self.assertFalse(observation.applied)
            self.assertEqual(
                observation.termination_reason,
                MutationTerminationReason.TARGET_APPEARED,
            )
            self.assertIn("staging cleanup failed", observation.error or "")
            self.assertIn("cleanup blocked", observation.error or "")
            self.assertFalse((root / "file.txt").exists())
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)


if __name__ == "__main__":
    unittest.main()
