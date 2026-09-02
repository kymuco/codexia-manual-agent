from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    AuthorizationDecision,
    AuthorizationReceipt,
)
from codexia_manual_agent.domain.errors import (
    InvalidActionTransitionError,
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation.models import (
    MutationOperation,
    PreimageSnapshot,
    PreimageState,
)
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget
from codexia_manual_agent.mutation.patch_application import (
    PATCH_COMMIT_MODEL,
    PatchApplicationResult,
    PatchCommitState,
    PatchFailureStage,
    _runtime_step,
)
from codexia_manual_agent.mutation.patch_execution_plan import (
    PatchExecutionPlan,
    PatchExecutionStep,
    validate_patch_execution_plan_binding,
)
from codexia_manual_agent.mutation.preflight_executor import _is_windows_host
from codexia_manual_agent.mutation.workspace import _MAX_PREIMAGE_BYTES

PATCH_MUTATION_OBSERVATION_SCHEMA_VERSION = 1
PATCH_MUTATION_RECEIPT_SCHEMA_VERSION = 1


class PatchTerminalExpectation(StrEnum):
    POSTIMAGE = "postimage"
    PREIMAGE = "preimage"


class PatchFileObservationStatus(StrEnum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    INSPECTION_FAILED = "inspection_failed"


class PatchVerificationOutcome(StrEnum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    INCOMPLETE = "incomplete"


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


@dataclass(frozen=True, slots=True)
class PatchExpectedFileState:
    state: PreimageState
    size_bytes: int | None
    sha256: str | None
    mode: int | None

    @classmethod
    def absent(cls) -> "PatchExpectedFileState":
        return cls(PreimageState.ABSENT, None, None, None)

    @classmethod
    def present(
        cls,
        *,
        size_bytes: int,
        digest: str,
        mode: int | None,
    ) -> "PatchExpectedFileState":
        return cls(PreimageState.PRESENT, size_bytes, digest, mode)

    @classmethod
    def from_preimage(cls, snapshot: PreimageSnapshot) -> "PatchExpectedFileState":
        return cls(snapshot.state, snapshot.size_bytes, snapshot.sha256, snapshot.mode)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", PreimageState(self.state))
        if self.state is PreimageState.ABSENT:
            if any(value is not None for value in (self.size_bytes, self.sha256, self.mode)):
                raise InvalidWorkspaceMutationError(
                    "Absent expected patch terminal state cannot carry file metadata"
                )
            return
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise InvalidWorkspaceMutationError(
                "Present expected patch terminal size must be non-negative"
            )
        if self.sha256 is None:
            raise InvalidWorkspaceMutationError(
                "Present expected patch terminal state requires sha256"
            )
        _require_digest(self.sha256, "Patch expected terminal sha256")
        if self.mode is not None and (type(self.mode) is not int or self.mode < 0):
            raise InvalidWorkspaceMutationError(
                "Patch expected terminal mode must be non-negative when bound"
            )

    def matches(self, observed: PreimageSnapshot) -> bool:
        if observed.state is not self.state:
            return False
        if self.state is PreimageState.ABSENT:
            return True
        if observed.size_bytes != self.size_bytes or observed.sha256 != self.sha256:
            return False
        return self.mode is None or observed.mode == self.mode

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class PatchFileMutationObservation:
    schema_version: int
    observation_id: str
    created_at: str
    proposal_id: str
    proposal_digest: str
    authorization_receipt_id: str
    authorization_receipt_digest: str
    execution_id: str
    plan_digest: str
    change_set_digest: str
    step_index: int
    change_digest: str
    primitive_digest: str
    operation: MutationOperation
    target: str
    terminal_expectation: PatchTerminalExpectation
    expected_terminal: PatchExpectedFileState
    observed_terminal: PreimageSnapshot | None
    status: PatchFileObservationStatus
    error: str | None
    observation_digest: str

    @classmethod
    def create(
        cls,
        *,
        proposal_id: str,
        proposal_digest: str,
        authorization_receipt_id: str,
        authorization_receipt_digest: str,
        execution_id: str,
        plan_digest: str,
        change_set_digest: str,
        step: PatchExecutionStep,
        terminal_expectation: PatchTerminalExpectation,
        expected_terminal: PatchExpectedFileState,
        observed_terminal: PreimageSnapshot | None,
        status: PatchFileObservationStatus,
        error: str | None = None,
        observation_id: str | None = None,
        created_at: str | None = None,
    ) -> "PatchFileMutationObservation":
        observation_id = observation_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        status = PatchFileObservationStatus(status)
        terminal_expectation = PatchTerminalExpectation(terminal_expectation)
        payload = {
            "schema_version": PATCH_MUTATION_OBSERVATION_SCHEMA_VERSION,
            "observation_id": observation_id,
            "created_at": created_at,
            "proposal_id": proposal_id,
            "proposal_digest": proposal_digest,
            "authorization_receipt_id": authorization_receipt_id,
            "authorization_receipt_digest": authorization_receipt_digest,
            "execution_id": execution_id,
            "plan_digest": plan_digest,
            "change_set_digest": change_set_digest,
            "step_index": step.index,
            "change_digest": step.change_digest,
            "primitive_digest": step.primitive_digest,
            "operation": step.operation.value,
            "target": step.target,
            "terminal_expectation": terminal_expectation.value,
            "expected_terminal": expected_terminal.to_dict(),
            "observed_terminal": (
                observed_terminal.to_dict() if observed_terminal is not None else None
            ),
            "status": status.value,
            "error": error,
        }
        return cls(
            schema_version=PATCH_MUTATION_OBSERVATION_SCHEMA_VERSION,
            observation_id=observation_id,
            created_at=created_at,
            proposal_id=proposal_id,
            proposal_digest=proposal_digest,
            authorization_receipt_id=authorization_receipt_id,
            authorization_receipt_digest=authorization_receipt_digest,
            execution_id=execution_id,
            plan_digest=plan_digest,
            change_set_digest=change_set_digest,
            step_index=step.index,
            change_digest=step.change_digest,
            primitive_digest=step.primitive_digest,
            operation=step.operation,
            target=step.target,
            terminal_expectation=terminal_expectation,
            expected_terminal=expected_terminal,
            observed_terminal=observed_terminal,
            status=status,
            error=error,
            observation_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        if self.schema_version != PATCH_MUTATION_OBSERVATION_SCHEMA_VERSION:
            raise InvalidWorkspaceMutationError(
                "Unsupported patch file mutation observation schema"
            )
        _require_uuid(self.observation_id, "Patch file observation_id")
        _require_timestamp(self.created_at, "Patch file observation created_at")
        _require_uuid(self.proposal_id, "Patch file proposal_id")
        _require_digest(self.proposal_digest, "Patch file proposal digest")
        _require_uuid(
            self.authorization_receipt_id,
            "Patch file authorization receipt_id",
        )
        _require_digest(
            self.authorization_receipt_digest,
            "Patch file authorization receipt digest",
        )
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise InvalidWorkspaceMutationError(
                "Patch file observation execution_id is required"
            )
        _require_digest(self.plan_digest, "Patch file plan digest")
        _require_digest(self.change_set_digest, "Patch file change-set digest")
        if type(self.step_index) is not int or self.step_index < 0:
            raise InvalidWorkspaceMutationError(
                "Patch file observation step_index must be non-negative"
            )
        _require_digest(self.change_digest, "Patch file change digest")
        _require_digest(self.primitive_digest, "Patch file primitive digest")
        object.__setattr__(self, "operation", MutationOperation(self.operation))
        if not isinstance(self.target, str) or not self.target:
            raise InvalidWorkspaceMutationError(
                "Patch file observation target is required"
            )
        object.__setattr__(
            self,
            "terminal_expectation",
            PatchTerminalExpectation(self.terminal_expectation),
        )
        if not isinstance(self.expected_terminal, PatchExpectedFileState):
            raise TypeError("expected_terminal must be PatchExpectedFileState")
        if self.observed_terminal is not None and not isinstance(
            self.observed_terminal, PreimageSnapshot
        ):
            raise TypeError("observed_terminal must be PreimageSnapshot or None")
        object.__setattr__(self, "status", PatchFileObservationStatus(self.status))
        if self.error is not None and (not isinstance(self.error, str) or not self.error):
            raise InvalidWorkspaceMutationError(
                "Patch file observation error must be non-empty when present"
            )
        if self.status is PatchFileObservationStatus.INSPECTION_FAILED:
            if self.observed_terminal is not None or self.error is None:
                raise InvalidWorkspaceMutationError(
                    "Inspection-failed file observation requires error and no observed state"
                )
        else:
            if self.observed_terminal is None or self.error is not None:
                raise InvalidWorkspaceMutationError(
                    "Verified/mismatched file observation requires observed state and no error"
                )
            matches = self.expected_terminal.matches(self.observed_terminal)
            if matches != (self.status is PatchFileObservationStatus.VERIFIED):
                raise InvalidWorkspaceMutationError(
                    "Patch file observation status disagrees with terminal state"
                )
        _require_digest(self.observation_digest, "Patch file observation digest")
        expected = _digest(self._payload())
        if not hmac.compare_digest(expected, self.observation_digest):
            raise InvalidWorkspaceMutationError(
                "Patch file observation digest does not match payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "created_at": self.created_at,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "authorization_receipt_id": self.authorization_receipt_id,
            "authorization_receipt_digest": self.authorization_receipt_digest,
            "execution_id": self.execution_id,
            "plan_digest": self.plan_digest,
            "change_set_digest": self.change_set_digest,
            "step_index": self.step_index,
            "change_digest": self.change_digest,
            "primitive_digest": self.primitive_digest,
            "operation": self.operation.value,
            "target": self.target,
            "terminal_expectation": self.terminal_expectation.value,
            "expected_terminal": self.expected_terminal.to_dict(),
            "observed_terminal": (
                self.observed_terminal.to_dict()
                if self.observed_terminal is not None
                else None
            ),
            "status": self.status.value,
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["observation_digest"] = self.observation_digest
        return payload


@dataclass(frozen=True, slots=True)
class PatchMutationReceipt:
    schema_version: int
    receipt_id: str
    created_at: str
    proposal_id: str
    proposal_digest: str
    authorization_receipt_id: str
    authorization_receipt_digest: str
    execution_id: str
    plan_digest: str
    change_set_digest: str
    commit_model: str
    application_commit_state: PatchCommitState
    application_result_digest: str
    application_failed_step_index: int | None
    application_failed_target: str | None
    application_failure_stage: PatchFailureStage | None
    application_error: str | None
    application_cleanup_error: str | None
    verification_outcome: PatchVerificationOutcome
    file_observations: tuple[PatchFileMutationObservation, ...]
    receipt_digest: str

    @classmethod
    def create(
        cls,
        *,
        proposal_id: str,
        proposal_digest: str,
        authorization_receipt_id: str,
        authorization_receipt_digest: str,
        execution_id: str,
        plan_digest: str,
        change_set_digest: str,
        application_result: PatchApplicationResult,
        file_observations: tuple[PatchFileMutationObservation, ...],
        receipt_id: str | None = None,
        created_at: str | None = None,
    ) -> "PatchMutationReceipt":
        receipt_id = receipt_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        if any(
            obs.status is PatchFileObservationStatus.INSPECTION_FAILED
            for obs in file_observations
        ):
            outcome = PatchVerificationOutcome.INCOMPLETE
        elif any(
            obs.status is PatchFileObservationStatus.MISMATCH
            for obs in file_observations
        ):
            outcome = PatchVerificationOutcome.MISMATCH
        else:
            outcome = PatchVerificationOutcome.VERIFIED
        result_payload = application_result.to_dict()
        payload = {
            "schema_version": PATCH_MUTATION_RECEIPT_SCHEMA_VERSION,
            "receipt_id": receipt_id,
            "created_at": created_at,
            "proposal_id": proposal_id,
            "proposal_digest": proposal_digest,
            "authorization_receipt_id": authorization_receipt_id,
            "authorization_receipt_digest": authorization_receipt_digest,
            "execution_id": execution_id,
            "plan_digest": plan_digest,
            "change_set_digest": change_set_digest,
            "commit_model": application_result.commit_model,
            "application_commit_state": application_result.commit_state.value,
            "application_result_digest": _digest(result_payload),
            "application_failed_step_index": application_result.failed_step_index,
            "application_failed_target": application_result.failed_target,
            "application_failure_stage": (
                application_result.failure_stage.value
                if application_result.failure_stage is not None
                else None
            ),
            "application_error": application_result.error,
            "application_cleanup_error": application_result.cleanup_error,
            "verification_outcome": outcome.value,
            "file_observation_digests": [
                obs.observation_digest for obs in file_observations
            ],
        }
        return cls(
            schema_version=PATCH_MUTATION_RECEIPT_SCHEMA_VERSION,
            receipt_id=receipt_id,
            created_at=created_at,
            proposal_id=proposal_id,
            proposal_digest=proposal_digest,
            authorization_receipt_id=authorization_receipt_id,
            authorization_receipt_digest=authorization_receipt_digest,
            execution_id=execution_id,
            plan_digest=plan_digest,
            change_set_digest=change_set_digest,
            commit_model=application_result.commit_model,
            application_commit_state=application_result.commit_state,
            application_result_digest=_digest(result_payload),
            application_failed_step_index=application_result.failed_step_index,
            application_failed_target=application_result.failed_target,
            application_failure_stage=application_result.failure_stage,
            application_error=application_result.error,
            application_cleanup_error=application_result.cleanup_error,
            verification_outcome=outcome,
            file_observations=file_observations,
            receipt_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        if self.schema_version != PATCH_MUTATION_RECEIPT_SCHEMA_VERSION:
            raise InvalidWorkspaceMutationError(
                "Unsupported patch mutation receipt schema"
            )
        _require_uuid(self.receipt_id, "Patch mutation receipt_id")
        _require_timestamp(self.created_at, "Patch mutation receipt created_at")
        _require_uuid(self.proposal_id, "Patch mutation proposal_id")
        _require_digest(self.proposal_digest, "Patch mutation proposal digest")
        _require_uuid(
            self.authorization_receipt_id,
            "Patch mutation authorization receipt_id",
        )
        _require_digest(
            self.authorization_receipt_digest,
            "Patch mutation authorization receipt digest",
        )
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise InvalidWorkspaceMutationError(
                "Patch mutation receipt execution_id is required"
            )
        _require_digest(self.plan_digest, "Patch mutation plan digest")
        _require_digest(self.change_set_digest, "Patch mutation change-set digest")
        if self.commit_model != PATCH_COMMIT_MODEL:
            raise InvalidWorkspaceMutationError(
                "Patch mutation receipt has unsupported commit model"
            )
        object.__setattr__(
            self,
            "application_commit_state",
            PatchCommitState(self.application_commit_state),
        )
        if self.application_commit_state is PatchCommitState.INDETERMINATE:
            raise InvalidWorkspaceMutationError(
                "Indeterminate patch outcome cannot produce an M2.4.4 mutation receipt"
            )
        _require_digest(
            self.application_result_digest,
            "Patch mutation application result digest",
        )
        if self.application_failed_step_index is not None and (
            type(self.application_failed_step_index) is not int
            or self.application_failed_step_index < 0
        ):
            raise InvalidWorkspaceMutationError(
                "Patch mutation failed step index must be non-negative"
            )
        if (self.application_failed_step_index is None) != (
            self.application_failed_target is None
        ):
            raise InvalidWorkspaceMutationError(
                "Patch mutation failed step index/target must be present together"
            )
        if self.application_failure_stage is not None:
            object.__setattr__(
                self,
                "application_failure_stage",
                PatchFailureStage(self.application_failure_stage),
            )
        for label, value in (
            ("application_error", self.application_error),
            ("application_cleanup_error", self.application_cleanup_error),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise InvalidWorkspaceMutationError(
                    f"Patch mutation {label} must be non-empty when present"
                )
        try:
            reconstructed_result = PatchApplicationResult(
                schema_version=1,
                execution_id=self.execution_id,
                proposal_id=self.proposal_id,
                proposal_digest=self.proposal_digest,
                change_set_digest=self.change_set_digest,
                plan_digest=self.plan_digest,
                commit_model=self.commit_model,
                commit_state=self.application_commit_state,
                failed_step_index=self.application_failed_step_index,
                failed_target=self.application_failed_target,
                failure_stage=self.application_failure_stage,
                error=self.application_error,
                cleanup_error=self.application_cleanup_error,
            )
        except (TypeError, ValueError, InvalidWorkspaceMutationError) as exc:
            raise InvalidWorkspaceMutationError(
                "Patch mutation receipt carries an invalid application result projection"
            ) from exc
        expected_result_digest = _digest(reconstructed_result.to_dict())
        if not hmac.compare_digest(
            expected_result_digest,
            self.application_result_digest,
        ):
            raise InvalidWorkspaceMutationError(
                "Patch mutation application result digest does not match projected result"
            )
        object.__setattr__(
            self,
            "verification_outcome",
            PatchVerificationOutcome(self.verification_outcome),
        )
        if not isinstance(self.file_observations, tuple) or not self.file_observations:
            raise InvalidWorkspaceMutationError(
                "Patch mutation receipt requires file observations"
            )
        if any(
            not isinstance(obs, PatchFileMutationObservation)
            for obs in self.file_observations
        ):
            raise InvalidWorkspaceMutationError(
                "Patch mutation receipt file observations are malformed"
            )
        if tuple(obs.step_index for obs in self.file_observations) != tuple(
            range(len(self.file_observations))
        ):
            raise InvalidWorkspaceMutationError(
                "Patch mutation receipt observations must be contiguous and ordered"
            )
        targets = tuple(obs.target for obs in self.file_observations)
        if targets != tuple(sorted(targets)) or len(set(targets)) != len(targets):
            raise InvalidWorkspaceMutationError(
                "Patch mutation receipt targets must be unique and sorted"
            )
        for obs in self.file_observations:
            obs.__post_init__()
            if (
                obs.proposal_id != self.proposal_id
                or obs.proposal_digest != self.proposal_digest
                or obs.authorization_receipt_id != self.authorization_receipt_id
                or obs.authorization_receipt_digest
                != self.authorization_receipt_digest
                or obs.execution_id != self.execution_id
                or obs.plan_digest != self.plan_digest
                or obs.change_set_digest != self.change_set_digest
            ):
                raise InvalidWorkspaceMutationError(
                    "Patch mutation file observation is not bound to this receipt"
                )
        if any(
            obs.status is PatchFileObservationStatus.INSPECTION_FAILED
            for obs in self.file_observations
        ):
            expected_outcome = PatchVerificationOutcome.INCOMPLETE
        elif any(
            obs.status is PatchFileObservationStatus.MISMATCH
            for obs in self.file_observations
        ):
            expected_outcome = PatchVerificationOutcome.MISMATCH
        else:
            expected_outcome = PatchVerificationOutcome.VERIFIED
        if self.verification_outcome is not expected_outcome:
            raise InvalidWorkspaceMutationError(
                "Patch mutation receipt verification outcome disagrees with file evidence"
            )
        _require_digest(self.receipt_digest, "Patch mutation receipt digest")
        expected = _digest(self._payload())
        if not hmac.compare_digest(expected, self.receipt_digest):
            raise InvalidWorkspaceMutationError(
                "Patch mutation receipt digest does not match payload"
            )

    @property
    def verified(self) -> bool:
        return self.verification_outcome is PatchVerificationOutcome.VERIFIED

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "created_at": self.created_at,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "authorization_receipt_id": self.authorization_receipt_id,
            "authorization_receipt_digest": self.authorization_receipt_digest,
            "execution_id": self.execution_id,
            "plan_digest": self.plan_digest,
            "change_set_digest": self.change_set_digest,
            "commit_model": self.commit_model,
            "application_commit_state": self.application_commit_state.value,
            "application_result_digest": self.application_result_digest,
            "application_failed_step_index": self.application_failed_step_index,
            "application_failed_target": self.application_failed_target,
            "application_failure_stage": (
                self.application_failure_stage.value
                if self.application_failure_stage is not None
                else None
            ),
            "application_error": self.application_error,
            "application_cleanup_error": self.application_cleanup_error,
            "verification_outcome": self.verification_outcome.value,
            "file_observation_digests": [
                obs.observation_digest for obs in self.file_observations
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["file_observations"] = [
            obs.to_dict() for obs in self.file_observations
        ]
        payload["receipt_digest"] = self.receipt_digest
        return payload


def _validated_authorization_receipt(receipt: AuthorizationReceipt) -> AuthorizationReceipt:
    if not isinstance(receipt, AuthorizationReceipt):
        raise InvalidActionTransitionError(
            "Executed patch lifecycle has no valid authorization receipt"
        )
    try:
        return AuthorizationReceipt(**receipt.to_dict())
    except Exception as exc:
        raise InvalidWorkspaceMutationError(
            "Patch authorization receipt integrity validation failed"
        ) from exc


def _validated_application_result(result: PatchApplicationResult) -> PatchApplicationResult:
    if not isinstance(result, PatchApplicationResult):
        raise TypeError("application_result must be a PatchApplicationResult")
    try:
        return PatchApplicationResult(**result.to_dict())
    except Exception as exc:
        raise InvalidWorkspaceMutationError(
            "Patch application result integrity validation failed"
        ) from exc


def _validate_observation_binding(
    lifecycle: ActionLifecycle,
    plan: PatchExecutionPlan,
    result: PatchApplicationResult,
) -> tuple[AuthorizationReceipt, PatchApplicationResult]:
    if lifecycle.phase is not ActionPhase.EXECUTED:
        raise InvalidActionTransitionError(
            "Patch mutation observation requires EXECUTED lifecycle"
        )
    if lifecycle.execution_id is None:
        raise InvalidActionTransitionError(
            "Executed patch lifecycle has no execution_id"
        )
    if lifecycle.observation_id is not None:
        raise InvalidActionTransitionError(
            "Executed patch lifecycle already carries an observation_id"
        )
    receipt = _validated_authorization_receipt(lifecycle.authorization)
    if (
        receipt.proposal_id != lifecycle.proposal.proposal_id
        or receipt.proposal_digest != lifecycle.proposal.proposal_digest
        or receipt.mode is not lifecycle.mode
        or receipt.decision is not AuthorizationDecision.ALLOW
    ):
        raise InvalidWorkspaceMutationError(
            "Patch authorization receipt is no longer bound to the executed lifecycle"
        )
    if getattr(lifecycle, "_consumed_receipt_id", None) != receipt.receipt_id:
        raise InvalidWorkspaceMutationError(
            "Patch mutation observation requires the exact consumed authorization receipt"
        )
    validate_patch_execution_plan_binding(lifecycle.proposal, plan)
    validated = _validated_application_result(result)
    if (
        validated.execution_id != lifecycle.execution_id
        or validated.proposal_id != plan.proposal_id
        or validated.proposal_digest != plan.proposal_digest
        or validated.change_set_digest != plan.change_set_digest
        or validated.plan_digest != plan.plan_digest
    ):
        raise InvalidWorkspaceMutationError(
            "Patch application result is not bound to this executed lifecycle and plan"
        )
    if validated.commit_state is PatchCommitState.INDETERMINATE:
        raise WorkspaceMutationBoundaryError(
            "M2.4.4 cannot issue terminal mutation evidence for an indeterminate "
            "M2.4.3 transaction; M2.4.5 recovery is required"
        )
    return receipt, validated


def _expected_terminal(
    step: PatchExecutionStep,
    commit_state: PatchCommitState,
) -> tuple[PatchTerminalExpectation, PatchExpectedFileState]:
    if commit_state is PatchCommitState.ROLLED_BACK:
        return (
            PatchTerminalExpectation.PREIMAGE,
            PatchExpectedFileState.from_preimage(step.expected_preimage),
        )
    if commit_state is not PatchCommitState.COMMITTED:
        raise InvalidWorkspaceMutationError(
            "M2.4.4 terminal expectation requires committed or rolled-back result"
        )
    mode = (
        step.expected_preimage.mode
        if step.operation is MutationOperation.REPLACE
        else None
    )
    return (
        PatchTerminalExpectation.POSTIMAGE,
        PatchExpectedFileState.present(
            size_bytes=len(step.postimage),
            digest=sha256(step.postimage).hexdigest(),
            mode=mode,
        ),
    )


def validate_patch_mutation_receipt_binding(
    proposal: ActionProposal,
    plan: PatchExecutionPlan,
    application_result: PatchApplicationResult,
    receipt: PatchMutationReceipt,
) -> None:
    """Bind a set-level mutation receipt to its exact proposal, plan and result."""

    if not isinstance(proposal, ActionProposal):
        raise TypeError("proposal must be an ActionProposal")
    if not isinstance(plan, PatchExecutionPlan):
        raise TypeError("plan must be a PatchExecutionPlan")
    validate_patch_execution_plan_binding(proposal, plan)
    result = _validated_application_result(application_result)
    if not isinstance(receipt, PatchMutationReceipt):
        raise TypeError("receipt must be a PatchMutationReceipt")
    receipt.__post_init__()
    if (
        receipt.proposal_id != plan.proposal_id
        or receipt.proposal_digest != plan.proposal_digest
        or receipt.execution_id != result.execution_id
        or receipt.plan_digest != plan.plan_digest
        or receipt.change_set_digest != plan.change_set_digest
        or receipt.application_commit_state is not result.commit_state
        or receipt.application_result_digest != _digest(result.to_dict())
    ):
        raise InvalidWorkspaceMutationError(
            "Patch mutation receipt is not bound to this exact plan/application result"
        )
    if len(receipt.file_observations) != len(plan.steps):
        raise InvalidWorkspaceMutationError(
            "Patch mutation receipt cardinality does not match execution plan"
        )
    for step, observation in zip(plan.steps, receipt.file_observations, strict=True):
        observation.__post_init__()
        expectation, expected = _expected_terminal(step, result.commit_state)
        if (
            observation.step_index != step.index
            or observation.change_digest != step.change_digest
            or observation.primitive_digest != step.primitive_digest
            or observation.operation is not step.operation
            or observation.target != step.target
            or observation.terminal_expectation is not expectation
            or observation.expected_terminal != expected
        ):
            raise InvalidWorkspaceMutationError(
                f"Patch mutation receipt file evidence is not bound to plan step {step.index}"
            )


class PatchMutationObserver:
    """Produce exact read-only M2.4.4 patch evidence after M2.4.3 execution.

    The set-level receipt is an ordered aggregation of exact per-file observations.
    It deliberately does not claim an atomic/durable filesystem snapshot: files are
    observed sequentially after the M2.4.3 transaction has reached a terminal
    COMMITTED or ROLLED_BACK state.
    """

    def observe(
        self,
        lifecycle: ActionLifecycle,
        plan: PatchExecutionPlan,
        application_result: PatchApplicationResult,
    ) -> PatchMutationReceipt:
        receipt, result = _validate_observation_binding(
            lifecycle,
            plan,
            application_result,
        )
        if not _is_windows_host():
            raise WorkspaceMutationBoundaryError(
                "M2.4.4 patch mutation observation is enabled only on the supported "
                "Windows mutation boundary"
            )

        observations: list[PatchFileMutationObservation] = []
        for step in plan.steps:
            terminal_expectation, expected = _expected_terminal(
                step,
                result.commit_state,
            )
            observed: PreimageSnapshot | None = None
            error: str | None = None
            try:
                runtime = _runtime_step(plan, step)
                with PinnedMutationTarget(
                    root=runtime.m23_plan.root,
                    parent=runtime.m23_plan.parent,
                    target_name=runtime.m23_plan.target_path.name,
                ) as pinned:
                    pinned.verify_parent_identity()
                    observed = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
                    pinned.verify_parent_identity()
            except (
                InvalidWorkspaceMutationError,
                WorkspaceMutationBoundaryError,
                WorkspaceMutationPreimageChangedError,
                OSError,
            ) as exc:
                status = PatchFileObservationStatus.INSPECTION_FAILED
                error = f"terminal inspection failed: {type(exc).__name__}: {exc}"
            else:
                status = (
                    PatchFileObservationStatus.VERIFIED
                    if expected.matches(observed)
                    else PatchFileObservationStatus.MISMATCH
                )

            observations.append(
                PatchFileMutationObservation.create(
                    proposal_id=plan.proposal_id,
                    proposal_digest=plan.proposal_digest,
                    authorization_receipt_id=receipt.receipt_id,
                    authorization_receipt_digest=receipt.receipt_digest,
                    execution_id=result.execution_id,
                    plan_digest=plan.plan_digest,
                    change_set_digest=plan.change_set_digest,
                    step=step,
                    terminal_expectation=terminal_expectation,
                    expected_terminal=expected,
                    observed_terminal=observed,
                    status=status,
                    error=error,
                )
            )

        mutation_receipt = PatchMutationReceipt.create(
            proposal_id=plan.proposal_id,
            proposal_digest=plan.proposal_digest,
            authorization_receipt_id=receipt.receipt_id,
            authorization_receipt_digest=receipt.receipt_digest,
            execution_id=result.execution_id,
            plan_digest=plan.plan_digest,
            change_set_digest=plan.change_set_digest,
            application_result=result,
            file_observations=tuple(observations),
        )
        validate_patch_mutation_receipt_binding(
            lifecycle.proposal,
            plan,
            result,
            mutation_receipt,
        )
        lifecycle.record_observed(mutation_receipt.receipt_id)
        return mutation_receipt


__all__ = [
    "PATCH_MUTATION_OBSERVATION_SCHEMA_VERSION",
    "PATCH_MUTATION_RECEIPT_SCHEMA_VERSION",
    "PatchExpectedFileState",
    "PatchFileMutationObservation",
    "PatchFileObservationStatus",
    "PatchMutationObserver",
    "PatchMutationReceipt",
    "PatchTerminalExpectation",
    "PatchVerificationOutcome",
    "validate_patch_mutation_receipt_binding",
]
