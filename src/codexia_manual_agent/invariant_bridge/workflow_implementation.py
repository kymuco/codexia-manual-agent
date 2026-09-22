from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

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
from codexia_manual_agent.workflow_runtime import WorkflowImplementationPort

WORKFLOW_IMPLEMENTATION_EXPORT = "codexia_workflow_implementation"


class InvariantWorkflowImplementationError(RuntimeError):
    """Invariant provider could not resolve exact Codexia Workflow semantics."""


class InvariantWorkflowImplementationBindingError(
    InvariantWorkflowImplementationError
):
    """Resolved provider or implementation changed pinned semantic identity."""


class InvariantWorkflowImplementationShapeError(
    InvariantWorkflowImplementationError
):
    """Provider returned something outside the G2.9 implementation shape."""


@dataclass(frozen=True, slots=True)
class ResolvedWorkflowImplementation:
    """One exact implementation resolved from one exact technical provider."""

    provider_ref: str
    workflow_binding: WorkflowBinding
    pack_binding_digest: str
    implementation: WorkflowImplementationPort

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
        _validate_implementation_shape(
            self.implementation,
            self.workflow_binding,
        )


def _validate_implementation_shape(
    implementation: Any,
    expected: WorkflowBinding,
) -> None:
    if isinstance(implementation, type):
        raise InvariantWorkflowImplementationShapeError(
            "Provider must return a Workflow implementation instance, not a class"
        )

    binding = getattr(implementation, "binding", None)
    if not isinstance(binding, WorkflowBinding):
        raise InvariantWorkflowImplementationShapeError(
            "Resolved implementation must expose WorkflowBinding as .binding"
        )
    if binding != expected:
        raise InvariantWorkflowImplementationBindingError(
            "Resolved implementation changed exact WorkflowBinding"
        )

    propose = getattr(implementation, "propose", None)
    if not callable(propose):
        raise InvariantWorkflowImplementationShapeError(
            "Resolved implementation must expose callable propose(context)"
        )


class InvariantWorkflowImplementationBridge:
    """Resolve exact G2.9 implementation through one exact Invariant build.

    Resolution activates/loads technical code only. It does not call propose(),
    mutate Work, admit proposals, grant authority, execute capabilities, schedule
    work, or choose provider versions.
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
    ) -> ResolvedWorkflowImplementation:
        if not isinstance(workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise InvariantWorkflowImplementationError(
                "Cannot resolve implementation for terminal WorkflowRun"
            )
        if not isinstance(pack_binding, PackWorkflowBinding):
            raise TypeError("pack_binding must be PackWorkflowBinding")

        run = workflow.run
        if pack_binding.workflow_run_id != run.workflow_run_id:
            raise InvariantWorkflowImplementationBindingError(
                "Pack binding belongs to another WorkflowRun"
            )
        if not hmac.compare_digest(
            pack_binding.workflow_run_digest,
            run.run_digest,
        ):
            raise InvariantWorkflowImplementationBindingError(
                "Pack binding changed WorkflowRun semantics"
            )
        if pack_binding.work_id != run.work_id:
            raise InvariantWorkflowImplementationBindingError(
                "Pack binding crossed Work identity"
            )
        if not hmac.compare_digest(
            pack_binding.work_digest,
            run.work_digest,
        ):
            raise InvariantWorkflowImplementationBindingError(
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
            raise InvariantWorkflowImplementationBindingError(
                "Pinned Pack does not contain exact WorkflowBinding"
            )

        try:
            distribution = self._distribution.resolve(provider_ref)
        except InvariantPackDistributionError as exc:
            raise InvariantWorkflowImplementationError(
                "Could not resolve provider Pack distribution"
            ) from exc

        if distribution.pack != pack_binding.pack:
            raise InvariantWorkflowImplementationBindingError(
                "Provider does not distribute the exact pinned Pack"
            )
        if target not in distribution.workflows:
            raise InvariantWorkflowImplementationBindingError(
                "Provider distribution lacks exact pinned WorkflowBinding"
            )

        plugin = self._service.get(distribution.provider_ref)
        export = getattr(plugin, WORKFLOW_IMPLEMENTATION_EXPORT, None)
        if not callable(export):
            raise InvariantWorkflowImplementationShapeError(
                "Invariant plugin does not expose codexia_workflow_implementation()"
            )

        implementation = export(target.to_dict())
        _validate_implementation_shape(implementation, target)

        return ResolvedWorkflowImplementation(
            provider_ref=distribution.provider_ref,
            workflow_binding=target,
            pack_binding_digest=pack_binding.pack.binding_digest,
            implementation=implementation,
        )
