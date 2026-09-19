from __future__ import annotations

import hmac

from codexia_manual_agent.work_core import (
    WorkConcurrencyError,
    WorkStore,
)
from codexia_manual_agent.workflow_core.models import (
    WorkflowCandidate,
    WorkflowRun,
    WorkflowRunSnapshot,
    WorkflowRunState,
)
from codexia_manual_agent.workflow_core.projection import project_workflow_run


class WorkflowAdmissionError(RuntimeError):
    """Base failure for the G2.2 workflow admission boundary."""


class WorkflowBindingError(WorkflowAdmissionError):
    """Workflow record no longer binds the exact Work/WorkflowRun identity."""


class WorkflowRunStateError(WorkflowAdmissionError):
    """Requested progression is invalid for the recovered WorkflowRun state."""


class WorkflowAdmission:
    """Admit prepared workflow records into canonical Work chronology.

    Workflow logic may prepare WorkflowRun/WorkflowCandidate values. Only this
    boundary, through WorkStore.append(), can make their WorkEvents canonical.
    """

    def __init__(self, store: WorkStore) -> None:
        self._store = store

    def admit_start(self, run: WorkflowRun) -> WorkflowRunSnapshot:
        if not isinstance(run, WorkflowRun):
            raise TypeError("run must be WorkflowRun")

        current = self._store.snapshot(run.work_id)
        if not hmac.compare_digest(current.work.work_digest, run.work_digest):
            raise WorkflowBindingError("WorkflowRun work binding changed")

        # WorkStore intentionally checks exact event identity before stale CAS.
        # Therefore retrying this exact start after an ambiguous caller ACK is
        # idempotent even if later Work events already exist.
        self._store.append(
            run.work_id,
            expected_revision=run.start_revision,
            event=run.to_start_event(),
        )
        return project_workflow_run(
            self._store.events(run.work_id),
            run.workflow_run_id,
        )

    def admit_candidate(
        self,
        candidate: WorkflowCandidate,
    ) -> WorkflowRunSnapshot:
        if not isinstance(candidate, WorkflowCandidate):
            raise TypeError("candidate must be WorkflowCandidate")

        work_id = candidate.event.work_id
        events = self._store.events(work_id)

        # Preserve exact retry semantics after an ambiguous caller ACK. If the
        # event identity is already present, WorkStore decides exact reuse vs
        # identity conflict before any stale-revision check.
        if any(event.event_id == candidate.event.event_id for event in events):
            self._store.append(
                work_id,
                expected_revision=candidate.expected_revision,
                event=candidate.event,
            )
            return project_workflow_run(
                self._store.events(work_id),
                candidate.workflow_run_id,
            )

        current = self._store.snapshot(work_id)
        if current.revision != candidate.expected_revision:
            raise WorkConcurrencyError(
                f"Stale Work revision: expected={candidate.expected_revision} "
                f"actual={current.revision}"
            )
        if current.last_event_digest != candidate.expected_event_digest:
            raise WorkConcurrencyError(
                "WorkflowCandidate does not bind exact current Work chronology"
            )

        run_snapshot = project_workflow_run(events, candidate.workflow_run_id)
        run = run_snapshot.run
        if run_snapshot.state is not WorkflowRunState.ACTIVE:
            raise WorkflowRunStateError(
                f"WorkflowRun is {run_snapshot.state.value}; no progression may follow"
            )
        if candidate.event.work_id != run.work_id:
            raise WorkflowBindingError("WorkflowCandidate belongs to another Work")
        if not hmac.compare_digest(
            candidate.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowBindingError("WorkflowCandidate changed WorkflowRun binding")
        if not hmac.compare_digest(current.work.work_digest, run.work_digest):
            raise WorkflowBindingError("WorkflowRun work binding changed")

        self._store.append(
            work_id,
            expected_revision=candidate.expected_revision,
            event=candidate.event,
        )
        return project_workflow_run(
            self._store.events(work_id),
            candidate.workflow_run_id,
        )
