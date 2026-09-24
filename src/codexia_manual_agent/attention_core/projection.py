from __future__ import annotations

import hmac

from codexia_manual_agent.attention_core.models import (
    ATTENTION_NEED_DECLARED_EVENT,
    ATTENTION_RESPONSE_RECORDED_EVENT,
    AttentionNeed,
    AttentionResponse,
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

def _workflow_wrapped_response(event: WorkEvent) -> AttentionResponse:
    raw = event.to_dict()["payload"]
    if not isinstance(raw, dict) or set(raw) != {"_workflow", "payload"}:
        raise AttentionProjectionError(
            "attention.response-recorded workflow wrapper is not exact"
        )
    provenance = raw["_workflow"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "workflow_run_id",
        "workflow_run_digest",
    }:
        raise AttentionProjectionError(
            "AttentionResponse workflow provenance is not exact"
        )
    body = raw["payload"]
    if (
        not isinstance(body, dict)
        or set(body) != {"attention_response"}
        or not isinstance(body["attention_response"], dict)
    ):
        raise AttentionProjectionError("AttentionResponse body is not exact")
    try:
        response = AttentionResponse.from_dict(body["attention_response"])
    except (InvalidAttentionRecord, KeyError, TypeError, ValueError) as exc:
        raise AttentionProjectionError(
            "attention.response-recorded contains invalid AttentionResponse"
        ) from exc

    if provenance["workflow_run_id"] != response.workflow_run_id:
        raise AttentionProjectionError(
            "AttentionResponse wrapper changed WorkflowRun identity"
        )
    if not hmac.compare_digest(
        provenance["workflow_run_digest"],
        response.workflow_run_digest,
    ):
        raise AttentionProjectionError(
            "AttentionResponse wrapper changed WorkflowRun binding"
        )
    return response


def project_attention_responses(
    events: tuple[WorkEvent, ...],
) -> tuple[AttentionResponse, ...]:
    needs = {
        need.attention_id: need
        for need in project_attention_needs(events)
    }
    responses: list[AttentionResponse] = []
    source_bindings: dict[tuple[str, str], AttentionResponse] = {}

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind != ATTENTION_RESPONSE_RECORDED_EVENT:
            continue

        response = _workflow_wrapped_response(event)
        need = needs.get(response.attention_id)
        if need is None:
            raise AttentionProjectionError(
                "AttentionResponse references unknown AttentionNeed"
            )
        if response.attention_need_digest != need.need_digest:
            raise AttentionProjectionError(
                "AttentionResponse changed AttentionNeed binding"
            )
        if response.work_id != need.work_id or response.work_digest != need.work_digest:
            raise AttentionProjectionError(
                "AttentionResponse changed Work binding"
            )
        if (
            response.workflow_run_id != need.workflow_run_id
            or response.workflow_run_digest != need.workflow_run_digest
        ):
            raise AttentionProjectionError(
                "AttentionResponse changed WorkflowRun binding"
            )
        if response.start_revision < need.start_revision + 1:
            raise AttentionProjectionError(
                "AttentionResponse predates its AttentionNeed"
            )
        if event.event_id != response.response_id:
            raise AttentionProjectionError(
                "AttentionResponse event identity differs from response"
            )
        if event.created_at != response.created_at:
            raise AttentionProjectionError(
                "AttentionResponse event timestamp differs from response"
            )
        if event.work_id != response.work_id:
            raise AttentionProjectionError(
                "AttentionResponse event crosses Work identity"
            )
        if event.sequence != response.start_revision + 1:
            raise AttentionProjectionError(
                "AttentionResponse sequence does not bind capture revision"
            )
        if event.previous_event_digest != response.start_event_digest:
            raise AttentionProjectionError(
                "AttentionResponse does not bind exact prior Work event"
            )

        source_key = (response.source_namespace, response.source_id)
        existing = source_bindings.get(source_key)
        if existing is not None:
            raise AttentionProjectionError(
                "AttentionResponse source identity was durably reused"
            )
        source_bindings[source_key] = response
        responses.append(response)

    return tuple(responses)


def project_attention_response(
    events: tuple[WorkEvent, ...],
    response_id: str,
) -> AttentionResponse:
    for response in project_attention_responses(events):
        if response.response_id == response_id:
            return response
    raise AttentionProjectionError(f"Unknown AttentionResponse: {response_id}")

