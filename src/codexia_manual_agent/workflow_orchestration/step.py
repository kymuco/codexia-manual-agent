from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from codexia_manual_agent.capability_core import (
    CapabilityNeedSnapshot,
    project_capability_needs,
)
from codexia_manual_agent.invariant_bridge import (
    InvariantWorkflowImplementationBridge,
    ResolvedWorkflowImplementation,
)
from codexia_manual_agent.pack_core import (
    PackWorkflowBinding,
    project_workflow_pack_binding,
)
from codexia_manual_agent.role_core import RoleRunSnapshot, project_role_runs
from codexia_manual_agent.work_core import WorkEvent, WorkSnapshot
from codexia_manual_agent.workflow_core import (
    WorkflowRunSnapshot,
    project_workflow_run,
)
from codexia_manual_agent.workflow_runtime import (
    WorkflowImplementationBoundary,
    WorkflowProposal,
    WorkflowStepContext,
)


class WorkflowStepError(RuntimeError):
    """Base failure for one bounded G2.11 Workflow step."""


class WorkflowStepReadConflictError(WorkflowStepError):
    """Work changed while the service was recovering one exact read view."""


class WorkflowStepPackRequiredError(WorkflowStepError):
    """G2.11 requires an exact durable Pack pin for the WorkflowRun."""


class WorkflowReadStorePort(Protocol):
    """Read-only WorkStore surface required by one Workflow step."""

    def snapshot(self, work_id: str) -> WorkSnapshot: ...

    def events(self, work_id: str) -> tuple[WorkEvent, ...]: ...


@dataclass(frozen=True, slots=True)
class WorkflowStepResult:
    """Ephemeral result of one recovered/read-only Workflow computation."""

    work_id: str
    workflow_run_id: str
    work_revision: int
    work_event_digest: str | None
    pack_binding_digest: str
    workflow_binding_digest: str
    provider_ref: str
    proposal: WorkflowProposal | None

    def __post_init__(self) -> None:
        if not isinstance(self.work_id, str) or not self.work_id:
            raise TypeError("work_id must be non-empty text")
        if not isinstance(self.workflow_run_id, str) or not self.workflow_run_id:
            raise TypeError("workflow_run_id must be non-empty text")
        if type(self.work_revision) is not int or self.work_revision < 0:
            raise TypeError("work_revision must be a non-negative integer")
        if self.work_event_digest is not None and (
            not isinstance(self.work_event_digest, str)
            or len(self.work_event_digest) != 64
        ):
            raise TypeError("work_event_digest must be SHA-256 text or None")
        for field_name, value in (
            ("pack_binding_digest", self.pack_binding_digest),
            ("workflow_binding_digest", self.workflow_binding_digest),
        ):
            if not isinstance(value, str) or len(value) != 64:
                raise TypeError(f"{field_name} must be SHA-256 text")
        if not isinstance(self.provider_ref, str) or not self.provider_ref:
            raise TypeError("provider_ref must be non-empty text")


class WorkflowStepService:
    """Recover one exact Workflow view and compute at most one proposal.

    The service owns no admission, no scheduler loop, no capability host, no
    executor, no authority and no retry policy. It performs one bounded read /
    projection / implementation-resolution / proposal computation.
    """

    def __init__(
        self,
        *,
        store: WorkflowReadStorePort,
        resolver: InvariantWorkflowImplementationBridge,
        boundary: WorkflowImplementationBoundary | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._boundary = boundary or WorkflowImplementationBoundary()

    def step(
        self,
        *,
        work_id: str,
        workflow_run_id: str,
        provider_ref: str,
    ) -> WorkflowStepResult:
        events = self._store.events(work_id)
        snapshot = self._store.snapshot(work_id)
        self._validate_read_view(events=events, snapshot=snapshot)

        workflow = project_workflow_run(events, workflow_run_id)
        if workflow.run.work_id != work_id:
            raise WorkflowStepError(
                "Recovered WorkflowRun belongs to another Work"
            )

        pack_binding = project_workflow_pack_binding(
            events,
            workflow_run_id,
        )
        if pack_binding is None:
            raise WorkflowStepPackRequiredError(
                "WorkflowRun has no durable PackWorkflowBinding"
            )

        roles = self._workflow_roles(events, workflow)
        capabilities = self._workflow_capabilities(events, workflow)

        context = WorkflowStepContext(
            work=snapshot,
            workflow=workflow,
            pack_binding=pack_binding,
            roles=roles,
            capabilities=capabilities,
        )

        resolved = self._resolver.resolve(
            provider_ref=provider_ref,
            workflow=workflow,
            pack_binding=pack_binding,
        )
        self._validate_resolution(
            resolved=resolved,
            workflow=workflow,
            pack_binding=pack_binding,
        )

        proposal = self._boundary.prepare(
            resolved.implementation,
            context,
        )

        return WorkflowStepResult(
            work_id=work_id,
            workflow_run_id=workflow_run_id,
            work_revision=snapshot.revision,
            work_event_digest=snapshot.last_event_digest,
            pack_binding_digest=pack_binding.pack.binding_digest,
            workflow_binding_digest=workflow.run.binding.binding_digest,
            provider_ref=resolved.provider_ref,
            proposal=proposal,
        )

    @staticmethod
    def _validate_read_view(
        *,
        events: tuple[WorkEvent, ...],
        snapshot: WorkSnapshot,
    ) -> None:
        if snapshot.revision != len(events):
            raise WorkflowStepReadConflictError(
                "Work changed while recovering events and snapshot"
            )
        expected_digest = None if not events else events[-1].event_digest
        if snapshot.last_event_digest != expected_digest:
            raise WorkflowStepReadConflictError(
                "Recovered snapshot does not match exact event chronology"
            )
        if events and any(event.work_id != snapshot.work.work_id for event in events):
            raise WorkflowStepReadConflictError(
                "Recovered event chronology crossed Work identity"
            )

    @staticmethod
    def _workflow_roles(
        events: tuple[WorkEvent, ...],
        workflow: WorkflowRunSnapshot,
    ) -> tuple[RoleRunSnapshot, ...]:
        run_id = workflow.run.workflow_run_id
        return tuple(
            role
            for role in project_role_runs(events)
            if role.run.workflow_run_id == run_id
        )

    @staticmethod
    def _workflow_capabilities(
        events: tuple[WorkEvent, ...],
        workflow: WorkflowRunSnapshot,
    ) -> tuple[CapabilityNeedSnapshot, ...]:
        run_id = workflow.run.workflow_run_id
        return tuple(
            need
            for need in project_capability_needs(events)
            if need.need.workflow_run_id == run_id
        )

    @staticmethod
    def _validate_resolution(
        *,
        resolved: ResolvedWorkflowImplementation,
        workflow: WorkflowRunSnapshot,
        pack_binding: PackWorkflowBinding,
    ) -> None:
        if resolved.workflow_binding != workflow.run.binding:
            raise WorkflowStepError(
                "Resolved implementation changed WorkflowBinding"
            )
        if resolved.pack_binding_digest != pack_binding.pack.binding_digest:
            raise WorkflowStepError(
                "Resolved implementation changed PackBinding"
            )
