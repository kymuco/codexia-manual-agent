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
    ResolvedPackDistribution,
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
from codexia_manual_agent.work_core import (
    Work,
    WorkConcurrencyError,
    WorkEvent,
    WorkSnapshot,
    WorkState,
    WorkStore,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
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


class _FrontierBoundWorkStore:
    """Bind one semantic step to the exact projected parent Work frontier.

    Reads and writes for the target Work fail closed if another writer advances
    the chronology. Successful writes by this step advance the local binding to
    the newly admitted snapshot. Reads of independently owned child Work remain
    delegated to the underlying store.
    """

    def __init__(self, store: WorkStore, snapshot: WorkSnapshot) -> None:
        self._store = store
        self._work_id = snapshot.work.work_id
        self._snapshot = snapshot

    def _require_current(self) -> WorkSnapshot:
        current = self._store.snapshot(self._work_id)
        if current != self._snapshot:
            raise WorkConcurrencyError(
                "bounded progression frontier changed before semantic transition"
            )
        return current

    def create(self, _work: Work) -> WorkSnapshot:
        raise BoundedExistingWorkProgressionBindingError(
            "bounded existing-Work progression cannot create another root Work"
        )

    def snapshot(self, work_id: str) -> WorkSnapshot:
        if work_id != self._work_id:
            return self._store.snapshot(work_id)
        return self._require_current()

    def events(self, work_id: str) -> tuple[WorkEvent, ...]:
        if work_id != self._work_id:
            return self._store.events(work_id)
        current = self._require_current()
        events = self._store.events(work_id)
        if (
            len(events) != current.revision
            or (
                (events[-1].event_digest if events else None)
                != current.last_event_digest
            )
            or any(event.work_id != work_id for event in events)
        ):
            raise WorkConcurrencyError(
                "bounded progression chronology changed during exact read"
            )
        return events

    def append(
        self,
        work_id: str,
        *,
        expected_revision: int,
        event: WorkEvent,
        read_preconditions: tuple[WorkSnapshot, ...] = (),
    ) -> WorkSnapshot:
        if work_id != self._work_id:
            raise BoundedExistingWorkProgressionBindingError(
                "bounded progression cannot append another Work"
            )
        self._require_current()
        if expected_revision != self._snapshot.revision:
            raise WorkConcurrencyError(
                "semantic transition is not based on bound Work frontier"
            )
        admitted = self._store.append(
            work_id,
            expected_revision=expected_revision,
            event=event,
            read_preconditions=read_preconditions,
        )
        self._snapshot = admitted
        return admitted

    def append_with_child_create(
        self,
        work_id: str,
        *,
        expected_revision: int,
        event: WorkEvent,
        child_work: Work,
        read_preconditions: tuple[WorkSnapshot, ...] = (),
    ) -> tuple[WorkSnapshot, WorkSnapshot]:
        if work_id != self._work_id:
            raise BoundedExistingWorkProgressionBindingError(
                "bounded progression cannot delegate from another Work"
            )
        self._require_current()
        if expected_revision != self._snapshot.revision:
            raise WorkConcurrencyError(
                "delegation is not based on bound Work frontier"
            )
        parent, child = self._store.append_with_child_create(
            work_id,
            expected_revision=expected_revision,
            event=event,
            child_work=child_work,
            read_preconditions=read_preconditions,
        )
        self._snapshot = parent
        return parent, child

    def _append_completion(
        self,
        work_id: str,
        *,
        expected_revision: int,
        event: WorkEvent,
        read_preconditions: tuple[WorkSnapshot, ...] = (),
    ) -> WorkSnapshot:
        if work_id != self._work_id:
            raise BoundedExistingWorkProgressionBindingError(
                "bounded progression cannot complete another Work"
            )
        self._require_current()
        if expected_revision != self._snapshot.revision:
            raise WorkConcurrencyError(
                "WorkCompletion is not based on bound Work frontier"
            )
        append_completion = getattr(self._store, "_append_completion", None)
        if append_completion is None:
            raise BoundedExistingWorkProgressionBindingError(
                "Work store does not expose guarded completion admission"
            )
        admitted = append_completion(
            work_id,
            expected_revision=expected_revision,
            event=event,
            read_preconditions=read_preconditions,
        )
        self._snapshot = admitted
        return admitted


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
            try:
                attempted = self._advance_one(
                    work_id,
                    expected_snapshot=before,
                )
            except WorkConcurrencyError:
                attempted = False
            if not attempted:
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

    def _advance_one(
        self,
        work_id: str,
        *,
        expected_snapshot: WorkSnapshot,
    ) -> bool:
        if not isinstance(expected_snapshot, WorkSnapshot):
            raise TypeError("expected_snapshot must be WorkSnapshot")

        snapshot, events = self._read_exact_work(work_id)
        if snapshot != expected_snapshot:
            # The caller decided to advance from another exact durable
            # frontier. Do not reinterpret the newer chronology here: it may
            # now expose AttentionNeed, WorkCompletion, cancellation, or any
            # other host-visible boundary. The caller re-projects it before
            # deciding what to do next.
            return False

        step_store = _FrontierBoundWorkStore(
            self._store,
            expected_snapshot,
        )
        workflows = project_workflow_runs(events)

        if not workflows:
            if events:
                raise BoundedExistingWorkProgressionBindingError(
                    "unconfigured Work already has durable chronology"
                )
            _, binding = self._resolve_activation_target()
            if snapshot.state is not WorkState.ACTIVE:
                return False
            WorkflowAdmission(step_store).admit_start(
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
        self._validate_workflow_target(
            work_id,
            snapshot,
            workflow.run.binding,
        )

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
            if (
                not events
                or events[-1].event_id != workflow.run.workflow_run_id
                or events[-1].sequence != snapshot.revision
                or events[-1].event_digest != snapshot.last_event_digest
            ):
                raise BoundedExistingWorkProgressionBindingError(
                    "unpinned WorkflowRun is not the exact current Work head"
                )
            PackAdmission(step_store).admit_workflow_binding(
                PackWorkflowBinding.create(
                    workflow=workflow,
                    snapshot=snapshot,
                    pack=distribution.pack,
                )
            )
            return True

        if workflow.state is not WorkflowRunState.ACTIVE:
            return False

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
                WorkCompletionAdmissionService(step_store).admit(completion)
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
                handoff = next(
                    (
                        item
                        for item in project_cognition_handoffs(events)
                        if item.request_id == role.request_id
                    ),
                    None,
                )
                if handoff is not None:
                    if (
                        self._cognition_port is not None
                        and handoff.port_id != self._cognition_port.port_id
                    ):
                        raise BoundedExistingWorkProgressionBindingError(
                            "durable cognition handoff is routed to another port"
                        )
                    return False
            self._progress_role(
                work_id,
                role.run.role_run_id,
                store=step_store,
            )
            return True

        if capabilities:
            capability = capabilities[0]
            handoff = next(
                (
                    item
                    for item in project_capability_handoffs(events)
                    if item.need_id == capability.need.need_id
                ),
                None,
            )
            if handoff is not None:
                if (
                    self._capability_port is not None
                    and handoff.host_id != self._capability_port.host_id
                ):
                    raise BoundedExistingWorkProgressionBindingError(
                        "durable capability handoff is routed to another host"
                    )
                return False
            self._progress_capability(
                work_id,
                capability.need.need_id,
                store=step_store,
            )
            return True

        WorkflowProgressionService(
            store=step_store,
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
            precondition=WorkflowStepReadPrecondition.from_snapshot(snapshot),
        )
        return True

    def _read_exact_work(
        self,
        work_id: str,
    ) -> tuple[WorkSnapshot, tuple[WorkEvent, ...]]:
        snapshot = self._store.snapshot(work_id)
        events = self._store.events(work_id)

        if snapshot.work.work_id != work_id:
            raise BoundedExistingWorkProgressionBindingError(
                "Work snapshot crossed requested Work identity"
            )
        if any(event.work_id != work_id for event in events):
            raise BoundedExistingWorkProgressionBindingError(
                "Work chronology crossed requested Work identity"
            )
        if snapshot.revision != len(events):
            raise BoundedExistingWorkProgressionBindingError(
                "Work snapshot revision differs from durable chronology"
            )
        if snapshot.revision == 0:
            if events or snapshot.last_event_digest is not None:
                raise BoundedExistingWorkProgressionBindingError(
                    "revision-zero Work has inconsistent durable chronology"
                )
            return snapshot, events

        if not events:
            raise BoundedExistingWorkProgressionBindingError(
                "nonzero Work revision has no durable chronology"
            )
        head = events[-1]
        if (
            head.sequence != snapshot.revision
            or head.event_digest != snapshot.last_event_digest
        ):
            raise BoundedExistingWorkProgressionBindingError(
                "Work snapshot does not bind exact durable chronology head"
            )
        return snapshot, events

    def _resolve_activation_target(
        self,
    ) -> tuple[ResolvedPackDistribution, WorkflowBinding]:
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

    def _validate_workflow_target(
        self,
        work_id: str,
        snapshot: WorkSnapshot,
        binding: WorkflowBinding,
    ) -> None:
        if snapshot.work.work_id != work_id:
            raise BoundedExistingWorkProgressionBindingError(
                "Work snapshot crossed requested Work identity"
            )
        if (
            binding.workflow_id != self._workflow_id
            or binding.version != self._workflow_version
        ):
            raise BoundedExistingWorkProgressionBindingError(
                "existing WorkflowRun differs from configured workflow selector"
            )

    def _progress_role(
        self,
        work_id: str,
        role_run_id: str,
        *,
        store: WorkStore,
    ) -> None:
        if (
            self._cognition_port is None
            or self._instructions is None
            or self._context is None
        ):
            raise BoundedExistingWorkProgressionConfigurationError(
                "RoleRun progression requires cognition port, instructions, and context"
            )
        RoleCognitionProgressionService(
            store=store,
            instructions=self._instructions,
            context=self._context,
        ).progress_once(
            work_id=work_id,
            role_run_id=role_run_id,
            port=self._cognition_port,
        )

    def _progress_capability(
        self,
        work_id: str,
        need_id: str,
        *,
        store: WorkStore,
    ) -> None:
        if self._capability_port is None:
            raise BoundedExistingWorkProgressionConfigurationError(
                "CapabilityNeed progression requires CapabilityHostPort"
            )
        CapabilityProgressionService(store).progress_once(
            work_id=work_id,
            need_id=need_id,
            port=self._capability_port,
        )
