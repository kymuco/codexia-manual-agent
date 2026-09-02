from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.application.mutate_workspace import MutateWorkspaceService
from codexia_manual_agent.domain.errors import (
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation import (
    MutationOperation,
    MutationTerminationReason,
    WorkspaceMutationExecutor,
    prepare_create_proposal,
    prepare_replace_proposal,
)
from codexia_manual_agent.mutation import metadata_executor as metadata_executor_module
from codexia_manual_agent.mutation import workspace as workspace_module
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget


class FifthReviewWorkspaceMutationTests(unittest.TestCase):
    def test_direct_workspace_module_import_uses_secure_executor(self) -> None:
        self.assertIs(workspace_module.WorkspaceMutationExecutor, WorkspaceMutationExecutor)

    def test_direct_legacy_replace_commit_is_always_sealed(self) -> None:
        anchor = PinnedMutationTarget(
            root=Path("."),
            parent=Path("."),
            target_name="target.txt",
        )
        with self.assertRaisesRegex(
            WorkspaceMutationBoundaryError,
            "strict replace requires",
        ):
            anchor.commit_replace(None)

    @unittest.skipIf(os.name == "nt", "Linux/other-host fail-closed regression")
    def test_direct_non_windows_create_commit_is_sealed(self) -> None:
        anchor = PinnedMutationTarget(
            root=Path("."),
            parent=Path("."),
            target_name="target.txt",
        )
        with self.assertRaisesRegex(
            WorkspaceMutationBoundaryError,
            "disabled outside Windows",
        ):
            anchor.commit_create(None)

    @unittest.skipIf(os.name == "nt", "Linux/other-host fail-closed regression")
    def test_non_windows_execution_fails_before_receipt_consumption(self) -> None:
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

            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "currently supported only on Windows",
            ):
                WorkspaceMutationExecutor().execute(lifecycle, authority=authority)

            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertFalse((root / "file.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Windows strict-replace regression")
    def test_replace_target_deleted_before_exact_pin_does_not_consume_or_recreate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "file.txt"
            target.write_bytes(b"old")
            proposal = prepare_replace_proposal(
                workspace=root,
                target="file.txt",
                content=b"new",
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(
                proposal,
                mode=ApprovalMode.RISKY,
                approved=True,
            )
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)
            original_pin = metadata_executor_module._win_pin_exact_replace_target

            def delete_then_pin(transaction, path: Path, *, max_bytes: int):
                path.unlink()
                return original_pin(transaction, path, max_bytes=max_bytes)

            with mock.patch.object(
                metadata_executor_module,
                "_win_pin_exact_replace_target",
                side_effect=delete_then_pin,
            ):
                with self.assertRaises(WorkspaceMutationPreimageChangedError):
                    WorkspaceMutationExecutor().execute(lifecycle, authority=authority)

            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertFalse(target.exists())

    @unittest.skipUnless(os.name == "nt", "Windows exclusive-target-pin regression")
    def test_pinned_replace_target_cannot_be_deleted_before_transacted_move(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "file.txt"
            target.write_bytes(b"old")
            original_pin = metadata_executor_module._win_pin_exact_replace_target
            attempted_delete = False

            def pin_then_try_delete(transaction, path: Path, *, max_bytes: int):
                nonlocal attempted_delete
                pin = original_pin(transaction, path, max_bytes=max_bytes)
                self.assertIsNotNone(pin)
                attempted_delete = True
                with self.assertRaises(OSError):
                    path.unlink()
                return pin

            with mock.patch.object(
                metadata_executor_module,
                "_win_pin_exact_replace_target",
                side_effect=pin_then_try_delete,
            ):
                result = MutateWorkspaceService().run(
                    workspace=root,
                    operation=MutationOperation.REPLACE,
                    target="file.txt",
                    content=b"new",
                    approved=True,
                )

            self.assertTrue(attempted_delete)
            self.assertTrue(result.observation.applied)
            self.assertEqual(
                result.observation.termination_reason,
                MutationTerminationReason.APPLIED,
            )
            self.assertEqual(target.read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main()
