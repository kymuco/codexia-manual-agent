from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from codexia_manual_agent.authority import ActionLifecycle, LocalApprovalAuthority
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
)
from codexia_manual_agent.mutation.patch_application import (
    PATCH_APPLICATION_SCHEMA_VERSION,
    PATCH_COMMIT_MODEL,
    PatchApplicationExecutor,
    PatchApplicationResult,
    PatchCommitState,
    PatchFailureStage,
)
from codexia_manual_agent.mutation.patch_execution_plan import PatchExecutionPlan
from codexia_manual_agent.mutation.preflight_executor import _is_windows_host
from codexia_manual_agent.mutation.windows_txf import WindowsTxFTransaction
from codexia_manual_agent.mutation._patch_recovery_common import (
    PatchKtmOutcome,
    PatchRecoveryJournalPhase,
    PatchRecoveryPersistenceError,
    PatchRecoverySource,
    _validate_executed_binding,
    _validated_application_result,
)
from codexia_manual_agent.mutation._patch_recovery_journal import (
    PatchRecoveryJournal,
    _validate_journal_binding,
)
from codexia_manual_agent.mutation._patch_recovery_observation import _observe_recovery_files
from codexia_manual_agent.mutation._patch_recovery_receipt import (
    PatchRecoveryReceipt,
    _finalize_recovery_receipt,
)
from codexia_manual_agent.mutation._patch_recovery_assessment import (
    PatchCrashRecoveryAssessment,
    validate_patch_crash_recovery_assessment_binding,
)


def _is_recovery_windows_host() -> bool:
    return _is_windows_host() and os.name == "nt"


def query_transaction_outcome(transaction: WindowsTxFTransaction) -> PatchKtmOutcome:
    if not _is_recovery_windows_host():
        raise WorkspaceMutationBoundaryError(
            "KTM transaction recovery outcome query is available only on Windows"
        )
    if not isinstance(transaction, WindowsTxFTransaction):
        raise TypeError("transaction must be WindowsTxFTransaction")
    if not transaction.handle:
        raise WorkspaceMutationBoundaryError(
            "Cannot query a closed Windows transaction handle"
        )
    from ctypes import wintypes

    ktm = ctypes.WinDLL("KtmW32.dll", use_last_error=True)
    get_info = ktm.GetTransactionInformation
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        wintypes.LPWSTR,
    ]
    get_info.restype = wintypes.BOOL
    outcome = wintypes.DWORD()
    if not get_info(
        wintypes.HANDLE(transaction.handle),
        ctypes.byref(outcome),
        None,
        None,
        None,
        0,
        None,
    ):
        error = ctypes.get_last_error()
        raise WorkspaceMutationBoundaryError(
            f"Windows KTM transaction outcome cannot be queried (winerror={error})"
        )
    mapping = {
        1: PatchKtmOutcome.UNDETERMINED,
        2: PatchKtmOutcome.COMMITTED,
        3: PatchKtmOutcome.ABORTED,
    }
    try:
        return mapping[int(outcome.value)]
    except KeyError as exc:
        raise WorkspaceMutationBoundaryError(
            f"Windows KTM returned an unknown transaction outcome {int(outcome.value)}"
        ) from exc


