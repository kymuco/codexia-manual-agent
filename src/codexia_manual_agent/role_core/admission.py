from __future__ import annotations

import hmac

from codexia_manual_agent.role_core.models import (
    CognitionOutcome,
    CognitionRequest,
    RoleRun,
    RoleRunSnapshot,
    RoleRunState,
)
from codexia_manual_agent.role_core.projection import project_role_run
from codexia_manual_agent.work_core import WorkStore
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowRunState,
    project_workflow_run,
)


class RoleAdmissionError(RuntimeError):
    """Base failure for the G2.3 role/cognition admission boundary."""


class RoleBindingError(RoleAdmissionError):
    """A role/cognition record changed its exact Work/Workflow/Role binding."""


class RoleRunStateError(RoleAdmissionError):
    """Requested operation is invalid for the recovered RoleRun state."""


class RoleAdmission:
    """Admit RoleRun, cognition request, and cognition outcome into Work truth."""

    def __init__(self, store: WorkStore) -> None:
        self._store = store
        self._workflow_admission = WorkflowAdmission(store)

    def admit_start(self, run: RoleRun) -> RoleRunSnapshot:
        if not isinstance(run, RoleRun):
            raise TypeError("run must be RoleRun")

        candidate = run.to_start_candidate()
        events = self._store.events(run.work_id)

        # Exact retry after ambiguous acknowledgement must remain idempotent even
        # if later events have advanced the Work.
        if any(event.event_id == candidate.event.event_id for event in events):
            self._workflow_admission.admit_candidate(candidate)
            recovered = project_role_run(
                self._store.events(run.work_id),
                run.role_run_id,
            )
            if recovered.run != run:
                raise RoleBindingError("role.start identity reused for different RoleRun")
            return recovered

        workflow = project_workflow_run(events, run.workflow_run_id)
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise RoleRunStateError("RoleRun cannot start under terminal WorkflowRun")
        if not hmac.compare_digest(
            workflow.run.run_digest,
            run.workflow_run_digest,
        ):
            raise RoleBindingError("RoleRun changed WorkflowRun binding")
        if workflow.run.work_id != run.work_id:
            raise RoleBindingError("RoleRun crossed Work identity")
        if not hmac.compare_digest(workflow.run.work_digest, run.work_digest):
            raise RoleBindingError("RoleRun changed Work binding")

        self._workflow_admission.admit_candidate(candidate)
        return project_role_run(
            self._store.events(run.work_id),
            run.role_run_id,
        )

    def admit_request(self, request: CognitionRequest) -> RoleRunSnapshot:
        if not isinstance(request, CognitionRequest):
            raise TypeError("request must be CognitionRequest")

        candidate = request.to_workflow_candidate()
        events = self._store.events(request.work_id)

        if any(event.event_id == candidate.event.event_id for event in events):
            self._workflow_admission.admit_candidate(candidate)
            recovered = project_role_run(
                self._store.events(request.work_id),
                request.role_run_id,
            )
            if recovered.request_id != request.request_id or not hmac.compare_digest(
                recovered.request_digest or "",
                request.request_digest,
            ):
                raise RoleBindingError(
                    "cognition request identity reused for different request"
                )
            return recovered

        role = project_role_run(events, request.role_run_id)
        if role.state is not RoleRunState.ACTIVE:
            raise RoleRunStateError(
                f"RoleRun is {role.state.value}; cognition request cannot start"
            )
        run = role.run
        if request.role_run_id != run.role_run_id:
            raise RoleBindingError("CognitionRequest changed RoleRun identity")
        if not hmac.compare_digest(request.role_run_digest, run.run_digest):
            raise RoleBindingError("CognitionRequest changed RoleRun binding")
        if request.work_id != run.work_id:
            raise RoleBindingError("CognitionRequest crossed Work identity")
        if not hmac.compare_digest(request.work_digest, run.work_digest):
            raise RoleBindingError("CognitionRequest changed Work binding")
        if request.workflow_run_id != run.workflow_run_id:
            raise RoleBindingError("CognitionRequest changed WorkflowRun identity")
        if not hmac.compare_digest(
            request.workflow_run_digest,
            run.workflow_run_digest,
        ):
            raise RoleBindingError("CognitionRequest changed WorkflowRun binding")
        if request.instructions_digest != run.binding.instructions_digest:
            raise RoleBindingError("CognitionRequest changed Role instructions")
        if request.context_digest != run.context.content_digest:
            raise RoleBindingError("CognitionRequest changed ContextProjection")

        self._workflow_admission.admit_candidate(candidate)
        return project_role_run(
            self._store.events(request.work_id),
            request.role_run_id,
        )

    def admit_outcome(self, outcome: CognitionOutcome) -> RoleRunSnapshot:
        if not isinstance(outcome, CognitionOutcome):
            raise TypeError("outcome must be CognitionOutcome")

        candidate = outcome.to_workflow_candidate()
        events = self._store.events(outcome.work_id)

        if any(event.event_id == candidate.event.event_id for event in events):
            self._workflow_admission.admit_candidate(candidate)
            recovered = project_role_run(
                self._store.events(outcome.work_id),
                outcome.role_run_id,
            )
            if recovered.terminal_event_id != outcome.outcome_id:
                raise RoleBindingError(
                    "cognition outcome identity reused for different outcome"
                )
            return recovered

        role = project_role_run(events, outcome.role_run_id)
        if role.state is not RoleRunState.REQUESTED:
            raise RoleRunStateError(
                f"RoleRun is {role.state.value}; no cognition outcome is pending"
            )
        run = role.run
        if role.request_id != outcome.request_id:
            raise RoleBindingError("CognitionOutcome changed request identity")
        if role.request_digest is None or not hmac.compare_digest(
            role.request_digest,
            outcome.request_digest,
        ):
            raise RoleBindingError("CognitionOutcome changed request binding")
        if outcome.role_run_id != run.role_run_id:
            raise RoleBindingError("CognitionOutcome changed RoleRun identity")
        if not hmac.compare_digest(outcome.role_run_digest, run.run_digest):
            raise RoleBindingError("CognitionOutcome changed RoleRun binding")
        if outcome.work_id != run.work_id:
            raise RoleBindingError("CognitionOutcome crossed Work identity")
        if not hmac.compare_digest(outcome.work_digest, run.work_digest):
            raise RoleBindingError("CognitionOutcome changed Work binding")
        if outcome.workflow_run_id != run.workflow_run_id:
            raise RoleBindingError("CognitionOutcome changed WorkflowRun identity")
        if not hmac.compare_digest(
            outcome.workflow_run_digest,
            run.workflow_run_digest,
        ):
            raise RoleBindingError("CognitionOutcome changed WorkflowRun binding")

        self._workflow_admission.admit_candidate(candidate)
        return project_role_run(
            self._store.events(outcome.work_id),
            outcome.role_run_id,
        )
