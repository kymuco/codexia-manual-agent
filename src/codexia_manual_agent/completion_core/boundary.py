from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Protocol

from codexia_manual_agent.capability_core import CapabilityNeedSnapshot
from codexia_manual_agent.completion_core.models import CompletionClaim
from codexia_manual_agent.pack_core import (
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import WorkSnapshot, WorkState
from codexia_manual_agent.workflow_core import (
    WorkflowBinding,
    WorkflowRunSnapshot,
    WorkflowRunState,
)

MAX_COMPLETION_CRITERION_REASON_CHARS = 8_192


class CompletionCriterionError(RuntimeError):
    """Base failure for one Pack-defined completion criterion call."""


class CompletionCriterionBindingError(CompletionCriterionError):
    """Criterion or context changed exact Work/Workflow/Pack semantics."""


class CompletionCriterionStateError(CompletionCriterionError):
    """Criterion cannot evaluate the supplied derived semantic state."""


class CompletionCriterionResultError(CompletionCriterionError):
    """Criterion returned a result outside the bounded completion shape."""


def _reason(value: object) -> str:
    if not isinstance(value, str):
        raise CompletionCriterionResultError("reason must be text")
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > MAX_COMPLETION_CRITERION_REASON_CHARS
    ):
        raise CompletionCriterionResultError(
            "reason must be non-empty canonical trimmed text "
            f"within {MAX_COMPLETION_CRITERION_REASON_CHARS} characters"
        )
    return value


