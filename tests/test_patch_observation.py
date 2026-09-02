from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ApprovalMode,
    AuthorizationDecision,
    AuthorizationReceipt,
    AuthorizationSource,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import (
    InvalidActionTransitionError,
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
)
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchApplicationExecutor,
    PatchApplicationResult,
    PatchCommitState,
    PatchFailureStage,
    PatchFileRequest,
    build_patch_execution_plan,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation import patch_application as patch_application_module
from codexia_manual_agent.mutation import patch_observation as patch_observation_module
from codexia_manual_agent.mutation.patch_observation import (
    PatchFileObservationStatus,
    PatchMutationObserver,
    PatchTerminalExpectation,
    PatchVerificationOutcome,
    validate_patch_mutation_receipt_binding,
)


def _authorized_patch(root: Path, changes: tuple[PatchFileRequest, ...]):
    proposal = prepare_patch_proposal(workspace=root, changes=changes)
    authority = LocalApprovalAuthority()
    receipt = authority.decide(
        proposal,
        mode=ApprovalMode.RISKY,
        approved=True,
    )
    lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
    lifecycle.apply_receipt(receipt, authority=authority)
    return proposal, authority, receipt, lifecycle, build_patch_execution_plan(proposal)


def _manual_executed_patch(root: Path):
    proposal, authority, receipt, lifecycle, plan = _authorized_patch(
        root,
        (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
    )
    lifecycle.consume_authorization(authority=authority)
    lifecycle.record_executed("manual-execution")
    result = PatchApplicationResult(
        schema_version=1,
        execution_id=lifecycle.execution_id,
        proposal_id=plan.proposal_id,
        proposal_digest=plan.proposal_digest,
        change_set_digest=plan.change_set_digest,
        plan_digest=plan.plan_digest,
        commit_model="windows.txf.single_transaction.v1",
        commit_state=PatchCommitState.COMMITTED,
    )
    return proposal, authority, receipt, lifecycle, plan, result


class PatchMutationObservationContractTests(unittest.TestCase):
    def test_non_windows_observation_fails_without_advancing_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, _, lifecycle, plan, result = _manual_executed_patch(root)
            with patch.object(
                patch_observation_module,
                "_is_windows_host",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "enabled only on the supported Windows mutation boundary",
                ):
                    PatchMutationObserver().observe(lifecycle, plan, result)
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
            self.assertIsNone(lifecycle.observation_id)

    def test_indeterminate_application_result_cannot_become_terminal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, _, lifecycle, plan, _ = _manual_executed_patch(root)
            result = PatchApplicationResult(
                schema_version=1,
                execution_id=lifecycle.execution_id,
                proposal_id=plan.proposal_id,
                proposal_digest=plan.proposal_digest,
                change_set_digest=plan.change_set_digest,
                plan_digest=plan.plan_digest,
                commit_model="windows.txf.single_transaction.v1",
                commit_state=PatchCommitState.INDETERMINATE,
                failure_stage=PatchFailureStage.ROLLBACK,
                error="rollback outcome unresolved",
            )
            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "M2.4.5 recovery is required",
            ):
                PatchMutationObserver().observe(lifecycle, plan, result)
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
            self.assertIsNone(lifecycle.observation_id)

    def test_application_result_must_match_exact_lifecycle_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, _, lifecycle, plan, result = _manual_executed_patch(root)
            wrong = PatchApplicationResult(
                schema_version=1,
                execution_id="different-execution",
                proposal_id=result.proposal_id,
                proposal_digest=result.proposal_digest,
                change_set_digest=result.change_set_digest,
                plan_digest=result.plan_digest,
                commit_model=result.commit_model,
                commit_state=result.commit_state,
            )
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "not bound to this executed lifecycle and plan",
            ):
                PatchMutationObserver().observe(lifecycle, plan, wrong)
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)

    def test_observation_rejects_replaced_unconsumed_authorization_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, _, lifecycle, plan, result = _manual_executed_patch(root)
            replacement = AuthorizationReceipt.issue(
                proposal=lifecycle.proposal,
                decision=AuthorizationDecision.ALLOW,
                mode=lifecycle.mode,
                source=AuthorizationSource.HUMAN,
                actor="test-replacement",
            )
            lifecycle.authorization = replacement
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "exact consumed authorization receipt",
            ):
                PatchMutationObserver().observe(lifecycle, plan, result)
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
            self.assertIsNone(lifecycle.observation_id)

    def test_observation_requires_executed_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, _, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            fake = PatchApplicationResult(
                schema_version=1,
                execution_id="execution",
                proposal_id=plan.proposal_id,
                proposal_digest=plan.proposal_digest,
                change_set_digest=plan.change_set_digest,
                plan_digest=plan.plan_digest,
                commit_model="windows.txf.single_transaction.v1",
                commit_state=PatchCommitState.COMMITTED,
            )
            with self.assertRaisesRegex(
                InvalidActionTransitionError,
                "requires EXECUTED lifecycle",
            ):
                PatchMutationObserver().observe(lifecycle, plan, fake)


