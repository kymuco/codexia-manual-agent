from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

from codexia_manual_agent.completion_core.boundary import CompletionCriterionPort
from codexia_manual_agent.invariant_bridge.pack_distribution import (
    InvariantPackDistributionBridge,
    InvariantPackDistributionError,
    ManagedPluginServicePort,
)
from codexia_manual_agent.pack_core import (
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.workflow_core import (
    WorkflowBinding,
    WorkflowRunSnapshot,
    WorkflowRunState,
)

COMPLETION_CRITERION_EXPORT = "codexia_completion_criterion"


class InvariantCompletionCriterionError(RuntimeError):
    """Invariant provider could not resolve exact completion semantics."""


class InvariantCompletionCriterionBindingError(
    InvariantCompletionCriterionError
):
    """Resolved provider or criterion changed pinned semantic identity."""


class InvariantCompletionCriterionShapeError(
    InvariantCompletionCriterionError
):
    """Provider returned something outside the completion criterion shape."""


@dataclass(frozen=True, slots=True)
class ResolvedCompletionCriterion:
    """One exact completion criterion resolved from one technical provider."""

    provider_ref: str
    workflow_binding: WorkflowBinding
    pack_binding_digest: str
    criterion: CompletionCriterionPort

    def __post_init__(self) -> None:
        if not isinstance(self.provider_ref, str) or not self.provider_ref:
            raise TypeError("provider_ref must be non-empty text")
        if not isinstance(self.workflow_binding, WorkflowBinding):
            raise TypeError("workflow_binding must be WorkflowBinding")
        if (
            not isinstance(self.pack_binding_digest, str)
            or len(self.pack_binding_digest) != 64
        ):
            raise TypeError("pack_binding_digest must be SHA-256 text")
        _validate_criterion_shape(
            self.criterion,
            self.workflow_binding,
        )


def _validate_criterion_shape(
    criterion: Any,
    expected: WorkflowBinding,
) -> None:
    if isinstance(criterion, type):
        raise InvariantCompletionCriterionShapeError(
            "Provider must return a completion criterion instance, not a class"
        )

    binding = getattr(criterion, "binding", None)
    if not isinstance(binding, WorkflowBinding):
        raise InvariantCompletionCriterionShapeError(
            "Resolved criterion must expose WorkflowBinding as .binding"
        )
    if binding != expected:
        raise InvariantCompletionCriterionBindingError(
            "Resolved criterion changed exact WorkflowBinding"
        )

    evaluate = getattr(criterion, "evaluate", None)
    if not callable(evaluate):
        raise InvariantCompletionCriterionShapeError(
            "Resolved criterion must expose callable evaluate(context)"
        )


class InvariantCompletionCriterionBridge:
    """Resolve Pack-defined completion criteria through one exact Invariant build.

    A completion criterion is an implementation of the pinned Workflow semantics,
    not a new Pack member or authority surface. Resolution loads code only; it
    does not evaluate a claim, mutate Work, complete Work, schedule work, execute
    capabilities, or perform cleanup.
    """

    def __init__(self, service: ManagedPluginServicePort) -> None:
        self._service = service
        self._distribution = InvariantPackDistributionBridge(service)

    def resolve(
        self,
        *,
        provider_ref: str,
        workflow: WorkflowRunSnapshot,
        pack_binding: PackWorkflowBinding,
    ) -> ResolvedCompletionCriterion:
        if not isinstance(workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise InvariantCompletionCriterionError(
                "Cannot resolve criterion for terminal WorkflowRun"
            )
        if not isinstance(pack_binding, PackWorkflowBinding):
            raise TypeError("pack_binding must be PackWorkflowBinding")

        run = workflow.run
        if pack_binding.workflow_run_id != run.workflow_run_id:
            raise InvariantCompletionCriterionBindingError(
                "Pack binding belongs to another WorkflowRun"
            )
        if not hmac.compare_digest(
            pack_binding.workflow_run_digest,
            run.run_digest,
        ):
            raise InvariantCompletionCriterionBindingError(
                "Pack binding changed WorkflowRun semantics"
            )
        if pack_binding.work_id != run.work_id:
            raise InvariantCompletionCriterionBindingError(
                "Pack binding crossed Work identity"
            )
        if not hmac.compare_digest(
            pack_binding.work_digest,
            run.work_digest,
        ):
            raise InvariantCompletionCriterionBindingError(
                "Pack binding changed Work semantics"
            )

        target = run.binding
        workflow_member = PackMemberBinding.create(
            kind=PackMemberKind.WORKFLOW,
            semantic_id=target.workflow_id,
            version=target.version,
            binding_digest=target.binding_digest,
        )
        if not pack_binding.pack.contains(workflow_member):
            raise InvariantCompletionCriterionBindingError(
                "Pinned Pack does not contain exact WorkflowBinding"
            )

        try:
            distribution = self._distribution.resolve(provider_ref)
        except InvariantPackDistributionError as exc:
            raise InvariantCompletionCriterionError(
                "Could not resolve provider Pack distribution"
            ) from exc

        if distribution.pack != pack_binding.pack:
            raise InvariantCompletionCriterionBindingError(
                "Provider does not distribute the exact pinned Pack"
            )
        if target not in distribution.workflows:
            raise InvariantCompletionCriterionBindingError(
                "Provider distribution lacks exact pinned WorkflowBinding"
            )

        plugin = self._service.get(distribution.provider_ref)
        export = getattr(plugin, COMPLETION_CRITERION_EXPORT, None)
        if not callable(export):
            raise InvariantCompletionCriterionShapeError(
                "Invariant plugin does not expose codexia_completion_criterion()"
            )

        criterion = export(target.to_dict())
        _validate_criterion_shape(criterion, target)

        return ResolvedCompletionCriterion(
            provider_ref=distribution.provider_ref,
            workflow_binding=target,
            pack_binding_digest=pack_binding.pack.binding_digest,
            criterion=criterion,
        )
