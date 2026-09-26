from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codexia_manual_agent.capability_core import (
    CapabilityHostPort,
    CapabilityNeedState,
    project_capability_handoffs,
    project_capability_needs,
)
from codexia_manual_agent.completion_core import (
    COMPLETION_CLAIM_ADMITTED_EVENT,
    WorkCompletion,
    WorkCompletionAdmissionService,
    project_admitted_completion_claim,
)
from codexia_manual_agent.delegation_core import DelegationChildrenLiveError
from codexia_manual_agent.invariant_bridge import (
    InvariantCompletionCriterionBridge,
    InvariantPackDistributionBridge,
    InvariantWorkflowImplementationBridge,
    ManagedPluginServicePort,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackWorkflowBinding,
    project_workflow_pack_binding,
)
from codexia_manual_agent.role_core import (
    CognitionPort,
    RoleRunState,
    project_cognition_handoffs,
    project_role_runs,
)
from codexia_manual_agent.work_core import WorkSnapshot, WorkState, WorkStore
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowRun,
    WorkflowRunState,
    project_workflow_runs,
)
from codexia_manual_agent.workflow_orchestration.capability_progression import (
    CapabilityProgressionService,
)
from codexia_manual_agent.workflow_orchestration.cognition_progression import (
    RoleCognitionProgressionService,
)
from codexia_manual_agent.workflow_orchestration.role_cognition import (
    ContextProjectionMaterialPort,
    RoleInstructionsMaterialPort,
)
from codexia_manual_agent.workflow_orchestration.step import (
    WorkflowStepReadPrecondition,
)
from codexia_manual_agent.workflow_orchestration.work_yield import (
    DurableWorkYield,
    DurableWorkYieldKind,
    project_durable_work_yield,
)
from codexia_manual_agent.workflow_orchestration.workflow_progression import (
    WorkflowProgressionService,
)

MAX_BOUNDED_EXISTING_WORK_STEPS = 64


class BoundedExistingWorkProgressionError(RuntimeError):
    """Base failure for finite progression of one already-existing Work."""


class BoundedExistingWorkProgressionBindingError(
    BoundedExistingWorkProgressionError
):
    """Recovered Work semantics differ from the configured progression target."""


class BoundedExistingWorkProgressionAmbiguityError(
    BoundedExistingWorkProgressionError
):
    """Current Work exposes more than one unresolved lane requiring selection."""


class BoundedExistingWorkProgressionConfigurationError(
    BoundedExistingWorkProgressionError
):
    """A required caller-supplied Codexia port or material source is absent."""


class BoundedExistingWorkProgressionStatus(StrEnum):
    YIELDED = "yielded"
    BOUND_EXHAUSTED = "bound_exhausted"
    QUIESCENT = "quiescent"
    TERMINAL_NON_YIELD = "terminal_non_yield"


@dataclass(frozen=True, slots=True)
class BoundedExistingWorkProgressionResult:
    """Ephemeral result of one finite single-Work progression call."""

    status: BoundedExistingWorkProgressionStatus
    frontier: DurableWorkYield
    steps_used: int

    def __post_init__(self) -> None:
        if type(self.status) is not BoundedExistingWorkProgressionStatus:
            raise TypeError(
                "status must be an exact BoundedExistingWorkProgressionStatus"
            )
        if not isinstance(self.frontier, DurableWorkYield):
            raise TypeError("frontier must be DurableWorkYield")
        if type(self.steps_used) is not int or self.steps_used < 0:
            raise ValueError("steps_used must be a non-negative integer")

        yielded = self.frontier.kind is not DurableWorkYieldKind.NONE
        if self.status is BoundedExistingWorkProgressionStatus.YIELDED:
            if not yielded:
                raise ValueError("YIELDED result requires durable yield material")
        elif yielded:
            raise ValueError(
                "non-YIELDED progression result cannot carry durable yield material"
            )

        if (
            self.status
            is BoundedExistingWorkProgressionStatus.TERMINAL_NON_YIELD
            and self.frontier.snapshot.state is WorkState.ACTIVE
        ):
            raise ValueError("TERMINAL_NON_YIELD requires terminal Work")
        if (
            self.status
            in {
                BoundedExistingWorkProgressionStatus.BOUND_EXHAUSTED,
                BoundedExistingWorkProgressionStatus.QUIESCENT,
            }
            and self.frontier.snapshot.state is not WorkState.ACTIVE
        ):
            raise ValueError(
                "active progression stop requires ACTIVE Work"
            )

    @property
    def work_id(self) -> str:
        return self.frontier.snapshot.work.work_id


