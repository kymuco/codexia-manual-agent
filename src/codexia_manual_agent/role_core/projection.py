from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

from codexia_manual_agent.work_core import WorkEvent
from codexia_manual_agent.role_core.models import (
    ROLE_COMPLETED_EVENT,
    ROLE_FAILED_EVENT,
    ROLE_OUTCOME_UNKNOWN_EVENT,
    ROLE_STARTED_EVENT,
    CognitionOutcome,
    CognitionOutcomeStatus,
    InvalidRoleRecord,
    RoleRun,
    RoleRunSnapshot,
    RoleRunState,
)


class RoleProjectionError(RuntimeError):
    """Durable Work chronology violates G2.3 role semantics."""


@dataclass(slots=True)
class _RoleState:
    run: RoleRun
    state: RoleRunState = RoleRunState.ACTIVE
    terminal_event_id: str | None = None
    output_text: str | None = None
    error: str | None = None


_ROLE_EVENT_KINDS = {
    ROLE_STARTED_EVENT,
    ROLE_COMPLETED_EVENT,
    ROLE_FAILED_EVENT,
    ROLE_OUTCOME_UNKNOWN_EVENT,
}


def _unwrap_role_event(event: WorkEvent) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = event.to_dict()["payload"]
    if not isinstance(raw, dict) or set(raw) != {"_workflow", "payload"}:
        raise RoleProjectionError("Role event workflow wrapper is not exact")
    provenance = raw["_workflow"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "workflow_run_id",
        "workflow_run_digest",
    }:
        raise RoleProjectionError("Role event workflow provenance is not exact")
    if not all(isinstance(provenance[key], str) for key in provenance):
        raise RoleProjectionError("Role event workflow provenance types are invalid")
    body = raw["payload"]
    if not isinstance(body, dict):
        raise RoleProjectionError("Role event body must be an object")
    return provenance, body


