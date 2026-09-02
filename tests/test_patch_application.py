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
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchFileRequest,
    build_patch_execution_plan,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation.patch_application import (
    PATCH_COMMIT_MODEL,
    PatchApplicationExecutor,
    PatchApplicationResult,
    PatchCommitState,
    PatchFailureStage,
)
from codexia_manual_agent.mutation import patch_application as patch_application_module


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


class PatchApplicationContractTests(unittest.TestCase):
    def test_non_windows_application_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            with patch.object(
                patch_application_module,
                "_is_windows_host",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "disabled outside Windows",
                ):
                    PatchApplicationExecutor().execute(
                        lifecycle,
                        plan,
                        authority=authority,
                    )
            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertFalse((root / "new.txt").exists())

    def test_final_revalidation_drift_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "old.txt"
            target.write_bytes(b"old\n")
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.REPLACE, "old.txt", b"new\n"),),
            )
            target.write_bytes(b"raced\n")
            with (
                patch.object(patch_application_module, "_is_windows_host", return_value=True),
                patch.object(patch_application_module, "preflight_patch_execution_plan"),
                patch.object(patch_application_module, "require_windows_txf_support"),
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationPreimageChangedError,
                    "pre-authority revalidation",
                ):
                    PatchApplicationExecutor().execute(
                        lifecycle,
                        plan,
                        authority=authority,
                    )
            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertEqual(target.read_bytes(), b"raced\n")

    def test_application_result_rejects_malformed_identity_and_failure_state(self) -> None:
        proposal_id = "12345678-1234-5678-1234-567812345678"
        digest = "a" * 64
        with self.assertRaisesRegex(InvalidWorkspaceMutationError, "proposal digest"):
            PatchApplicationResult(
                schema_version=1,
                execution_id="execution",
                proposal_id=proposal_id,
                proposal_digest="not-a-digest",
                change_set_digest=digest,
                plan_digest=digest,
                commit_model=PATCH_COMMIT_MODEL,
                commit_state=PatchCommitState.COMMITTED,
            )

        with self.assertRaisesRegex(InvalidWorkspaceMutationError, "must classify its failure"):
            PatchApplicationResult(
                schema_version=1,
                execution_id="execution",
                proposal_id=proposal_id,
                proposal_digest=digest,
                change_set_digest=digest,
                plan_digest=digest,
                commit_model=PATCH_COMMIT_MODEL,
                commit_state=PatchCommitState.ROLLED_BACK,
            )

    def test_authority_consumption_failure_rolls_back_inspection_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )

            class FakeTransaction:
                finished = False

                def __init__(self) -> None:
                    self.rollback_calls = 0
                    self.close_calls = 0

                def rollback(self) -> None:
                    self.rollback_calls += 1
                    self.finished = True

                def close(self) -> None:
                    self.close_calls += 1

            transaction = FakeTransaction()
            executor = PatchApplicationExecutor()
            with (
                patch.object(patch_application_module, "_is_windows_host", return_value=True),
                patch.object(patch_application_module, "preflight_patch_execution_plan"),
                patch.object(patch_application_module, "require_windows_txf_support"),
                patch.object(patch_application_module, "revalidate_patch_execution_plan"),
                patch.object(patch_application_module, "create_transaction", return_value=transaction),
                patch.object(executor, "_admit_transaction_targets"),
                patch.object(authority, "consume", side_effect=RuntimeError("receipt race")),
            ):
                with self.assertRaisesRegex(RuntimeError, "receipt race"):
                    executor.execute(lifecycle, plan, authority=authority)

            self.assertEqual(transaction.rollback_calls, 1)
            self.assertEqual(transaction.close_calls, 1)
            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertFalse((root / "new.txt").exists())

    def test_create_also_requires_atomic_txf_support_before_receipt_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            with (
                patch.object(
                    patch_application_module,
                    "_is_windows_host",
                    return_value=True,
                ),
                patch.object(
                    patch_application_module,
                    "preflight_patch_execution_plan",
                ),
                patch.object(
                    patch_application_module,
                    "require_windows_txf_support",
                    side_effect=WorkspaceMutationBoundaryError("no TxF"),
                ) as support,
                patch.object(patch_application_module, "create_transaction") as create_tx,
            ):
                with self.assertRaisesRegex(WorkspaceMutationBoundaryError, "no TxF"):
                    PatchApplicationExecutor().execute(
                        lifecycle,
                        plan,
                        authority=authority,
                    )
            support.assert_called_once_with(root.resolve())
            create_tx.assert_not_called()
            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)


