from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from codexia_manual_agent.capability_core import (
    CapabilityNeed,
    CapabilityNeedSnapshot,
)
from codexia_manual_agent.pack_core import (
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.role_core import RoleRun, RoleRunSnapshot
from codexia_manual_agent.work_core import WorkSnapshot, WorkState
from codexia_manual_agent.workflow_core import (
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRunSnapshot,
    WorkflowRunState,
)

WorkflowProposal: TypeAlias = WorkflowCandidate | RoleRun | CapabilityNeed


class WorkflowImplementationError(RuntimeError):
    """Base failure for the G2.9 workflow implementation boundary."""


class WorkflowImplementationBindingError(WorkflowImplementationError):
    """Implementation or proposal changed exact bound semantics."""


class WorkflowImplementationStateError(WorkflowImplementationError):
    """Implementation cannot safely propose from the supplied derived state."""


class WorkflowImplementationPort(Protocol):
    """One exact pure proposal implementation for one WorkflowBinding."""

    @property
    def binding(self) -> WorkflowBinding: ...

    def propose(
        self,
        context: WorkflowStepContext,
    ) -> WorkflowProposal | None: ...


@dataclass(frozen=True, slots=True)
class WorkflowStepContext:
    """Immutable derived state supplied to one workflow implementation call.

    This object carries no WorkStore, admission surface, authority, executor,
    scheduler, model provider, host handle, or retry permission.
    """

    work: WorkSnapshot
    workflow: WorkflowRunSnapshot
    pack_binding: PackWorkflowBinding
    roles: tuple[RoleRunSnapshot, ...] = ()
    capabilities: tuple[CapabilityNeedSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.work, WorkSnapshot):
            raise TypeError("work must be WorkSnapshot")
        if not isinstance(self.workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if not isinstance(self.pack_binding, PackWorkflowBinding):
            raise TypeError("pack_binding must be PackWorkflowBinding")
        if self.work.state is not WorkState.ACTIVE:
            raise WorkflowImplementationStateError(
                "Workflow implementation requires active Work"
            )
        if self.workflow.state is not WorkflowRunState.ACTIVE:
            raise WorkflowImplementationStateError(
                "Workflow implementation requires active WorkflowRun"
            )

        run = self.workflow.run
        if run.work_id != self.work.work.work_id:
            raise WorkflowImplementationBindingError(
                "WorkflowRun belongs to another Work"
            )
        if not hmac.compare_digest(
            run.work_digest,
            self.work.work.work_digest,
        ):
            raise WorkflowImplementationBindingError(
                "WorkflowRun changed Work binding"
            )

        pin = self.pack_binding
        if pin.work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "Pack binding crossed Work identity"
            )
        if not hmac.compare_digest(pin.work_digest, run.work_digest):
            raise WorkflowImplementationBindingError(
                "Pack binding changed Work semantics"
            )
        if pin.workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "Pack binding changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            pin.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "Pack binding changed WorkflowRun semantics"
            )

        workflow_member = PackMemberBinding.create(
            kind=PackMemberKind.WORKFLOW,
            semantic_id=run.binding.workflow_id,
            version=run.binding.version,
            binding_digest=run.binding.binding_digest,
        )
        if not pin.pack.contains(workflow_member):
            raise WorkflowImplementationBindingError(
                "Pack does not contain exact WorkflowBinding"
            )

        role_ids: set[str] = set()
        for snapshot in self.roles:
            if not isinstance(snapshot, RoleRunSnapshot):
                raise TypeError("roles must contain RoleRunSnapshot values")
            role = snapshot.run
            if role.role_run_id in role_ids:
                raise WorkflowImplementationStateError(
                    "roles contains duplicate RoleRun identity"
                )
            role_ids.add(role.role_run_id)
            self._validate_child_binding(
                child_work_id=role.work_id,
                child_work_digest=role.work_digest,
                workflow_run_id=role.workflow_run_id,
                workflow_run_digest=role.workflow_run_digest,
            )

        need_ids: set[str] = set()
        for snapshot in self.capabilities:
            if not isinstance(snapshot, CapabilityNeedSnapshot):
                raise TypeError(
                    "capabilities must contain CapabilityNeedSnapshot values"
                )
            need = snapshot.need
            if need.need_id in need_ids:
                raise WorkflowImplementationStateError(
                    "capabilities contains duplicate CapabilityNeed identity"
                )
            need_ids.add(need.need_id)
            self._validate_child_binding(
                child_work_id=need.work_id,
                child_work_digest=need.work_digest,
                workflow_run_id=need.workflow_run_id,
                workflow_run_digest=need.workflow_run_digest,
            )

    def _validate_child_binding(
        self,
        *,
        child_work_id: str,
        child_work_digest: str,
        workflow_run_id: str,
        workflow_run_digest: str,
    ) -> None:
        run = self.workflow.run
        if child_work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "derived child state crossed Work identity"
            )
        if not hmac.compare_digest(child_work_digest, run.work_digest):
            raise WorkflowImplementationBindingError(
                "derived child state changed Work binding"
            )
        if workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "derived child state changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "derived child state changed WorkflowRun binding"
            )


