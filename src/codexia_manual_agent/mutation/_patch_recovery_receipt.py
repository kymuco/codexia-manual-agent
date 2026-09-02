from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from codexia_manual_agent.authority import (
    ActionLifecycle, ActionPhase, AuthorizationDecision, AuthorizationReceipt,
)
from codexia_manual_agent.domain.errors import (
    InvalidActionTransitionError,
    InvalidWorkspaceMutationError,
)
from codexia_manual_agent.mutation.patch_application import (
    PATCH_COMMIT_MODEL,
    PatchApplicationResult,
    PatchCommitState,
)
from codexia_manual_agent.mutation.patch_execution_plan import (
    PatchExecutionPlan,
    validate_patch_execution_plan_binding,
)
from codexia_manual_agent.mutation._patch_recovery_common import (
    PATCH_RECOVERY_RECEIPT_SCHEMA_VERSION, PatchKtmOutcome, PatchRecoveryJournalPhase,
    PatchRecoverySource, PatchRecoveryVerificationOutcome, _digest, _expected_postimage,
    _expected_preimage, _require_digest, _require_timestamp, _require_uuid, _utc_now,
    _validated_authorization_receipt,
)
from codexia_manual_agent.mutation._patch_recovery_observation import (
    PatchRecoveryFileObservation, _verification_for_state,
)

