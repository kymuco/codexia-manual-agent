from __future__ import annotations

import hmac

from codexia_manual_agent.delegation_core.models import (
    DELEGATION_CHILD_OWNED_EVENT,
    Delegation,
    InvalidDelegationRecord,
)
from codexia_manual_agent.work_core import WorkEvent


class DelegationProjectionError(RuntimeError):
    """Durable Work chronology violates Gen2 delegation semantics."""


def project_delegations(events: tuple[WorkEvent, ...]) -> tuple[Delegation, ...]:
    items: list[Delegation] = []
    seen_ids: set[str] = set()
    seen_children: set[str] = set()

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind != DELEGATION_CHILD_OWNED_EVENT:
            continue
        payload = event.to_dict()["payload"]
        if (
            not isinstance(payload, dict)
            or set(payload) != {"delegation"}
            or not isinstance(payload["delegation"], dict)
        ):
            raise DelegationProjectionError(
                "delegation.child-owned payload is not exact"
            )
        try:
            delegation = Delegation.from_dict(payload["delegation"])
        except (InvalidDelegationRecord, KeyError, TypeError, ValueError) as exc:
            raise DelegationProjectionError(
                "delegation.child-owned contains invalid Delegation"
            ) from exc
        if delegation.delegation_id in seen_ids:
            raise DelegationProjectionError(
                "Delegation identity was durably declared twice"
            )
        if delegation.child_work.work_id in seen_children:
            raise DelegationProjectionError(
                "Child Work is owned twice in one parent chronology"
            )
        if event.event_id != delegation.delegation_id:
            raise DelegationProjectionError(
                "Delegation event identity differs from delegation"
            )
        if event.created_at != delegation.created_at:
            raise DelegationProjectionError(
                "Delegation event timestamp differs from delegation"
            )
        if event.work_id != delegation.parent_work_id:
            raise DelegationProjectionError(
                "Delegation event crosses parent Work identity"
            )
        if event.sequence != delegation.start_revision + 1:
            raise DelegationProjectionError(
                "Delegation sequence does not bind parent revision"
            )
        if event.previous_event_digest != delegation.start_event_digest:
            raise DelegationProjectionError(
                "Delegation does not bind exact prior parent event"
            )
        seen_ids.add(delegation.delegation_id)
        seen_children.add(delegation.child_work.work_id)
        items.append(delegation)

    return tuple(items)


def project_delegation(
    events: tuple[WorkEvent, ...],
    delegation_id: str,
) -> Delegation:
    for delegation in project_delegations(events):
        if delegation.delegation_id == delegation_id:
            return delegation
    raise DelegationProjectionError(f"Unknown Delegation: {delegation_id}")
