from __future__ import annotations

import json
import os
import tempfile
import unittest
from hashlib import sha256
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
)
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchFileRequest,
    build_patch_execution_plan,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation.models import PreimageSnapshot
from codexia_manual_agent.mutation.patch_application import (
    PATCH_APPLICATION_SCHEMA_VERSION,
    PATCH_COMMIT_MODEL,
    PatchApplicationExecutor,
    PatchApplicationResult,
    PatchCommitState,
    PatchFailureStage,
)
from codexia_manual_agent.mutation import patch_application as patch_application_module
from codexia_manual_agent.mutation import _patch_recovery_common as common_module
from codexia_manual_agent.mutation import _patch_recovery_journal as journal_module
from codexia_manual_agent.mutation import _patch_recovery_parent as parent_module
from codexia_manual_agent.mutation import _patch_recovery_runtime as runtime_module
from codexia_manual_agent.mutation.patch_recovery import (
    MAX_RECOVERY_JOURNAL_BYTES,
    PatchCrashFilesystemClassification,
    PatchCrashRecoveryAssessment,
    PatchKtmOutcome,
    PatchRecoveryFileObservation,
    PatchRecoveryJournal,
    PatchRecoveryJournalPhase,
    PatchRecoveryManager,
    PatchRecoveryPersistenceError,
    PatchRecoveryReceipt,
    PatchRecoverySource,
    PatchRecoveryVerificationOutcome,
    RecoverablePatchApplicationExecutor,
    validate_patch_crash_recovery_assessment_binding,
    validate_patch_recovery_receipt_binding,
)


def _authorized_patch(root: Path, changes: tuple[PatchFileRequest, ...]):
    proposal = prepare_patch_proposal(workspace=root, changes=changes)
    plan = build_patch_execution_plan(proposal)
    authority = LocalApprovalAuthority()
    receipt = authority.decide(
        proposal,
        mode=ApprovalMode.RISKY,
        approved=True,
    )
    lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
    lifecycle.apply_receipt(receipt, authority=authority)
    return proposal, authority, receipt, lifecycle, plan


def _executed_patch(root: Path, changes: tuple[PatchFileRequest, ...]):
    proposal, authority, receipt, lifecycle, plan = _authorized_patch(root, changes)
    lifecycle.consume_authorization(authority=authority)
    lifecycle.record_executed("recovery-test-execution")
    return proposal, authority, receipt, lifecycle, plan


def _committed_result(lifecycle: ActionLifecycle, plan) -> PatchApplicationResult:
    return PatchApplicationResult(
        schema_version=PATCH_APPLICATION_SCHEMA_VERSION,
        execution_id=lifecycle.execution_id,
        proposal_id=plan.proposal_id,
        proposal_digest=plan.proposal_digest,
        change_set_digest=plan.change_set_digest,
        plan_digest=plan.plan_digest,
        commit_model=PATCH_COMMIT_MODEL,
        commit_state=PatchCommitState.COMMITTED,
    )



def _postimage_snapshot(step) -> PreimageSnapshot:
    return PreimageSnapshot.present(
        size_bytes=len(step.postimage),
        digest=sha256(step.postimage).hexdigest(),
        mode=step.expected_preimage.mode or 0o644,
    )


def _observations_for(plan, state: str) -> tuple[PatchRecoveryFileObservation, ...]:
    observations = []
    for step in plan.steps:
        if state == "pre":
            observed = step.expected_preimage
        elif state == "post":
            observed = _postimage_snapshot(step)
        else:
            raise ValueError(state)
        observations.append(
            PatchRecoveryFileObservation.create(
                step=step,
                observed_terminal=observed,
                error=None,
            )
        )
    return tuple(observations)