@unittest.skipUnless(os.name == "nt", "M2.4.4 real mutation observation requires Windows")
class PatchMutationObservationWindowsTests(unittest.TestCase):
    def test_committed_mixed_patch_produces_exact_per_file_and_set_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = root / "old.txt"
            old.write_bytes(b"old\n")
            proposal, authority, auth_receipt, lifecycle, plan = _authorized_patch(
                root,
                (
                    PatchFileRequest(MutationOperation.REPLACE, "old.txt", b"changed\n"),
                    PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),
                ),
            )
            application = PatchApplicationExecutor().execute(
                lifecycle,
                plan,
                authority=authority,
            )
            receipt = PatchMutationObserver().observe(lifecycle, plan, application)

            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)
            self.assertEqual(lifecycle.observation_id, receipt.receipt_id)
            self.assertTrue(receipt.verified)
            self.assertEqual(receipt.verification_outcome, PatchVerificationOutcome.VERIFIED)
            self.assertEqual(receipt.application_commit_state, PatchCommitState.COMMITTED)
            self.assertEqual(receipt.proposal_id, proposal.proposal_id)
            self.assertEqual(receipt.authorization_receipt_id, auth_receipt.receipt_id)
            self.assertEqual(len(receipt.file_observations), 2)
            self.assertEqual(
                tuple(obs.step_index for obs in receipt.file_observations),
                (0, 1),
            )
            self.assertTrue(
                all(
                    obs.status is PatchFileObservationStatus.VERIFIED
                    for obs in receipt.file_observations
                )
            )
            self.assertTrue(
                all(
                    obs.terminal_expectation is PatchTerminalExpectation.POSTIMAGE
                    for obs in receipt.file_observations
                )
            )
            validate_patch_mutation_receipt_binding(proposal, plan, application, receipt)

    def test_rolled_back_patch_receipt_verifies_original_preimages_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name, payload in (("a.txt", b"a-old\n"), ("b.txt", b"b-old\n")):
                (root / name).write_bytes(payload)
            _, authority, _, lifecycle, plan = _authorized_patch(
                root,
                (
                    PatchFileRequest(MutationOperation.REPLACE, "a.txt", b"a-new\n"),
                    PatchFileRequest(MutationOperation.REPLACE, "b.txt", b"b-new\n"),
                ),
            )
            real_stage = patch_application_module.create_metadata_stage
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated second-step stage failure")
                return real_stage(*args, **kwargs)

            with patch.object(
                patch_application_module,
                "create_metadata_stage",
                side_effect=fail_second,
            ):
                application = PatchApplicationExecutor().execute(
                    lifecycle,
                    plan,
                    authority=authority,
                )

            self.assertEqual(application.commit_state, PatchCommitState.ROLLED_BACK)
            receipt = PatchMutationObserver().observe(lifecycle, plan, application)
            self.assertTrue(receipt.verified)
            self.assertEqual(receipt.application_commit_state, PatchCommitState.ROLLED_BACK)
            self.assertEqual(receipt.application_failed_step_index, 1)
            self.assertEqual(receipt.application_failed_target, "b.txt")
            self.assertEqual(receipt.application_failure_stage, PatchFailureStage.STAGING)
            self.assertIn("simulated second-step stage failure", receipt.application_error)
            self.assertTrue(
                all(
                    obs.terminal_expectation is PatchTerminalExpectation.PREIMAGE
                    for obs in receipt.file_observations
                )
            )
            self.assertEqual((root / "a.txt").read_bytes(), b"a-old\n")
            self.assertEqual((root / "b.txt").read_bytes(), b"b-old\n")

    def test_post_commit_external_drift_is_recorded_as_mismatch_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, authority, _, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            application = PatchApplicationExecutor().execute(
                lifecycle,
                plan,
                authority=authority,
            )
            (root / "new.txt").write_bytes(b"external\n")

            receipt = PatchMutationObserver().observe(lifecycle, plan, application)
            self.assertEqual(receipt.verification_outcome, PatchVerificationOutcome.MISMATCH)
            self.assertFalse(receipt.verified)
            self.assertEqual(
                receipt.file_observations[0].status,
                PatchFileObservationStatus.MISMATCH,
            )
            self.assertNotEqual(
                receipt.file_observations[0].observed_terminal.sha256,
                receipt.file_observations[0].expected_terminal.sha256,
            )
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)

    def test_post_rollback_external_drift_is_recorded_as_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "old.txt"
            target.write_bytes(b"old\n")
            _, authority, _, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.REPLACE, "old.txt", b"new\n"),),
            )
            with patch.object(
                patch_application_module.WindowsTxFTransaction,
                "commit",
                side_effect=OSError("simulated commit failure"),
            ):
                application = PatchApplicationExecutor().execute(
                    lifecycle,
                    plan,
                    authority=authority,
                )
            self.assertEqual(application.commit_state, PatchCommitState.ROLLED_BACK)
            target.write_bytes(b"external\n")

            receipt = PatchMutationObserver().observe(lifecycle, plan, application)
            self.assertEqual(receipt.verification_outcome, PatchVerificationOutcome.MISMATCH)
            self.assertEqual(
                receipt.file_observations[0].terminal_expectation,
                PatchTerminalExpectation.PREIMAGE,
            )
            self.assertEqual(
                receipt.file_observations[0].status,
                PatchFileObservationStatus.MISMATCH,
            )

    def test_terminal_inspection_failure_is_incomplete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, authority, _, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            application = PatchApplicationExecutor().execute(
                lifecycle,
                plan,
                authority=authority,
            )
            with patch.object(
                patch_observation_module.PinnedMutationTarget,
                "capture_preimage",
                side_effect=WorkspaceMutationBoundaryError("simulated inspection failure"),
            ):
                receipt = PatchMutationObserver().observe(lifecycle, plan, application)

            self.assertEqual(receipt.verification_outcome, PatchVerificationOutcome.INCOMPLETE)
            observation = receipt.file_observations[0]
            self.assertEqual(observation.status, PatchFileObservationStatus.INSPECTION_FAILED)
            self.assertIsNone(observation.observed_terminal)
            self.assertIn("simulated inspection failure", observation.error)
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)

    def test_live_namespace_reparse_failure_is_incomplete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, authority, _, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            application = PatchApplicationExecutor().execute(
                lifecycle,
                plan,
                authority=authority,
            )
            with patch.object(
                patch_observation_module,
                "_runtime_step",
                side_effect=WorkspaceMutationBoundaryError("simulated namespace loss"),
            ):
                receipt = PatchMutationObserver().observe(lifecycle, plan, application)

            self.assertEqual(receipt.verification_outcome, PatchVerificationOutcome.INCOMPLETE)
            self.assertEqual(
                receipt.file_observations[0].status,
                PatchFileObservationStatus.INSPECTION_FAILED,
            )
            self.assertIn("simulated namespace loss", receipt.file_observations[0].error)
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)

    def test_receipt_validation_rejects_nested_file_observation_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal, authority, _, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            application = PatchApplicationExecutor().execute(
                lifecycle,
                plan,
                authority=authority,
            )
            receipt = PatchMutationObserver().observe(lifecycle, plan, application)
            observation = receipt.file_observations[0]
            object.__setattr__(observation, "target", "tampered.txt")
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "digest does not match payload|not bound to plan step",
            ):
                validate_patch_mutation_receipt_binding(
                    proposal,
                    plan,
                    application,
                    receipt,
                )

    def test_observer_does_not_consume_authority_again(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, authority, auth_receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            application = PatchApplicationExecutor().execute(
                lifecycle,
                plan,
                authority=authority,
            )
            self.assertTrue(authority.is_consumed(auth_receipt))
            with patch.object(
                ActionLifecycle,
                "consume_authorization",
                autospec=True,
                wraps=ActionLifecycle.consume_authorization,
            ) as consume:
                PatchMutationObserver().observe(lifecycle, plan, application)
            consume.assert_not_called()

    def test_second_observation_attempt_is_rejected_by_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, authority, _, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            application = PatchApplicationExecutor().execute(
                lifecycle,
                plan,
                authority=authority,
            )
            observer = PatchMutationObserver()
            observer.observe(lifecycle, plan, application)
            with self.assertRaisesRegex(
                InvalidActionTransitionError,
                "requires EXECUTED lifecycle",
            ):
                observer.observe(lifecycle, plan, application)


if __name__ == "__main__":
    unittest.main()
