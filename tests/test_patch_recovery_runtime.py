from patch_recovery_test_support import *
from patch_recovery_test_support import (
    _authorized_patch, _executed_patch, _committed_result, _observations_for,
)

class PatchRecoveryRuntimeContractTests(unittest.TestCase):
    def test_restart_recovery_requires_original_process_termination_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
            )
            manager = PatchRecoveryManager()
            with (
                patch.object(
                    runtime_module,
                    "_is_recovery_windows_host",
                    return_value=True,
                ),
            ):
                with self.assertRaisesRegex(WorkspaceMutationBoundaryError, "confirmation"):
                    manager.recover_restart(
                        lifecycle=lifecycle,
                        plan=plan,
                        journal=journal,
                        original_process_confirmed_terminated=False,
                    )
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
    def test_commit_intent_restart_returns_assessment_not_terminal_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
            )
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.COMMIT_INTENT,
            )
            observations = _observations_for(plan, "pre")
            with (
                patch.object(
                    runtime_module,
                    "_is_recovery_windows_host",
                    return_value=True,
                ),
                patch.object(
                    runtime_module,
                    "_observe_recovery_files",
                    return_value=observations,
                ),
            ):
                recovered = PatchRecoveryManager().recover_restart(
                    lifecycle=lifecycle,
                    plan=plan,
                    journal=journal,
                    original_process_confirmed_terminated=True,
                )
            self.assertIsInstance(recovered, PatchCrashRecoveryAssessment)
            self.assertEqual(
                recovered.filesystem_classification,
                PatchCrashFilesystemClassification.PREIMAGE_SET,
            )
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
            self.assertIsNone(lifecycle.observation_id)
    def test_commit_intent_postimage_still_does_not_claim_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
            )
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.COMMIT_INTENT,
            )
            observations = _observations_for(plan, "post")
            with (
                patch.object(
                    runtime_module,
                    "_is_recovery_windows_host",
                    return_value=True,
                ),
                patch.object(
                    runtime_module,
                    "_observe_recovery_files",
                    return_value=observations,
                ),
            ):
                recovered = PatchRecoveryManager().recover_restart(
                    lifecycle=lifecycle,
                    plan=plan,
                    journal=journal,
                    original_process_confirmed_terminated=True,
                )
            self.assertIsInstance(recovered, PatchCrashRecoveryAssessment)
            self.assertEqual(
                recovered.filesystem_classification,
                PatchCrashFilesystemClassification.POSTIMAGE_SET,
            )
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
            self.assertIsNone(lifecycle.observation_id)
    def test_execution_started_restart_produces_presumed_abort_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
            )
            observations = _observations_for(plan, "pre")
            with (
                patch.object(
                    runtime_module,
                    "_is_recovery_windows_host",
                    return_value=True,
                ),
                patch.object(
                    runtime_module,
                    "_observe_recovery_files",
                    return_value=observations,
                ),
            ):
                recovery = PatchRecoveryManager().recover_restart(
                    lifecycle=lifecycle,
                    plan=plan,
                    journal=journal,
                    original_process_confirmed_terminated=True,
                )
            self.assertEqual(recovery.source, PatchRecoverySource.PRESUMED_ABORT)
            self.assertEqual(recovery.recovered_commit_state, PatchCommitState.ROLLED_BACK)
            self.assertEqual(
                recovery.verification_outcome,
                PatchRecoveryVerificationOutcome.VERIFIED,
            )
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)
            self.assertEqual(lifecycle.observation_id, recovery.receipt_id)
            validate_patch_recovery_receipt_binding(lifecycle, plan, recovery)
    def test_terminal_journal_restart_recovers_exact_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, _, _, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
            )
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.COMMIT_INTENT,
            )
            result = _committed_result(lifecycle, plan)
            journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.TERMINAL,
                application_result=result,
            )
            observations = _observations_for(plan, "post")
            with (
                patch.object(
                    runtime_module,
                    "_is_recovery_windows_host",
                    return_value=True,
                ),
                patch.object(
                    runtime_module,
                    "_observe_recovery_files",
                    return_value=observations,
                ),
            ):
                recovery = PatchRecoveryManager().recover_restart(
                    lifecycle=lifecycle,
                    plan=plan,
                    journal=journal,
                    original_process_confirmed_terminated=True,
                )
            self.assertEqual(recovery.source, PatchRecoverySource.JOURNAL_TERMINAL)
            self.assertEqual(recovery.recovered_commit_state, PatchCommitState.COMMITTED)
            self.assertEqual(recovery.journal_last_phase, PatchRecoveryJournalPhase.TERMINAL)
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)
            validate_patch_recovery_receipt_binding(lifecycle, plan, recovery)


