from __future__ import annotations

import hmac

from codexia_manual_agent.capability_core.host_models import (
    CAPABILITY_HANDOFF_ADMITTED_EVENT,
    CapabilityHandoff,
    InvalidCapabilityHostRecord,
)
from codexia_manual_agent.capability_core.projection import project_capability_needs
from codexia_manual_agent.work_core import WorkEvent


class CapabilityHandoffProjectionError(RuntimeError):
    """Durable Work chronology violates G2.5 host-handoff semantics."""


def _handoff_record(event: WorkEvent) -> CapabilityHandoff:
    raw = event.to_dict()["payload"]
    if (
        not isinstance(raw, dict)
        or set(raw) != {"capability_handoff"}
        or not isinstance(raw["capability_handoff"], dict)
    ):
        raise CapabilityHandoffProjectionError(
            "capability.handoff-admitted body is not exact"
        )
    try:
        return CapabilityHandoff.from_dict(raw["capability_handoff"])
    except (InvalidCapabilityHostRecord, KeyError, TypeError, ValueError) as exc:
        raise CapabilityHandoffProjectionError(
            "capability.handoff-admitted contains invalid CapabilityHandoff"
        ) from exc


def project_capability_handoffs(
    events: tuple[WorkEvent, ...],
) -> tuple[CapabilityHandoff, ...]:
    """Project durable host routing facts without implying delivery or attempt."""

    needs = {
        snapshot.need.need_id: snapshot.need
        for snapshot in project_capability_needs(events)
    }
    by_need: dict[str, CapabilityHandoff] = {}
    order: list[str] = []

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind != CAPABILITY_HANDOFF_ADMITTED_EVENT:
            continue

        handoff = _handoff_record(event)
        need = needs.get(handoff.need_id)
        if need is None:
            raise CapabilityHandoffProjectionError(
                "CapabilityHandoff references unknown CapabilityNeed"
            )
        if handoff.need_id in by_need:
            raise CapabilityHandoffProjectionError(
                "CapabilityNeed received more than one host handoff"
            )
        if event.event_id != handoff.handoff_id:
            raise CapabilityHandoffProjectionError(
                "CapabilityHandoff event identity differs from handoff"
            )
        if event.created_at != handoff.created_at:
            raise CapabilityHandoffProjectionError(
                "CapabilityHandoff event timestamp differs from handoff"
            )
        if event.work_id != handoff.work_id or handoff.work_id != need.work_id:
            raise CapabilityHandoffProjectionError(
                "CapabilityHandoff crosses Work identity"
            )
        if not hmac.compare_digest(handoff.work_digest, need.work_digest):
            raise CapabilityHandoffProjectionError(
                "CapabilityHandoff changed Work binding"
            )
        if not hmac.compare_digest(handoff.need_digest, need.need_digest):
            raise CapabilityHandoffProjectionError(
                "CapabilityHandoff changed Need binding"
            )
        if event.sequence != handoff.start_revision + 1:
            raise CapabilityHandoffProjectionError(
                "CapabilityHandoff sequence does not bind start revision"
            )
        if event.previous_event_digest != handoff.start_event_digest:
            raise CapabilityHandoffProjectionError(
                "CapabilityHandoff does not bind exact prior Work event"
            )

        by_need[handoff.need_id] = handoff
        order.append(handoff.need_id)

    return tuple(by_need[need_id] for need_id in order)


def project_capability_handoff(
    events: tuple[WorkEvent, ...],
    need_id: str,
) -> CapabilityHandoff:
    for handoff in project_capability_handoffs(events):
        if handoff.need_id == need_id:
            return handoff
    raise CapabilityHandoffProjectionError(
        f"No CapabilityHandoff for Need: {need_id}"
    )
