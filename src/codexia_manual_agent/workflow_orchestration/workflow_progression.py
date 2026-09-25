from __future__ import annotations

from dataclasses import dataclass

from codexia_manual_agent.completion_core import CompletionCriterionResolverPort
from codexia_manual_agent.invariant_bridge import (
    InvariantWorkflowImplementationBridge,
)
from codexia_manual_agent.work_core import WorkStore
from codexia_manual_agent.workflow_orchestration.proposal_admission import (
    WorkflowProposalAdmissionResult,
    WorkflowProposalAdmissionService,
)
from codexia_manual_agent.workflow_orchestration.step import (
    WorkflowStepReadPrecondition,
    WorkflowStepResult,
    WorkflowStepService,
)


@dataclass(frozen=True, slots=True)
class WorkflowProgressionResult:
    """Ephemeral result of exactly one bounded Workflow progression call."""

    step: WorkflowStepResult
    admitted: WorkflowProposalAdmissionResult

    def __post_init__(self) -> None:
        if not isinstance(self.step, WorkflowStepResult):
            raise TypeError("step must be WorkflowStepResult")
        if self.step.proposal is None and self.admitted is not None:
            raise ValueError("no-proposal Workflow step cannot admit semantic truth")
        if self.step.proposal is not None and self.admitted is None:
            raise ValueError("Workflow proposal requires an admission result")


class WorkflowProgressionService:
    """Compute and admit at most one caller-bound Workflow proposal.

    The caller supplies the exact G2.18 Work read precondition. This service
    performs one G2.11 step followed by one G2.17 typed admission. It owns no
    loop, scheduler, retry policy, capability execution, cognition dispatch,
    provider selection, authority, or Work completion policy.

    If the Work has advanced before a retry, G2.18 fails closed before
    implementation resolution. The service does not infer whether that advance
    came from its own prior admission or from unrelated concurrent work.
    """

    def __init__(
        self,
        *,
        store: WorkStore,
        resolver: InvariantWorkflowImplementationBridge,
        completion_resolver: CompletionCriterionResolverPort | None = None,
    ) -> None:
        self._step = WorkflowStepService(
            store=store,
            resolver=resolver,
        )
        self._admission = WorkflowProposalAdmissionService(
            store,
            completion_resolver=completion_resolver,
        )

    def progress_once(
        self,
        *,
        work_id: str,
        workflow_run_id: str,
        provider_ref: str,
        precondition: WorkflowStepReadPrecondition,
    ) -> WorkflowProgressionResult:
        if not isinstance(precondition, WorkflowStepReadPrecondition):
            raise TypeError("precondition must be WorkflowStepReadPrecondition")

        step = self._step.step(
            work_id=work_id,
            workflow_run_id=workflow_run_id,
            provider_ref=provider_ref,
            precondition=precondition,
        )
        admitted = self._admission.admit(
            step,
            completion_provider_ref=provider_ref,
        )
        return WorkflowProgressionResult(
            step=step,
            admitted=admitted,
        )