class RecoverablePatchApplicationExecutor(PatchApplicationExecutor):
    """M2.4.3 executor with durable M2.4.5 crash-boundary journal barriers."""

    def __init__(self, journal: PatchRecoveryJournal) -> None:
        super().__init__()
        if not isinstance(journal, PatchRecoveryJournal):
            raise TypeError("journal must be PatchRecoveryJournal")
        self.journal = journal
        self._active_lifecycle: ActionLifecycle | None = None
        self._active_plan: PatchExecutionPlan | None = None
        self._journal_started = False

    def _stage_and_publish(self, *, transaction, runtimes) -> None:
        lifecycle = self._active_lifecycle
        plan = self._active_plan
        if lifecycle is None or plan is None:
            raise WorkspaceMutationBoundaryError(
                "Recovery-aware patch executor lost its active journal binding"
            )
        self.journal.append_phase(
            lifecycle=lifecycle,
            plan=plan,
            phase=PatchRecoveryJournalPhase.EXECUTION_STARTED,
        )
        self._journal_started = True
        super()._stage_and_publish(transaction=transaction, runtimes=runtimes)
        self.journal.append_phase(
            lifecycle=lifecycle,
            plan=plan,
            phase=PatchRecoveryJournalPhase.COMMIT_INTENT,
        )

    def _indeterminate_result_from_retained_failure(
        self,
        *,
        lifecycle: ActionLifecycle,
        plan: PatchExecutionPlan,
        error: Exception,
    ) -> PatchApplicationResult | None:
        execution_id = lifecycle.execution_id
        if not execution_id:
            return None
        unfinished = [tx for tx in self._retained_transactions if not tx.finished]
        if len(unfinished) != 1:
            return None
        transaction = unfinished[0]
        binding = self._retained_transaction_binding(transaction)
        if binding is None or not binding.matches(
            transaction=transaction,
            execution_id=execution_id,
            plan=plan,
        ):
            return None
        return PatchApplicationResult(
            schema_version=PATCH_APPLICATION_SCHEMA_VERSION,
            execution_id=execution_id,
            proposal_id=plan.proposal_id,
            proposal_digest=plan.proposal_digest,
            change_set_digest=plan.change_set_digest,
            plan_digest=plan.plan_digest,
            commit_model=PATCH_COMMIT_MODEL,
            commit_state=PatchCommitState.INDETERMINATE,
            failure_stage=PatchFailureStage.ROLLBACK,
            error=f"{type(error).__name__}: {error}",
            cleanup_error=(
                "rollback could not be proven; unfinished TxF transaction retained "
                "for live recovery"
            ),
        )

    def execute(
        self,
        lifecycle: ActionLifecycle,
        plan: PatchExecutionPlan,
        *,
        authority: LocalApprovalAuthority,
    ) -> PatchApplicationResult:
        # Preserve M2.4.3's cleanup gate ordering: a finished retained handle may
        # need one close retry before a stale journal is inspected. Unfinished
        # retained transactions still fail closed here before new authority use.
        self._retry_retained_transaction_cleanup()
        self.journal.assert_fresh(workspace_root=plan.workspace_root)
        self._active_lifecycle = lifecycle
        self._active_plan = plan
        self._journal_started = False
        try:
            try:
                result = super().execute(lifecycle, plan, authority=authority)
            except Exception as exc:
                indeterminate = self._indeterminate_result_from_retained_failure(
                    lifecycle=lifecycle,
                    plan=plan,
                    error=exc,
                )
                if indeterminate is None:
                    raise
                result = indeterminate
        finally:
            self._active_lifecycle = None
            self._active_plan = None
        if not self._journal_started:
            # Failures before the first durable journal marker can still become
            # live-recoverable when M2.4.3 retained an exact executed TxF lineage.
            # Otherwise the accepted pre-authority/pre-stage semantics are preserved.
            return result
        if result.commit_state is PatchCommitState.INDETERMINATE:
            return result
        try:
            self.journal.append_phase(
                lifecycle=lifecycle,
                plan=plan,
                phase=PatchRecoveryJournalPhase.TERMINAL,
                application_result=result,
            )
        except Exception as exc:
            raise PatchRecoveryPersistenceError(
                "Patch reached a terminal filesystem outcome but its durable "
                "recovery terminal marker failed",
                result,
            ) from exc
        return result


