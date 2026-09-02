from __future__ import annotations

import hmac
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from codexia_manual_agent.delegation.coordinator import (
    MAX_DELEGATION_RESULT_CHARS,
    DelegationSnapshot,
)
from codexia_manual_agent.delegation.errors import (
    DelegationAuthorityError,
    DelegationBudgetError,
    DelegationError,
    DelegationPersistenceError,
    DelegationPersistenceIntegrityError,
    DelegationReplayError,
    DelegationStateError,
    EscalationBindingError,
    InvalidDelegationError,
)
from codexia_manual_agent.delegation.models import (
    ContinuationDecision,
    DelegationBudget,
    DelegationEnvelope,
    DelegationLimits,
    DelegationState,
    EscalationRequest,
    OperatorContinuation,
)


DELEGATION_EVENT_SCHEMA_VERSION = 1
MAX_DELEGATION_EVENT_PAYLOAD_BYTES = 2_097_152


class DelegationEventKind(StrEnum):
    ROOT_CREATED = "root_created"
    CHILD_CREATED = "child_created"
    CONTROL_REQUEST_CLAIMED = "control_request_claimed"
    BUDGET_CONSUMED = "budget_consumed"
    ESCALATION_REQUESTED = "escalation_requested"
    ESCALATION_RESOLVED = "escalation_resolved"
    DELEGATION_COMPLETED = "delegation_completed"


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DelegationPersistenceIntegrityError(
                f"Persisted delegation JSON contains duplicate key: {key}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise DelegationPersistenceIntegrityError(
        f"Persisted delegation JSON contains non-finite constant: {value}"
    )


def _load_json(raw: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except DelegationPersistenceIntegrityError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DelegationPersistenceIntegrityError(
            "Persisted delegation event payload is not canonical JSON"
        ) from exc


def _json_compatible(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidDelegationError("Delegation event payload cannot contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidDelegationError("Delegation event object keys must be strings")
            normalized[key] = _json_compatible(item)
        return dict(sorted(normalized.items()))
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    raise InvalidDelegationError(
        f"Delegation event payload must be JSON-compatible, got {type(value).__name__}"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidDelegationError(f"{field_name} must be a UUID")
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidDelegationError(f"{field_name} must be a UUID") from exc
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidDelegationError(f"{field_name} must be ISO-8601")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidDelegationError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise InvalidDelegationError(f"{field_name} must include a timezone")
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise InvalidDelegationError(f"{field_name} must be SHA-256 hex")
    try:
        int(value, 16)
    except ValueError as exc:
        raise InvalidDelegationError(f"{field_name} must be SHA-256 hex") from exc
    return value


def _exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidDelegationError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise InvalidDelegationError(
            f"{label} keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _budget_from_dict(value: Any) -> DelegationBudget:
    payload = _exact_keys(value, {"turns", "tool_calls", "model_chars"}, "budget")
    return DelegationBudget(
        turns=payload["turns"],
        tool_calls=payload["tool_calls"],
        model_chars=payload["model_chars"],
    )


def _limits_from_dict(value: Any) -> DelegationLimits:
    payload = _exact_keys(value, {"max_depth", "max_total_delegations"}, "limits")
    return DelegationLimits(
        max_depth=payload["max_depth"],
        max_total_delegations=payload["max_total_delegations"],
    )


def delegation_envelope_from_dict(value: Any) -> DelegationEnvelope:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "delegation_id",
            "created_at",
            "root_delegation_id",
            "parent_delegation_id",
            "parent_delegation_digest",
            "depth",
            "workspace_root",
            "task",
            "capabilities",
            "budget",
            "limits",
            "delegation_digest",
        },
        "delegation envelope",
    )
    capabilities = payload["capabilities"]
    if not isinstance(capabilities, (list, tuple)):
        raise InvalidDelegationError("delegation capabilities must be an array")
    return DelegationEnvelope(
        schema_version=payload["schema_version"],
        delegation_id=payload["delegation_id"],
        created_at=payload["created_at"],
        root_delegation_id=payload["root_delegation_id"],
        parent_delegation_id=payload["parent_delegation_id"],
        parent_delegation_digest=payload["parent_delegation_digest"],
        depth=payload["depth"],
        workspace_root=payload["workspace_root"],
        task=payload["task"],
        capabilities=tuple(capabilities),
        budget=_budget_from_dict(payload["budget"]),
        limits=_limits_from_dict(payload["limits"]),
        delegation_digest=payload["delegation_digest"],
    )


def escalation_from_dict(value: Any) -> EscalationRequest:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "escalation_id",
            "created_at",
            "delegation_id",
            "delegation_digest",
            "reason",
            "requested_capability",
            "requested_action",
            "summary",
            "escalation_digest",
        },
        "escalation",
    )
    return EscalationRequest(
        schema_version=payload["schema_version"],
        escalation_id=payload["escalation_id"],
        created_at=payload["created_at"],
        delegation_id=payload["delegation_id"],
        delegation_digest=payload["delegation_digest"],
        reason=payload["reason"],
        requested_capability=payload["requested_capability"],
        requested_action=payload["requested_action"],
        summary=payload["summary"],
        escalation_digest=payload["escalation_digest"],
    )


def continuation_from_dict(value: Any) -> OperatorContinuation:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "continuation_id",
            "created_at",
            "escalation_id",
            "escalation_digest",
            "decision",
            "actor",
            "note",
            "continuation_digest",
        },
        "operator continuation",
    )
    return OperatorContinuation(
        schema_version=payload["schema_version"],
        continuation_id=payload["continuation_id"],
        created_at=payload["created_at"],
        escalation_id=payload["escalation_id"],
        escalation_digest=payload["escalation_digest"],
        decision=payload["decision"],
        actor=payload["actor"],
        note=payload["note"],
        continuation_digest=payload["continuation_digest"],
    )


