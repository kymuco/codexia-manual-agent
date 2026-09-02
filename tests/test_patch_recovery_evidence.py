from patch_recovery_test_support import *
from patch_recovery_test_support import (
    _authorized_patch, _executed_patch, _committed_result, _observations_for,
)

class PatchRecoveryEvidenceContractTests(unittest.TestCase):
    def test_recovery_receipt_rejects_missing_journal_terminal_process_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, receipt, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            result = _committed_result(lifecycle, plan)
            observations = _observations_for(plan, "post")
            with self.assertRaisesRegex(InvalidWorkspaceMutationError, "Journal-terminal"):
                PatchRecoveryReceipt.create(
                    source=PatchRecoverySource.JOURNAL_TERMINAL,
                    lifecycle=lifecycle,
                    plan=plan,
                    authorization_receipt=receipt,
                    recovered_commit_state=PatchCommitState.COMMITTED,
                    file_observations=observations,
                    journal_last_record_digest="a" * 64,
                    journal_last_phase=PatchRecoveryJournalPhase.TERMINAL,
                    journal_terminal_result=result,
                    original_process_confirmed_terminated=False,
                )
    def test_recovery_receipt_nested_observation_tamper_fails_plan_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, receipt, lifecycle, plan = _executed_patch(
                root,
                (PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),),
            )
            observations = list(_observations_for(plan, "pre"))
            tampered = observations[0]
            object.__setattr__(tampered, "change_digest", "b" * 64)
            object.__setattr__(
                tampered,
                "observation_digest",
                common_module._digest(tampered._payload()),
            )
            recovery = PatchRecoveryReceipt.create(
                source=PatchRecoverySource.PRESUMED_ABORT,
                lifecycle=lifecycle,
                plan=plan,
                authorization_receipt=receipt,
                recovered_commit_state=PatchCommitState.ROLLED_BACK,
                file_observations=tuple(observations),
                journal_last_record_digest="a" * 64,
                journal_last_phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
                original_process_confirmed_terminated=True,
            )
            with self.assertRaisesRegex(InvalidWorkspaceMutationError, "plan step"):
                validate_patch_recovery_receipt_binding(lifecycle, plan, recovery)
    def test_crash_assessment_nested_observation_tamper_fails_plan_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as state:
            root = Path(raw)
            _, _, receipt, lifecycle, plan = _executed_patch(
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
            read = journal.read(workspace_root=root)
            observations = list(_observations_for(plan, "pre"))
            assessment = PatchCrashRecoveryAssessment.create(
                lifecycle=lifecycle,
                plan=plan,
                receipt=receipt,
                journal_read=read,
                file_observations=tuple(observations),
            )
            tampered = assessment.file_observations[0]
            object.__setattr__(tampered, "primitive_digest", "c" * 64)
            object.__setattr__(
                tampered,
                "observation_digest",
                common_module._digest(tampered._payload()),
            )
            object.__setattr__(
                assessment,
                "assessment_digest",
                common_module._digest(assessment._payload()),
            )
            with self.assertRaisesRegex(InvalidWorkspaceMutationError, "plan step"):
                validate_patch_crash_recovery_assessment_binding(
                    lifecycle,
                    plan,
                    assessment,
                )

