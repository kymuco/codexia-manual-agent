from __future__ import annotations

import os
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchFileRequest,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation.patch_execution_plan import (
    PATCH_EXECUTION_BACKEND,
    PATCH_EXECUTION_PLATFORM,
    build_patch_execution_plan,
    preflight_patch_execution_plan,
    revalidate_patch_execution_plan,
    validate_patch_execution_plan_binding,
)
from codexia_manual_agent.mutation.workspace import (
    CREATE_ACTION,
    REPLACE_ACTION,
    _validate_proposal,
)


class PatchExecutionPlanTests(unittest.TestCase):
    def test_revalidation_builds_exact_sorted_m23_plan_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = root / "old.py"
            old.write_bytes(b"print('old')\n")
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(
                        MutationOperation.REPLACE,
                        "old.py",
                        b"print('changed')\n",
                    ),
                    PatchFileRequest(
                        MutationOperation.CREATE,
                        "new.py",
                        b"print('new')\n",
                    ),
                ),
            )

            plan = build_patch_execution_plan(proposal)
            revalidate_patch_execution_plan(proposal, plan)

            self.assertEqual(plan.proposal_id, proposal.proposal_id)
            self.assertEqual(plan.proposal_digest, proposal.proposal_digest)
            self.assertEqual(plan.change_set_digest, proposal.parameters["change_set_digest"])
            self.assertEqual(plan.backend, PATCH_EXECUTION_BACKEND)
            self.assertEqual(plan.execution_platform, PATCH_EXECUTION_PLATFORM)
            self.assertEqual([step.target for step in plan.steps], ["new.py", "old.py"])
            self.assertEqual(
                [step.action for step in plan.steps],
                [CREATE_ACTION, REPLACE_ACTION],
            )

            # Every step serializes exactly into the accepted M2.3 primitive schema.
            for step in plan.steps:
                primitive = ActionProposal.create(
                    capability=Capability.WRITE_WORKSPACE,
                    action=step.action,
                    workspace_root=plan.workspace_root,
                    parameters=step.m23_parameters(),
                    summary="M2.4.2 schema-validation probe",
                )
                parsed = _validate_proposal(primitive)
                self.assertEqual(parsed.target, step.target)
                self.assertEqual(parsed.operation, step.operation)
                self.assertEqual(parsed.expected_preimage, step.expected_preimage)
                self.assertEqual(parsed.postimage, step.postimage)

            # M2.4.2 is read-only: it plans but does not apply either file change.
            self.assertEqual(old.read_bytes(), b"print('old')\n")
            self.assertFalse((root / "new.py").exists())

    def test_plan_is_deterministic_for_one_proposal_and_bound_to_proposal_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "old.py").write_bytes(b"old\n")
            changes = (
                PatchFileRequest(MutationOperation.REPLACE, "old.py", b"new\n"),
            )
            first_proposal = prepare_patch_proposal(workspace=root, changes=changes)

            first = build_patch_execution_plan(first_proposal)
            second = build_patch_execution_plan(first_proposal)
            revalidate_patch_execution_plan(first_proposal, first)
            revalidate_patch_execution_plan(first_proposal, second)
            self.assertEqual(first.plan_digest, second.plan_digest)
            self.assertEqual(first.to_dict(), second.to_dict())

            second_proposal = prepare_patch_proposal(workspace=root, changes=changes)
            same_change_set = build_patch_execution_plan(second_proposal)
            revalidate_patch_execution_plan(second_proposal, same_change_set)
            self.assertEqual(first.change_set_digest, same_change_set.change_set_digest)
            self.assertNotEqual(first.proposal_id, same_change_set.proposal_id)
            self.assertNotEqual(first.plan_digest, same_change_set.plan_digest)

    def test_replace_content_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "old.py"
            target.write_bytes(b"old\n")
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.REPLACE, "old.py", b"new\n"),
                ),
            )
            target.write_bytes(b"raced\n")

            with self.assertRaisesRegex(
                WorkspaceMutationPreimageChangedError,
                "pre-authority revalidation",
            ):
                revalidate_patch_execution_plan(proposal, build_patch_execution_plan(proposal))

            self.assertEqual(target.read_bytes(), b"raced\n")

    def test_create_target_appearance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.py", b"new\n"),
                ),
            )
            (root / "new.py").write_bytes(b"foreign\n")

            with self.assertRaisesRegex(
                WorkspaceMutationPreimageChangedError,
                "live preimage drift",
            ):
                revalidate_patch_execution_plan(proposal, build_patch_execution_plan(proposal))

    def test_replace_target_disappearance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "old.py"
            target.write_bytes(b"old\n")
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.REPLACE, "old.py", b"new\n"),
                ),
            )
            target.unlink()

            with self.assertRaisesRegex(
                WorkspaceMutationPreimageChangedError,
                "live preimage drift",
            ):
                revalidate_patch_execution_plan(proposal, build_patch_execution_plan(proposal))

    @unittest.skipIf(os.name == "nt", "POSIX permission-mode regression")
    def test_replace_mode_drift_is_part_of_complete_preimage_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "old.py"
            target.write_bytes(b"old\n")
            target.chmod(0o600)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.REPLACE, "old.py", b"new\n"),
                ),
            )
            target.chmod(0o644)

            with self.assertRaisesRegex(
                WorkspaceMutationPreimageChangedError,
                "preimage identity changed",
            ):
                revalidate_patch_execution_plan(proposal, build_patch_execution_plan(proposal))



    def test_plan_digest_rejects_step_payload_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.py", b"new\n"),
                ),
            )
            plan = build_patch_execution_plan(proposal)
            tampered_step = replace(plan.steps[0], postimage=b"other\n")

            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "primitive digest",
            ):
                replace(plan, steps=(tampered_step,))

    def test_plan_binding_cannot_transfer_between_distinct_patch_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            changes = (
                PatchFileRequest(MutationOperation.CREATE, "new.py", b"new\n"),
            )
            first_proposal = prepare_patch_proposal(workspace=root, changes=changes)
            second_proposal = prepare_patch_proposal(workspace=root, changes=changes)
            first_plan = build_patch_execution_plan(first_proposal)
            revalidate_patch_execution_plan(first_proposal, first_plan)

            validate_patch_execution_plan_binding(first_proposal, first_plan)
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "not bound to this exact patch proposal",
            ):
                validate_patch_execution_plan_binding(second_proposal, first_plan)

    def test_execution_preflight_fails_closed_outside_windows(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.py", b"new\n"),
                ),
            )
            plan = build_patch_execution_plan(proposal)

            with patch(
                "codexia_manual_agent.mutation.patch_execution_plan._is_windows_host",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "disabled outside Windows",
                ):
                    preflight_patch_execution_plan(proposal, plan)

    def test_execution_preflight_checks_every_windows_target_and_txf_replace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "old.py").write_bytes(b"old\n")
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.py", b"new\n"),
                    PatchFileRequest(MutationOperation.REPLACE, "old.py", b"changed\n"),
                ),
            )
            plan = build_patch_execution_plan(proposal)

            with (
                patch(
                    "codexia_manual_agent.mutation.patch_execution_plan._is_windows_host",
                    return_value=True,
                ),
                patch(
                    "codexia_manual_agent.mutation.patch_execution_plan.validate_windows_relative_target"
                ) as validate_target,
                patch(
                    "codexia_manual_agent.mutation.patch_execution_plan._require_windows_strict_replace_support",
                    return_value="NTFS",
                ) as require_txf,
            ):
                preflight_patch_execution_plan(proposal, plan)

            revalidate_patch_execution_plan(proposal, plan)
            self.assertEqual(
                validate_target.call_args_list,
                [call("new.py"), call("old.py")],
            )
            require_txf.assert_called_once_with(root.resolve() / "old.py")

    @unittest.skipIf(os.name == "nt", "POSIX symlink namespace regression")
    def test_parent_symlink_namespace_drift_stays_a_boundary_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            parent = root / "pkg"
            parent.mkdir()
            (parent / "old.py").write_bytes(b"old\n")
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(
                        MutationOperation.REPLACE,
                        "pkg/old.py",
                        b"new\n",
                    ),
                ),
            )
            moved = root / "pkg_real"
            parent.rename(moved)
            (root / "pkg").symlink_to(moved, target_is_directory=True)

            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "pre-authority namespace revalidation",
            ):
                revalidate_patch_execution_plan(proposal, build_patch_execution_plan(proposal))

    def test_drift_failure_does_not_consume_an_existing_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.py", b"new\n"),
                ),
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(
                proposal,
                mode=ApprovalMode.RISKY,
                approved=True,
            )
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)
            (root / "new.py").write_bytes(b"raced\n")

            with self.assertRaises(WorkspaceMutationPreimageChangedError):
                revalidate_patch_execution_plan(
                    lifecycle.proposal,
                    build_patch_execution_plan(lifecycle.proposal),
                )

            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertEqual((root / "new.py").read_bytes(), b"raced\n")

    def test_revalidation_never_consumes_an_existing_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.py", b"new\n"),
                ),
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(
                proposal,
                mode=ApprovalMode.RISKY,
                approved=True,
            )
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)

            plan = build_patch_execution_plan(lifecycle.proposal)
            revalidate_patch_execution_plan(lifecycle.proposal, plan)

            self.assertEqual(plan.proposal_id, proposal.proposal_id)
            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertFalse((root / "new.py").exists())


if __name__ == "__main__":
    unittest.main()
