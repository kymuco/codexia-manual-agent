from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

from codexia_manual_agent.work_core import (
    WORK_CANCELLED_EVENT,
    WORK_COMPLETED_EVENT,
    WorkEvent,
)
from codexia_manual_agent.workflow_core.models import (
    WORKFLOW_CANCELLED_EVENT,
    WORKFLOW_COMPLETED_EVENT,
    WORKFLOW_STARTED_EVENT,
    InvalidWorkflowRecord,
    WorkflowRun,
    WorkflowRunSnapshot,
    WorkflowRunState,
)


class WorkflowProjectionError(RuntimeError):
    """Durable Work chronology violates the G2.2 workflow semantics."""


@dataclass(slots=True)
class _WorkflowState:
    run: WorkflowRun
    state: WorkflowRunState = WorkflowRunState.ACTIVE
    terminal_event_id: str | None = None


def _event_payload(event: WorkEvent) -> dict[str, Any]:
    payload = event.to_dict()["payload"]
    if not isinstance(payload, dict):
        raise WorkflowProjectionError("Workflow event payload must be an object")
    return payload


def _candidate_provenance(event: WorkEvent) -> tuple[str, str]:
    payload = _event_payload(event)
    if set(payload) != {"_workflow", "payload"}:
        raise WorkflowProjectionError(
            "Workflow-produced event payload wrapper is not exact"
        )
    provenance = payload["_workflow"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "workflow_run_id",
        "workflow_run_digest",
    }:
        raise WorkflowProjectionError("Workflow event provenance is not exact")
    run_id = provenance["workflow_run_id"]
    run_digest = provenance["workflow_run_digest"]
    if not isinstance(run_id, str) or not isinstance(run_digest, str):
        raise WorkflowProjectionError("Workflow event provenance types are invalid")
    return run_id, run_digest


def project_workflow_runs(
    events: tuple[WorkEvent, ...],
) -> tuple[WorkflowRunSnapshot, ...]:
    """Project admitted WorkflowRun lifecycle from one Work chronology.

    This function performs no scheduling, execution, model call, retry, or mutation.
    It only interprets already-admitted WorkEvents.
    """

    states: dict[str, _WorkflowState] = {}
    order: list[str] = []
    chronology_work_id: str | None = None

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if chronology_work_id is None:
            chronology_work_id = event.work_id
        elif event.work_id != chronology_work_id:
            raise WorkflowProjectionError(
                "Workflow projection crossed Work chronology identity"
            )

        if event.kind == WORKFLOW_STARTED_EVENT:
            payload = _event_payload(event)
            if set(payload) != {"workflow_run"}:
                raise WorkflowProjectionError(
                    "workflow.started payload wrapper is not exact"
                )
            raw_run = payload["workflow_run"]
            if not isinstance(raw_run, dict):
                raise WorkflowProjectionError(
                    "workflow.started must contain WorkflowRun object"
                )
            try:
                run = WorkflowRun.from_dict(raw_run)
            except (InvalidWorkflowRecord, KeyError, TypeError, ValueError) as exc:
                raise WorkflowProjectionError(
                    "workflow.started contains invalid WorkflowRun"
                ) from exc

            if run.workflow_run_id in states:
                raise WorkflowProjectionError(
                    "WorkflowRun identity was durably started twice"
                )
            if event.event_id != run.workflow_run_id:
                raise WorkflowProjectionError(
                    "workflow.started event identity differs from WorkflowRun"
                )
            if event.created_at != run.created_at:
                raise WorkflowProjectionError(
                    "workflow.started timestamp differs from WorkflowRun"
                )
            if event.work_id != run.work_id:
                raise WorkflowProjectionError(
                    "workflow.started crosses Work identity"
                )
            if event.sequence != run.start_revision + 1:
                raise WorkflowProjectionError(
                    "workflow.started sequence does not bind start revision"
                )
            if event.previous_event_digest != run.start_event_digest:
                raise WorkflowProjectionError(
                    "workflow.started does not bind exact prior Work event"
                )

            states[run.workflow_run_id] = _WorkflowState(run=run)
            order.append(run.workflow_run_id)
            continue

        raw_payload = event.to_dict()["payload"]
        is_terminal_workflow_event = event.kind in {
            WORKFLOW_COMPLETED_EVENT,
            WORKFLOW_CANCELLED_EVENT,
        }
        if not isinstance(raw_payload, dict):
            if is_terminal_workflow_event:
                raise WorkflowProjectionError(
                    "Workflow terminal event payload must be an object"
                )
            continue

        has_workflow_provenance = "_workflow" in raw_payload
        if not has_workflow_provenance and not is_terminal_workflow_event:
            continue
        if has_workflow_provenance and event.kind in {
            WORK_COMPLETED_EVENT,
            WORK_CANCELLED_EVENT,
        }:
            raise WorkflowProjectionError(
                "Workflow-produced event cannot directly terminate Work"
            )

        run_id, run_digest = _candidate_provenance(event)
        state = states.get(run_id)
        if state is None:
            raise WorkflowProjectionError(
                "Workflow-produced event references unknown WorkflowRun"
            )
        if event.work_id != state.run.work_id:
            raise WorkflowProjectionError(
                "Workflow-produced event crosses Work identity"
            )
        if not hmac.compare_digest(run_digest, state.run.run_digest):
            raise WorkflowProjectionError(
                "Workflow-produced event changed WorkflowRun binding"
            )
        if state.state is not WorkflowRunState.ACTIVE:
            raise WorkflowProjectionError(
                "Workflow-produced event follows terminal WorkflowRun state"
            )

        if event.kind == WORKFLOW_COMPLETED_EVENT:
            state.state = WorkflowRunState.COMPLETED
            state.terminal_event_id = event.event_id
        elif event.kind == WORKFLOW_CANCELLED_EVENT:
            state.state = WorkflowRunState.CANCELLED
            state.terminal_event_id = event.event_id

    return tuple(
        WorkflowRunSnapshot(
            run=states[run_id].run,
            state=states[run_id].state,
            terminal_event_id=states[run_id].terminal_event_id,
        )
        for run_id in order
    )


def project_workflow_run(
    events: tuple[WorkEvent, ...],
    workflow_run_id: str,
) -> WorkflowRunSnapshot:
    for snapshot in project_workflow_runs(events):
        if snapshot.run.workflow_run_id == workflow_run_id:
            return snapshot
    raise WorkflowProjectionError(f"Unknown WorkflowRun: {workflow_run_id}")
