from __future__ import annotations

import hmac

from codexia_manual_agent.capability_core.models import (
    CapabilityNeed,
    CapabilityNeedSnapshot,
    CapabilityNeedState,
    CapabilityOutcome,
)
from codexia_manual_agent.capability_core.projection import project_capability_need
from codexia_manual_agent.pack_core.models import (
    PackMemberBinding,
    PackMemberKind,
)
from codexia_manual_agent.pack_core.projection import (
    project_workflow_pack_binding,
)
from codexia_manual_agent.work_core import WorkEvent, WorkStore
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowRunState,
    project_workflow_run,
)


class CapabilityAdmissionError(RuntimeError):
    """Base failure for the G2.4 capability admission boundary."""


class CapabilityBindingError(CapabilityAdmissionError):
    """Capability record changed exact Work/Workflow/Need binding."""


class CapabilityNeedStateError(CapabilityAdmissionError):
    """Requested operation is invalid for the recovered Need lifecycle."""




def _require_pack_capability_membership(
    events: tuple[WorkEvent, ...],
    need: CapabilityNeed,
) -> None:
    pin = project_workflow_pack_binding(events, need.workflow_run_id)
    if pin is None:
        return
    member = PackMemberBinding.create(
        kind=PackMemberKind.CAPABILITY,
        semantic_id=need.binding.capability_id,
        version=need.binding.version,
        binding_digest=need.binding.binding_digest,
    )
    if not pin.pack.contains(member):
        raise CapabilityBindingError(
            "CapabilityBinding is not a member of the WorkflowRun Pack"
        )

class CapabilityAdmission:
    """Admit Need declarations and host-originated outcomes into Work truth."""

    def __init__(self, store: WorkStore) -> None:
        self._store = store
        self._workflow_admission = WorkflowAdmission(store)

    def admit_need(self, need: CapabilityNeed) -> CapabilityNeedSnapshot:
        if not isinstance(need, CapabilityNeed):
            raise TypeError("need must be CapabilityNeed")

        candidate = need.to_workflow_candidate()
        events = self._store.events(need.work_id)

        # Exact retry after an ambiguous caller acknowledgement stays idempotent
        # even if unrelated Work events have advanced the chronology.
        if any(event.event_id == candidate.event.event_id for event in events):
            self._workflow_admission.admit_candidate(candidate)
            recovered = project_capability_need(
                self._store.events(need.work_id),
                need.need_id,
            )
            if recovered.need != need:
                raise CapabilityBindingError(
                    "capability need identity reused for different exact Need"
                )
            _require_pack_capability_membership(
                self._store.events(need.work_id),
                recovered.need,
            )
            return recovered

        workflow = project_workflow_run(events, need.workflow_run_id)
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise CapabilityNeedStateError(
                "CapabilityNeed cannot be declared by terminal WorkflowRun"
            )
        if not hmac.compare_digest(
            workflow.run.run_digest,
            need.workflow_run_digest,
        ):
            raise CapabilityBindingError("CapabilityNeed changed WorkflowRun binding")
        if workflow.run.work_id != need.work_id:
            raise CapabilityBindingError("CapabilityNeed crossed Work identity")
        if not hmac.compare_digest(workflow.run.work_digest, need.work_digest):
            raise CapabilityBindingError("CapabilityNeed changed Work binding")

        _require_pack_capability_membership(events, need)
        self._workflow_admission.admit_candidate(candidate)
        return project_capability_need(
            self._store.events(need.work_id),
            need.need_id,
        )

    def admit_outcome(self, outcome: CapabilityOutcome) -> CapabilityNeedSnapshot:
        if not isinstance(outcome, CapabilityOutcome):
            raise TypeError("outcome must be CapabilityOutcome")

        events = self._store.events(outcome.work_id)

        # Outcome events are host-originated observations rather than workflow
        # progression. Exact retries therefore recover the existing event instead
        # of manufacturing a new WorkflowCandidate at a later Work revision.
        if any(event.event_id == outcome.outcome_id for event in events):
            recovered = project_capability_need(events, outcome.need_id)
            if recovered.outcome != outcome:
                raise CapabilityBindingError(
                    "capability outcome identity reused for different exact Outcome"
                )
            return recovered

        need = project_capability_need(events, outcome.need_id)
        if need.state is not CapabilityNeedState.PENDING:
            raise CapabilityNeedStateError(
                f"CapabilityNeed is {need.state.value}; no new outcome may follow"
            )

        source = need.need
        if outcome.need_id != source.need_id:
            raise CapabilityBindingError("CapabilityOutcome changed Need identity")
        if not hmac.compare_digest(outcome.need_digest, source.need_digest):
            raise CapabilityBindingError("CapabilityOutcome changed Need binding")
        if outcome.work_id != source.work_id:
            raise CapabilityBindingError("CapabilityOutcome crossed Work identity")
        if not hmac.compare_digest(outcome.work_digest, source.work_digest):
            raise CapabilityBindingError("CapabilityOutcome changed Work binding")
        if outcome.workflow_run_id != source.workflow_run_id:
            raise CapabilityBindingError(
                "CapabilityOutcome changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            outcome.workflow_run_digest,
            source.workflow_run_digest,
        ):
            raise CapabilityBindingError(
                "CapabilityOutcome changed WorkflowRun binding"
            )

        current = self._store.snapshot(outcome.work_id)
        event = outcome.to_event(current)
        self._store.append(
            outcome.work_id,
            expected_revision=current.revision,
            event=event,
        )
        return project_capability_need(
            self._store.events(outcome.work_id),
            outcome.need_id,
        )
