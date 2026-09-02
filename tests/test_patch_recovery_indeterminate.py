from patch_recovery_test_support import *
from patch_recovery_test_support import _authorized_patch


@unittest.skipUnless(os.name == "nt", "M2.4.5 real recovery requires Windows TxF")
class PatchRecoveryIndeterminateResultTests(unittest.TestCase):
    def test_commit_intent_failure_with_indeterminate_rollback_remains_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            target = root / "old.txt"
            target.write_bytes(b"old\n")
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.REPLACE, "old.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            executor = RecoverablePatchApplicationExecutor(journal)
            real_append = journal.append_phase

            def fail_commit_intent(*, lifecycle, plan, phase, application_result=None):
                if phase is PatchRecoveryJournalPhase.COMMIT_INTENT:
                    raise OSError("simulated commit-intent fsync failure")
                return real_append(
                    lifecycle=lifecycle,
                    plan=plan,
                    phase=phase,
                    application_result=application_result,
                )

            def retain_indeterminate(transaction):
                executor._retained_transactions.append(transaction)
                return (
                    PatchCommitState.INDETERMINATE,
                    PatchFailureStage.ROLLBACK,
                    "rollback failed: OSError: simulated rollback failure",
                )

            with (
                patch.object(journal, "append_phase", side_effect=fail_commit_intent),
                patch.object(
                    executor,
                    "_rollback_after_failure",
                    side_effect=retain_indeterminate,
                ),
            ):
                result = executor.execute(lifecycle, plan, authority=authority)

            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(result.commit_state, PatchCommitState.INDETERMINATE)
            self.assertEqual(result.failure_stage, PatchFailureStage.ROLLBACK)
            self.assertIn("commit-intent fsync failure", result.error)
            self.assertIn("rollback could not be proven", result.cleanup_error)
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
            self.assertEqual(len(executor._retained_transactions), 1)
            transaction = executor._retained_transactions[0]
            binding = executor._retained_transaction_binding(transaction)
            self.assertIsNotNone(binding)
            self.assertTrue(
                binding.matches(
                    transaction=transaction,
                    execution_id=result.execution_id,
                    plan=plan,
                )
            )

            with patch.object(
                runtime_module,
                "query_transaction_outcome",
                return_value=PatchKtmOutcome.UNDETERMINED,
            ):
                recovery = PatchRecoveryManager().recover_live(
                    executor=executor,
                    lifecycle=lifecycle,
                    plan=plan,
                    application_result=result,
                )

            self.assertEqual(recovery.source, PatchRecoverySource.LIVE_KTM)
            self.assertEqual(recovery.ktm_outcome_before, PatchKtmOutcome.UNDETERMINED)
            self.assertEqual(recovery.ktm_outcome_after, PatchKtmOutcome.ABORTED)
            self.assertTrue(recovery.rollback_retry_attempted)
            self.assertEqual(recovery.recovered_commit_state, PatchCommitState.ROLLED_BACK)
            self.assertEqual(
                recovery.verification_outcome,
                PatchRecoveryVerificationOutcome.VERIFIED,
            )
            self.assertEqual(target.read_bytes(), b"old\n")
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)
            self.assertEqual(lifecycle.observation_id, recovery.receipt_id)
            self.assertEqual(executor._retained_transactions, [])
            self.assertIsNone(executor._retained_transaction_binding(transaction))
