from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from codexia_manual_agent.role_core.models import (
    COGNITION_REQUEST_SCHEMA_VERSION,
    COGNITION_REQUESTED_EVENT,
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
from codexia_manual_agent.work_core import WorkEvent

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RoleProjectionError(RuntimeError):
    """Durable Work chronology violates G2.3 role/cognition semantics."""


@dataclass(slots=True)
class _RoleState:
    run: RoleRun
    state: RoleRunState = RoleRunState.ACTIVE
    request_id: str | None = None
    request_digest: str | None = None
    terminal_event_id: str | None = None
    output_text: str | None = None
    error: str | None = None


_ROLE_EVENT_KINDS = {
    ROLE_STARTED_EVENT,
    COGNITION_REQUESTED_EVENT,
    ROLE_COMPLETED_EVENT,
    ROLE_FAILED_EVENT,
    ROLE_OUTCOME_UNKNOWN_EVENT,
}

_REQUEST_RECORD_KEYS = {
    "schema_version",
    "request_id",
    "created_at",
    "role_run_id",
    "role_run_digest",
    "work_id",
    "work_digest",
    "workflow_run_id",
    "workflow_run_digest",
    "expected_revision",
    "expected_event_digest",
    "instructions_digest",
    "context_digest",
    "request_digest",
}


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


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


def _request_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _REQUEST_RECORD_KEYS:
        raise RoleProjectionError("cognition request record keys are not exact")
    if value["schema_version"] != COGNITION_REQUEST_SCHEMA_VERSION:
        raise RoleProjectionError("Unsupported cognition request record schema")
    for field_name in ("request_id", "role_run_id", "work_id", "workflow_run_id"):
        if not _is_uuid(value[field_name]):
            raise RoleProjectionError(
                f"cognition request {field_name} is not canonical UUID"
            )
    for field_name in (
        "request_digest",
        "role_run_digest",
        "work_digest",
        "workflow_run_digest",
        "instructions_digest",
        "context_digest",
    ):
        if not _is_digest(value[field_name]):
            raise RoleProjectionError(
                f"cognition request {field_name} is not SHA-256"
            )
    if (
        type(value["expected_revision"]) is not int
        or value["expected_revision"] < 0
    ):
        raise RoleProjectionError(
            "cognition request expected_revision must be non-negative integer"
        )
    if value["expected_event_digest"] is None:
        if value["expected_revision"] != 0:
            raise RoleProjectionError(
                "nonzero cognition request revision requires event digest"
            )
    elif not _is_digest(value["expected_event_digest"]):
        raise RoleProjectionError(
            "cognition request expected_event_digest is not SHA-256"
        )
    if not isinstance(value["created_at"], str) or not value["created_at"]:
        raise RoleProjectionError("cognition request created_at is invalid")
    try:
        parsed = datetime.fromisoformat(value["created_at"])
    except ValueError as exc:
        raise RoleProjectionError(
            "cognition request created_at is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value["created_at"]:
        raise RoleProjectionError(
            "cognition request created_at is not canonical ISO-8601"
        )

    digest_base = {
        key: item
        for key, item in value.items()
        if key != "request_digest"
    }
    if not hmac.compare_digest(value["request_digest"], _digest(digest_base)):
        raise RoleProjectionError("cognition request digest mismatch")
    return value


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

        if event.kind == COGNITION_REQUESTED_EVENT:
            if set(body) != {"cognition_request"}:
                raise RoleProjectionError(
                    "role.cognition-requested body is not exact"
                )
            request = _request_record(body["cognition_request"])
            state = states.get(request["role_run_id"])
            if state is None:
                raise RoleProjectionError(
                    "cognition request references unknown RoleRun"
                )
            run = state.run
            if state.state is not RoleRunState.ACTIVE:
                raise RoleProjectionError(
                    "RoleRun received more than one cognition request"
                )
            if event.event_id != request["request_id"]:
                raise RoleProjectionError(
                    "cognition request event identity changed"
                )
            if event.created_at != request["created_at"]:
                raise RoleProjectionError(
                    "cognition request event timestamp changed"
                )
            if event.work_id != run.work_id or request["work_id"] != run.work_id:
                raise RoleProjectionError("cognition request crosses Work identity")
            if not hmac.compare_digest(request["work_digest"], run.work_digest):
                raise RoleProjectionError("cognition request changed Work binding")
            if request["workflow_run_id"] != run.workflow_run_id:
                raise RoleProjectionError(
                    "cognition request changed WorkflowRun identity"
                )
            if not hmac.compare_digest(
                request["workflow_run_digest"],
                run.workflow_run_digest,
            ):
                raise RoleProjectionError(
                    "cognition request changed WorkflowRun binding"
                )
            if request["role_run_id"] != run.role_run_id:
                raise RoleProjectionError("cognition request changed RoleRun identity")
            if not hmac.compare_digest(
                request["role_run_digest"],
                run.run_digest,
            ):
                raise RoleProjectionError("cognition request changed RoleRun binding")
            if request["instructions_digest"] != run.binding.instructions_digest:
                raise RoleProjectionError(
                    "cognition request changed RoleBinding instructions"
                )
            if request["context_digest"] != run.context.content_digest:
                raise RoleProjectionError(
                    "cognition request changed ContextProjection"
                )
            if event.sequence != request["expected_revision"] + 1:
                raise RoleProjectionError(
                    "cognition request event sequence changed"
                )
            if event.previous_event_digest != request["expected_event_digest"]:
                raise RoleProjectionError(
                    "cognition request prior event binding changed"
                )
            if workflow_provenance["workflow_run_id"] != run.workflow_run_id:
                raise RoleProjectionError(
                    "cognition request wrapper changed WorkflowRun identity"
                )
            if not hmac.compare_digest(
                workflow_provenance["workflow_run_digest"],
                run.workflow_run_digest,
            ):
                raise RoleProjectionError(
                    "cognition request wrapper changed WorkflowRun binding"
                )

            state.state = RoleRunState.REQUESTED
            state.request_id = request["request_id"]
            state.request_digest = request["request_digest"]
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
        if state.state is not RoleRunState.REQUESTED:
            raise RoleProjectionError(
                "Role terminal outcome requires one admitted cognition request"
            )
        if state.request_id != outcome.request_id:
            raise RoleProjectionError(
                "CognitionOutcome changed cognition request identity"
            )
        if state.request_digest is None or not hmac.compare_digest(
            state.request_digest,
            outcome.request_digest,
        ):
            raise RoleProjectionError(
                "CognitionOutcome changed cognition request binding"
            )
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
            request_id=states[role_run_id].request_id,
            request_digest=states[role_run_id].request_digest,
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
