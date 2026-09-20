from __future__ import annotations

import hmac

from codexia_manual_agent.pack_core.models import (
    PACK_WORKFLOW_BOUND_EVENT,
    InvalidPackRecord,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import WorkEvent
from codexia_manual_agent.workflow_core.models import (
    WORKFLOW_STARTED_EVENT,
    InvalidWorkflowRecord,
    WorkflowRun,
)


class PackProjectionError(RuntimeError):
    """Durable Work chronology violates G2.7 Pack semantics."""


def _binding_record(event: WorkEvent) -> PackWorkflowBinding:
    raw = event.to_dict()["payload"]
    if (
        not isinstance(raw, dict)
        or set(raw) != {"pack_workflow_binding"}
        or not isinstance(raw["pack_workflow_binding"], dict)
    ):
        raise PackProjectionError(
            "pack.workflow-bound payload is not exact"
        )
    try:
        return PackWorkflowBinding.from_dict(
            raw["pack_workflow_binding"]
        )
    except (InvalidPackRecord, KeyError, TypeError, ValueError) as exc:
        raise PackProjectionError(
            "pack.workflow-bound contains invalid PackWorkflowBinding"
        ) from exc


def _workflow_start(event: WorkEvent) -> WorkflowRun:
    if event.kind != WORKFLOW_STARTED_EVENT:
        raise PackProjectionError(
            "PackWorkflowBinding must immediately follow workflow.started"
        )
    raw = event.to_dict()["payload"]
    if (
        not isinstance(raw, dict)
        or set(raw) != {"workflow_run"}
        or not isinstance(raw["workflow_run"], dict)
    ):
        raise PackProjectionError(
            "workflow.started payload is invalid for Pack recovery"
        )
    try:
        return WorkflowRun.from_dict(raw["workflow_run"])
    except (InvalidWorkflowRecord, KeyError, TypeError, ValueError) as exc:
        raise PackProjectionError(
            "workflow.started contains invalid WorkflowRun"
        ) from exc


def project_pack_workflow_bindings(
    events: tuple[WorkEvent, ...],
) -> tuple[PackWorkflowBinding, ...]:
    """Project exact Pack pins without loading code or selecting plugin versions."""

    by_sequence = {event.sequence: event for event in events}
    by_workflow: dict[str, PackWorkflowBinding] = {}
    order: list[str] = []

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind != PACK_WORKFLOW_BOUND_EVENT:
            continue

        pin = _binding_record(event)
        if pin.workflow_run_id in by_workflow:
            raise PackProjectionError(
                "WorkflowRun received more than one Pack binding"
            )
        if event.event_id != pin.binding_id:
            raise PackProjectionError(
                "PackWorkflowBinding event identity differs from binding"
            )
        if event.created_at != pin.created_at:
            raise PackProjectionError(
                "PackWorkflowBinding event timestamp differs from binding"
            )
        if event.work_id != pin.work_id:
            raise PackProjectionError(
                "PackWorkflowBinding event crosses Work identity"
            )
        if event.sequence != pin.start_revision + 1:
            raise PackProjectionError(
                "PackWorkflowBinding sequence does not bind exact revision"
            )
        if event.previous_event_digest != pin.start_event_digest:
            raise PackProjectionError(
                "PackWorkflowBinding changed prior Work event"
            )

        previous = by_sequence.get(pin.start_revision)
        if previous is None:
            raise PackProjectionError(
                "PackWorkflowBinding prior event is missing"
            )
        if previous.event_digest != pin.start_event_digest:
            raise PackProjectionError(
                "PackWorkflowBinding prior event digest is not exact"
            )

        run = _workflow_start(previous)
        if previous.event_id != run.workflow_run_id:
            raise PackProjectionError(
                "workflow.started identity differs from WorkflowRun"
            )
        if run.workflow_run_id != pin.workflow_run_id:
            raise PackProjectionError(
                "PackWorkflowBinding changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            run.run_digest,
            pin.workflow_run_digest,
        ):
            raise PackProjectionError(
                "PackWorkflowBinding changed WorkflowRun binding"
            )
        if run.work_id != pin.work_id:
            raise PackProjectionError(
                "PackWorkflowBinding changed Work identity"
            )
        if not hmac.compare_digest(run.work_digest, pin.work_digest):
            raise PackProjectionError(
                "PackWorkflowBinding changed Work binding"
            )

        workflow_member = PackMemberBinding.create(
            kind=PackMemberKind.WORKFLOW,
            semantic_id=run.binding.workflow_id,
            version=run.binding.version,
            binding_digest=run.binding.binding_digest,
        )
        if not pin.pack.contains(workflow_member):
            raise PackProjectionError(
                "Pack does not contain exact WorkflowBinding"
            )

        by_workflow[pin.workflow_run_id] = pin
        order.append(pin.workflow_run_id)

    return tuple(by_workflow[workflow_run_id] for workflow_run_id in order)


def project_workflow_pack_binding(
    events: tuple[WorkEvent, ...],
    workflow_run_id: str,
) -> PackWorkflowBinding | None:
    for pin in project_pack_workflow_bindings(events):
        if pin.workflow_run_id == workflow_run_id:
            return pin
    return None
