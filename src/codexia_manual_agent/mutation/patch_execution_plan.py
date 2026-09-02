from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
    WorkspaceMutationTargetExistsError,
    WorkspaceMutationTargetMissingError,
)
from codexia_manual_agent.mutation.models import MutationOperation, PreimageSnapshot
from codexia_manual_agent.mutation.patches import (
    PatchChangeSet,
    PatchFileChange,
    PatchFileRequest,
)
from codexia_manual_agent.mutation.patch_preview_budget_repairs import (
    parse_patch_proposal,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation.preflight_executor import (
    _is_windows_host,
    _require_windows_strict_replace_support,
)
from codexia_manual_agent.mutation.windows_metadata import validate_windows_relative_target
from codexia_manual_agent.mutation.workspace import CREATE_ACTION, REPLACE_ACTION

PATCH_EXECUTION_PLAN_SCHEMA_VERSION = 1
PATCH_EXECUTION_BACKEND = "m2.3.workspace_create_replace.v1"
PATCH_EXECUTION_PLATFORM = "windows"


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


def _require_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise InvalidWorkspaceMutationError(f"{label} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise InvalidWorkspaceMutationError(
            f"{label} must be a SHA-256 hex digest"
        ) from exc


def _postimage_parameter(change: PatchFileChange) -> dict[str, Any]:
    return {
        "size_bytes": len(change.postimage),
        "sha256": change.postimage_sha256,
        "data_base64": base64.b64encode(change.postimage).decode("ascii"),
    }


def _m23_action(operation: MutationOperation) -> str:
    if operation is MutationOperation.CREATE:
        return CREATE_ACTION
    if operation is MutationOperation.REPLACE:
        return REPLACE_ACTION
    raise InvalidWorkspaceMutationError(
        f"Unsupported patch execution operation: {operation!r}"
    )


@dataclass(frozen=True, slots=True)
class PatchExecutionStep:
    index: int
    operation: MutationOperation
    action: str
    target: str
    expected_preimage: PreimageSnapshot
    postimage: bytes
    change_digest: str
    primitive_digest: str

    @classmethod
    def create(
        cls,
        *,
        index: int,
        change: PatchFileChange,
        workspace_root: str,
    ) -> "PatchExecutionStep":
        action = _m23_action(change.operation)
        primitive = {
            "capability": Capability.WRITE_WORKSPACE.value,
            "action": action,
            "workspace_root": workspace_root,
            "parameters": {
                "operation": change.operation.value,
                "target": change.target,
                "expected_preimage": change.expected_preimage.to_dict(),
                "postimage": _postimage_parameter(change),
            },
        }
        return cls(
            index=index,
            operation=change.operation,
            action=action,
            target=change.target,
            expected_preimage=change.expected_preimage,
            postimage=change.postimage,
            change_digest=change.change_digest,
            primitive_digest=_digest(primitive),
        )

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise InvalidWorkspaceMutationError(
                "Patch execution step index must be a non-negative integer"
            )
        object.__setattr__(self, "operation", MutationOperation(self.operation))
        if self.action != _m23_action(self.operation):
            raise InvalidWorkspaceMutationError(
                "Patch execution step action does not match operation"
            )
        if not isinstance(self.target, str) or not self.target:
            raise InvalidWorkspaceMutationError(
                "Patch execution step target must be non-empty text"
            )
        if not isinstance(self.expected_preimage, PreimageSnapshot):
            raise TypeError("Patch execution step preimage must be a PreimageSnapshot")
        if not isinstance(self.postimage, bytes):
            raise TypeError("Patch execution step postimage must be bytes")
        _require_digest(self.change_digest, "Patch execution change digest")
        _require_digest(self.primitive_digest, "Patch execution primitive digest")

    def m23_parameters(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "target": self.target,
            "expected_preimage": self.expected_preimage.to_dict(),
            "postimage": {
                "size_bytes": len(self.postimage),
                "sha256": sha256(self.postimage).hexdigest(),
                "data_base64": base64.b64encode(self.postimage).decode("ascii"),
            },
        }

    def _payload(self, *, workspace_root: str) -> dict[str, Any]:
        return {
            "index": self.index,
            "capability": Capability.WRITE_WORKSPACE.value,
            "operation": self.operation.value,
            "action": self.action,
            "target": self.target,
            "expected_preimage": self.expected_preimage.to_dict(),
            "postimage": self.m23_parameters()["postimage"],
            "change_digest": self.change_digest,
            "primitive_digest": self.primitive_digest,
            "workspace_root": workspace_root,
        }

    def validate_primitive_digest(self, *, workspace_root: str) -> None:
        primitive = {
            "capability": Capability.WRITE_WORKSPACE.value,
            "action": self.action,
            "workspace_root": workspace_root,
            "parameters": self.m23_parameters(),
        }
        expected = _digest(primitive)
        if not hmac.compare_digest(expected, self.primitive_digest):
            raise InvalidWorkspaceMutationError(
                "Patch execution primitive digest does not match M2.3 payload"
            )

    def to_dict(self, *, workspace_root: str) -> dict[str, Any]:
        self.validate_primitive_digest(workspace_root=workspace_root)
        return self._payload(workspace_root=workspace_root)


@dataclass(frozen=True, slots=True)
class PatchExecutionPlan:
    schema_version: int
    proposal_id: str
    proposal_digest: str
    workspace_root: str
    change_set_digest: str
    backend: str
    execution_platform: str
    steps: tuple[PatchExecutionStep, ...]
    plan_digest: str

    @classmethod
    def create(
        cls,
        *,
        proposal: ActionProposal,
        change_set: PatchChangeSet,
    ) -> "PatchExecutionPlan":
        steps = tuple(
            PatchExecutionStep.create(
                index=index,
                change=change,
                workspace_root=change_set.workspace_root,
            )
            for index, change in enumerate(change_set.changes)
        )
        payload = {
            "schema_version": PATCH_EXECUTION_PLAN_SCHEMA_VERSION,
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "workspace_root": change_set.workspace_root,
            "change_set_digest": change_set.change_set_digest,
            "backend": PATCH_EXECUTION_BACKEND,
            "execution_platform": PATCH_EXECUTION_PLATFORM,
            "steps": [
                step.to_dict(workspace_root=change_set.workspace_root)
                for step in steps
            ],
        }
        return cls(
            schema_version=PATCH_EXECUTION_PLAN_SCHEMA_VERSION,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            workspace_root=change_set.workspace_root,
            change_set_digest=change_set.change_set_digest,
            backend=PATCH_EXECUTION_BACKEND,
            execution_platform=PATCH_EXECUTION_PLATFORM,
            steps=steps,
            plan_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.schema_version != PATCH_EXECUTION_PLAN_SCHEMA_VERSION:
            raise InvalidWorkspaceMutationError(
                "Unsupported patch execution plan schema version"
            )
        try:
            UUID(self.proposal_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise InvalidWorkspaceMutationError(
                "Patch execution proposal_id must be a UUID"
            ) from exc
        if not isinstance(self.workspace_root, str) or not self.workspace_root:
            raise InvalidWorkspaceMutationError(
                "Patch execution workspace_root must be non-empty text"
            )
        _require_digest(self.proposal_digest, "Patch execution proposal digest")
        _require_digest(self.change_set_digest, "Patch execution change-set digest")
        _require_digest(self.plan_digest, "Patch execution plan digest")
        if self.backend != PATCH_EXECUTION_BACKEND:
            raise InvalidWorkspaceMutationError("Unsupported patch execution backend")
        if self.execution_platform != PATCH_EXECUTION_PLATFORM:
            raise InvalidWorkspaceMutationError("Unsupported patch execution platform")
        if not isinstance(self.steps, tuple) or any(
            not isinstance(step, PatchExecutionStep) for step in self.steps
        ):
            raise InvalidWorkspaceMutationError(
                "Patch execution plan steps must be a tuple of PatchExecutionStep"
            )
        if not self.steps:
            raise InvalidWorkspaceMutationError(
                "Patch execution plan must contain at least one step"
            )
        if tuple(step.index for step in self.steps) != tuple(range(len(self.steps))):
            raise InvalidWorkspaceMutationError(
                "Patch execution plan step indexes must be contiguous and ordered"
            )
        targets = tuple(step.target for step in self.steps)
        if targets != tuple(sorted(targets)) or len(set(targets)) != len(targets):
            raise InvalidWorkspaceMutationError(
                "Patch execution plan targets must be unique and sorted"
            )
        expected = _digest(self._payload())
        if not hmac.compare_digest(expected, self.plan_digest):
            raise InvalidWorkspaceMutationError(
                "Patch execution plan digest does not match payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "workspace_root": self.workspace_root,
            "change_set_digest": self.change_set_digest,
            "backend": self.backend,
            "execution_platform": self.execution_platform,
            "steps": [
                step.to_dict(workspace_root=self.workspace_root)
                for step in self.steps
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["plan_digest"] = self.plan_digest
        return payload


def _revalidation_requests(change_set: PatchChangeSet) -> tuple[PatchFileRequest, ...]:
    return tuple(
        PatchFileRequest(change.operation, change.target, change.postimage)
        for change in change_set.changes
    )


def _first_change_mismatch(
    expected: PatchChangeSet,
    observed: PatchChangeSet,
) -> str:
    if expected.workspace_root != observed.workspace_root:
        return "workspace root changed"
    if len(expected.changes) != len(observed.changes):
        return "change-set cardinality changed"
    for expected_change, observed_change in zip(
        expected.changes,
        observed.changes,
        strict=True,
    ):
        if expected_change.target != observed_change.target:
            return f"target namespace changed at {expected_change.target}"
        if expected_change.operation is not observed_change.operation:
            return f"operation changed at {expected_change.target}"
        if expected_change.expected_preimage != observed_change.expected_preimage:
            return f"preimage identity changed at {expected_change.target}"
        if expected_change.preimage != observed_change.preimage:
            return f"exact preimage bytes changed at {expected_change.target}"
        if expected_change.postimage != observed_change.postimage:
            return f"postimage changed at {expected_change.target}"
        if expected_change.change_digest != observed_change.change_digest:
            return f"change digest changed at {expected_change.target}"
    return "change-set digest changed"


def _capture_live_change_set(expected: PatchChangeSet) -> PatchChangeSet:
    try:
        observed_proposal = prepare_patch_proposal(
            workspace=expected.workspace_root,
            changes=_revalidation_requests(expected),
            summary="M2.4.2 pre-authority patch revalidation.",
        )
    except WorkspaceMutationBoundaryError as exc:
        raise WorkspaceMutationBoundaryError(
            "Patch pre-authority namespace revalidation failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    except (
        WorkspaceMutationTargetExistsError,
        WorkspaceMutationTargetMissingError,
        WorkspaceMutationPreimageChangedError,
        InvalidWorkspaceMutationError,
    ) as exc:
        raise WorkspaceMutationPreimageChangedError(
            "Patch pre-authority revalidation detected live preimage drift: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return parse_patch_proposal(observed_proposal)


def build_patch_execution_plan(proposal: ActionProposal) -> PatchExecutionPlan:
    """Build the deterministic proposal-bound M2.3 mapping without claiming freshness."""

    change_set = parse_patch_proposal(proposal)
    return PatchExecutionPlan.create(proposal=proposal, change_set=change_set)


def validate_patch_execution_plan_binding(
    proposal: ActionProposal,
    plan: PatchExecutionPlan,
) -> None:
    """Require a plan to be the exact deterministic plan for one patch proposal."""

    if not isinstance(plan, PatchExecutionPlan):
        raise TypeError("plan must be a PatchExecutionPlan")
    change_set = parse_patch_proposal(proposal)
    plan.validate()
    expected = PatchExecutionPlan.create(proposal=proposal, change_set=change_set)
    if not hmac.compare_digest(expected.plan_digest, plan.plan_digest):
        raise InvalidWorkspaceMutationError(
            "Patch execution plan is not bound to this exact patch proposal"
        )


def preflight_patch_execution_plan(
    proposal: ActionProposal,
    plan: PatchExecutionPlan,
) -> None:
    """Prove that every planned M2.3 primitive is supported on this execution host.

    This is read-only capability/namespace preflight. It does not consume
    authorization and does not make the plan a freshness or authority token.
    """

    validate_patch_execution_plan_binding(proposal, plan)
    if not _is_windows_host():
        raise WorkspaceMutationBoundaryError(
            "M2.4 patch execution remains disabled outside Windows"
        )

    root = Path(plan.workspace_root)
    for step in plan.steps:
        validate_windows_relative_target(step.target)
        if step.operation is MutationOperation.REPLACE:
            _require_windows_strict_replace_support(root / Path(step.target))


def revalidate_patch_execution_plan(
    proposal: ActionProposal,
    plan: PatchExecutionPlan,
) -> None:
    """Run the final live whole-set gate immediately before future authority use.

    The operation is read-only and consumes no authorization. Freshness is a
    point-in-time gate, not a durable property stored on ``PatchExecutionPlan``.
    A future M2.4.3 executor must call this after support preflight and directly
    before patch-level authorization consumption, while still preserving the
    accepted per-primitive M2.3 checks during application.
    """

    validate_patch_execution_plan_binding(proposal, plan)
    expected = parse_patch_proposal(proposal)
    observed = _capture_live_change_set(expected)
    if not hmac.compare_digest(
        expected.change_set_digest,
        observed.change_set_digest,
    ):
        detail = _first_change_mismatch(expected, observed)
        raise WorkspaceMutationPreimageChangedError(
            f"Patch pre-authority revalidation failed: {detail}"
        )


__all__ = [
    "PATCH_EXECUTION_BACKEND",
    "PATCH_EXECUTION_PLAN_SCHEMA_VERSION",
    "PATCH_EXECUTION_PLATFORM",
    "PatchExecutionPlan",
    "PatchExecutionStep",
    "build_patch_execution_plan",
    "preflight_patch_execution_plan",
    "revalidate_patch_execution_plan",
    "validate_patch_execution_plan_binding",
]
