from __future__ import annotations

import hmac

from codexia_manual_agent.role_core.models import RoleRunState
from codexia_manual_agent.role_core.projection import (
    RoleProjectionError,
    project_role_run,
)
from codexia_manual_agent.role_core.transport_models import (
    COGNITION_HANDOFF_ADMITTED_EVENT,
    CognitionHandoff,
    InvalidCognitionTransportRecord,
)
from codexia_manual_agent.work_core import WorkEvent
from codexia_manual_agent.workflow_core import (
    WorkflowRunState,
    project_workflow_run,
)


class CognitionHandoffProjectionError(RuntimeError):
    """Durable Work chronology violates G2.13 cognition routing semantics."""


def project_cognition_handoffs(
    events: tuple[WorkEvent, ...],
) -> tuple[CognitionHandoff, ...]:
    handoffs: list[CognitionHandoff] = []
    request_ids: set[str] = set()

    for index, event in enumerate(events):
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind != COGNITION_HANDOFF_ADMITTED_EVENT:
            continue

        raw = event.to_dict()["payload"]
        if (
            not isinstance(raw, dict)
            or set(raw) != {"cognition_handoff"}
            or not isinstance(raw["cognition_handoff"], dict)
        ):
            raise CognitionHandoffProjectionError(
                "CognitionHandoff event body is not exact"
            )
        try:
            handoff = CognitionHandoff.from_dict(raw["cognition_handoff"])
        except (
            InvalidCognitionTransportRecord,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff event contains invalid record"
            ) from exc

        if event.event_id != handoff.handoff_id:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff event identity changed"
            )
        if event.created_at != handoff.created_at:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff event timestamp changed"
            )
        if event.work_id != handoff.work_id:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff event crossed Work identity"
            )
        if event.sequence != handoff.start_revision + 1:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff sequence changed"
            )
        if event.previous_event_digest != handoff.start_event_digest:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff prior event binding changed"
            )
        if handoff.request_id in request_ids:
            raise CognitionHandoffProjectionError(
                "CognitionRequest received more than one durable handoff"
            )

        prefix = events[:index]
        try:
            role = project_role_run(prefix, handoff.role_run_id)
        except RoleProjectionError as exc:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff references unknown RoleRun"
            ) from exc
        if role.state is not RoleRunState.REQUESTED:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff requires durable REQUESTED RoleRun"
            )
        if role.request_id != handoff.request_id:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff changed request identity"
            )
        if role.request_digest is None or not hmac.compare_digest(
            role.request_digest,
            handoff.request_digest,
        ):
            raise CognitionHandoffProjectionError(
                "CognitionHandoff changed request binding"
            )
        run = role.run
        if handoff.role_run_id != run.role_run_id:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff changed RoleRun identity"
            )
        if not hmac.compare_digest(
            handoff.role_run_digest,
            run.run_digest,
        ):
            raise CognitionHandoffProjectionError(
                "CognitionHandoff changed RoleRun binding"
            )
        if handoff.work_id != run.work_id:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff changed Work identity"
            )
        if not hmac.compare_digest(
            handoff.work_digest,
            run.work_digest,
        ):
            raise CognitionHandoffProjectionError(
                "CognitionHandoff changed Work binding"
            )
        if handoff.workflow_run_id != run.workflow_run_id:
            raise CognitionHandoffProjectionError(
                "CognitionHandoff changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            handoff.workflow_run_digest,
            run.workflow_run_digest,
        ):
            raise CognitionHandoffProjectionError(
                "CognitionHandoff changed WorkflowRun binding"
            )

        workflow = project_workflow_run(prefix, run.workflow_run_id)
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise CognitionHandoffProjectionError(
                "New CognitionHandoff requires active WorkflowRun"
            )

        request_ids.add(handoff.request_id)
        handoffs.append(handoff)

    return tuple(handoffs)


def project_cognition_handoff(
    events: tuple[WorkEvent, ...],
    request_id: str,
) -> CognitionHandoff:
    for handoff in project_cognition_handoffs(events):
        if handoff.request_id == request_id:
            return handoff
    raise CognitionHandoffProjectionError(
        f"Unknown CognitionHandoff for request: {request_id}"
    )
