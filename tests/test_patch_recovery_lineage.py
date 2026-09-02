from patch_recovery_test_support import *
from patch_recovery_test_support import _authorized_patch, _observations_for


def _executed_patch_with_id(root, changes, execution_id):
    proposal, authority, receipt, lifecycle, plan = _authorized_patch(root, changes)
    lifecycle.consume_authorization(authority=authority)
    lifecycle.record_executed(execution_id)
    return proposal, authority, receipt, lifecycle, plan


def _indeterminate_result(lifecycle, plan):
    return PatchApplicationResult(
        schema_version=PATCH_APPLICATION_SCHEMA_VERSION,
        execution_id=lifecycle.execution_id,
        proposal_id=plan.proposal_id,
        proposal_digest=plan.proposal_digest,
        change_set_digest=plan.change_set_digest,
        plan_digest=plan.plan_digest,
        commit_model=PATCH_COMMIT_MODEL,
        commit_state=PatchCommitState.INDETERMINATE,
        failure_stage=PatchFailureStage.ROLLBACK,
        error="simulated unresolved transaction",
    )


def _seed_retained_binding(executor, transaction, lifecycle, plan):
    executor._retained_transactions = [transaction]
    executor._bind_transaction_to_execution(
        transaction,
        execution_id=lifecycle.execution_id,
        plan=plan,
    )


class PatchRecoveryRetainedTransactionLineageTests(unittest.TestCase):
    def test_unbound_retained_transaction_is_rejected_before_ktm_query(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch_with_id(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
                "execution-a",
            )
            result = _indeterminate_result(lifecycle, plan)
            transaction = runtime_module.WindowsTxFTransaction(handle=123)
            executor = PatchApplicationExecutor()
            executor._retained_transactions = [transaction]
            with (
                patch.object(runtime_module, "_is_recovery_windows_host", return_value=True),
                patch.object(runtime_module, "query_transaction_outcome") as query,
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "no executed recovery lineage",
                ):
                    PatchRecoveryManager().recover_live(
                        executor=executor,
                        lifecycle=lifecycle,
                        plan=plan,
                        application_result=result,
                    )
            query.assert_not_called()
            self.assertFalse(transaction.finished)
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
            self.assertIsNone(lifecycle.observation_id)

    def test_foreign_execution_is_rejected_before_ktm_query_or_rollback(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw_a,
            tempfile.TemporaryDirectory() as raw_b,
        ):
            root_a = Path(raw_a)
            root_b = Path(raw_b)
            _, _, _, lifecycle_a, plan_a = _executed_patch_with_id(
                root_a,
                (PatchFileRequest(MutationOperation.CREATE, "a.txt", b"a\n"),),
                "execution-a",
            )
            _, _, _, lifecycle_b, plan_b = _executed_patch_with_id(
                root_b,
                (PatchFileRequest(MutationOperation.CREATE, "b.txt", b"b\n"),),
                "execution-b",
            )
            transaction = runtime_module.WindowsTxFTransaction(handle=123)
            executor = PatchApplicationExecutor()
            _seed_retained_binding(executor, transaction, lifecycle_a, plan_a)
            result_b = _indeterminate_result(lifecycle_b, plan_b)
            with (
                patch.object(runtime_module, "_is_recovery_windows_host", return_value=True),
                patch.object(runtime_module, "query_transaction_outcome") as query,
                patch.object(
                    runtime_module.WindowsTxFTransaction,
                    "rollback",
                    autospec=True,
                ) as rollback,
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "lineage",
                ):
                    PatchRecoveryManager().recover_live(
                        executor=executor,
                        lifecycle=lifecycle_b,
                        plan=plan_b,
                        application_result=result_b,
                    )
            query.assert_not_called()
            rollback.assert_not_called()
            self.assertFalse(transaction.finished)
            self.assertEqual(lifecycle_b.phase, ActionPhase.EXECUTED)
            self.assertIsNone(lifecycle_b.observation_id)

    def test_foreign_plan_is_rejected_even_with_reused_execution_id(self) -> None:
        with (
            tempfile.TemporaryDirectory() as raw_a,
            tempfile.TemporaryDirectory() as raw_b,
        ):
            root_a = Path(raw_a)
            root_b = Path(raw_b)
            _, _, _, lifecycle_a, plan_a = _executed_patch_with_id(
                root_a,
                (PatchFileRequest(MutationOperation.CREATE, "a.txt", b"a\n"),),
                "shared-execution-id",
            )
            _, _, _, lifecycle_b, plan_b = _executed_patch_with_id(
                root_b,
                (PatchFileRequest(MutationOperation.CREATE, "b.txt", b"b\n"),),
                "shared-execution-id",
            )
            transaction = runtime_module.WindowsTxFTransaction(handle=123)
            executor = PatchApplicationExecutor()
            _seed_retained_binding(executor, transaction, lifecycle_a, plan_a)
            result_b = _indeterminate_result(lifecycle_b, plan_b)
            with (
                patch.object(runtime_module, "_is_recovery_windows_host", return_value=True),
                patch.object(runtime_module, "query_transaction_outcome") as query,
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "lineage",
                ):
                    PatchRecoveryManager().recover_live(
                        executor=executor,
                        lifecycle=lifecycle_b,
                        plan=plan_b,
                        application_result=result_b,
                    )
            query.assert_not_called()
            self.assertFalse(transaction.finished)
            self.assertEqual(lifecycle_b.phase, ActionPhase.EXECUTED)

    def test_exact_binding_can_reconcile_and_forget_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch_with_id(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
                "execution-a",
            )
            result = _indeterminate_result(lifecycle, plan)
            transaction = runtime_module.WindowsTxFTransaction(handle=123)
            executor = PatchApplicationExecutor()
            _seed_retained_binding(executor, transaction, lifecycle, plan)
            observations = _observations_for(plan, "pre")
            with (
                patch.object(runtime_module, "_is_recovery_windows_host", return_value=True),
                patch.object(
                    runtime_module,
                    "query_transaction_outcome",
                    return_value=PatchKtmOutcome.ABORTED,
                ) as query,
                patch.object(
                    runtime_module,
                    "_observe_recovery_files",
                    return_value=observations,
                ),
                patch.object(
                    runtime_module.WindowsTxFTransaction,
                    "close",
                    autospec=True,
                ) as close,
            ):
                recovery = PatchRecoveryManager().recover_live(
                    executor=executor,
                    lifecycle=lifecycle,
                    plan=plan,
                    application_result=result,
                )
            query.assert_called_once_with(transaction)
            close.assert_called_once_with(transaction)
            self.assertEqual(recovery.source, PatchRecoverySource.LIVE_KTM)
            self.assertEqual(recovery.recovered_commit_state, PatchCommitState.ROLLED_BACK)
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)
            self.assertEqual(executor._retained_transactions, [])
            self.assertEqual(executor._retained_transaction_bindings, {})