@dataclass(frozen=True, slots=True)
class CompletionCriterionContext:
    """Exact read-only semantic state supplied to one completion criterion.

    This context carries no WorkStore, admission surface, authority, executor,
    scheduler, provider, host handle, retry permission, or cleanup control.
    """

    claim: CompletionClaim
    work: WorkSnapshot
    workflow: WorkflowRunSnapshot
    pack_binding: PackWorkflowBinding
    capabilities: tuple[CapabilityNeedSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.claim, CompletionClaim):
            raise TypeError("claim must be CompletionClaim")
        if not isinstance(self.work, WorkSnapshot):
            raise TypeError("work must be WorkSnapshot")
        if not isinstance(self.workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if not isinstance(self.pack_binding, PackWorkflowBinding):
            raise TypeError("pack_binding must be PackWorkflowBinding")
        if not isinstance(self.capabilities, tuple):
            raise TypeError(
                "capabilities must be tuple[CapabilityNeedSnapshot, ...]"
            )
        if self.work.state is not WorkState.ACTIVE:
            raise CompletionCriterionStateError(
                "Completion criterion requires active Work"
            )
        if self.workflow.state is not WorkflowRunState.ACTIVE:
            raise CompletionCriterionStateError(
                "Completion criterion requires active WorkflowRun"
            )

        claim = self.claim
        snapshot = self.work
        run = self.workflow.run
        pin = self.pack_binding

        if claim.work_id != snapshot.work.work_id:
            raise CompletionCriterionBindingError(
                "CompletionClaim crossed Work identity"
            )
        if not hmac.compare_digest(
            claim.work_digest,
            snapshot.work.work_digest,
        ):
            raise CompletionCriterionBindingError(
                "CompletionClaim changed Work binding"
            )
        if claim.work_revision != snapshot.revision:
            raise CompletionCriterionStateError(
                "CompletionClaim does not bind current Work revision"
            )
        if claim.work_event_digest != snapshot.last_event_digest:
            raise CompletionCriterionStateError(
                "CompletionClaim does not bind current Work chronology"
            )

        if claim.workflow_run_id != run.workflow_run_id:
            raise CompletionCriterionBindingError(
                "CompletionClaim changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            claim.workflow_run_digest,
            run.run_digest,
        ):
            raise CompletionCriterionBindingError(
                "CompletionClaim changed WorkflowRun binding"
            )
        if run.work_id != snapshot.work.work_id:
            raise CompletionCriterionBindingError(
                "WorkflowRun belongs to another Work"
            )
        if not hmac.compare_digest(
            run.work_digest,
            snapshot.work.work_digest,
        ):
            raise CompletionCriterionBindingError(
                "WorkflowRun changed Work binding"
            )

        if pin.work_id != snapshot.work.work_id:
            raise CompletionCriterionBindingError(
                "Pack binding crossed Work identity"
            )
        if not hmac.compare_digest(
            pin.work_digest,
            snapshot.work.work_digest,
        ):
            raise CompletionCriterionBindingError(
                "Pack binding changed Work semantics"
            )
        if pin.workflow_run_id != run.workflow_run_id:
            raise CompletionCriterionBindingError(
                "Pack binding changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            pin.workflow_run_digest,
            run.run_digest,
        ):
            raise CompletionCriterionBindingError(
                "Pack binding changed WorkflowRun semantics"
            )
        if claim.pack_workflow_binding_id != pin.binding_id:
            raise CompletionCriterionBindingError(
                "CompletionClaim changed PackWorkflowBinding identity"
            )
        if not hmac.compare_digest(
            claim.pack_workflow_binding_digest,
            pin.pin_digest,
        ):
            raise CompletionCriterionBindingError(
                "CompletionClaim changed PackWorkflowBinding digest"
            )
        if not hmac.compare_digest(
            claim.pack_binding_digest,
            pin.pack.binding_digest,
        ):
            raise CompletionCriterionBindingError(
                "CompletionClaim changed PackBinding semantics"
            )

        capability_ids: set[str] = set()
        for capability in self.capabilities:
            if not isinstance(capability, CapabilityNeedSnapshot):
                raise TypeError(
                    "capabilities must contain CapabilityNeedSnapshot values"
                )
            need = capability.need
            if need.need_id in capability_ids:
                raise CompletionCriterionStateError(
                    "capabilities contains duplicate CapabilityNeed identity"
                )
            capability_ids.add(need.need_id)

            if need.work_id != snapshot.work.work_id:
                raise CompletionCriterionBindingError(
                    "CapabilityNeed crossed Work identity"
                )
            if not hmac.compare_digest(
                need.work_digest,
                snapshot.work.work_digest,
            ):
                raise CompletionCriterionBindingError(
                    "CapabilityNeed changed Work binding"
                )
            if need.workflow_run_id != run.workflow_run_id:
                raise CompletionCriterionBindingError(
                    "CapabilityNeed changed WorkflowRun identity"
                )
            if not hmac.compare_digest(
                need.workflow_run_digest,
                run.run_digest,
            ):
                raise CompletionCriterionBindingError(
                    "CapabilityNeed changed WorkflowRun binding"
                )

            member = PackMemberBinding.create(
                kind=PackMemberKind.CAPABILITY,
                semantic_id=need.binding.capability_id,
                version=need.binding.version,
                binding_digest=need.binding.binding_digest,
            )
            if not pin.pack.contains(member):
                raise CompletionCriterionBindingError(
                    "CapabilityNeed is outside pinned Pack"
                )


@dataclass(frozen=True, slots=True)
class CompletionCriterionResult:
    """Ephemeral Pack-defined completion judgment.

    This result is not WorkCompletion and grants no terminal transition.
    """

    accepted: bool
    reason: str

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise CompletionCriterionResultError(
                "accepted must be boolean"
            )
        _reason(self.reason)


class CompletionCriterionPort(Protocol):
    """Pure completion criterion for one exact WorkflowBinding."""

    @property
    def binding(self) -> WorkflowBinding: ...

    def evaluate(
        self,
        context: CompletionCriterionContext,
    ) -> CompletionCriterionResult: ...


class CompletionCriterionBoundary:
    """Invoke one exact criterion and validate one bounded judgment.

    The boundary performs no Work mutation, completion admission, terminal
    transition, authority grant, scheduling, execution, or cleanup.
    """

    def evaluate(
        self,
        criterion: CompletionCriterionPort,
        context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        if not isinstance(context, CompletionCriterionContext):
            raise TypeError("context must be CompletionCriterionContext")

        binding = criterion.binding
        if not isinstance(binding, WorkflowBinding):
            raise TypeError("criterion.binding must be WorkflowBinding")
        if binding != context.workflow.run.binding:
            raise CompletionCriterionBindingError(
                "Completion criterion does not match exact WorkflowBinding"
            )

        result = criterion.evaluate(context)
        if not isinstance(result, CompletionCriterionResult):
            raise CompletionCriterionResultError(
                "Completion criterion returned unsupported result type"
            )
        return result