class WorkflowImplementationBoundary:
    """Invoke one exact implementation and validate one prepared proposal.

    The boundary performs no admission and owns no durable state.
    """

    def prepare(
        self,
        implementation: WorkflowImplementationPort,
        context: WorkflowStepContext,
    ) -> WorkflowProposal | None:
        if not isinstance(context, WorkflowStepContext):
            raise TypeError("context must be WorkflowStepContext")

        binding = implementation.binding
        if not isinstance(binding, WorkflowBinding):
            raise TypeError("implementation.binding must be WorkflowBinding")

        expected = context.workflow.run.binding
        if binding != expected:
            raise WorkflowImplementationBindingError(
                "Workflow implementation does not match exact WorkflowBinding"
            )

        proposal = implementation.propose(context)
        if proposal is None:
            return None
        if isinstance(proposal, WorkflowCandidate):
            self._validate_candidate(proposal, context)
            return proposal
        if isinstance(proposal, RoleRun):
            self._validate_role_run(proposal, context)
            return proposal
        if isinstance(proposal, CapabilityNeed):
            self._validate_capability_need(proposal, context)
            return proposal
        raise WorkflowImplementationError(
            "Workflow implementation returned unsupported proposal type"
        )

    @staticmethod
    def _validate_candidate(
        candidate: WorkflowCandidate,
        context: WorkflowStepContext,
    ) -> None:
        run = context.workflow.run
        if candidate.workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "WorkflowCandidate changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            candidate.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "WorkflowCandidate changed WorkflowRun binding"
            )
        if candidate.event.work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "WorkflowCandidate crossed Work identity"
            )
        if candidate.expected_revision != context.work.revision:
            raise WorkflowImplementationStateError(
                "WorkflowCandidate does not bind current Work revision"
            )
        if (
            candidate.expected_event_digest
            != context.work.last_event_digest
        ):
            raise WorkflowImplementationStateError(
                "WorkflowCandidate does not bind current Work chronology"
            )

    @staticmethod
    def _validate_role_run(
        role: RoleRun,
        context: WorkflowStepContext,
    ) -> None:
        run = context.workflow.run
        if role.work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "RoleRun crossed Work identity"
            )
        if not hmac.compare_digest(role.work_digest, run.work_digest):
            raise WorkflowImplementationBindingError(
                "RoleRun changed Work binding"
            )
        if role.workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "RoleRun changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            role.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "RoleRun changed WorkflowRun binding"
            )
        if role.start_revision != context.work.revision:
            raise WorkflowImplementationStateError(
                "RoleRun does not bind current Work revision"
            )
        if role.start_event_digest != context.work.last_event_digest:
            raise WorkflowImplementationStateError(
                "RoleRun does not bind current Work chronology"
            )
        member = PackMemberBinding.create(
            kind=PackMemberKind.ROLE,
            semantic_id=role.binding.role_id,
            version=role.binding.version,
            binding_digest=role.binding.binding_digest,
        )
        if not context.pack_binding.pack.contains(member):
            raise WorkflowImplementationBindingError(
                "RoleRun proposal is outside pinned Pack"
            )

    @staticmethod
    def _validate_capability_need(
        need: CapabilityNeed,
        context: WorkflowStepContext,
    ) -> None:
        run = context.workflow.run
        if need.work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "CapabilityNeed crossed Work identity"
            )
        if not hmac.compare_digest(need.work_digest, run.work_digest):
            raise WorkflowImplementationBindingError(
                "CapabilityNeed changed Work binding"
            )
        if need.workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "CapabilityNeed changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            need.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "CapabilityNeed changed WorkflowRun binding"
            )
        if need.start_revision != context.work.revision:
            raise WorkflowImplementationStateError(
                "CapabilityNeed does not bind current Work revision"
            )
        if need.start_event_digest != context.work.last_event_digest:
            raise WorkflowImplementationStateError(
                "CapabilityNeed does not bind current Work chronology"
            )
        member = PackMemberBinding.create(
            kind=PackMemberKind.CAPABILITY,
            semantic_id=need.binding.capability_id,
            version=need.binding.version,
            binding_digest=need.binding.binding_digest,
        )
        if not context.pack_binding.pack.contains(member):
            raise WorkflowImplementationBindingError(
                "CapabilityNeed proposal is outside pinned Pack"
            )
