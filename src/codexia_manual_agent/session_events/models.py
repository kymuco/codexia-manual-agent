from __future__ import annotations

import hmac
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID, uuid4

from codexia_manual_agent.authority.models import ActionProposal, AuthorizationReceipt
from codexia_manual_agent.domain.errors import ActionIntegrityError, CodexiaError


EVENT_SCHEMA_VERSION = 1
MAX_EVENT_TEXT_CHARS = 1_048_576
MAX_EVENT_PAYLOAD_BYTES = 2_097_152


class SessionEventError(CodexiaError):
    """Base class for M3 persistent-session event failures."""


class SessionEventIntegrityError(SessionEventError):
    """A durable event or event chain violates its digest-bound contract."""


class SessionEventStateError(SessionEventError):
    """An event is invalid for the durable chronology reconstructed so far."""


class UnknownSessionError(SessionEventError):
    """A persistent session does not exist in the M3 ledger."""


class EventKind(StrEnum):
    SESSION_STARTED = "session_started"
    RUN_STARTED = "run_started"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_RESPONSE_RECORDED = "model_response_recorded"
    TOOL_OBSERVATION_RECORDED = "tool_observation_recorded"
    RUN_COMPLETED = "run_completed"
    RUN_INTERRUPTED = "run_interrupted"
    SESSION_COMPLETED = "session_completed"
    ACTION_PROPOSED = "action_proposed"
    AUTHORIZATION_RECORDED = "authorization_recorded"
    AUTHORIZATION_CONSUMED = "authorization_consumed"
    ACTION_EXECUTED = "action_executed"
    ACTION_OBSERVED = "action_observed"


class ActionRecoveryState(StrEnum):
    PROPOSED = "proposed"
    AUTHORIZED_UNCONSUMED = "authorized_unconsumed"
    DENIED = "denied"
    CONSUMED_NOT_EXECUTION_RECORDED = "consumed_not_execution_recorded"
    EXECUTED = "executed"
    OBSERVED = "observed"


