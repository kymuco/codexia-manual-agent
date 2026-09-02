from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable, Mapping

from codexia_manual_agent.delegation.errors import InvalidDelegationError
from codexia_manual_agent.delegation.models import (
    DELEGABLE_CAPABILITIES,
    DelegationBudget,
    EscalationReason,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import ProtocolError


DELEGATION_CONTROL_REQUEST_SCHEMA_VERSION = 1
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
MAX_DELEGATION_CONTROL_RESPONSE_CHARS = 32_768
MAX_DELEGATION_TASK_CHARS = 8_192
MAX_ESCALATION_SUMMARY_CHARS = 8_192
MAX_REQUESTED_ACTION_CHARS = 256


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_digest(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ProtocolError("Delegation request digest must be SHA-256 hex")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ProtocolError("Delegation request digest must be SHA-256 hex") from exc


def _request_id(value: Any) -> str:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise ProtocolError("Delegation request_id has an invalid format")
    return value


def _bounded_string(value: Any, field_name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars or "\x00" in normalized:
        raise ProtocolError(f"{field_name} is empty or exceeds its budget")
    return normalized


def _normalize_delegate_capabilities(
    values: Iterable[Capability | str],
) -> tuple[Capability, ...]:
    try:
        capabilities = tuple(Capability(item) for item in values)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("delegate_request contains an unknown capability") from exc
    if len(set(capabilities)) != len(capabilities):
        raise ProtocolError("delegate_request capabilities must not contain duplicates")
    if any(item not in DELEGABLE_CAPABILITIES for item in capabilities):
        raise ProtocolError(
            "delegate_request cannot carry mutation/external authority; use escalation_request"
        )
    return tuple(sorted(capabilities, key=lambda item: item.value))


def _normalize_budget(value: DelegationBudget) -> DelegationBudget:
    if not isinstance(value, DelegationBudget):
        raise ProtocolError("delegate_request budget has an invalid type")
    try:
        return value.require_allocation()
    except InvalidDelegationError as exc:
        raise ProtocolError(str(exc)) from exc


def _normalize_reason(value: EscalationReason | str) -> EscalationReason:
    try:
        return EscalationReason(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("escalation_request.reason is unsupported") from exc


def _normalize_optional_capability(value: Capability | str | None) -> Capability | None:
    if value is None:
        return None
    try:
        return Capability(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            "escalation_request.requested_capability is unsupported"
        ) from exc


def _normalize_optional_action(value: str | None) -> str | None:
    if value is None:
        return None
    return _bounded_string(value, "requested_action", MAX_REQUESTED_ACTION_CHARS)


@dataclass(frozen=True, slots=True)
class DelegateWorkRequest:
    request_id: str
    task: str
    capabilities: tuple[Capability, ...]
    budget: DelegationBudget
    request_digest: str

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        task: str,
        capabilities: Iterable[Capability | str],
        budget: DelegationBudget,
    ) -> "DelegateWorkRequest":
        normalized_request_id = _request_id(request_id)
        normalized_task = _bounded_string(
            task,
            "delegate_request.task",
            MAX_DELEGATION_TASK_CHARS,
        )
        normalized_capabilities = _normalize_delegate_capabilities(capabilities)
        normalized_budget = _normalize_budget(budget)
        payload = {
            "schema_version": DELEGATION_CONTROL_REQUEST_SCHEMA_VERSION,
            "type": "delegate_request",
            "request_id": normalized_request_id,
            "task": normalized_task,
            "capabilities": [item.value for item in normalized_capabilities],
            "budget": normalized_budget.to_dict(),
        }
        return cls(
            request_id=normalized_request_id,
            task=normalized_task,
            capabilities=normalized_capabilities,
            budget=normalized_budget,
            request_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        request_id = _request_id(self.request_id)
        task = _bounded_string(
            self.task,
            "delegate_request.task",
            MAX_DELEGATION_TASK_CHARS,
        )
        capabilities = _normalize_delegate_capabilities(self.capabilities)
        budget = _normalize_budget(self.budget)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "budget", budget)
        _validate_digest(self.request_digest)
        if not hmac.compare_digest(self.request_digest, _digest(self.to_digest_dict())):
            raise ProtocolError("Delegation request digest does not match payload")

    def to_digest_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DELEGATION_CONTROL_REQUEST_SCHEMA_VERSION,
            "type": "delegate_request",
            "request_id": self.request_id,
            "task": self.task,
            "capabilities": [item.value for item in self.capabilities],
            "budget": self.budget.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class EscalateWorkRequest:
    request_id: str
    reason: EscalationReason
    requested_capability: Capability | None
    requested_action: str | None
    summary: str
    request_digest: str

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        reason: EscalationReason | str,
        requested_capability: Capability | str | None,
        requested_action: str | None,
        summary: str,
    ) -> "EscalateWorkRequest":
        normalized_request_id = _request_id(request_id)
        normalized_reason = _normalize_reason(reason)
        capability = _normalize_optional_capability(requested_capability)
        action = _normalize_optional_action(requested_action)
        normalized_summary = _bounded_string(
            summary,
            "summary",
            MAX_ESCALATION_SUMMARY_CHARS,
        )
        payload = {
            "schema_version": DELEGATION_CONTROL_REQUEST_SCHEMA_VERSION,
            "type": "escalation_request",
            "request_id": normalized_request_id,
            "reason": normalized_reason.value,
            "requested_capability": capability.value if capability is not None else None,
            "requested_action": action,
            "summary": normalized_summary,
        }
        return cls(
            request_id=normalized_request_id,
            reason=normalized_reason,
            requested_capability=capability,
            requested_action=action,
            summary=normalized_summary,
            request_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        request_id = _request_id(self.request_id)
        reason = _normalize_reason(self.reason)
        capability = _normalize_optional_capability(self.requested_capability)
        action = _normalize_optional_action(self.requested_action)
        summary = _bounded_string(
            self.summary,
            "summary",
            MAX_ESCALATION_SUMMARY_CHARS,
        )
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "requested_capability", capability)
        object.__setattr__(self, "requested_action", action)
        object.__setattr__(self, "summary", summary)
        _validate_digest(self.request_digest)
        if not hmac.compare_digest(self.request_digest, _digest(self.to_digest_dict())):
            raise ProtocolError("Delegation request digest does not match payload")

    def to_digest_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DELEGATION_CONTROL_REQUEST_SCHEMA_VERSION,
            "type": "escalation_request",
            "request_id": self.request_id,
            "reason": self.reason.value,
            "requested_capability": (
                self.requested_capability.value
                if self.requested_capability is not None
                else None
            ),
            "requested_action": self.requested_action,
            "summary": self.summary,
        }


DelegationControlRequest = DelegateWorkRequest | EscalateWorkRequest


def parse_delegation_control_request(
    text: str,
    *,
    max_chars: int = MAX_DELEGATION_CONTROL_RESPONSE_CHARS,
) -> DelegationControlRequest:
    """Parse only non-authority orchestration intent.

    Local lineage/workspace ids and all proposal/receipt/approval state are absent
    from this schema on purpose; callers derive those from the coordinator.
    """

    if not isinstance(text, str):
        raise ProtocolError("Delegation control response must be text")
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if len(text) > max_chars:
        raise ProtocolError(
            f"Delegation control response exceeds limit ({len(text)} > {max_chars} chars)"
        )
    try:
        payload = json.loads(
            _unwrap_json_fence(text),
            object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ProtocolError(
            f"Delegation control response is not valid JSON: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Delegation control response must be one JSON object")

    request_type = payload.get("type")
    if request_type == "delegate_request":
        _require_exact_keys(
            payload,
            {"type", "request_id", "task", "capabilities", "budget"},
        )
        raw_capabilities = payload.get("capabilities")
        if not isinstance(raw_capabilities, list):
            raise ProtocolError("delegate_request.capabilities must be a JSON array")
        raw_budget = payload.get("budget")
        if not isinstance(raw_budget, dict):
            raise ProtocolError("delegate_request.budget must be a JSON object")
        _require_exact_keys(raw_budget, {"turns", "tool_calls", "model_chars"})
        try:
            budget = DelegationBudget(
                turns=_integer(raw_budget.get("turns"), "budget.turns"),
                tool_calls=_integer(raw_budget.get("tool_calls"), "budget.tool_calls"),
                model_chars=_integer(raw_budget.get("model_chars"), "budget.model_chars"),
            )
        except InvalidDelegationError as exc:
            raise ProtocolError(str(exc)) from exc
        return DelegateWorkRequest.create(
            request_id=payload.get("request_id"),
            task=payload.get("task"),
            capabilities=raw_capabilities,
            budget=budget,
        )

    if request_type == "escalation_request":
        _require_exact_keys(
            payload,
            {
                "type",
                "request_id",
                "reason",
                "requested_capability",
                "requested_action",
                "summary",
            },
        )
        return EscalateWorkRequest.create(
            request_id=payload.get("request_id"),
            reason=payload.get("reason"),
            requested_capability=payload.get("requested_capability"),
            requested_action=payload.get("requested_action"),
            summary=payload.get("summary"),
        )

    raise ProtocolError(
        "Delegation control response type must be 'delegate_request' or 'escalation_request'"
    )


def _reject_constant(value: str) -> None:
    raise ProtocolError(
        f"Delegation control response contains invalid JSON constant: {value}"
    )


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError(
                f"Delegation control response contains duplicate JSON key: {key}"
            )
        value[key] = item
    return value


def _unwrap_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        raise ProtocolError("Delegation response contains an incomplete code fence")
    if lines[0].strip().lower() not in {"```", "```json"}:
        raise ProtocolError("Only a single JSON code fence is allowed")
    return "\n".join(lines[1:-1]).strip()


def _integer(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolError(f"{field_name} must be a non-negative integer")
    return value


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str]) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProtocolError(
            f"Delegation protocol keys mismatch; missing={missing}, extra={extra}"
        )
