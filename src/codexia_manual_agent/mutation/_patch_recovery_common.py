from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID

from codexia_manual_agent.authority import (
    ActionLifecycle, ActionPhase, AuthorizationDecision, AuthorizationReceipt,
)
from codexia_manual_agent.domain.errors import (
    InvalidActionTransitionError, InvalidWorkspaceMutationError, WorkspaceMutationBoundaryError,
)
from codexia_manual_agent.mutation.models import MutationOperation
from codexia_manual_agent.mutation.patch_application import PatchApplicationResult
from codexia_manual_agent.mutation.patch_execution_plan import (
    PatchExecutionPlan, PatchExecutionStep, validate_patch_execution_plan_binding,
)
from codexia_manual_agent.mutation.patch_observation import PatchExpectedFileState

PATCH_RECOVERY_JOURNAL_SCHEMA_VERSION = 1
PATCH_RECOVERY_FILE_OBSERVATION_SCHEMA_VERSION = 1
PATCH_RECOVERY_RECEIPT_SCHEMA_VERSION = 1
PATCH_CRASH_ASSESSMENT_SCHEMA_VERSION = 1

MAX_RECOVERY_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_RECOVERY_JOURNAL_RECORDS = 4096
MAX_RECOVERY_JOURNAL_LINE_BYTES = 64 * 1024

class PatchRecoveryJournalPhase(StrEnum):
    EXECUTION_STARTED = "execution_started"
    COMMIT_INTENT = "commit_intent"
    TERMINAL = "terminal"


class PatchRecoverySource(StrEnum):
    LIVE_KTM = "live_ktm"
    JOURNAL_TERMINAL = "journal_terminal"
    PRESUMED_ABORT = "presumed_abort"


class PatchKtmOutcome(StrEnum):
    UNDETERMINED = "undetermined"
    COMMITTED = "committed"
    ABORTED = "aborted"


class PatchRecoveryVerificationOutcome(StrEnum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    INCOMPLETE = "incomplete"


class PatchCrashFilesystemClassification(StrEnum):
    PREIMAGE_SET = "preimage_set"
    POSTIMAGE_SET = "postimage_set"
    MIXED = "mixed"
    INCOMPLETE = "incomplete"


class PatchRecoveryPersistenceError(WorkspaceMutationBoundaryError):
    """A terminal filesystem result exists but its recovery marker did not persist."""

    def __init__(self, message: str, application_result: PatchApplicationResult) -> None:
        super().__init__(message)
        self.application_result = application_result


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidWorkspaceMutationError(f"{label} must be a UUID") from exc


def _require_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise InvalidWorkspaceMutationError(f"{label} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise InvalidWorkspaceMutationError(
            f"{label} must be a SHA-256 hex digest"
        ) from exc


def _require_timestamp(value: str, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkspaceMutationError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise InvalidWorkspaceMutationError(f"{label} must include a timezone")


def _validated_application_result(result: PatchApplicationResult) -> PatchApplicationResult:
    if not isinstance(result, PatchApplicationResult):
        raise TypeError("application_result must be a PatchApplicationResult")
    try:
        return PatchApplicationResult(**result.to_dict())
    except Exception as exc:
        raise InvalidWorkspaceMutationError(
            "Patch recovery application result integrity validation failed"
        ) from exc


def _validated_authorization_receipt(receipt: AuthorizationReceipt) -> AuthorizationReceipt:
    if not isinstance(receipt, AuthorizationReceipt):
        raise InvalidActionTransitionError(
            "Patch recovery requires a valid authorization receipt"
        )
    try:
        return AuthorizationReceipt(**receipt.to_dict())
    except Exception as exc:
        raise InvalidWorkspaceMutationError(
            "Patch recovery authorization receipt integrity validation failed"
        ) from exc


def _validate_executed_binding(
    lifecycle: ActionLifecycle,
    plan: PatchExecutionPlan,
) -> AuthorizationReceipt:
    if lifecycle.phase is not ActionPhase.EXECUTED:
        raise InvalidActionTransitionError(
            "Patch recovery requires an EXECUTED lifecycle"
        )
    if lifecycle.execution_id is None or not lifecycle.execution_id.strip():
        raise InvalidActionTransitionError(
            "Executed patch lifecycle has no execution_id"
        )
    if lifecycle.observation_id is not None:
        raise InvalidActionTransitionError(
            "Patch recovery lifecycle already carries an observation_id"
        )
    receipt = _validated_authorization_receipt(lifecycle.authorization)
    if (
        receipt.decision is not AuthorizationDecision.ALLOW
        or receipt.proposal_id != lifecycle.proposal.proposal_id
        or receipt.proposal_digest != lifecycle.proposal.proposal_digest
        or receipt.mode is not lifecycle.mode
    ):
        raise InvalidWorkspaceMutationError(
            "Patch recovery authorization receipt is not bound to lifecycle"
        )
    if getattr(lifecycle, "_consumed_receipt_id", None) != receipt.receipt_id:
        raise InvalidWorkspaceMutationError(
            "Patch recovery requires the exact consumed authorization receipt"
        )
    validate_patch_execution_plan_binding(lifecycle.proposal, plan)
    if (
        plan.proposal_id != lifecycle.proposal.proposal_id
        or plan.proposal_digest != lifecycle.proposal.proposal_digest
    ):
        raise InvalidWorkspaceMutationError(
            "Patch recovery plan is not bound to lifecycle proposal"
        )
    return receipt


def _expected_preimage(step: PatchExecutionStep) -> PatchExpectedFileState:
    return PatchExpectedFileState.from_preimage(step.expected_preimage)


def _expected_postimage(step: PatchExecutionStep) -> PatchExpectedFileState:
    mode = step.expected_preimage.mode if step.operation is MutationOperation.REPLACE else None
    return PatchExpectedFileState.present(
        size_bytes=len(step.postimage),
        digest=sha256(step.postimage).hexdigest(),
        mode=mode,
    )
