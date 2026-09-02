from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from codexia_manual_agent.authority import ActionLifecycle, ActionPhase, AuthorizationReceipt
from codexia_manual_agent.domain.errors import (
    InvalidActionTransitionError,
    InvalidWorkspaceMutationError,
)
from codexia_manual_agent.mutation.patch_execution_plan import PatchExecutionPlan
from codexia_manual_agent.mutation._patch_recovery_common import (
    PATCH_CRASH_ASSESSMENT_SCHEMA_VERSION, PatchCrashFilesystemClassification,
    PatchRecoveryJournalPhase, _digest, _expected_postimage, _expected_preimage,
    _require_digest, _require_timestamp, _require_uuid, _utc_now,
)
from codexia_manual_agent.mutation._patch_recovery_journal import PatchRecoveryJournalRead
from codexia_manual_agent.mutation._patch_recovery_observation import (
    PatchRecoveryFileObservation,
    _crash_classification,
)
from codexia_manual_agent.mutation._patch_recovery_receipt import (
    _validate_recovery_lifecycle_identity,
)

@dataclass(frozen=True, slots=True)
class PatchCrashRecoveryAssessment:
    schema_version: int
    assessment_id: str
    created_at: str
    proposal_id: str
    proposal_digest: str
    authorization_receipt_id: str
    authorization_receipt_digest: str
    execution_id: str
    plan_digest: str
    change_set_digest: str
    journal_last_phase: PatchRecoveryJournalPhase
    journal_last_record_digest: str
    journal_torn_tail: bool
    filesystem_classification: PatchCrashFilesystemClassification
    file_observations: tuple[PatchRecoveryFileObservation, ...]
    assessment_digest: str

    @classmethod
    def create(
        cls,
        *,
        lifecycle: ActionLifecycle,
        plan: PatchExecutionPlan,
        receipt: AuthorizationReceipt,
        journal_read: PatchRecoveryJournalRead,
        file_observations: tuple[PatchRecoveryFileObservation, ...],
    ) -> "PatchCrashRecoveryAssessment":
        last = journal_read.records[-1]
        assessment_id = str(uuid4())
        created_at = _utc_now()
        classification = _crash_classification(file_observations)
        payload = {
            "schema_version": PATCH_CRASH_ASSESSMENT_SCHEMA_VERSION,
            "assessment_id": assessment_id,
            "created_at": created_at,
            "proposal_id": plan.proposal_id,
            "proposal_digest": plan.proposal_digest,
            "authorization_receipt_id": receipt.receipt_id,
            "authorization_receipt_digest": receipt.receipt_digest,
            "execution_id": lifecycle.execution_id,
            "plan_digest": plan.plan_digest,
            "change_set_digest": plan.change_set_digest,
            "journal_last_phase": last.phase.value,
            "journal_last_record_digest": last.record_digest,
            "journal_torn_tail": journal_read.torn_tail,
            "filesystem_classification": classification.value,
            "file_observation_digests": [obs.observation_digest for obs in file_observations],
        }
        return cls(
            schema_version=PATCH_CRASH_ASSESSMENT_SCHEMA_VERSION,
            assessment_id=assessment_id,
            created_at=created_at,
            proposal_id=plan.proposal_id,
            proposal_digest=plan.proposal_digest,
            authorization_receipt_id=receipt.receipt_id,
            authorization_receipt_digest=receipt.receipt_digest,
            execution_id=lifecycle.execution_id,
            plan_digest=plan.plan_digest,
            change_set_digest=plan.change_set_digest,
            journal_last_phase=last.phase,
            journal_last_record_digest=last.record_digest,
            journal_torn_tail=journal_read.torn_tail,
            filesystem_classification=classification,
            file_observations=file_observations,
            assessment_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        if self.schema_version != PATCH_CRASH_ASSESSMENT_SCHEMA_VERSION:
            raise InvalidWorkspaceMutationError("Unsupported patch crash assessment schema")
        _require_uuid(self.assessment_id, "Patch crash assessment_id")
        _require_timestamp(self.created_at, "Patch crash assessment created_at")
        _require_uuid(self.proposal_id, "Patch crash proposal_id")
        _require_digest(self.proposal_digest, "Patch crash proposal digest")
        _require_uuid(self.authorization_receipt_id, "Patch crash authorization receipt_id")
        _require_digest(
            self.authorization_receipt_digest,
            "Patch crash authorization receipt digest",
        )
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise InvalidWorkspaceMutationError("Patch crash execution_id is required")
        _require_digest(self.plan_digest, "Patch crash plan digest")
        _require_digest(self.change_set_digest, "Patch crash change-set digest")
        object.__setattr__(
            self,
            "journal_last_phase",
            PatchRecoveryJournalPhase(self.journal_last_phase),
        )
        if self.journal_last_phase is not PatchRecoveryJournalPhase.COMMIT_INTENT:
            raise InvalidWorkspaceMutationError(
                "Crash assessment is valid only for ambiguous COMMIT_INTENT"
            )
        _require_digest(self.journal_last_record_digest, "Patch crash journal digest")
        if type(self.journal_torn_tail) is not bool:
            raise InvalidWorkspaceMutationError("Patch crash torn-tail flag must be boolean")
        object.__setattr__(
            self,
            "filesystem_classification",
            PatchCrashFilesystemClassification(self.filesystem_classification),
        )
        if not isinstance(self.file_observations, tuple) or not self.file_observations:
            raise InvalidWorkspaceMutationError("Patch crash assessment requires file observations")
        for obs in self.file_observations:
            obs.__post_init__()
        expected_class = _crash_classification(self.file_observations)
        if self.filesystem_classification is not expected_class:
            raise InvalidWorkspaceMutationError(
                "Patch crash filesystem classification disagrees with observations"
            )
        _require_digest(self.assessment_digest, "Patch crash assessment digest")
        if not hmac.compare_digest(_digest(self._payload()), self.assessment_digest):
            raise InvalidWorkspaceMutationError(
                "Patch crash assessment digest does not match payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assessment_id": self.assessment_id,
            "created_at": self.created_at,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "authorization_receipt_id": self.authorization_receipt_id,
            "authorization_receipt_digest": self.authorization_receipt_digest,
            "execution_id": self.execution_id,
            "plan_digest": self.plan_digest,
            "change_set_digest": self.change_set_digest,
            "journal_last_phase": self.journal_last_phase.value,
            "journal_last_record_digest": self.journal_last_record_digest,
            "journal_torn_tail": self.journal_torn_tail,
            "filesystem_classification": self.filesystem_classification.value,
            "file_observation_digests": [obs.observation_digest for obs in self.file_observations],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["file_observations"] = [obs.to_dict() for obs in self.file_observations]
        payload["assessment_digest"] = self.assessment_digest
        return payload

def validate_patch_crash_recovery_assessment_binding(
    lifecycle: ActionLifecycle,
    plan: PatchExecutionPlan,
    assessment: PatchCrashRecoveryAssessment,
) -> None:
    if not isinstance(lifecycle, ActionLifecycle):
        raise TypeError("lifecycle must be ActionLifecycle")
    if not isinstance(plan, PatchExecutionPlan):
        raise TypeError("plan must be PatchExecutionPlan")
    authorization = _validate_recovery_lifecycle_identity(lifecycle, plan)
    if lifecycle.phase is not ActionPhase.EXECUTED:
        raise InvalidActionTransitionError(
            "Ambiguous crash assessment must leave lifecycle EXECUTED"
        )
    if not isinstance(assessment, PatchCrashRecoveryAssessment):
        raise TypeError("assessment must be PatchCrashRecoveryAssessment")
    assessment.__post_init__()
    if (
        assessment.proposal_id != plan.proposal_id
        or assessment.proposal_digest != plan.proposal_digest
        or assessment.authorization_receipt_id != authorization.receipt_id
        or assessment.authorization_receipt_digest != authorization.receipt_digest
        or assessment.execution_id != lifecycle.execution_id
        or assessment.plan_digest != plan.plan_digest
        or assessment.change_set_digest != plan.change_set_digest
        or len(assessment.file_observations) != len(plan.steps)
    ):
        raise InvalidWorkspaceMutationError(
            "Patch crash assessment is not bound to exact lifecycle and plan"
        )
    for step, observation in zip(plan.steps, assessment.file_observations, strict=True):
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
                f"Patch crash file observation is not bound to plan step {step.index}"
            )