def project_role_runs(events: tuple[WorkEvent, ...]) -> tuple[RoleRunSnapshot, ...]:
    """Project RoleRun lifecycle from already-admitted Work chronology only."""

    states: dict[str, _RoleState] = {}
    order: list[str] = []

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind not in _ROLE_EVENT_KINDS:
            continue

        workflow_provenance, body = _unwrap_role_event(event)

        if event.kind == ROLE_STARTED_EVENT:
            if set(body) != {"role_run"} or not isinstance(body["role_run"], dict):
                raise RoleProjectionError("role.started body is not exact")
            try:
                run = RoleRun.from_dict(body["role_run"])
            except (InvalidRoleRecord, KeyError, TypeError, ValueError) as exc:
                raise RoleProjectionError("role.started contains invalid RoleRun") from exc

            if run.role_run_id in states:
                raise RoleProjectionError("RoleRun identity was durably started twice")
            if event.event_id != run.role_run_id:
                raise RoleProjectionError(
                    "role.started event identity differs from RoleRun"
                )
            if event.created_at != run.created_at:
                raise RoleProjectionError(
                    "role.started timestamp differs from RoleRun"
                )
            if event.work_id != run.work_id:
                raise RoleProjectionError("role.started crosses Work identity")
            if event.sequence != run.start_revision + 1:
                raise RoleProjectionError(
                    "role.started sequence does not bind start revision"
                )
            if event.previous_event_digest != run.start_event_digest:
                raise RoleProjectionError(
                    "role.started does not bind exact prior Work event"
                )
            if workflow_provenance["workflow_run_id"] != run.workflow_run_id:
                raise RoleProjectionError("role.started changed WorkflowRun identity")
            if not hmac.compare_digest(
                workflow_provenance["workflow_run_digest"],
                run.workflow_run_digest,
            ):
                raise RoleProjectionError("role.started changed WorkflowRun binding")

            states[run.role_run_id] = _RoleState(run=run)
            order.append(run.role_run_id)
            continue

        if set(body) != {"cognition_outcome"} or not isinstance(
            body["cognition_outcome"],
            dict,
        ):
            raise RoleProjectionError("Role terminal event body is not exact")
        try:
            outcome = CognitionOutcome.from_dict(body["cognition_outcome"])
        except (InvalidRoleRecord, KeyError, TypeError, ValueError) as exc:
            raise RoleProjectionError(
                "Role terminal event contains invalid CognitionOutcome"
            ) from exc

        state = states.get(outcome.role_run_id)
        if state is None:
            raise RoleProjectionError(
                "Role terminal event references unknown RoleRun"
            )
        run = state.run
        if state.state is not RoleRunState.ACTIVE:
            raise RoleProjectionError("Role terminal event follows terminal RoleRun")
        if event.event_id != outcome.outcome_id:
            raise RoleProjectionError(
                "Role terminal event identity differs from CognitionOutcome"
            )
        if event.created_at != outcome.created_at:
            raise RoleProjectionError(
                "Role terminal timestamp differs from CognitionOutcome"
            )
        if event.work_id != run.work_id or outcome.work_id != run.work_id:
            raise RoleProjectionError("Role terminal event crosses Work identity")
        if not hmac.compare_digest(outcome.work_digest, run.work_digest):
            raise RoleProjectionError("CognitionOutcome changed Work binding")
        if outcome.workflow_run_id != run.workflow_run_id:
            raise RoleProjectionError("CognitionOutcome changed WorkflowRun identity")
        if not hmac.compare_digest(
            outcome.workflow_run_digest,
            run.workflow_run_digest,
        ):
            raise RoleProjectionError("CognitionOutcome changed WorkflowRun binding")
        if outcome.role_run_id != run.role_run_id:
            raise RoleProjectionError("CognitionOutcome changed RoleRun identity")
        if not hmac.compare_digest(outcome.role_run_digest, run.run_digest):
            raise RoleProjectionError("CognitionOutcome changed RoleRun binding")
        if workflow_provenance["workflow_run_id"] != run.workflow_run_id:
            raise RoleProjectionError(
                "Role terminal wrapper changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            workflow_provenance["workflow_run_digest"],
            run.workflow_run_digest,
        ):
            raise RoleProjectionError(
                "Role terminal wrapper changed WorkflowRun binding"
            )

        expected_kind = {
            CognitionOutcomeStatus.SUCCEEDED: ROLE_COMPLETED_EVENT,
            CognitionOutcomeStatus.FAILED: ROLE_FAILED_EVENT,
            CognitionOutcomeStatus.UNKNOWN: ROLE_OUTCOME_UNKNOWN_EVENT,
        }[outcome.status]
        if event.kind != expected_kind:
            raise RoleProjectionError(
                "Role terminal event kind does not match CognitionOutcome status"
            )

        if outcome.status is CognitionOutcomeStatus.SUCCEEDED:
            state.state = RoleRunState.COMPLETED
            state.output_text = outcome.output_text
        elif outcome.status is CognitionOutcomeStatus.FAILED:
            state.state = RoleRunState.FAILED
            state.error = outcome.error
        else:
            state.state = RoleRunState.OUTCOME_UNKNOWN
            state.error = outcome.error
        state.terminal_event_id = event.event_id

    return tuple(
        RoleRunSnapshot(
            run=states[role_run_id].run,
            state=states[role_run_id].state,
            terminal_event_id=states[role_run_id].terminal_event_id,
            output_text=states[role_run_id].output_text,
            error=states[role_run_id].error,
        )
        for role_run_id in order
    )


def project_role_run(
    events: tuple[WorkEvent, ...],
    role_run_id: str,
) -> RoleRunSnapshot:
    for snapshot in project_role_runs(events):
        if snapshot.run.role_run_id == role_run_id:
            return snapshot
    raise RoleProjectionError(f"Unknown RoleRun: {role_run_id}")