@unittest.skipUnless(os.name == "nt", "M2.4.3 real application requires Windows TxF")
class PatchApplicationWindowsTests(unittest.TestCase):
    def test_mixed_patch_commits_once_and_consumes_only_patch_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = root / "old.txt"
            old.write_bytes(b"old\n")
            proposal, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (
                    PatchFileRequest(MutationOperation.REPLACE, "old.txt", b"changed\n"),
                    PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),
                ),
            )
            executor = PatchApplicationExecutor()
            original_commit = patch_application_module.WindowsTxFTransaction.commit
            commit_calls = 0

            def counted_commit(transaction) -> None:
                nonlocal commit_calls
                commit_calls += 1
                original_commit(transaction)

            with (
                patch.object(
                    patch_application_module.WindowsTxFTransaction,
                    "commit",
                    new=counted_commit,
                ),
                patch.object(authority, "consume", wraps=authority.consume) as consume,
            ):
                result = executor.execute(lifecycle, plan, authority=authority)

            self.assertEqual(commit_calls, 1)
            self.assertEqual(consume.call_count, 1)
            consumed_proposal = consume.call_args.args[0]
            self.assertEqual(consumed_proposal.proposal_id, proposal.proposal_id)
            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(result.commit_model, PATCH_COMMIT_MODEL)
            self.assertEqual(result.commit_state, PatchCommitState.COMMITTED)
            self.assertTrue(result.committed)
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
            self.assertIsNone(lifecycle.observation_id)
            self.assertEqual(old.read_bytes(), b"changed\n")
            self.assertEqual((root / "new.txt").read_bytes(), b"new\n")

    def test_second_step_stage_failure_rolls_back_first_and_stops_later_steps(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name, payload in (("a.txt", b"a-old\n"), ("b.txt", b"b-old\n"), ("c.txt", b"c-old\n")):
                (root / name).write_bytes(payload)
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (
                    PatchFileRequest(MutationOperation.REPLACE, "a.txt", b"a-new\n"),
                    PatchFileRequest(MutationOperation.REPLACE, "b.txt", b"b-new\n"),
                    PatchFileRequest(MutationOperation.REPLACE, "c.txt", b"c-new\n"),
                ),
            )
            real_stage = patch_application_module.create_metadata_stage
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated second-step staging failure")
                return real_stage(*args, **kwargs)

            with patch.object(
                patch_application_module,
                "create_metadata_stage",
                side_effect=fail_second,
            ):
                result = PatchApplicationExecutor().execute(
                    lifecycle,
                    plan,
                    authority=authority,
                )

            self.assertEqual(calls, 2)
            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(result.commit_state, PatchCommitState.ROLLED_BACK)
            self.assertEqual(result.failed_step_index, 1)
            self.assertEqual(result.failed_target, "b.txt")
            self.assertEqual(result.failure_stage, PatchFailureStage.STAGING)
            self.assertEqual((root / "a.txt").read_bytes(), b"a-old\n")
            self.assertEqual((root / "b.txt").read_bytes(), b"b-old\n")
            self.assertEqual((root / "c.txt").read_bytes(), b"c-old\n")
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)

    def test_create_target_appearance_after_consumption_is_no_clobber_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = root / "a.txt"
            old.write_bytes(b"old\n")
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (
                    PatchFileRequest(MutationOperation.REPLACE, "a.txt", b"changed\n"),
                    PatchFileRequest(MutationOperation.CREATE, "z.txt", b"ours\n"),
                ),
            )
            real_move = patch_application_module._move_create_staged

            def race_create(transaction, staged, target):
                target.write_bytes(b"foreign\n")
                return real_move(transaction, staged, target)

            with patch.object(
                patch_application_module,
                "_move_create_staged",
                side_effect=race_create,
            ):
                result = PatchApplicationExecutor().execute(
                    lifecycle,
                    plan,
                    authority=authority,
                )

            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(result.commit_state, PatchCommitState.ROLLED_BACK)
            self.assertEqual(result.failed_target, "z.txt")
            self.assertEqual(result.failure_stage, PatchFailureStage.PUBLISH)
            self.assertEqual(old.read_bytes(), b"old\n")
            self.assertEqual((root / "z.txt").read_bytes(), b"foreign\n")

    def test_replace_metadata_drift_before_publish_rolls_back_patch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "old.txt"
            target.write_bytes(b"old\n")
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.REPLACE, "old.txt", b"new\n"),),
            )
            real_capture = patch_application_module.capture_windows_replace_metadata_fd
            calls = 0

            def drift_on_second(fd, *, expected_path=None):
                nonlocal calls
                calls += 1
                metadata = real_capture(fd, expected_path=expected_path)
                if calls == 2:
                    metadata.binding = dict(metadata.binding)
                    metadata.binding["file_attributes"] = int(
                        metadata.binding["file_attributes"]
                    ) ^ 0x20
                return metadata

            with patch.object(
                patch_application_module,
                "capture_windows_replace_metadata_fd",
                side_effect=drift_on_second,
            ):
                result = PatchApplicationExecutor().execute(
                    lifecycle,
                    plan,
                    authority=authority,
                )

            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(result.commit_state, PatchCommitState.ROLLED_BACK)
            self.assertEqual(result.failed_target, "old.txt")
            self.assertEqual(result.failure_stage, PatchFailureStage.REVALIDATION)
            self.assertEqual(target.read_bytes(), b"old\n")

    def test_commit_failure_with_successful_rollback_changes_no_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "old.txt"
            target.write_bytes(b"old\n")
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.REPLACE, "old.txt", b"new\n"),),
            )
            with patch.object(
                patch_application_module.WindowsTxFTransaction,
                "commit",
                side_effect=OSError("simulated commit failure"),
            ):
                result = PatchApplicationExecutor().execute(
                    lifecycle,
                    plan,
                    authority=authority,
                )

            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(result.commit_state, PatchCommitState.ROLLED_BACK)
            self.assertEqual(result.failure_stage, PatchFailureStage.COMMIT)
            self.assertEqual(target.read_bytes(), b"old\n")
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)

    def test_failed_rollback_is_indeterminate_and_blocks_next_patch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_target = root / "first.txt"
            first_target.write_bytes(b"old\n")
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.REPLACE, "first.txt", b"new\n"),),
            )
            executor = PatchApplicationExecutor()
            original_rollback = patch_application_module.WindowsTxFTransaction.rollback

            with (
                patch.object(
                    patch_application_module,
                    "preflight_patch_execution_plan",
                ),
                patch.object(
                    patch_application_module,
                    "require_windows_txf_support",
                ),
                patch.object(
                    patch_application_module.WindowsTxFTransaction,
                    "commit",
                    side_effect=OSError("simulated commit failure"),
                ),
                patch.object(
                    patch_application_module.WindowsTxFTransaction,
                    "rollback",
                    side_effect=OSError("simulated rollback failure"),
                ),
            ):
                result = executor.execute(lifecycle, plan, authority=authority)

            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(result.commit_state, PatchCommitState.INDETERMINATE)
            self.assertEqual(result.failure_stage, PatchFailureStage.ROLLBACK)
            self.assertEqual(len(executor._retained_transactions), 1)

            # A second patch must fail before consuming its own receipt.
            second = root / "second.txt"
            _, authority2, receipt2, lifecycle2, plan2 = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "second.txt", b"second\n"),),
            )
            with self.assertRaisesRegex(WorkspaceMutationBoundaryError, "recovery remains unresolved"):
                executor.execute(lifecycle2, plan2, authority=authority2)
            self.assertFalse(authority2.is_consumed(receipt2))
            self.assertEqual(lifecycle2.phase, ActionPhase.AUTHORIZED)
            self.assertFalse(second.exists())

            # Explicit test cleanup after the simulated failure boundary.
            retained = executor._retained_transactions.pop()
            original_rollback(retained)
            retained.close()

    def test_result_is_not_observation_and_does_not_advance_to_observed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, authority, _, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            result = PatchApplicationExecutor().execute(
                lifecycle,
                plan,
                authority=authority,
            )
            payload = result.to_dict()
            self.assertEqual(payload["commit_state"], "committed")
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
            self.assertIsNone(lifecycle.observation_id)
            self.assertNotIn("observation_id", payload)
            self.assertNotIn("receipt_id", payload)


if __name__ == "__main__":
    unittest.main()