def _validate_control_request_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise InvalidDelegationError("Control request id is invalid")
    return value


def _validate_result_summary(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidDelegationError("result_summary must be text")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > MAX_DELEGATION_RESULT_CHARS
        or "\x00" in normalized
    ):
        raise InvalidDelegationError("result_summary is empty or exceeds the M2.6 budget")
    return normalized


def validate_delegation_event_payload(
    kind: DelegationEventKind | str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        normalized_kind = DelegationEventKind(kind)
    except (TypeError, ValueError) as exc:
        raise InvalidDelegationError("Unknown M3.2 delegation event kind") from exc

    if normalized_kind is DelegationEventKind.ROOT_CREATED:
        value = _exact_keys(payload, {"envelope"}, normalized_kind.value)
        delegation_envelope_from_dict(value["envelope"])
    elif normalized_kind is DelegationEventKind.CHILD_CREATED:
        value = _exact_keys(payload, {"envelope"}, normalized_kind.value)
        delegation_envelope_from_dict(value["envelope"])
    elif normalized_kind is DelegationEventKind.CONTROL_REQUEST_CLAIMED:
        value = _exact_keys(
            payload,
            {"delegation_id", "request_id", "request_digest"},
            normalized_kind.value,
        )
        _validate_uuid(value["delegation_id"], "delegation_id")
        _validate_control_request_id(value["request_id"])
        _validate_digest(value["request_digest"], "request_digest")
    elif normalized_kind is DelegationEventKind.BUDGET_CONSUMED:
        value = _exact_keys(payload, {"delegation_id", "amount"}, normalized_kind.value)
        _validate_uuid(value["delegation_id"], "delegation_id")
        _budget_from_dict(value["amount"])
    elif normalized_kind is DelegationEventKind.ESCALATION_REQUESTED:
        value = _exact_keys(payload, {"escalation"}, normalized_kind.value)
        escalation_from_dict(value["escalation"])
    elif normalized_kind is DelegationEventKind.ESCALATION_RESOLVED:
        value = _exact_keys(payload, {"continuation"}, normalized_kind.value)
        continuation_from_dict(value["continuation"])
    elif normalized_kind is DelegationEventKind.DELEGATION_COMPLETED:
        value = _exact_keys(
            payload,
            {"delegation_id", "result_summary"},
            normalized_kind.value,
        )
        _validate_uuid(value["delegation_id"], "delegation_id")
        _validate_result_summary(value["result_summary"])
    else:  # pragma: no cover - enum exhaustiveness
        raise InvalidDelegationError("Unknown M3.2 delegation event kind")

    normalized = _json_compatible(payload)
    encoded = canonical_json(normalized).encode("utf-8")
    if len(encoded) > MAX_DELEGATION_EVENT_PAYLOAD_BYTES:
        raise InvalidDelegationError("Delegation event payload exceeds the M3.2 byte budget")
    assert isinstance(normalized, Mapping)
    return normalized


def _event_digest(payload: Mapping[str, Any]) -> str:
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DelegationEventReceipt:
    schema_version: int
    event_id: str
    root_delegation_id: str
    sequence: int
    created_at: str
    kind: DelegationEventKind
    payload: Mapping[str, Any]
    previous_event_digest: str | None
    event_digest: str

    @classmethod
    def create(
        cls,
        *,
        root_delegation_id: str,
        sequence: int,
        kind: DelegationEventKind | str,
        payload: Mapping[str, Any],
        previous_event_digest: str | None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> "DelegationEventReceipt":
        event_id = event_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        normalized_kind = DelegationEventKind(kind)
        normalized_payload = validate_delegation_event_payload(normalized_kind, payload)
        base = {
            "schema_version": DELEGATION_EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "root_delegation_id": root_delegation_id,
            "sequence": sequence,
            "created_at": created_at,
            "kind": normalized_kind.value,
            "payload": normalized_payload,
            "previous_event_digest": previous_event_digest,
        }
        return cls(
            schema_version=DELEGATION_EVENT_SCHEMA_VERSION,
            event_id=event_id,
            root_delegation_id=root_delegation_id,
            sequence=sequence,
            created_at=created_at,
            kind=normalized_kind,
            payload=normalized_payload,
            previous_event_digest=previous_event_digest,
            event_digest=_event_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != DELEGATION_EVENT_SCHEMA_VERSION:
            raise InvalidDelegationError("Unsupported M3.2 delegation event schema version")
        _validate_uuid(self.event_id, "event_id")
        _validate_uuid(self.root_delegation_id, "root_delegation_id")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise InvalidDelegationError("Delegation event sequence must be non-negative")
        _validate_timestamp(self.created_at, "created_at")
        kind = DelegationEventKind(self.kind)
        payload = validate_delegation_event_payload(kind, self.payload)
        if self.sequence == 0:
            if self.previous_event_digest is not None:
                raise InvalidDelegationError(
                    "Sequence-zero delegation event cannot have a previous digest"
                )
        else:
            if self.previous_event_digest is None:
                raise InvalidDelegationError("Non-first delegation event requires a previous digest")
            _validate_digest(self.previous_event_digest, "previous_event_digest")
        _validate_digest(self.event_digest, "event_digest")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "payload", payload)
        if not hmac.compare_digest(_event_digest(self._base_payload()), self.event_digest):
            raise InvalidDelegationError("Delegation event digest does not match exact payload")

    def _base_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "root_delegation_id": self.root_delegation_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "kind": self.kind.value,
            "payload": self.payload,
            "previous_event_digest": self.previous_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        value = _json_compatible(self._base_payload())
        value["event_digest"] = self.event_digest
        return value


@dataclass(slots=True)
class _ReplayNode:
    envelope: DelegationEnvelope
    state: DelegationState
    remaining_budget: DelegationBudget
    child_ids: list[str] = field(default_factory=list)
    pending_escalation: EscalationRequest | None = None
    result_summary: str | None = None
    continuation_ids: list[str] = field(default_factory=list)
    control_requests: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class _ReplayState:
    root_delegation_id: str
    nodes: dict[str, _ReplayNode] = field(default_factory=dict)
    escalations: dict[str, EscalationRequest] = field(default_factory=dict)
    continuations: dict[str, OperatorContinuation] = field(default_factory=dict)


def _require_active(node: _ReplayNode) -> None:
    if node.state is not DelegationState.ACTIVE:
        raise DelegationStateError(
            f"Delegation is {node.state.value}; expected {DelegationState.ACTIVE.value}"
        )


def _cancel_subtree(state: _ReplayState, delegation_id: str) -> None:
    node = state.nodes[delegation_id]
    for child_id in node.child_ids:
        child = state.nodes[child_id]
        if child.state not in {DelegationState.COMPLETED, DelegationState.CANCELLED}:
            _cancel_subtree(state, child_id)
    if node.state is not DelegationState.COMPLETED:
        node.pending_escalation = None
        node.state = DelegationState.CANCELLED


def apply_delegation_event(
    state: _ReplayState | None,
    kind: DelegationEventKind | str,
    payload: Mapping[str, Any],
    *,
    root_delegation_id: str,
) -> _ReplayState:
    normalized_kind = DelegationEventKind(kind)
    value = validate_delegation_event_payload(normalized_kind, payload)

    if normalized_kind is DelegationEventKind.ROOT_CREATED:
        if state is not None:
            raise DelegationStateError("root_created can appear only once")
        envelope = delegation_envelope_from_dict(value["envelope"])
        if (
            envelope.parent_delegation_id is not None
            or envelope.delegation_id != root_delegation_id
            or envelope.root_delegation_id != root_delegation_id
            or envelope.depth != 0
        ):
            raise InvalidDelegationError("root_created envelope does not bind the exact root")
        state = _ReplayState(root_delegation_id=root_delegation_id)
        state.nodes[envelope.delegation_id] = _ReplayNode(
            envelope=envelope,
            state=DelegationState.ACTIVE,
            remaining_budget=envelope.budget,
        )
        return state

    if state is None:
        raise DelegationStateError("Delegation chronology must start with root_created")
    if state.root_delegation_id != root_delegation_id:
        raise InvalidDelegationError("Delegation event root differs from replay root")

    if normalized_kind is DelegationEventKind.CHILD_CREATED:
        child = delegation_envelope_from_dict(value["envelope"])
        if child.delegation_id in state.nodes:
            raise InvalidDelegationError("Delegation id collision")
        parent_id = child.parent_delegation_id
        if parent_id is None or parent_id not in state.nodes:
            raise InvalidDelegationError("Child delegation has an unknown parent")
        parent = state.nodes[parent_id]
        _require_active(parent)
        if child.root_delegation_id != state.root_delegation_id:
            raise InvalidDelegationError("Child delegation root does not match the durable root")
        if child.parent_delegation_digest != parent.envelope.delegation_digest:
            raise InvalidDelegationError("Child delegation does not bind the exact parent digest")
        if child.depth != parent.envelope.depth + 1:
            raise InvalidDelegationError("Child delegation depth does not follow its parent")
        if child.workspace_root != parent.envelope.workspace_root:
            raise InvalidDelegationError("Child delegation workspace differs from its parent")
        if child.limits != parent.envelope.limits:
            raise InvalidDelegationError("Child delegation root limits differ from its parent")
        if not set(child.capabilities).issubset(parent.envelope.capabilities):
            raise DelegationAuthorityError(
                "Child delegation capabilities must be a subset of the parent envelope"
            )
        if len(state.nodes) >= parent.envelope.limits.max_total_delegations:
            raise DelegationBudgetError("Total delegation-count limit exhausted")
        if not parent.remaining_budget.contains(child.budget):
            raise DelegationBudgetError(
                "Child budget allocation exceeds the parent's remaining budget"
            )
        parent.remaining_budget = parent.remaining_budget.subtract(child.budget)
        parent.child_ids.append(child.delegation_id)
        state.nodes[child.delegation_id] = _ReplayNode(
            envelope=child,
            state=DelegationState.ACTIVE,
            remaining_budget=child.budget,
        )

    elif normalized_kind is DelegationEventKind.CONTROL_REQUEST_CLAIMED:
        delegation_id = value["delegation_id"]
        try:
            node = state.nodes[delegation_id]
        except KeyError as exc:
            raise InvalidDelegationError("Unknown delegation id") from exc
        _require_active(node)
        request_id = value["request_id"]
        request_digest = value["request_digest"]
        existing = node.control_requests.get(request_id)
        if existing is not None:
            if hmac.compare_digest(existing, request_digest):
                raise DelegationReplayError("Control request was already claimed")
            raise DelegationReplayError("Control request id was rebound to a different payload")
        node.control_requests[request_id] = request_digest

    elif normalized_kind is DelegationEventKind.BUDGET_CONSUMED:
        delegation_id = value["delegation_id"]
        try:
            node = state.nodes[delegation_id]
        except KeyError as exc:
            raise InvalidDelegationError("Unknown delegation id") from exc
        _require_active(node)
        amount = _budget_from_dict(value["amount"])
        if amount.turns == 0 and amount.tool_calls == 0 and amount.model_chars == 0:
            raise DelegationBudgetError("Budget consumption must consume at least one unit")
        if not node.remaining_budget.contains(amount):
            raise DelegationBudgetError("Delegation budget exhausted")
        node.remaining_budget = node.remaining_budget.subtract(amount)

    elif normalized_kind is DelegationEventKind.ESCALATION_REQUESTED:
        escalation = escalation_from_dict(value["escalation"])
        if escalation.escalation_id in state.escalations:
            raise InvalidDelegationError("Escalation id collision")
        try:
            node = state.nodes[escalation.delegation_id]
        except KeyError as exc:
            raise InvalidDelegationError("Escalation references an unknown delegation") from exc
        _require_active(node)
        if not hmac.compare_digest(
            escalation.delegation_digest,
            node.envelope.delegation_digest,
        ):
            raise EscalationBindingError("Escalation does not bind the exact delegation")
        if node.pending_escalation is not None:
            raise DelegationStateError("Delegation already has a pending escalation")
        node.pending_escalation = escalation
        node.state = DelegationState.WAITING_HUMAN
        state.escalations[escalation.escalation_id] = escalation

    elif normalized_kind is DelegationEventKind.ESCALATION_RESOLVED:
        continuation = continuation_from_dict(value["continuation"])
        if continuation.continuation_id in state.continuations:
            raise InvalidDelegationError("Continuation id collision")
        try:
            escalation = state.escalations[continuation.escalation_id]
        except KeyError as exc:
            raise EscalationBindingError("Unknown escalation") from exc
        if not hmac.compare_digest(
            continuation.escalation_digest,
            escalation.escalation_digest,
        ):
            raise EscalationBindingError("Continuation does not bind the exact escalation")
        node = state.nodes[escalation.delegation_id]
        if node.state is not DelegationState.WAITING_HUMAN:
            raise DelegationStateError("Delegation is not waiting for an operator decision")
        pending = node.pending_escalation
        if (
            pending is None
            or pending.escalation_id != escalation.escalation_id
            or not hmac.compare_digest(pending.escalation_digest, escalation.escalation_digest)
        ):
            raise EscalationBindingError("Escalation is not the exact pending record")
        state.continuations[continuation.continuation_id] = continuation
        node.continuation_ids.append(continuation.continuation_id)
        node.pending_escalation = None
        if continuation.decision is ContinuationDecision.CONTINUE:
            node.state = DelegationState.ACTIVE
        else:
            _cancel_subtree(state, node.envelope.delegation_id)

    elif normalized_kind is DelegationEventKind.DELEGATION_COMPLETED:
        delegation_id = value["delegation_id"]
        try:
            node = state.nodes[delegation_id]
        except KeyError as exc:
            raise InvalidDelegationError("Unknown delegation id") from exc
        _require_active(node)
        live_children = [
            child_id
            for child_id in node.child_ids
            if state.nodes[child_id].state
            not in {DelegationState.COMPLETED, DelegationState.CANCELLED}
        ]
        if live_children:
            raise DelegationStateError(
                "Parent delegation cannot complete while child work is still live"
            )
        node.result_summary = _validate_result_summary(value["result_summary"])
        node.state = DelegationState.COMPLETED

    return state


def _snapshot(node: _ReplayNode) -> DelegationSnapshot:
    return DelegationSnapshot(
        envelope=node.envelope,
        state=node.state,
        remaining_budget=node.remaining_budget,
        child_ids=tuple(node.child_ids),
        pending_escalation=node.pending_escalation,
        result_summary=node.result_summary,
        continuation_ids=tuple(node.continuation_ids),
        control_request_ids=tuple(sorted(node.control_requests)),
    )


@dataclass(frozen=True, slots=True)
class DelegationRecovery:
    root_delegation_id: str
    events: tuple[DelegationEventReceipt, ...]
    snapshots: Mapping[str, DelegationSnapshot]
    escalations: Mapping[str, EscalationRequest]
    continuations: Mapping[str, OperatorContinuation]
    root_delegation_count: int

    def snapshot(self, delegation_id: str) -> DelegationSnapshot:
        try:
            return self.snapshots[delegation_id]
        except KeyError as exc:
            raise InvalidDelegationError("Unknown delegation id") from exc

    def continuation(self, continuation_id: str) -> OperatorContinuation:
        try:
            return self.continuations[continuation_id]
        except KeyError as exc:
            raise InvalidDelegationError("Unknown continuation id") from exc


def _public_recovery(
    state: _ReplayState,
    events: tuple[DelegationEventReceipt, ...],
) -> DelegationRecovery:
    return DelegationRecovery(
        root_delegation_id=state.root_delegation_id,
        events=events,
        snapshots=MappingProxyType(
            {delegation_id: _snapshot(node) for delegation_id, node in state.nodes.items()}
        ),
        escalations=MappingProxyType(dict(state.escalations)),
        continuations=MappingProxyType(dict(state.continuations)),
        root_delegation_count=len(state.nodes),
    )


PreparedEvent = tuple[DelegationEventKind, Mapping[str, Any]]
PrepareMutation = Callable[[_ReplayState], PreparedEvent]


class SqliteDelegationEventStore:
    """Authoritative append-only M3.2 orchestration ledger."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS delegation_roots (
                    root_delegation_id TEXT PRIMARY KEY,
                    head_sequence INTEGER NOT NULL,
                    head_event_digest TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS delegation_events (
                    root_delegation_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_event_digest TEXT,
                    event_digest TEXT NOT NULL,
                    PRIMARY KEY (root_delegation_id, sequence),
                    FOREIGN KEY (root_delegation_id)
                        REFERENCES delegation_roots(root_delegation_id)
                );

                CREATE TABLE IF NOT EXISTS delegation_index (
                    delegation_id TEXT PRIMARY KEY,
                    root_delegation_id TEXT NOT NULL,
                    delegation_digest TEXT NOT NULL,
                    FOREIGN KEY (root_delegation_id)
                        REFERENCES delegation_roots(root_delegation_id)
                );

                CREATE TABLE IF NOT EXISTS escalation_index (
                    escalation_id TEXT PRIMARY KEY,
                    root_delegation_id TEXT NOT NULL,
                    escalation_digest TEXT NOT NULL,
                    FOREIGN KEY (root_delegation_id)
                        REFERENCES delegation_roots(root_delegation_id)
                );

                CREATE TABLE IF NOT EXISTS continuation_index (
                    continuation_id TEXT PRIMARY KEY,
                    root_delegation_id TEXT NOT NULL,
                    continuation_digest TEXT NOT NULL,
                    FOREIGN KEY (root_delegation_id)
                        REFERENCES delegation_roots(root_delegation_id)
                );
                """
            )

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("ROLLBACK")

    def create_root(self, envelope: DelegationEnvelope) -> DelegationRecovery:
        if not isinstance(envelope, DelegationEnvelope):
            raise TypeError("envelope must be a DelegationEnvelope")
        root_id = envelope.delegation_id
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM delegation_roots WHERE root_delegation_id = ?",
                    (root_id,),
                ).fetchone() is not None:
                    raise InvalidDelegationError("Delegation id collision")
                if connection.execute(
                    "SELECT 1 FROM delegation_index WHERE delegation_id = ?",
                    (root_id,),
                ).fetchone() is not None:
                    raise InvalidDelegationError("Delegation id collision")
                connection.execute(
                    """
                    INSERT INTO delegation_roots(
                        root_delegation_id, head_sequence, head_event_digest, created_at
                    ) VALUES (?, -1, NULL, ?)
                    """,
                    (root_id, datetime.now(timezone.utc).isoformat()),
                )
                receipt = DelegationEventReceipt.create(
                    root_delegation_id=root_id,
                    sequence=0,
                    kind=DelegationEventKind.ROOT_CREATED,
                    payload={"envelope": envelope.to_dict()},
                    previous_event_digest=None,
                )
                state = apply_delegation_event(
                    None,
                    receipt.kind,
                    receipt.payload,
                    root_delegation_id=root_id,
                )
                self._insert_event(connection, receipt, old_sequence=-1, old_digest=None)
                self._insert_derived_index(connection, receipt)
                connection.execute("COMMIT")
                return _public_recovery(state, (receipt,))
            except Exception:
                self._rollback(connection)
                raise

    def recover(self, root_delegation_id: str) -> DelegationRecovery:
        _validate_uuid(root_delegation_id, "root_delegation_id")
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN")
                state, events = self._load_root(connection, root_delegation_id)
                connection.execute("COMMIT")
                return _public_recovery(state, events)
            except Exception:
                self._rollback(connection)
                raise

    def recover_for_delegation(self, delegation_id: str) -> DelegationRecovery:
        _validate_uuid(delegation_id, "delegation_id")
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN")
                root_id = self._root_for_delegation(connection, delegation_id)
                state, events = self._load_root(connection, root_id)
                connection.execute("COMMIT")
                return _public_recovery(state, events)
            except Exception:
                self._rollback(connection)
                raise

    def recover_for_continuation(self, continuation_id: str) -> DelegationRecovery:
        _validate_uuid(continuation_id, "continuation_id")
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT root_delegation_id FROM continuation_index WHERE continuation_id = ?",
                    (continuation_id,),
                ).fetchone()
                if row is None:
                    raise InvalidDelegationError("Unknown continuation id")
                state, events = self._load_root(connection, row["root_delegation_id"])
                connection.execute("COMMIT")
                return _public_recovery(state, events)
            except Exception:
                self._rollback(connection)
                raise

    def mutate_delegation(
        self,
        delegation_id: str,
        prepare: PrepareMutation,
    ) -> DelegationRecovery:
        _validate_uuid(delegation_id, "delegation_id")
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                root_id = self._root_for_delegation(connection, delegation_id)
                state, events = self._load_root(connection, root_id)
                kind, payload = prepare(state)
                receipt = self._candidate_receipt(root_id, events, kind, payload)
                state = apply_delegation_event(
                    state,
                    receipt.kind,
                    receipt.payload,
                    root_delegation_id=root_id,
                )
                previous = events[-1]
                self._insert_event(
                    connection,
                    receipt,
                    old_sequence=previous.sequence,
                    old_digest=previous.event_digest,
                )
                self._insert_derived_index(connection, receipt)
                connection.execute("COMMIT")
                return _public_recovery(state, events + (receipt,))
            except Exception:
                self._rollback(connection)
                raise

    def mutate_escalation(
        self,
        escalation_id: str,
        prepare: PrepareMutation,
    ) -> DelegationRecovery:
        _validate_uuid(escalation_id, "escalation_id")
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT root_delegation_id FROM escalation_index WHERE escalation_id = ?",
                    (escalation_id,),
                ).fetchone()
                if row is None:
                    raise EscalationBindingError("Unknown escalation")
                root_id = row["root_delegation_id"]
                state, events = self._load_root(connection, root_id)
                kind, payload = prepare(state)
                receipt = self._candidate_receipt(root_id, events, kind, payload)
                state = apply_delegation_event(
                    state,
                    receipt.kind,
                    receipt.payload,
                    root_delegation_id=root_id,
                )
                previous = events[-1]
                self._insert_event(
                    connection,
                    receipt,
                    old_sequence=previous.sequence,
                    old_digest=previous.event_digest,
                )
                self._insert_derived_index(connection, receipt)
                connection.execute("COMMIT")
                return _public_recovery(state, events + (receipt,))
            except Exception:
                self._rollback(connection)
                raise

    @staticmethod
    def _candidate_receipt(
        root_id: str,
        events: tuple[DelegationEventReceipt, ...],
        kind: DelegationEventKind,
        payload: Mapping[str, Any],
    ) -> DelegationEventReceipt:
        previous = events[-1]
        return DelegationEventReceipt.create(
            root_delegation_id=root_id,
            sequence=previous.sequence + 1,
            kind=kind,
            payload=payload,
            previous_event_digest=previous.event_digest,
        )

    @staticmethod
    def _root_for_delegation(connection: sqlite3.Connection, delegation_id: str) -> str:
        row = connection.execute(
            "SELECT root_delegation_id FROM delegation_index WHERE delegation_id = ?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            raise InvalidDelegationError("Unknown delegation id")
        return str(row["root_delegation_id"])

    def _load_root(
        self,
        connection: sqlite3.Connection,
        root_id: str,
    ) -> tuple[_ReplayState, tuple[DelegationEventReceipt, ...]]:
        root_row = connection.execute(
            """
            SELECT head_sequence, head_event_digest
            FROM delegation_roots
            WHERE root_delegation_id = ?
            """,
            (root_id,),
        ).fetchone()
        if root_row is None:
            raise InvalidDelegationError("Unknown root delegation id")
        rows = connection.execute(
            """
            SELECT sequence, event_id, created_at, kind, payload_json,
                   previous_event_digest, event_digest
            FROM delegation_events
            WHERE root_delegation_id = ?
            ORDER BY sequence ASC
            """,
            (root_id,),
        ).fetchall()
        if not rows:
            raise DelegationPersistenceIntegrityError(
                "Durable delegation root has no event chronology"
            )
        head_sequence = root_row["head_sequence"]
        head_digest = root_row["head_event_digest"]
        if not isinstance(head_sequence, int) or head_sequence < 0 or not isinstance(head_digest, str):
            raise DelegationPersistenceIntegrityError("Durable delegation root head is malformed")
        if len(rows) != head_sequence + 1:
            raise DelegationPersistenceIntegrityError(
                "Durable delegation root head sequence disagrees with event count"
            )

        events: list[DelegationEventReceipt] = []
        state: _ReplayState | None = None
        previous_digest: str | None = None
        for expected_sequence, row in enumerate(rows):
            if row["sequence"] != expected_sequence:
                raise DelegationPersistenceIntegrityError(
                    "Durable delegation event sequence is not contiguous"
                )
            payload = _load_json(row["payload_json"])
            if not isinstance(payload, Mapping):
                raise DelegationPersistenceIntegrityError(
                    "Persisted delegation event payload must be an object"
                )
            try:
                receipt = DelegationEventReceipt(
                    schema_version=DELEGATION_EVENT_SCHEMA_VERSION,
                    event_id=row["event_id"],
                    root_delegation_id=root_id,
                    sequence=row["sequence"],
                    created_at=row["created_at"],
                    kind=row["kind"],
                    payload=payload,
                    previous_event_digest=row["previous_event_digest"],
                    event_digest=row["event_digest"],
                )
                if receipt.previous_event_digest != previous_digest:
                    raise DelegationPersistenceIntegrityError(
                        "Durable delegation hash chain previous digest mismatch"
                    )
                state = apply_delegation_event(
                    state,
                    receipt.kind,
                    receipt.payload,
                    root_delegation_id=root_id,
                )
            except DelegationPersistenceIntegrityError:
                raise
            except DelegationError as exc:
                raise DelegationPersistenceIntegrityError(
                    f"Persisted delegation chronology is semantically invalid: {exc}"
                ) from exc
            events.append(receipt)
            previous_digest = receipt.event_digest

        if state is None:
            raise DelegationPersistenceIntegrityError("Durable delegation replay produced no root")
        if events[-1].sequence != head_sequence or not hmac.compare_digest(
            events[-1].event_digest,
            head_digest,
        ):
            raise DelegationPersistenceIntegrityError(
                "Durable delegation root head digest disagrees with event tail"
            )
        self._verify_derived_indexes(connection, state)
        return state, tuple(events)

    @staticmethod
    def _verify_derived_indexes(
        connection: sqlite3.Connection,
        state: _ReplayState,
    ) -> None:
        root_id = state.root_delegation_id
        delegation_rows = connection.execute(
            """
            SELECT delegation_id, delegation_digest
            FROM delegation_index
            WHERE root_delegation_id = ?
            """,
            (root_id,),
        ).fetchall()
        actual_delegations = {
            row["delegation_id"]: row["delegation_digest"] for row in delegation_rows
        }
        expected_delegations = {
            delegation_id: node.envelope.delegation_digest
            for delegation_id, node in state.nodes.items()
        }
        if actual_delegations != expected_delegations:
            raise DelegationPersistenceIntegrityError(
                "Derived delegation index disagrees with durable event chronology"
            )

        escalation_rows = connection.execute(
            """
            SELECT escalation_id, escalation_digest
            FROM escalation_index
            WHERE root_delegation_id = ?
            """,
            (root_id,),
        ).fetchall()
        actual_escalations = {
            row["escalation_id"]: row["escalation_digest"] for row in escalation_rows
        }
        expected_escalations = {
            escalation_id: escalation.escalation_digest
            for escalation_id, escalation in state.escalations.items()
        }
        if actual_escalations != expected_escalations:
            raise DelegationPersistenceIntegrityError(
                "Derived escalation index disagrees with durable event chronology"
            )

        continuation_rows = connection.execute(
            """
            SELECT continuation_id, continuation_digest
            FROM continuation_index
            WHERE root_delegation_id = ?
            """,
            (root_id,),
        ).fetchall()
        actual_continuations = {
            row["continuation_id"]: row["continuation_digest"]
            for row in continuation_rows
        }
        expected_continuations = {
            continuation_id: continuation.continuation_digest
            for continuation_id, continuation in state.continuations.items()
        }
        if actual_continuations != expected_continuations:
            raise DelegationPersistenceIntegrityError(
                "Derived continuation index disagrees with durable event chronology"
            )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        receipt: DelegationEventReceipt,
        *,
        old_sequence: int,
        old_digest: str | None,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO delegation_events(
                    root_delegation_id, sequence, event_id, created_at, kind,
                    payload_json, previous_event_digest, event_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.root_delegation_id,
                    receipt.sequence,
                    receipt.event_id,
                    receipt.created_at,
                    receipt.kind.value,
                    canonical_json(receipt.payload),
                    receipt.previous_event_digest,
                    receipt.event_digest,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DelegationPersistenceError("Could not append durable delegation event") from exc

        if old_digest is None:
            cursor = connection.execute(
                """
                UPDATE delegation_roots
                SET head_sequence = ?, head_event_digest = ?
                WHERE root_delegation_id = ?
                  AND head_sequence = ?
                  AND head_event_digest IS NULL
                """,
                (
                    receipt.sequence,
                    receipt.event_digest,
                    receipt.root_delegation_id,
                    old_sequence,
                ),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE delegation_roots
                SET head_sequence = ?, head_event_digest = ?
                WHERE root_delegation_id = ?
                  AND head_sequence = ?
                  AND head_event_digest = ?
                """,
                (
                    receipt.sequence,
                    receipt.event_digest,
                    receipt.root_delegation_id,
                    old_sequence,
                    old_digest,
                ),
            )
        if cursor.rowcount != 1:
            raise DelegationPersistenceIntegrityError(
                "Durable delegation root head changed during append"
            )

    @staticmethod
    def _insert_derived_index(
        connection: sqlite3.Connection,
        receipt: DelegationEventReceipt,
    ) -> None:
        try:
            if receipt.kind in {
                DelegationEventKind.ROOT_CREATED,
                DelegationEventKind.CHILD_CREATED,
            }:
                envelope = delegation_envelope_from_dict(receipt.payload["envelope"])
                connection.execute(
                    """
                    INSERT INTO delegation_index(
                        delegation_id, root_delegation_id, delegation_digest
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        envelope.delegation_id,
                        receipt.root_delegation_id,
                        envelope.delegation_digest,
                    ),
                )
            elif receipt.kind is DelegationEventKind.ESCALATION_REQUESTED:
                escalation = escalation_from_dict(receipt.payload["escalation"])
                connection.execute(
                    """
                    INSERT INTO escalation_index(
                        escalation_id, root_delegation_id, escalation_digest
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        escalation.escalation_id,
                        receipt.root_delegation_id,
                        escalation.escalation_digest,
                    ),
                )
            elif receipt.kind is DelegationEventKind.ESCALATION_RESOLVED:
                continuation = continuation_from_dict(receipt.payload["continuation"])
                connection.execute(
                    """
                    INSERT INTO continuation_index(
                        continuation_id, root_delegation_id, continuation_digest
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        continuation.continuation_id,
                        receipt.root_delegation_id,
                        continuation.continuation_digest,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DelegationPersistenceError(
                "Durable delegation derived index rejected a duplicate identity"
            ) from exc