@dataclass(frozen=True, slots=True)
class PatchRecoveryReceipt:
    schema_version: int
    receipt_id: str
    created_at: str
    source: PatchRecoverySource
    proposal_id: str
    proposal_digest: str
    authorization_receipt_id: str
    authorization_receipt_digest: str
    execution_id: str
    plan_digest: str
    change_set_digest: str
    recovered_commit_state: PatchCommitState
    original_application_result: dict[str, Any] | None
    original_application_result_digest: str | None
    journal_last_record_digest: str | None
    journal_last_phase: PatchRecoveryJournalPhase | None
    journal_torn_tail: bool
    journal_terminal_result: dict[str, Any] | None
    journal_terminal_result_digest: str | None
    original_process_confirmed_terminated: bool
    ktm_outcome_before: PatchKtmOutcome | None
    ktm_outcome_after: PatchKtmOutcome | None
    rollback_retry_attempted: bool
    transaction_cleanup_error: str | None
    verification_outcome: PatchRecoveryVerificationOutcome
    file_observations: tuple[PatchRecoveryFileObservation, ...]
    receipt_digest: str

    @classmethod
    def create(
        cls,
        *,
        source: PatchRecoverySource,
        lifecycle: ActionLifecycle,
        plan: PatchExecutionPlan,
        authorization_receipt: AuthorizationReceipt,
        recovered_commit_state: PatchCommitState,
        file_observations: tuple[PatchRecoveryFileObservation, ...],
        original_application_result: PatchApplicationResult | None = None,
        journal_last_record_digest: str | None = None,
        journal_last_phase: PatchRecoveryJournalPhase | None = None,
        journal_torn_tail: bool = False,
        journal_terminal_result: PatchApplicationResult | None = None,
        original_process_confirmed_terminated: bool = False,
        ktm_outcome_before: PatchKtmOutcome | None = None,
        ktm_outcome_after: PatchKtmOutcome | None = None,
        rollback_retry_attempted: bool = False,
        transaction_cleanup_error: str | None = None,
        receipt_id: str | None = None,
        created_at: str | None = None,
    ) -> "PatchRecoveryReceipt":
        source = PatchRecoverySource(source)
        recovered_commit_state = PatchCommitState(recovered_commit_state)
        receipt_id = receipt_id or str(uuid4())
        created_at = created_at or _utc_now()
        original_payload = (
            original_application_result.to_dict()
            if original_application_result
            else None
        )
        terminal_payload = journal_terminal_result.to_dict() if journal_terminal_result else None
        verification = _verification_for_state(recovered_commit_state, file_observations)
        payload = {
            "schema_version": PATCH_RECOVERY_RECEIPT_SCHEMA_VERSION,
            "receipt_id": receipt_id,
            "created_at": created_at,
            "source": source.value,
            "proposal_id": plan.proposal_id,
            "proposal_digest": plan.proposal_digest,
            "authorization_receipt_id": authorization_receipt.receipt_id,
            "authorization_receipt_digest": authorization_receipt.receipt_digest,
            "execution_id": lifecycle.execution_id,
            "plan_digest": plan.plan_digest,
            "change_set_digest": plan.change_set_digest,
            "recovered_commit_state": recovered_commit_state.value,
            "original_application_result": original_payload,
            "original_application_result_digest": (
                _digest(original_payload) if original_payload else None
            ),
            "journal_last_record_digest": journal_last_record_digest,
            "journal_last_phase": journal_last_phase.value if journal_last_phase else None,
            "journal_torn_tail": journal_torn_tail,
            "journal_terminal_result": terminal_payload,
            "journal_terminal_result_digest": (
                _digest(terminal_payload) if terminal_payload else None
            ),
            "original_process_confirmed_terminated": original_process_confirmed_terminated,
            "ktm_outcome_before": ktm_outcome_before.value if ktm_outcome_before else None,
            "ktm_outcome_after": ktm_outcome_after.value if ktm_outcome_after else None,
            "rollback_retry_attempted": rollback_retry_attempted,
            "transaction_cleanup_error": transaction_cleanup_error,
            "verification_outcome": verification.value,
            "file_observation_digests": [obs.observation_digest for obs in file_observations],
        }
        return cls(
            schema_version=PATCH_RECOVERY_RECEIPT_SCHEMA_VERSION,
            receipt_id=receipt_id,
            created_at=created_at,
            source=source,
            proposal_id=plan.proposal_id,
            proposal_digest=plan.proposal_digest,
            authorization_receipt_id=authorization_receipt.receipt_id,
            authorization_receipt_digest=authorization_receipt.receipt_digest,
            execution_id=lifecycle.execution_id,
            plan_digest=plan.plan_digest,
            change_set_digest=plan.change_set_digest,
            recovered_commit_state=recovered_commit_state,
            original_application_result=original_payload,
            original_application_result_digest=(
                _digest(original_payload) if original_payload else None
            ),
            journal_last_record_digest=journal_last_record_digest,
            journal_last_phase=journal_last_phase,
            journal_torn_tail=journal_torn_tail,
            journal_terminal_result=terminal_payload,
            journal_terminal_result_digest=_digest(terminal_payload) if terminal_payload else None,
            original_process_confirmed_terminated=original_process_confirmed_terminated,
            ktm_outcome_before=ktm_outcome_before,
            ktm_outcome_after=ktm_outcome_after,
            rollback_retry_attempted=rollback_retry_attempted,
            transaction_cleanup_error=transaction_cleanup_error,
            verification_outcome=verification,
            file_observations=file_observations,
            receipt_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        if self.schema_version != PATCH_RECOVERY_RECEIPT_SCHEMA_VERSION:
            raise InvalidWorkspaceMutationError("Unsupported patch recovery receipt schema")
        _require_uuid(self.receipt_id, "Patch recovery receipt_id")
        _require_timestamp(self.created_at, "Patch recovery receipt created_at")
        object.__setattr__(self, "source", PatchRecoverySource(self.source))
        _require_uuid(self.proposal_id, "Patch recovery proposal_id")
        _require_digest(self.proposal_digest, "Patch recovery proposal digest")
        _require_uuid(self.authorization_receipt_id, "Patch recovery authorization receipt_id")
        _require_digest(
            self.authorization_receipt_digest,
            "Patch recovery authorization receipt digest",
        )
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise InvalidWorkspaceMutationError("Patch recovery execution_id is required")
        _require_digest(self.plan_digest, "Patch recovery plan digest")
        _require_digest(self.change_set_digest, "Patch recovery change-set digest")
        object.__setattr__(
            self,
            "recovered_commit_state",
            PatchCommitState(self.recovered_commit_state),
        )
        if self.recovered_commit_state is PatchCommitState.INDETERMINATE:
            raise InvalidWorkspaceMutationError(
                "Patch recovery receipt cannot claim an indeterminate terminal outcome"
            )
        if self.original_application_result is not None:
            if not isinstance(self.original_application_result, dict):
                raise InvalidWorkspaceMutationError("Original application result must be an object")
            result = PatchApplicationResult(**self.original_application_result)
            _require_digest(
                self.original_application_result_digest,
                "Patch recovery original application result digest",
            )
            if not hmac.compare_digest(
                _digest(result.to_dict()), self.original_application_result_digest
            ):
                raise InvalidWorkspaceMutationError(
                    "Original application result digest does not match projection"
                )
        elif self.original_application_result_digest is not None:
            raise InvalidWorkspaceMutationError(
                "Original application result digest requires its projection"
            )
        if self.journal_last_record_digest is not None:
            _require_digest(self.journal_last_record_digest, "Patch recovery journal record digest")
        if self.journal_last_phase is not None:
            object.__setattr__(
                self,
                "journal_last_phase",
                PatchRecoveryJournalPhase(self.journal_last_phase),
            )
        if type(self.journal_torn_tail) is not bool:
            raise InvalidWorkspaceMutationError(
                "Patch recovery journal torn-tail flag must be boolean"
            )
        if self.journal_terminal_result is not None:
            if not isinstance(self.journal_terminal_result, dict):
                raise InvalidWorkspaceMutationError("Journal terminal result must be an object")
            terminal = PatchApplicationResult(**self.journal_terminal_result)
            _require_digest(
                self.journal_terminal_result_digest,
                "Patch recovery journal terminal result digest",
            )
            if terminal.commit_state is PatchCommitState.INDETERMINATE:
                raise InvalidWorkspaceMutationError(
                    "Journal terminal result cannot be indeterminate"
                )
            if terminal.commit_state is not self.recovered_commit_state:
                raise InvalidWorkspaceMutationError(
                    "Journal terminal state disagrees with recovered state"
                )
            if not hmac.compare_digest(
                _digest(terminal.to_dict()), self.journal_terminal_result_digest
            ):
                raise InvalidWorkspaceMutationError(
                    "Journal terminal result digest does not match projection"
                )
        elif self.journal_terminal_result_digest is not None:
            raise InvalidWorkspaceMutationError(
                "Journal terminal result digest requires its projection"
            )
        if type(self.original_process_confirmed_terminated) is not bool:
            raise InvalidWorkspaceMutationError(
                "Original process termination confirmation must be boolean"
            )
        if self.ktm_outcome_before is not None:
            object.__setattr__(self, "ktm_outcome_before", PatchKtmOutcome(self.ktm_outcome_before))
        if self.ktm_outcome_after is not None:
            object.__setattr__(self, "ktm_outcome_after", PatchKtmOutcome(self.ktm_outcome_after))
        if type(self.rollback_retry_attempted) is not bool:
            raise InvalidWorkspaceMutationError("Rollback retry flag must be boolean")
        if self.transaction_cleanup_error is not None and (
            not isinstance(self.transaction_cleanup_error, str)
            or not self.transaction_cleanup_error
        ):
            raise InvalidWorkspaceMutationError(
                "Transaction cleanup error must be non-empty text"
            )
        object.__setattr__(
            self,
            "verification_outcome",
            PatchRecoveryVerificationOutcome(self.verification_outcome),
        )
        if not isinstance(self.file_observations, tuple) or not self.file_observations:
            raise InvalidWorkspaceMutationError("Patch recovery receipt requires file observations")
        for obs in self.file_observations:
            obs.__post_init__()
        expected_verification = _verification_for_state(
            self.recovered_commit_state, self.file_observations
        )
        if self.verification_outcome is not expected_verification:
            raise InvalidWorkspaceMutationError(
                "Patch recovery verification outcome disagrees with file evidence"
            )
        self._validate_source_contract()
        _require_digest(self.receipt_digest, "Patch recovery receipt digest")
        if not hmac.compare_digest(_digest(self._payload()), self.receipt_digest):
            raise InvalidWorkspaceMutationError(
                "Patch recovery receipt digest does not match payload"
            )

    def _validate_source_contract(self) -> None:
        if self.source is PatchRecoverySource.LIVE_KTM:
            if self.original_application_result is None:
                raise InvalidWorkspaceMutationError(
                    "Live KTM recovery requires original indeterminate application result"
                )
            original = PatchApplicationResult(**self.original_application_result)
            if original.commit_state is not PatchCommitState.INDETERMINATE:
                raise InvalidWorkspaceMutationError(
                    "Live KTM recovery requires original INDETERMINATE result"
                )
            if (
                self.journal_last_record_digest is not None
                or self.journal_last_phase is not None
                or self.journal_torn_tail
                or self.journal_terminal_result is not None
                or self.original_process_confirmed_terminated
                or self.ktm_outcome_before is None
                or self.ktm_outcome_after is None
            ):
                raise InvalidWorkspaceMutationError(
                    "Live KTM recovery source fields are inconsistent"
                )
            terminal_outcome = (
                PatchKtmOutcome.COMMITTED
                if self.recovered_commit_state is PatchCommitState.COMMITTED
                else PatchKtmOutcome.ABORTED
            )
            if self.ktm_outcome_after is not terminal_outcome:
                raise InvalidWorkspaceMutationError(
                    "Live KTM final outcome disagrees with recovered commit state"
                )
            if (
                self.ktm_outcome_before is PatchKtmOutcome.UNDETERMINED
            ) != self.rollback_retry_attempted:
                raise InvalidWorkspaceMutationError(
                    "Live KTM rollback retry flag disagrees with initial outcome"
                )
            return
        if self.source is PatchRecoverySource.JOURNAL_TERMINAL:
            if (
                self.original_application_result is not None
                or self.journal_last_record_digest is None
                or self.journal_last_phase is not PatchRecoveryJournalPhase.TERMINAL
                or self.journal_terminal_result is None
                or not self.original_process_confirmed_terminated
                or self.ktm_outcome_before is not None
                or self.ktm_outcome_after is not None
                or self.rollback_retry_attempted
                or self.transaction_cleanup_error is not None
            ):
                raise InvalidWorkspaceMutationError(
                    "Journal-terminal recovery source fields are inconsistent"
                )
            return
        if (
            self.source is not PatchRecoverySource.PRESUMED_ABORT
            or self.recovered_commit_state is not PatchCommitState.ROLLED_BACK
            or self.original_application_result is not None
            or self.journal_last_record_digest is None
            or self.journal_last_phase is not PatchRecoveryJournalPhase.EXECUTION_STARTED
            or self.journal_terminal_result is not None
            or not self.original_process_confirmed_terminated
            or self.ktm_outcome_before is not None
            or self.ktm_outcome_after is not None
            or self.rollback_retry_attempted
            or self.transaction_cleanup_error is not None
        ):
            raise InvalidWorkspaceMutationError(
                "Presumed-abort recovery source fields are inconsistent"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "created_at": self.created_at,
            "source": self.source.value,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "authorization_receipt_id": self.authorization_receipt_id,
            "authorization_receipt_digest": self.authorization_receipt_digest,
            "execution_id": self.execution_id,
            "plan_digest": self.plan_digest,
            "change_set_digest": self.change_set_digest,
            "recovered_commit_state": self.recovered_commit_state.value,
            "original_application_result": self.original_application_result,
            "original_application_result_digest": self.original_application_result_digest,
            "journal_last_record_digest": self.journal_last_record_digest,
            "journal_last_phase": (
                self.journal_last_phase.value if self.journal_last_phase else None
            ),
            "journal_torn_tail": self.journal_torn_tail,
            "journal_terminal_result": self.journal_terminal_result,
            "journal_terminal_result_digest": self.journal_terminal_result_digest,
            "original_process_confirmed_terminated": self.original_process_confirmed_terminated,
            "ktm_outcome_before": (
                self.ktm_outcome_before.value if self.ktm_outcome_before else None
            ),
            "ktm_outcome_after": self.ktm_outcome_after.value if self.ktm_outcome_after else None,
            "rollback_retry_attempted": self.rollback_retry_attempted,
            "transaction_cleanup_error": self.transaction_cleanup_error,
            "verification_outcome": self.verification_outcome.value,
            "file_observation_digests": [obs.observation_digest for obs in self.file_observations],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["file_observations"] = [obs.to_dict() for obs in self.file_observations]
        payload["receipt_digest"] = self.receipt_digest
        return payload


def _validate_recovery_lifecycle_identity(
    lifecycle: ActionLifecycle,
    plan: PatchExecutionPlan,
) -> AuthorizationReceipt:
    if lifecycle.phase not in {ActionPhase.EXECUTED, ActionPhase.OBSERVED}:
        raise InvalidActionTransitionError(
            "Patch recovery evidence requires EXECUTED or OBSERVED lifecycle"
        )
    if lifecycle.execution_id is None or not lifecycle.execution_id.strip():
        raise InvalidActionTransitionError(
            "Patch recovery evidence lifecycle has no execution_id"
        )
    receipt = _validated_authorization_receipt(lifecycle.authorization)
    if (
        receipt.decision is not AuthorizationDecision.ALLOW
        or receipt.proposal_id != lifecycle.proposal.proposal_id
        or receipt.proposal_digest != lifecycle.proposal.proposal_digest
        or receipt.mode is not lifecycle.mode
        or getattr(lifecycle, "_consumed_receipt_id", None) != receipt.receipt_id
    ):
        raise InvalidWorkspaceMutationError(
            "Patch recovery evidence is not bound to the consumed authorization"
        )
    validate_patch_execution_plan_binding(lifecycle.proposal, plan)
    return receipt


def validate_patch_recovery_receipt_binding(
    lifecycle: ActionLifecycle,
    plan: PatchExecutionPlan,
    recovery_receipt: PatchRecoveryReceipt,
) -> None:
    if not isinstance(lifecycle, ActionLifecycle):
        raise TypeError("lifecycle must be ActionLifecycle")
    if not isinstance(plan, PatchExecutionPlan):
        raise TypeError("plan must be PatchExecutionPlan")
    authorization = _validate_recovery_lifecycle_identity(lifecycle, plan)
    if not isinstance(recovery_receipt, PatchRecoveryReceipt):
        raise TypeError("recovery_receipt must be PatchRecoveryReceipt")
    recovery_receipt.__post_init__()
    if (
        recovery_receipt.proposal_id != plan.proposal_id
        or recovery_receipt.proposal_digest != plan.proposal_digest
        or recovery_receipt.authorization_receipt_id != authorization.receipt_id
        or recovery_receipt.authorization_receipt_digest != authorization.receipt_digest
        or recovery_receipt.execution_id != lifecycle.execution_id
        or recovery_receipt.plan_digest != plan.plan_digest
        or recovery_receipt.change_set_digest != plan.change_set_digest
        or len(recovery_receipt.file_observations) != len(plan.steps)
    ):
        raise InvalidWorkspaceMutationError(
            "Patch recovery receipt is not bound to exact lifecycle and plan"
        )
    if (
        lifecycle.phase is ActionPhase.OBSERVED
        and lifecycle.observation_id != recovery_receipt.receipt_id
    ):
        raise InvalidWorkspaceMutationError(
            "Observed lifecycle is not bound to this patch recovery receipt"
        )
    for projected in (
        recovery_receipt.original_application_result,
        recovery_receipt.journal_terminal_result,
    ):
        if projected is None:
            continue
        result = PatchApplicationResult(**projected)
        if (
            result.execution_id != recovery_receipt.execution_id
            or result.proposal_id != recovery_receipt.proposal_id
            or result.proposal_digest != recovery_receipt.proposal_digest
            or result.plan_digest != recovery_receipt.plan_digest
            or result.change_set_digest != recovery_receipt.change_set_digest
            or result.commit_model != PATCH_COMMIT_MODEL
        ):
            raise InvalidWorkspaceMutationError(
                "Patch recovery application-result projection is not bound to receipt"
            )
    for step, observation in zip(
        plan.steps, recovery_receipt.file_observations, strict=True
    ):
        observation.__post_init__()
        if (
            observation.step_index != step.index
            or observation.change_digest != step.change_digest
            or observation.primitive_digest != step.primitive_digest
            or observation.operation is not step.operation
            or observation.target != step.target
            or observation.expected_preimage != _expected_preimage(step)
            or observation.expected_postimage != _expected_postimage(step)
        ):
            raise InvalidWorkspaceMutationError(
                f"Patch recovery file observation is not bound to plan step {step.index}"
            )

def _finalize_recovery_receipt(
    lifecycle: ActionLifecycle,
    plan: PatchExecutionPlan,
    recovery: PatchRecoveryReceipt,
) -> PatchRecoveryReceipt:
    validate_patch_recovery_receipt_binding(lifecycle, plan, recovery)
    lifecycle.record_observed(recovery.receipt_id)
    return recovery
