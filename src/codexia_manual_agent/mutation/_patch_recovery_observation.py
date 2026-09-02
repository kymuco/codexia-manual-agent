from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation.models import MutationOperation, PreimageSnapshot
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget
from codexia_manual_agent.mutation.patch_application import PatchCommitState, _runtime_step
from codexia_manual_agent.mutation.patch_execution_plan import (
    PatchExecutionPlan,
    PatchExecutionStep,
)
from codexia_manual_agent.mutation.patch_observation import PatchExpectedFileState
from codexia_manual_agent.mutation.workspace import _MAX_PREIMAGE_BYTES
from codexia_manual_agent.mutation._patch_recovery_common import (
    PATCH_RECOVERY_FILE_OBSERVATION_SCHEMA_VERSION, PatchCrashFilesystemClassification,
    PatchRecoveryVerificationOutcome, _digest, _expected_postimage, _expected_preimage,
    _require_digest, _require_timestamp, _require_uuid, _utc_now,
)

@dataclass(frozen=True, slots=True)
class PatchRecoveryFileObservation:
    schema_version: int
    observation_id: str
    created_at: str
    step_index: int
    change_digest: str
    primitive_digest: str
    operation: MutationOperation
    target: str
    expected_preimage: PatchExpectedFileState
    expected_postimage: PatchExpectedFileState
    observed_terminal: PreimageSnapshot | None
    error: str | None
    matches_preimage: bool
    matches_postimage: bool
    observation_digest: str

    @classmethod
    def create(
        cls,
        *,
        step: PatchExecutionStep,
        observed_terminal: PreimageSnapshot | None,
        error: str | None,
        observation_id: str | None = None,
        created_at: str | None = None,
    ) -> "PatchRecoveryFileObservation":
        pre = _expected_preimage(step)
        post = _expected_postimage(step)
        matches_pre = observed_terminal is not None and pre.matches(observed_terminal)
        matches_post = observed_terminal is not None and post.matches(observed_terminal)
        observation_id = observation_id or str(uuid4())
        created_at = created_at or _utc_now()
        payload = {
            "schema_version": PATCH_RECOVERY_FILE_OBSERVATION_SCHEMA_VERSION,
            "observation_id": observation_id,
            "created_at": created_at,
            "step_index": step.index,
            "change_digest": step.change_digest,
            "primitive_digest": step.primitive_digest,
            "operation": step.operation.value,
            "target": step.target,
            "expected_preimage": pre.to_dict(),
            "expected_postimage": post.to_dict(),
            "observed_terminal": observed_terminal.to_dict() if observed_terminal else None,
            "error": error,
            "matches_preimage": matches_pre,
            "matches_postimage": matches_post,
        }
        return cls(
            schema_version=PATCH_RECOVERY_FILE_OBSERVATION_SCHEMA_VERSION,
            observation_id=observation_id,
            created_at=created_at,
            step_index=step.index,
            change_digest=step.change_digest,
            primitive_digest=step.primitive_digest,
            operation=step.operation,
            target=step.target,
            expected_preimage=pre,
            expected_postimage=post,
            observed_terminal=observed_terminal,
            error=error,
            matches_preimage=matches_pre,
            matches_postimage=matches_post,
            observation_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        if self.schema_version != PATCH_RECOVERY_FILE_OBSERVATION_SCHEMA_VERSION:
            raise InvalidWorkspaceMutationError(
                "Unsupported patch recovery file observation schema"
            )
        _require_uuid(self.observation_id, "Patch recovery file observation_id")
        _require_timestamp(self.created_at, "Patch recovery file observation created_at")
        if type(self.step_index) is not int or self.step_index < 0:
            raise InvalidWorkspaceMutationError("Patch recovery step_index is invalid")
        _require_digest(self.change_digest, "Patch recovery change digest")
        _require_digest(self.primitive_digest, "Patch recovery primitive digest")
        object.__setattr__(self, "operation", MutationOperation(self.operation))
        if not isinstance(self.target, str) or not self.target:
            raise InvalidWorkspaceMutationError("Patch recovery target is required")
        if not isinstance(self.expected_preimage, PatchExpectedFileState):
            raise TypeError("expected_preimage must be PatchExpectedFileState")
        if not isinstance(self.expected_postimage, PatchExpectedFileState):
            raise TypeError("expected_postimage must be PatchExpectedFileState")
        if self.observed_terminal is None:
            if self.error is None or self.matches_preimage or self.matches_postimage:
                raise InvalidWorkspaceMutationError(
                    "Failed recovery observation must carry error and no match claim"
                )
        else:
            if not isinstance(self.observed_terminal, PreimageSnapshot) or self.error is not None:
                raise InvalidWorkspaceMutationError(
                    "Successful recovery observation must carry one snapshot and no error"
                )
            if self.matches_preimage != self.expected_preimage.matches(self.observed_terminal):
                raise InvalidWorkspaceMutationError(
                    "Patch recovery preimage match claim is inconsistent"
                )
            if self.matches_postimage != self.expected_postimage.matches(self.observed_terminal):
                raise InvalidWorkspaceMutationError(
                    "Patch recovery postimage match claim is inconsistent"
                )
        _require_digest(self.observation_digest, "Patch recovery file observation digest")
        if not hmac.compare_digest(_digest(self._payload()), self.observation_digest):
            raise InvalidWorkspaceMutationError(
                "Patch recovery file observation digest does not match payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "created_at": self.created_at,
            "step_index": self.step_index,
            "change_digest": self.change_digest,
            "primitive_digest": self.primitive_digest,
            "operation": self.operation.value,
            "target": self.target,
            "expected_preimage": self.expected_preimage.to_dict(),
            "expected_postimage": self.expected_postimage.to_dict(),
            "observed_terminal": (
                self.observed_terminal.to_dict() if self.observed_terminal else None
            ),
            "error": self.error,
            "matches_preimage": self.matches_preimage,
            "matches_postimage": self.matches_postimage,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["observation_digest"] = self.observation_digest
        return payload


def _observe_recovery_files(plan: PatchExecutionPlan) -> tuple[PatchRecoveryFileObservation, ...]:
    observations: list[PatchRecoveryFileObservation] = []
    for step in plan.steps:
        observed: PreimageSnapshot | None = None
        error: str | None = None
        try:
            runtime = _runtime_step_for_recovery(plan, step)
            with PinnedMutationTarget(
                root=runtime.root,
                parent=runtime.parent,
                target_name=runtime.target_path.name,
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
            error = f"terminal recovery inspection failed: {type(exc).__name__}: {exc}"
        observations.append(
            PatchRecoveryFileObservation.create(
                step=step,
                observed_terminal=observed,
                error=error,
            )
        )
    return tuple(observations)


def _runtime_step_for_recovery(plan: PatchExecutionPlan, step: PatchExecutionStep):
    # Reuse M2.4.3's accepted structural parser without importing its private
    # _RuntimeStep type into the public recovery schema.
    from codexia_manual_agent.mutation.patch_application import _runtime_step

    return _runtime_step(plan, step).m23_plan


def _verification_for_state(
    recovered_state: PatchCommitState,
    observations: tuple[PatchRecoveryFileObservation, ...],
) -> PatchRecoveryVerificationOutcome:
    if any(obs.observed_terminal is None for obs in observations):
        return PatchRecoveryVerificationOutcome.INCOMPLETE
    if recovered_state is PatchCommitState.COMMITTED:
        matches = all(obs.matches_postimage for obs in observations)
    elif recovered_state is PatchCommitState.ROLLED_BACK:
        matches = all(obs.matches_preimage for obs in observations)
    else:
        raise InvalidWorkspaceMutationError(
            "Recovery verification requires committed or rolled-back state"
        )
    return (
        PatchRecoveryVerificationOutcome.VERIFIED
        if matches
        else PatchRecoveryVerificationOutcome.MISMATCH
    )


def _crash_classification(
    observations: tuple[PatchRecoveryFileObservation, ...],
) -> PatchCrashFilesystemClassification:
    if any(obs.observed_terminal is None for obs in observations):
        return PatchCrashFilesystemClassification.INCOMPLETE
    if all(obs.matches_preimage for obs in observations):
        return PatchCrashFilesystemClassification.PREIMAGE_SET
    if all(obs.matches_postimage for obs in observations):
        return PatchCrashFilesystemClassification.POSTIMAGE_SET
    return PatchCrashFilesystemClassification.MIXED