class PatchRecoveryManager:
    """Resolve retained/live transactions or classify restart journal state."""

    def recover_live(
        self,
        *,
        executor: PatchApplicationExecutor,
        lifecycle: ActionLifecycle,
        plan: PatchExecutionPlan,
        application_result: PatchApplicationResult,
    ) -> PatchRecoveryReceipt:
        if not _is_recovery_windows_host():
            raise WorkspaceMutationBoundaryError(
                "Live TxF recovery is available only on Windows"
            )
        receipt = _validate_executed_binding(lifecycle, plan)
        result = _validated_application_result(application_result)
        if (
            result.commit_state is not PatchCommitState.INDETERMINATE
            or result.execution_id != lifecycle.execution_id
            or result.proposal_id != plan.proposal_id
            or result.proposal_digest != plan.proposal_digest
            or result.plan_digest != plan.plan_digest
            or result.change_set_digest != plan.change_set_digest
        ):
            raise InvalidWorkspaceMutationError(
                "Live recovery requires exact INDETERMINATE application result binding"
            )
        retained = list(getattr(executor, "_retained_transactions", ()))
        unfinished = [tx for tx in retained if not tx.finished]
        if len(unfinished) != 1:
            raise WorkspaceMutationBoundaryError(
                "Live recovery requires exactly one unfinished retained TxF transaction"
            )
        transaction = unfinished[0]
        binding = executor._retained_transaction_binding(transaction)
        if binding is None:
            raise WorkspaceMutationBoundaryError(
                "Retained TxF transaction has no executed recovery lineage"
            )
        if (
            not binding.matches(
                transaction=transaction,
                execution_id=result.execution_id,
                plan=plan,
            )
            or result.execution_id != lifecycle.execution_id
        ):
            raise WorkspaceMutationBoundaryError(
                "Retained TxF transaction lineage does not match recovered execution"
            )

        before = query_transaction_outcome(transaction)
        after = before
        rollback_retry = False
        if before is PatchKtmOutcome.UNDETERMINED:
            rollback_retry = True
            # RollbackTransaction is synchronous. A successful return is itself
            # terminal rollback proof; recovery must not become dependent on a
            # second diagnostic query after the transaction is already aborted.
            transaction.rollback()
            after = PatchKtmOutcome.ABORTED
        if after is PatchKtmOutcome.COMMITTED:
            recovered_state = PatchCommitState.COMMITTED
            transaction.finished = True
        elif after is PatchKtmOutcome.ABORTED:
            recovered_state = PatchCommitState.ROLLED_BACK
            transaction.finished = True
        else:
            raise WorkspaceMutationBoundaryError(
                "Live TxF recovery remains undetermined after rollback retry"
            )

        cleanup_error: str | None = None
        try:
            transaction.close()
        except OSError as exc:
            cleanup_error = (
                "recovered transaction handle cleanup failed: "
                f"{type(exc).__name__}: {exc}"
            )
        else:
            executor._retained_transactions = [
                tx for tx in executor._retained_transactions if tx is not transaction
            ]
            executor._forget_transaction_binding(transaction)

        observations = _observe_recovery_files(plan)
        recovery = PatchRecoveryReceipt.create(
            source=PatchRecoverySource.LIVE_KTM,
            lifecycle=lifecycle,
            plan=plan,
            authorization_receipt=receipt,
            recovered_commit_state=recovered_state,
            file_observations=observations,
            original_application_result=result,
            ktm_outcome_before=before,
            ktm_outcome_after=after,
            rollback_retry_attempted=rollback_retry,
            transaction_cleanup_error=cleanup_error,
        )
        return _finalize_recovery_receipt(lifecycle, plan, recovery)

    def recover_restart(
        self,
        *,
        lifecycle: ActionLifecycle,
        plan: PatchExecutionPlan,
        journal: PatchRecoveryJournal,
        original_process_confirmed_terminated: bool,
    ) -> PatchRecoveryReceipt | PatchCrashRecoveryAssessment:
        if not _is_recovery_windows_host():
            raise WorkspaceMutationBoundaryError(
                "Restart recovery is available only on Windows"
            )
        receipt = _validate_executed_binding(lifecycle, plan)
        if not original_process_confirmed_terminated:
            raise WorkspaceMutationBoundaryError(
                "Restart recovery requires explicit confirmation that the "
                "original execution process terminated"
            )
        read = journal.read(workspace_root=plan.workspace_root)
        _validate_journal_binding(read.records, lifecycle, plan, receipt)
        last = read.records[-1]
        observations = _observe_recovery_files(plan)

        if last.phase is PatchRecoveryJournalPhase.TERMINAL:
            if last.application_result is None:
                raise InvalidWorkspaceMutationError(
                    "Terminal recovery journal is missing application result"
                )
            terminal = PatchApplicationResult(**last.application_result)
            recovery = PatchRecoveryReceipt.create(
                source=PatchRecoverySource.JOURNAL_TERMINAL,
                lifecycle=lifecycle,
                plan=plan,
                authorization_receipt=receipt,
                recovered_commit_state=terminal.commit_state,
                file_observations=observations,
                journal_last_record_digest=last.record_digest,
                journal_last_phase=last.phase,
                journal_torn_tail=read.torn_tail,
                journal_terminal_result=terminal,
                original_process_confirmed_terminated=True,
            )
            return _finalize_recovery_receipt(lifecycle, plan, recovery)

        if last.phase is PatchRecoveryJournalPhase.EXECUTION_STARTED:
            recovery = PatchRecoveryReceipt.create(
                source=PatchRecoverySource.PRESUMED_ABORT,
                lifecycle=lifecycle,
                plan=plan,
                authorization_receipt=receipt,
                recovered_commit_state=PatchCommitState.ROLLED_BACK,
                file_observations=observations,
                journal_last_record_digest=last.record_digest,
                journal_last_phase=last.phase,
                journal_torn_tail=read.torn_tail,
                original_process_confirmed_terminated=True,
            )
            return _finalize_recovery_receipt(lifecycle, plan, recovery)

        # COMMIT_INTENT without a durable terminal marker is intentionally not
        # recoverable from current file contents. The assessment is evidence only.
        assessment = PatchCrashRecoveryAssessment.create(
            lifecycle=lifecycle,
            plan=plan,
            receipt=receipt,
            journal_read=read,
            file_observations=observations,
        )
        validate_patch_crash_recovery_assessment_binding(
            lifecycle, plan, assessment
        )
        return assessment
