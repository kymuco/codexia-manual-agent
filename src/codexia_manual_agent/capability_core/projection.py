from __future__ import annotations

import hmac
from dataclasses import dataclass

from codexia_manual_agent.capability_core.models import (
    CAPABILITY_NEED_DECLARED_EVENT,
    CAPABILITY_OUTCOME_RECORDED_EVENT,
    CapabilityNeed,
    CapabilityNeedSnapshot,
    CapabilityNeedState,
    CapabilityOutcome,
    CapabilityOutcomeStatus,
    InvalidCapabilityRecord,
)
from codexia_manual_agent.pack_core.models import (
    PackMemberBinding,
    PackMemberKind,
)
from codexia_manual_agent.pack_core.projection import (
    project_workflow_pack_binding,
)
from codexia_manual_agent.work_core import WorkEvent


class CapabilityProjectionError(RuntimeError):
    """Durable Work chronology violates G2.4 capability semantics."""


@dataclass(slots=True)
class _CapabilityState:
    need: CapabilityNeed
    state: CapabilityNeedState = CapabilityNeedState.PENDING
    outcome: CapabilityOutcome | None = None


def _workflow_wrapped_need(event: WorkEvent) -> CapabilityNeed:
    raw = event.to_dict()["payload"]
    if not isinstance(raw, dict) or set(raw) != {"_workflow", "payload"}:
        raise CapabilityProjectionError(
            "capability.need-declared workflow wrapper is not exact"
        )
    provenance = raw["_workflow"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "workflow_run_id",
        "workflow_run_digest",
    }:
        raise CapabilityProjectionError(
            "CapabilityNeed workflow provenance is not exact"
        )
    if not all(isinstance(provenance[key], str) for key in provenance):
        raise CapabilityProjectionError(
            "CapabilityNeed workflow provenance types are invalid"
        )
    body = raw["payload"]
    if (
        not isinstance(body, dict)
        or set(body) != {"capability_need"}
        or not isinstance(body["capability_need"], dict)
    ):
        raise CapabilityProjectionError("CapabilityNeed body is not exact")
    try:
        need = CapabilityNeed.from_dict(body["capability_need"])
    except (InvalidCapabilityRecord, KeyError, TypeError, ValueError) as exc:
        raise CapabilityProjectionError(
            "capability.need-declared contains invalid CapabilityNeed"
        ) from exc

    if provenance["workflow_run_id"] != need.workflow_run_id:
        raise CapabilityProjectionError(
            "CapabilityNeed wrapper changed WorkflowRun identity"
        )
    if not hmac.compare_digest(
        provenance["workflow_run_digest"],
        need.workflow_run_digest,
    ):
        raise CapabilityProjectionError(
            "CapabilityNeed wrapper changed WorkflowRun binding"
        )
    return need


def _outcome_record(event: WorkEvent) -> CapabilityOutcome:
    raw = event.to_dict()["payload"]
    if (
        not isinstance(raw, dict)
        or set(raw) != {"capability_outcome"}
        or not isinstance(raw["capability_outcome"], dict)
    ):
        raise CapabilityProjectionError("CapabilityOutcome body is not exact")
    try:
        return CapabilityOutcome.from_dict(raw["capability_outcome"])
    except (InvalidCapabilityRecord, KeyError, TypeError, ValueError) as exc:
        raise CapabilityProjectionError(
            "capability.outcome-recorded contains invalid CapabilityOutcome"
        ) from exc


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
        raise CapabilityProjectionError(
            "CapabilityBinding is not a member of the WorkflowRun Pack"
        )

def project_capability_needs(
    events: tuple[WorkEvent, ...],
) -> tuple[CapabilityNeedSnapshot, ...]:
    """Project exact Need/Outcome lifecycle from admitted Work chronology."""

    states: dict[str, _CapabilityState] = {}
    order: list[str] = []

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")

        if event.kind == CAPABILITY_NEED_DECLARED_EVENT:
            need = _workflow_wrapped_need(event)
            if need.need_id in states:
                raise CapabilityProjectionError(
                    "CapabilityNeed identity was durably declared twice"
                )
            if event.event_id != need.need_id:
                raise CapabilityProjectionError(
                    "CapabilityNeed event identity differs from need"
                )
            if event.created_at != need.created_at:
                raise CapabilityProjectionError(
                    "CapabilityNeed event timestamp differs from need"
                )
            if event.work_id != need.work_id:
                raise CapabilityProjectionError(
                    "CapabilityNeed event crosses Work identity"
                )
            if event.sequence != need.start_revision + 1:
                raise CapabilityProjectionError(
                    "CapabilityNeed sequence does not bind start revision"
                )
            if event.previous_event_digest != need.start_event_digest:
                raise CapabilityProjectionError(
                    "CapabilityNeed does not bind exact prior Work event"
                )

            _require_pack_capability_membership(events, need)
            states[need.need_id] = _CapabilityState(need=need)
            order.append(need.need_id)
            continue

        if event.kind != CAPABILITY_OUTCOME_RECORDED_EVENT:
            continue

        outcome = _outcome_record(event)
        state = states.get(outcome.need_id)
        if state is None:
            raise CapabilityProjectionError(
                "CapabilityOutcome references unknown CapabilityNeed"
            )
        if state.state is not CapabilityNeedState.PENDING:
            raise CapabilityProjectionError(
                "CapabilityNeed received more than one outcome"
            )

        need = state.need
        if event.event_id != outcome.outcome_id:
            raise CapabilityProjectionError(
                "CapabilityOutcome event identity differs from outcome"
            )
        if event.created_at != outcome.created_at:
            raise CapabilityProjectionError(
                "CapabilityOutcome event timestamp differs from outcome"
            )
        if event.work_id != need.work_id or outcome.work_id != need.work_id:
            raise CapabilityProjectionError(
                "CapabilityOutcome crosses Work identity"
            )
        if not hmac.compare_digest(outcome.work_digest, need.work_digest):
            raise CapabilityProjectionError(
                "CapabilityOutcome changed Work binding"
            )
        if outcome.workflow_run_id != need.workflow_run_id:
            raise CapabilityProjectionError(
                "CapabilityOutcome changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            outcome.workflow_run_digest,
            need.workflow_run_digest,
        ):
            raise CapabilityProjectionError(
                "CapabilityOutcome changed WorkflowRun binding"
            )
        if outcome.need_id != need.need_id:
            raise CapabilityProjectionError(
                "CapabilityOutcome changed CapabilityNeed identity"
            )
        if not hmac.compare_digest(outcome.need_digest, need.need_digest):
            raise CapabilityProjectionError(
                "CapabilityOutcome changed CapabilityNeed binding"
            )

        state.outcome = outcome
        state.state = {
            CapabilityOutcomeStatus.SUCCEEDED: CapabilityNeedState.SUCCEEDED,
            CapabilityOutcomeStatus.FAILED: CapabilityNeedState.FAILED,
            CapabilityOutcomeStatus.UNKNOWN: CapabilityNeedState.OUTCOME_UNKNOWN,
        }[outcome.status]

    return tuple(
        CapabilityNeedSnapshot(
            need=states[need_id].need,
            state=states[need_id].state,
            outcome=states[need_id].outcome,
        )
        for need_id in order
    )


def project_capability_need(
    events: tuple[WorkEvent, ...],
    need_id: str,
) -> CapabilityNeedSnapshot:
    for snapshot in project_capability_needs(events):
        if snapshot.need.need_id == need_id:
            return snapshot
    raise CapabilityProjectionError(f"Unknown CapabilityNeed: {need_id}")