class RecoveryDisposition(StrEnum):
    RESUMABLE = "resumable"
    WAITING_HUMAN = "waiting_human"
    BLOCKED_CONSUMED_AUTHORITY = "blocked_consumed_authority"
    UNKNOWN_PROVIDER_OUTCOME = "unknown_provider_outcome"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SessionEventIntegrityError("Event payload cannot contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SessionEventIntegrityError("Event payload object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise SessionEventIntegrityError(
        f"Event payload must be JSON-compatible, got {type(value).__name__}"
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise SessionEventIntegrityError(f"{field_name} must be a UUID") from exc


def _validate_timestamp(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SessionEventIntegrityError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SessionEventIntegrityError(f"{field_name} must include a timezone")


def _validate_digest(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise SessionEventIntegrityError(f"{field_name} must be SHA-256 hex")
    try:
        int(value, 16)
    except ValueError as exc:
        raise SessionEventIntegrityError(f"{field_name} must be SHA-256 hex") from exc


def _bounded_text(value: Any, field_name: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise SessionEventIntegrityError(f"{field_name} must be text")
    if len(value) > MAX_EVENT_TEXT_CHARS or "\x00" in value:
        raise SessionEventIntegrityError(f"{field_name} exceeds the M3 event budget")
    return value


def _non_empty_text(value: Any, field_name: str) -> str:
    text = _bounded_text(value, field_name)
    assert isinstance(text, str)
    if not text.strip():
        raise SessionEventIntegrityError(f"{field_name} must be non-empty")
    return text


def _non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SessionEventIntegrityError(f"{field_name} must be a non-negative integer")
    return value


def _exact_keys(payload: Mapping[str, Any], expected: set[str], kind: EventKind) -> None:
    actual = set(payload)
    if actual != expected:
        raise SessionEventIntegrityError(
            f"{kind.value} payload keys mismatch; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def _validate_conversation(value: Any, field_name: str = "conversation") -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise SessionEventIntegrityError(f"{field_name} must be an object or null")
    expected = {"conversation_id", "message_id", "parent_message_id", "finish_reason"}
    if set(value) != expected:
        raise SessionEventIntegrityError(f"{field_name} keys mismatch")
    for key in expected:
        item = value[key]
        if item is not None and not isinstance(item, str):
            raise SessionEventIntegrityError(f"{field_name}.{key} must be text or null")


def _validate_budget(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise SessionEventIntegrityError("budgets must be an object")
    expected = {
        "max_turns",
        "max_tool_calls",
        "max_response_chars",
        "max_total_model_chars",
        "max_observation_chars",
    }
    if set(value) != expected:
        raise SessionEventIntegrityError("budgets keys mismatch")
    for key in expected:
        amount = value[key]
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise SessionEventIntegrityError(f"budgets.{key} must be a positive integer")


def _reconstruct_proposal(value: Any) -> ActionProposal:
    if not isinstance(value, Mapping):
        raise SessionEventIntegrityError("proposal must be an object")
    expected = {
        "schema_version",
        "proposal_id",
        "created_at",
        "capability",
        "action",
        "workspace_root",
        "parameters",
        "summary",
        "proposal_digest",
    }
    if set(value) != expected:
        raise SessionEventIntegrityError("proposal keys mismatch")
    try:
        return ActionProposal(**dict(value))
    except (TypeError, ValueError, ActionIntegrityError) as exc:
        raise SessionEventIntegrityError("proposal does not satisfy the M2.x contract") from exc


def _reconstruct_receipt(value: Any) -> AuthorizationReceipt:
    if not isinstance(value, Mapping):
        raise SessionEventIntegrityError("receipt must be an object")
    expected = {
        "schema_version",
        "receipt_id",
        "created_at",
        "proposal_id",
        "proposal_digest",
        "decision",
        "mode",
        "source",
        "actor",
        "reason",
        "single_use",
        "receipt_digest",
    }
    if set(value) != expected:
        raise SessionEventIntegrityError("receipt keys mismatch")
    try:
        return AuthorizationReceipt(**dict(value))
    except (TypeError, ValueError, ActionIntegrityError) as exc:
        raise SessionEventIntegrityError("receipt does not satisfy the M2.x contract") from exc


def validate_event_payload(kind: EventKind | str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        normalized_kind = EventKind(kind)
    except (TypeError, ValueError) as exc:
        raise SessionEventIntegrityError("Unknown M3 event kind") from exc
    if not isinstance(payload, Mapping):
        raise SessionEventIntegrityError("Event payload must be an object")

    if normalized_kind is EventKind.SESSION_STARTED:
        _exact_keys(
            payload,
            {
                "workspace",
                "prompt_version",
                "mode",
                "capabilities",
                "provider",
                "title",
                "model",
                "reasoning_effort",
            },
            normalized_kind,
        )
        _non_empty_text(payload["workspace"], "workspace")
        _non_empty_text(payload["prompt_version"], "prompt_version")
        _non_empty_text(payload["mode"], "mode")
        if not isinstance(payload["capabilities"], (list, tuple)):
            raise SessionEventIntegrityError("capabilities must be an array")
        capabilities = payload["capabilities"]
        if any(not isinstance(item, str) or not item for item in capabilities):
            raise SessionEventIntegrityError("capabilities must contain non-empty strings")
        if len(set(capabilities)) != len(capabilities):
            raise SessionEventIntegrityError("capabilities must not contain duplicates")
        _non_empty_text(payload["provider"], "provider")
        _bounded_text(payload["title"], "title", allow_none=True)
        _bounded_text(payload["model"], "model", allow_none=True)
        _bounded_text(payload["reasoning_effort"], "reasoning_effort", allow_none=True)

    elif normalized_kind is EventKind.RUN_STARTED:
        _exact_keys(payload, {"run_id", "task", "budgets"}, normalized_kind)
        _validate_uuid(payload["run_id"], "run_id")
        _non_empty_text(payload["task"], "task")
        _validate_budget(payload["budgets"])

    elif normalized_kind is EventKind.MODEL_REQUEST_STARTED:
        _exact_keys(
            payload,
            {"run_id", "request_id", "provider", "prompt", "system", "conversation"},
            normalized_kind,
        )
        _validate_uuid(payload["run_id"], "run_id")
        _validate_uuid(payload["request_id"], "request_id")
        _non_empty_text(payload["provider"], "provider")
        _bounded_text(payload["prompt"], "prompt")
        _bounded_text(payload["system"], "system", allow_none=True)
        _validate_conversation(payload["conversation"])

    elif normalized_kind is EventKind.MODEL_RESPONSE_RECORDED:
        full_keys = frozenset(
            {
                "run_id",
                "request_id",
                "provider",
                "response_text",
                "conversation",
                "model",
                "reasoning_effort",
                "metrics",
            }
        )
        digest_only_keys = frozenset(
            {
                "run_id",
                "request_id",
                "provider",
                "response_chars",
                "response_bytes",
                "response_digest",
                "response_storage",
                "conversation",
                "model",
                "reasoning_effort",
            }
        )
        actual_keys = frozenset(payload)
        if actual_keys not in {full_keys, digest_only_keys}:
            raise SessionEventIntegrityError(
                f"{normalized_kind.value} payload keys mismatch; expected exact-text "
                "or digest-only response representation"
            )
        _validate_uuid(payload["run_id"], "run_id")
        _validate_uuid(payload["request_id"], "request_id")
        _non_empty_text(payload["provider"], "provider")
        _validate_conversation(payload["conversation"])
        _bounded_text(payload["model"], "model", allow_none=True)
        _bounded_text(payload["reasoning_effort"], "reasoning_effort", allow_none=True)
        if actual_keys == full_keys:
            _bounded_text(payload["response_text"], "response_text")
            if not isinstance(payload["metrics"], Mapping):
                raise SessionEventIntegrityError("metrics must be an object")
        else:
            _non_negative_int(payload["response_chars"], "response_chars")
            _non_negative_int(payload["response_bytes"], "response_bytes")
            _validate_digest(payload["response_digest"], "response_digest")
            if payload["response_storage"] != "digest_only":
                raise SessionEventIntegrityError(
                    "digest-only model response must declare response_storage=digest_only"
                )

    elif normalized_kind is EventKind.TOOL_OBSERVATION_RECORDED:
        _exact_keys(payload, {"run_id", "request_id", "tool", "observation_json"}, normalized_kind)
        _validate_uuid(payload["run_id"], "run_id")
        _non_empty_text(payload["request_id"], "request_id")
        _non_empty_text(payload["tool"], "tool")
        _bounded_text(payload["observation_json"], "observation_json")

    elif normalized_kind is EventKind.RUN_COMPLETED:
        _exact_keys(
            payload,
            {
                "run_id",
                "status",
                "final_text",
                "turns",
                "tool_calls",
                "model_chars",
                "conversation",
                "model",
                "reasoning_effort",
                "error",
            },
            normalized_kind,
        )
        _validate_uuid(payload["run_id"], "run_id")
        _non_empty_text(payload["status"], "status")
        _bounded_text(payload["final_text"], "final_text", allow_none=True)
        _non_negative_int(payload["turns"], "turns")
        _non_negative_int(payload["tool_calls"], "tool_calls")
        _non_negative_int(payload["model_chars"], "model_chars")
        _validate_conversation(payload["conversation"])
        _bounded_text(payload["model"], "model", allow_none=True)
        _bounded_text(payload["reasoning_effort"], "reasoning_effort", allow_none=True)
        _bounded_text(payload["error"], "error", allow_none=True)

    elif normalized_kind is EventKind.RUN_INTERRUPTED:
        _exact_keys(payload, {"run_id", "reason", "detail", "request_id"}, normalized_kind)
        _validate_uuid(payload["run_id"], "run_id")
        _non_empty_text(payload["reason"], "reason")
        _bounded_text(payload["detail"], "detail")
        if payload["request_id"] is not None:
            _validate_uuid(payload["request_id"], "request_id")

    elif normalized_kind is EventKind.SESSION_COMPLETED:
        _exact_keys(payload, {"status", "detail"}, normalized_kind)
        _non_empty_text(payload["status"], "status")
        _bounded_text(payload["detail"], "detail", allow_none=True)

    elif normalized_kind is EventKind.ACTION_PROPOSED:
        _exact_keys(payload, {"proposal"}, normalized_kind)
        _reconstruct_proposal(payload["proposal"])

    elif normalized_kind is EventKind.AUTHORIZATION_RECORDED:
        _exact_keys(payload, {"receipt"}, normalized_kind)
        _reconstruct_receipt(payload["receipt"])

    elif normalized_kind is EventKind.AUTHORIZATION_CONSUMED:
        _exact_keys(
            payload,
            {"receipt_id", "receipt_digest", "proposal_id", "proposal_digest"},
            normalized_kind,
        )
        _validate_uuid(payload["receipt_id"], "receipt_id")
        _validate_digest(payload["receipt_digest"], "receipt_digest")
        _validate_uuid(payload["proposal_id"], "proposal_id")
        _validate_digest(payload["proposal_digest"], "proposal_digest")

    elif normalized_kind is EventKind.ACTION_EXECUTED:
        _exact_keys(
            payload,
            {"proposal_id", "proposal_digest", "receipt_id", "receipt_digest", "execution_id"},
            normalized_kind,
        )
        _validate_uuid(payload["proposal_id"], "proposal_id")
        _validate_digest(payload["proposal_digest"], "proposal_digest")
        _validate_uuid(payload["receipt_id"], "receipt_id")
        _validate_digest(payload["receipt_digest"], "receipt_digest")
        _non_empty_text(payload["execution_id"], "execution_id")

    elif normalized_kind is EventKind.ACTION_OBSERVED:
        _exact_keys(
            payload,
            {"proposal_id", "proposal_digest", "execution_id", "observation_id"},
            normalized_kind,
        )
        _validate_uuid(payload["proposal_id"], "proposal_id")
        _validate_digest(payload["proposal_digest"], "proposal_digest")
        _non_empty_text(payload["execution_id"], "execution_id")
        _non_empty_text(payload["observation_id"], "observation_id")

    frozen = _freeze_json(payload)
    encoded = canonical_json(frozen).encode("utf-8")
    if len(encoded) > MAX_EVENT_PAYLOAD_BYTES:
        raise SessionEventIntegrityError("Event payload exceeds the M3 byte budget")
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(frozen=True, slots=True)
class SessionEventReceipt:
    schema_version: int
    event_id: str
    session_id: str
    sequence: int
    created_at: str
    kind: EventKind
    payload: Mapping[str, Any]
    previous_event_digest: str | None
    event_digest: str

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        sequence: int,
        kind: EventKind | str,
        payload: Mapping[str, Any],
        previous_event_digest: str | None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> "SessionEventReceipt":
        event_id = event_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        normalized_kind = EventKind(kind)
        frozen_payload = validate_event_payload(normalized_kind, payload)
        normalized = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "session_id": session_id,
            "sequence": sequence,
            "created_at": created_at,
            "kind": normalized_kind.value,
            "payload": frozen_payload,
            "previous_event_digest": previous_event_digest,
        }
        return cls(
            schema_version=EVENT_SCHEMA_VERSION,
            event_id=event_id,
            session_id=session_id,
            sequence=sequence,
            created_at=created_at,
            kind=normalized_kind,
            payload=frozen_payload,
            previous_event_digest=previous_event_digest,
            event_digest=_digest(normalized),
        )

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise SessionEventIntegrityError("Unsupported M3 event schema version")
        _validate_uuid(self.event_id, "event_id")
        _validate_uuid(self.session_id, "session_id")
        _non_negative_int(self.sequence, "sequence")
        _validate_timestamp(self.created_at, "created_at")
        try:
            kind = EventKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise SessionEventIntegrityError("Unknown M3 event kind") from exc
        frozen_payload = validate_event_payload(kind, self.payload)
        if self.sequence == 0:
            if self.previous_event_digest is not None:
                raise SessionEventIntegrityError("Sequence-zero event cannot have a previous digest")
            if kind is not EventKind.SESSION_STARTED:
                raise SessionEventIntegrityError("Sequence-zero event must be session_started")
        else:
            if self.previous_event_digest is None:
                raise SessionEventIntegrityError("Non-first event requires a previous digest")
            _validate_digest(self.previous_event_digest, "previous_event_digest")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "payload", frozen_payload)
        _validate_digest(self.event_digest, "event_digest")
        expected = _digest(self._payload())
        if not hmac.compare_digest(expected, self.event_digest):
            raise SessionEventIntegrityError("Event digest does not match exact payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "kind": self.kind.value,
            "payload": self.payload,
            "previous_event_digest": self.previous_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = _thaw_json(self._payload())
        payload["event_digest"] = self.event_digest
        return payload


def proposal_from_event_payload(payload: Mapping[str, Any]) -> ActionProposal:
    return _reconstruct_proposal(payload["proposal"])


def receipt_from_event_payload(payload: Mapping[str, Any]) -> AuthorizationReceipt:
    return _reconstruct_receipt(payload["receipt"])