@unittest.skipUnless(os.name == "nt", "M2.4.5 real recovery requires Windows TxF")
class PatchRecoveryWindowsTests(unittest.TestCase):
    def test_recovery_aware_commit_writes_three_durable_records(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            result = RecoverablePatchApplicationExecutor(journal).execute(
                lifecycle,
                plan,
                authority=authority,
            )
            read = journal.read(workspace_root=root)
            self.assertEqual(
                tuple(record.phase for record in read.records),
                (
                    PatchRecoveryJournalPhase.EXECUTION_STARTED,
                    PatchRecoveryJournalPhase.COMMIT_INTENT,
                    PatchRecoveryJournalPhase.TERMINAL,
                ),
            )
            self.assertFalse(read.torn_tail)
            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(result.commit_state, PatchCommitState.COMMITTED)
            self.assertEqual((root / "new.txt").read_bytes(), b"new\n")
            self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)
    def test_commit_intent_journal_failure_rolls_back_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            target = root / "old.txt"
            target.write_bytes(b"old\n")
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.REPLACE, "old.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
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

            executor = RecoverablePatchApplicationExecutor(journal)
            with patch.object(journal, "append_phase", side_effect=fail_commit_intent):
                with self.assertRaisesRegex(OSError, "commit-intent"):
                    executor.execute(lifecycle, plan, authority=authority)
            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(target.read_bytes(), b"old\n")
            read = journal.read(workspace_root=root)
            self.assertEqual(len(read.records), 1)
            self.assertEqual(
                read.records[0].phase,
                PatchRecoveryJournalPhase.EXECUTION_STARTED,
            )
    def test_terminal_journal_failure_preserves_known_committed_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            journal = PatchRecoveryJournal((Path(state) / "recovery.jsonl").resolve())
            real_append = journal.append_phase

            def fail_terminal(*, lifecycle, plan, phase, application_result=None):
                if phase is PatchRecoveryJournalPhase.TERMINAL:
                    raise OSError("simulated terminal journal fsync failure")
                return real_append(
                    lifecycle=lifecycle,
                    plan=plan,
                    phase=phase,
                    application_result=application_result,
                )

            executor = RecoverablePatchApplicationExecutor(journal)
            with patch.object(journal, "append_phase", side_effect=fail_terminal):
                with self.assertRaises(PatchRecoveryPersistenceError) as caught:
                    executor.execute(lifecycle, plan, authority=authority)
            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(
                caught.exception.application_result.commit_state,
                PatchCommitState.COMMITTED,
            )
            self.assertEqual((root / "new.txt").read_bytes(), b"new\n")
            read = journal.read(workspace_root=root)
            self.assertEqual(read.records[-1].phase, PatchRecoveryJournalPhase.COMMIT_INTENT)
    def test_real_retained_transaction_is_recovered_by_ktm_and_rollback_retry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "old.txt"
            target.write_bytes(b"old\n")
            _, authority, receipt, lifecycle, plan = _authorized_patch(
                root,
                (PatchFileRequest(MutationOperation.REPLACE, "old.txt", b"new\n"),),
            )
            executor = PatchApplicationExecutor()

            def retain_without_rollback(base_executor, transaction):
                base_executor._retained_transactions.append(transaction)
                return (
                    PatchCommitState.INDETERMINATE,
                    PatchFailureStage.ROLLBACK,
                    "simulated rollback failure",
                )

            with (
                patch.object(patch_application_module, "preflight_patch_execution_plan"),
                patch.object(patch_application_module, "require_windows_txf_support"),
                patch.object(
                    patch_application_module.WindowsTxFTransaction,
                    "commit",
                    side_effect=OSError("simulated commit failure"),
                ),
                patch.object(
                    PatchApplicationExecutor,
                    "_rollback_after_failure",
                    autospec=True,
                    side_effect=retain_without_rollback,
                ),
            ):
                result = executor.execute(lifecycle, plan, authority=authority)
            self.assertTrue(authority.is_consumed(receipt))
            self.assertEqual(result.commit_state, PatchCommitState.INDETERMINATE)
            self.assertEqual(len(executor._retained_transactions), 1)
            transaction = executor._retained_transactions[0]
            binding = executor._retained_transaction_binding(transaction)
            self.assertIsNotNone(binding)
            self.assertEqual(binding.execution_id, lifecycle.execution_id)
            self.assertEqual(binding.plan_digest, plan.plan_digest)

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
            self.assertEqual(executor._retained_transactions, [])
            self.assertEqual(executor._retained_transaction_bindings, {})
