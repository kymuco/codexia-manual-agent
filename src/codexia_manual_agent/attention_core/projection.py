from __future__ import annotations

import hmac

from codexia_manual_agent.attention_core.models import (
    ATTENTION_NEED_DECLARED_EVENT,
    AttentionNeed,
    InvalidAttentionRecord,
)
from codexia_manual_agent.work_core import WorkEvent


class AttentionProjectionError(RuntimeError):
    """Durable Work chronology violates Gen2 AttentionNeed semantics."""


def _workflow_wrapped_need(event: WorkEvent) -> AttentionNeed:
    raw = event.to_dict()["payload"]
    if not isinstance(raw, dict) or set(raw) != {"_workflow", "payload"}:
        raise AttentionProjectionError(
            "attention.need-declared workflow wrapper is not exact"
        )
    provenance = raw["_workflow"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "workflow_run_id",
        "workflow_run_digest",
    }:
        raise AttentionProjectionError(
            "AttentionNeed workflow provenance is not exact"
        )
    if not all(isinstance(provenance[key], str) for key in provenance):
        raise AttentionProjectionError(
            "AttentionNeed workflow provenance types are invalid"
        )
    body = raw["payload"]
    if (
        not isinstance(body, dict)
        or set(body) != {"attention_need"}
        or not isinstance(body["attention_need"], dict)
    ):
        raise AttentionProjectionError("AttentionNeed body is not exact")
    try:
        need = AttentionNeed.from_dict(body["attention_need"])
    except (InvalidAttentionRecord, KeyError, TypeError, ValueError) as exc:
        raise AttentionProjectionError(
            "attention.need-declared contains invalid AttentionNeed"
        ) from exc

    if provenance["workflow_run_id"] != need.workflow_run_id:
        raise AttentionProjectionError(
            "AttentionNeed wrapper changed WorkflowRun identity"
        )
    if not hmac.compare_digest(
        provenance["workflow_run_digest"],
        need.workflow_run_digest,
    ):
        raise AttentionProjectionError(
            "AttentionNeed wrapper changed WorkflowRun binding"
        )
    return need


def project_attention_needs(
    events: tuple[WorkEvent, ...],
) -> tuple[AttentionNeed, ...]:
    needs: dict[str, AttentionNeed] = {}
    order: list[str] = []

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind != ATTENTION_NEED_DECLARED_EVENT:
            continue

        need = _workflow_wrapped_need(event)
        if need.attention_id in needs:
            raise AttentionProjectionError(
                "AttentionNeed identity was durably declared twice"
            )
        if event.event_id != need.attention_id:
            raise AttentionProjectionError(
                "AttentionNeed event identity differs from need"
            )
        if event.created_at != need.created_at:
            raise AttentionProjectionError(
                "AttentionNeed event timestamp differs from need"
            )
        if event.work_id != need.work_id:
            raise AttentionProjectionError(
                "AttentionNeed event crosses Work identity"
            )
        if event.sequence != need.start_revision + 1:
            raise AttentionProjectionError(
                "AttentionNeed sequence does not bind start revision"
            )
        if event.previous_event_digest != need.start_event_digest:
            raise AttentionProjectionError(
                "AttentionNeed does not bind exact prior Work event"
            )

        needs[need.attention_id] = need
        order.append(need.attention_id)

    return tuple(needs[attention_id] for attention_id in order)


def project_attention_need(
    events: tuple[WorkEvent, ...],
    attention_id: str,
) -> AttentionNeed:
    for need in project_attention_needs(events):
        if need.attention_id == attention_id:
            return need
    raise AttentionProjectionError(f"Unknown AttentionNeed: {attention_id}")