class BoundedExistingWorkProgressionService:
    """Progress one existing Work through a finite number of Codexia steps.

    This is a single-Work interpreter, not a scheduler. It never creates Work,
    chooses between Work items, recursively progresses child Work, retries an
    already-handed-off external request, or maps Codexia state into host/IRR
    result semantics.

    One budget step means one invocation of an existing bounded Codexia
    admission/progression boundary. Such an invocation may append more than one
    event when the underlying boundary already defines that as one bounded
    handoff operation.
    """

    def __init__(
        self,
        *,
        store: WorkStore,
        plugin_service: ManagedPluginServicePort,
        provider_ref: str,
        workflow_id: str,
        workflow_version: str,
        cognition_port: CognitionPort | None = None,
        capability_port: CapabilityHostPort | None = None,
        instructions: RoleInstructionsMaterialPort | None = None,
        context: ContextProjectionMaterialPort | None = None,
    ) -> None:
        for label, value in (
            ("provider_ref", provider_ref),
            ("workflow_id", workflow_id),
            ("workflow_version", workflow_version),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{label} must be non-empty canonical text")

        self._store = store
        self._plugin_service = plugin_service
        self._provider_ref = provider_ref
        self._workflow_id = workflow_id
        self._workflow_version = workflow_version
        self._cognition_port = cognition_port
        self._capability_port = capability_port
        self._instructions = instructions
        self._context = context

    def progress(
        self,
        work_id: str,
        *,
        max_steps: int,
    ) -> BoundedExistingWorkProgressionResult:
        if type(work_id) is not str or not work_id:
            raise ValueError("work_id must be non-empty text")
        if (
            type(max_steps) is not int
            or max_steps <= 0
            or max_steps > MAX_BOUNDED_EXISTING_WORK_STEPS
        ):
            raise ValueError(
                "max_steps must be an integer in "
                f"[1, {MAX_BOUNDED_EXISTING_WORK_STEPS}]"
            )

        frontier = project_durable_work_yield(work_id, store=self._store)
        if frontier.kind is not DurableWorkYieldKind.NONE:
            return BoundedExistingWorkProgressionResult(
                status=BoundedExistingWorkProgressionStatus.YIELDED,
                frontier=frontier,
                steps_used=0,
            )
        if frontier.snapshot.state is not WorkState.ACTIVE:
            return BoundedExistingWorkProgressionResult(
                status=(
                    BoundedExistingWorkProgressionStatus.TERMINAL_NON_YIELD
                ),
                frontier=frontier,
                steps_used=0,
            )

        steps_used = 0
        for _ in range(max_steps):
            before = frontier.snapshot
            attempted = self._advance_one(work_id)
            if not attempted:
                frontier = project_durable_work_yield(
                    work_id,
                    store=self._store,
                )
                return BoundedExistingWorkProgressionResult(
                    status=BoundedExistingWorkProgressionStatus.QUIESCENT,
                    frontier=frontier,
                    steps_used=steps_used,
                )

            steps_used += 1
            frontier = project_durable_work_yield(
                work_id,
                store=self._store,
            )
            if frontier.kind is not DurableWorkYieldKind.NONE:
                return BoundedExistingWorkProgressionResult(
                    status=BoundedExistingWorkProgressionStatus.YIELDED,
                    frontier=frontier,
                    steps_used=steps_used,
                )
            if frontier.snapshot.state is not WorkState.ACTIVE:
                return BoundedExistingWorkProgressionResult(
                    status=(
                        BoundedExistingWorkProgressionStatus
                        .TERMINAL_NON_YIELD
                    ),
                    frontier=frontier,
                    steps_used=steps_used,
                )
            if frontier.snapshot.revision == before.revision:
                return BoundedExistingWorkProgressionResult(
                    status=BoundedExistingWorkProgressionStatus.QUIESCENT,
                    frontier=frontier,
                    steps_used=steps_used,
                )

        return BoundedExistingWorkProgressionResult(
            status=BoundedExistingWorkProgressionStatus.BOUND_EXHAUSTED,
            frontier=frontier,
            steps_used=steps_used,
        )

    def _advance_one(self, work_id: str) -> bool:
        events = self._store.events(work_id)
        workflows = project_workflow_runs(events)

        if not workflows:
            distribution, binding = self._resolve_activation_target()
            snapshot = self._store.snapshot(work_id)
            if snapshot.state is not WorkState.ACTIVE:
                return False
            WorkflowAdmission(self._store).admit_start(
                WorkflowRun.create(
                    snapshot=snapshot,
                    binding=binding,
                )
            )
            return True

        if len(workflows) != 1:
            raise BoundedExistingWorkProgressionAmbiguityError(
                "bounded existing-Work progression requires exactly one WorkflowRun"
            )
        workflow = workflows[0]
        self._validate_workflow_target(work_id, workflow.run.binding)

        pin = project_workflow_pack_binding(
            events,
            workflow.run.workflow_run_id,
        )
        if pin is None:
            distribution, binding = self._resolve_activation_target()
            if workflow.run.binding != binding:
                raise BoundedExistingWorkProgressionBindingError(
                    "existing WorkflowRun differs from exact provider distribution"
                )
            snapshot = self._store.snapshot(work_id)
            if (
                not events
                or events[-1].event_id != workflow.run.workflow_run_id
                or events[-1].sequence != snapshot.revision
                or events[-1].event_digest != snapshot.last_event_digest
            ):
                raise BoundedExistingWorkProgressionBindingError(
                    "unpinned WorkflowRun is not the exact current Work head"
                )
            PackAdmission(self._store).admit_workflow_binding(
                PackWorkflowBinding.create(
                    workflow=workflow,
                    snapshot=snapshot,
                    pack=distribution.pack,
                )
            )
            return True

        if workflow.state is not WorkflowRunState.ACTIVE:
            return False

        snapshot = self._store.snapshot(work_id)
        if pin.work_id != work_id or pin.work_digest != snapshot.work.work_digest:
            raise BoundedExistingWorkProgressionBindingError(
                "Pack pin changed exact Work binding"
            )

        if events and events[-1].kind == COMPLETION_CLAIM_ADMITTED_EVENT:
            head = events[-1]
            claim = project_admitted_completion_claim(
                events,
                head.event_id,
            )
            try:
                completion = WorkCompletion.create(
                    snapshot=snapshot,
                    claim=claim,
                    claim_admission_event=head,
                )
                WorkCompletionAdmissionService(self._store).admit(completion)
            except DelegationChildrenLiveError:
                return False
            return True

        roles = tuple(
            item
            for item in project_role_runs(events)
            if item.run.workflow_run_id == workflow.run.workflow_run_id
            and item.state in {RoleRunState.ACTIVE, RoleRunState.REQUESTED}
        )
        capabilities = tuple(
            item
            for item in project_capability_needs(events)
            if item.need.workflow_run_id == workflow.run.workflow_run_id
            and item.state is CapabilityNeedState.PENDING
        )
        unresolved_count = len(roles) + len(capabilities)
        if unresolved_count > 1:
            raise BoundedExistingWorkProgressionAmbiguityError(
                "current Work exposes multiple unresolved progression lanes"
            )

        if roles:
            role = roles[0]
            if role.state is RoleRunState.REQUESTED:
                if role.request_id is None:
                    raise BoundedExistingWorkProgressionBindingError(
                        "REQUESTED RoleRun lacks exact cognition request identity"
                    )
                if any(
                    handoff.request_id == role.request_id
                    for handoff in project_cognition_handoffs(events)
                ):
                    return False
            self._progress_role(work_id, role.run.role_run_id)
            return True

        if capabilities:
            capability = capabilities[0]
            if any(
                handoff.need_id == capability.need.need_id
                for handoff in project_capability_handoffs(events)
            ):
                return False
            self._progress_capability(
                work_id,
                capability.need.need_id,
            )
            return True

        WorkflowProgressionService(
            store=self._store,
            resolver=InvariantWorkflowImplementationBridge(
                self._plugin_service
            ),
            completion_resolver=InvariantCompletionCriterionBridge(
                self._plugin_service
            ),
        ).progress_once(
            work_id=work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=self._provider_ref,
            precondition=WorkflowStepReadPrecondition.from_snapshot(
                self._store.snapshot(work_id)
            ),
        )
        return True

    def _resolve_activation_target(self):
        distribution = InvariantPackDistributionBridge(
            self._plugin_service
        ).resolve(self._provider_ref)
        matches = tuple(
            binding
            for binding in distribution.workflows
            if binding.workflow_id == self._workflow_id
            and binding.version == self._workflow_version
        )
        if len(matches) != 1:
            raise BoundedExistingWorkProgressionConfigurationError(
                "provider distribution does not expose one exact configured Workflow"
            )
        return distribution, matches[0]

    def _validate_workflow_target(self, work_id: str, binding) -> None:
        if (
            binding.workflow_id != self._workflow_id
            or binding.version != self._workflow_version
        ):
            raise BoundedExistingWorkProgressionBindingError(
                "existing WorkflowRun differs from configured workflow selector"
            )
        snapshot = self._store.snapshot(work_id)
        if snapshot.work.work_id != work_id:
            raise BoundedExistingWorkProgressionBindingError(
                "Work snapshot crossed requested Work identity"
            )

    def _progress_role(self, work_id: str, role_run_id: str) -> None:
        if (
            self._cognition_port is None
            or self._instructions is None
            or self._context is None
        ):
            raise BoundedExistingWorkProgressionConfigurationError(
                "RoleRun progression requires cognition port, instructions, and context"
            )
        RoleCognitionProgressionService(
            store=self._store,
            instructions=self._instructions,
            context=self._context,
        ).progress_once(
            work_id=work_id,
            role_run_id=role_run_id,
            port=self._cognition_port,
        )

    def _progress_capability(self, work_id: str, need_id: str) -> None:
        if self._capability_port is None:
            raise BoundedExistingWorkProgressionConfigurationError(
                "CapabilityNeed progression requires CapabilityHostPort"
            )
        CapabilityProgressionService(self._store).progress_once(
            work_id=work_id,
            need_id=need_id,
            port=self._capability_port,
        )
