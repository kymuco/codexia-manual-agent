from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

from codexia_manual_agent.delegation.errors import InvalidDelegationError
from codexia_manual_agent.domain.capabilities import Capability


DELEGATION_SCHEMA_VERSION = 1
MAX_DELEGATION_TASK_CHARS = 8_192
MAX_ESCALATION_SUMMARY_CHARS = 8_192
MAX_OPERATOR_NOTE_CHARS = 8_192
MAX_REQUESTED_ACTION_CHARS = 256

# M2.6 v1 deliberately delegates only the already read-only workspace surface.
# Mutation/external capabilities may be named by an escalation, but are never
# admitted into a delegation envelope.
DELEGABLE_CAPABILITIES: frozenset[Capability] = frozenset({Capability.READ_WORKSPACE})


class DelegationState(StrEnum):
    ACTIVE = "active"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class EscalationReason(StrEnum):
    NOVEL = "novel"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"
    AMBIGUOUS = "ambiguous"
    POLICY_SENSITIVE = "policy_sensitive"


class ContinuationDecision(StrEnum):
    CONTINUE = "continue"
    CANCEL = "cancel"


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidDelegationError(f"{field_name} must be a UUID") from exc


def _validate_timestamp(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidDelegationError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise InvalidDelegationError(f"{field_name} must include a timezone")


def _validate_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise InvalidDelegationError(f"{field_name} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise InvalidDelegationError(f"{field_name} must be a SHA-256 hex digest") from exc


def _bounded_text(value: str, *, field_name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidDelegationError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars or "\x00" in normalized:
        raise InvalidDelegationError(f"{field_name} is empty or exceeds the M2.6 budget")
    return normalized


def _optional_bounded_text(
    value: str | None,
    *,
    field_name: str,
    max_chars: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name=field_name, max_chars=max_chars)


def _canonical_workspace_root(value: str | Path) -> str:
    try:
        resolved = Path(value).resolve(strict=True)
    except (TypeError, OSError, RuntimeError) as exc:
        raise InvalidDelegationError("Delegation workspace does not resolve") from exc
    if not resolved.is_dir():
        raise InvalidDelegationError("Delegation workspace must be a directory")
    return str(resolved)


def _normalize_capabilities(values: Iterable[Capability | str]) -> tuple[Capability, ...]:
    try:
        capabilities = tuple(Capability(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise InvalidDelegationError("Delegation contains an unknown capability") from exc
    if len(set(capabilities)) != len(capabilities):
        raise InvalidDelegationError("Delegation capabilities must not contain duplicates")
    if any(capability not in DELEGABLE_CAPABILITIES for capability in capabilities):
        raise InvalidDelegationError(
            "M2.6 v1 delegation envelopes admit read_workspace only"
        )
    return tuple(sorted(capabilities, key=lambda item: item.value))


def _normalize_escalation_reason(value: EscalationReason | str) -> EscalationReason:
    try:
        return EscalationReason(value)
    except (TypeError, ValueError) as exc:
        raise InvalidDelegationError("Escalation reason is unsupported") from exc


def _normalize_capability(value: Capability | str, *, field_name: str) -> Capability:
    try:
        return Capability(value)
    except (TypeError, ValueError) as exc:
        raise InvalidDelegationError(f"{field_name} is unsupported") from exc


def _normalize_continuation_decision(
    value: ContinuationDecision | str,
) -> ContinuationDecision:
    try:
        return ContinuationDecision(value)
    except (TypeError, ValueError) as exc:
        raise InvalidDelegationError("Continuation decision is unsupported") from exc


def _require_budget(value: DelegationBudget) -> DelegationBudget:
    if not isinstance(value, DelegationBudget):
        raise InvalidDelegationError("Delegation budget has an invalid type")
    return value.require_allocation()


def _require_limits(value: DelegationLimits) -> DelegationLimits:
    if not isinstance(value, DelegationLimits):
        raise InvalidDelegationError("Delegation limits have an invalid type")
    return value


@dataclass(frozen=True, slots=True)
class DelegationBudget:
    turns: int
    tool_calls: int
    model_chars: int

    def __post_init__(self) -> None:
        for field_name in ("turns", "tool_calls", "model_chars"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise InvalidDelegationError(
                    f"Delegation budget {field_name} must be a non-negative integer"
                )

    def require_allocation(self) -> "DelegationBudget":
        if self.turns <= 0 or self.model_chars <= 0:
            raise InvalidDelegationError(
                "A delegation allocation requires positive turns and model_chars"
            )
        return self

    def contains(self, requested: "DelegationBudget") -> bool:
        if not isinstance(requested, DelegationBudget):
            return False
        return (
            requested.turns <= self.turns
            and requested.tool_calls <= self.tool_calls
            and requested.model_chars <= self.model_chars
        )

    def subtract(self, amount: "DelegationBudget") -> "DelegationBudget":
        if not self.contains(amount):
            raise InvalidDelegationError("Delegation budget subtraction would become negative")
        return DelegationBudget(
            turns=self.turns - amount.turns,
            tool_calls=self.tool_calls - amount.tool_calls,
            model_chars=self.model_chars - amount.model_chars,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "model_chars": self.model_chars,
        }


@dataclass(frozen=True, slots=True)
class DelegationLimits:
    max_depth: int = 2
    max_total_delegations: int = 8

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_depth, int)
            or isinstance(self.max_depth, bool)
            or self.max_depth < 0
        ):
            raise InvalidDelegationError("max_depth must be a non-negative integer")
        if (
            not isinstance(self.max_total_delegations, int)
            or isinstance(self.max_total_delegations, bool)
            or self.max_total_delegations <= 0
        ):
            raise InvalidDelegationError("max_total_delegations must be a positive integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_depth": self.max_depth,
            "max_total_delegations": self.max_total_delegations,
        }


@dataclass(frozen=True, slots=True)
class DelegationEnvelope:
    schema_version: int
    delegation_id: str
    created_at: str
    root_delegation_id: str
    parent_delegation_id: str | None
    parent_delegation_digest: str | None
    depth: int
    workspace_root: str
    task: str
    capabilities: tuple[Capability, ...]
    budget: DelegationBudget
    limits: DelegationLimits
    delegation_digest: str

    @classmethod
    def create_root(
        cls,
        *,
        workspace_root: str | Path,
        task: str,
        capabilities: Iterable[Capability | str] = (Capability.READ_WORKSPACE,),
        budget: DelegationBudget,
        limits: DelegationLimits | None = None,
        delegation_id: str | None = None,
        created_at: str | None = None,
    ) -> "DelegationEnvelope":
        delegation_id = delegation_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        normalized_task = _bounded_text(
            task,
            field_name="task",
            max_chars=MAX_DELEGATION_TASK_CHARS,
        )
        normalized_capabilities = _normalize_capabilities(capabilities)
        normalized_budget = _require_budget(budget)
        normalized_limits = _require_limits(limits or DelegationLimits())
        canonical_root = _canonical_workspace_root(workspace_root)
        payload = {
            "schema_version": DELEGATION_SCHEMA_VERSION,
            "delegation_id": delegation_id,
            "created_at": created_at,
            "root_delegation_id": delegation_id,
            "parent_delegation_id": None,
            "parent_delegation_digest": None,
            "depth": 0,
            "workspace_root": canonical_root,
            "task": normalized_task,
            "capabilities": [item.value for item in normalized_capabilities],
            "budget": normalized_budget.to_dict(),
            "limits": normalized_limits.to_dict(),
        }
        return cls(
            schema_version=DELEGATION_SCHEMA_VERSION,
            delegation_id=delegation_id,
            created_at=created_at,
            root_delegation_id=delegation_id,
            parent_delegation_id=None,
            parent_delegation_digest=None,
            depth=0,
            workspace_root=canonical_root,
            task=normalized_task,
            capabilities=normalized_capabilities,
            budget=normalized_budget,
            limits=normalized_limits,
            delegation_digest=_digest(payload),
        )

    @classmethod
    def create_child(
        cls,
        *,
        parent: "DelegationEnvelope",
        task: str,
        capabilities: Iterable[Capability | str],
        budget: DelegationBudget,
        delegation_id: str | None = None,
        created_at: str | None = None,
    ) -> "DelegationEnvelope":
        if not isinstance(parent, DelegationEnvelope):
            raise TypeError("parent must be a DelegationEnvelope")
        delegation_id = delegation_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        normalized_task = _bounded_text(
            task,
            field_name="task",
            max_chars=MAX_DELEGATION_TASK_CHARS,
        )
        normalized_capabilities = _normalize_capabilities(capabilities)
        normalized_budget = _require_budget(budget)
        child_depth = parent.depth + 1
        if not set(normalized_capabilities).issubset(parent.capabilities):
            raise InvalidDelegationError(
                "Child delegation capabilities must be a subset of the parent envelope"
            )
        if child_depth > parent.limits.max_depth:
            raise InvalidDelegationError("Child delegation exceeds the root depth limit")
        if child_depth + 1 > parent.limits.max_total_delegations:
            raise InvalidDelegationError(
                "Child delegation exceeds the root total-node limit"
            )
        if not parent.budget.contains(normalized_budget):
            raise InvalidDelegationError(
                "Child allocation cannot exceed the parent's total envelope budget"
            )
        payload = {
            "schema_version": DELEGATION_SCHEMA_VERSION,
            "delegation_id": delegation_id,
            "created_at": created_at,
            "root_delegation_id": parent.root_delegation_id,
            "parent_delegation_id": parent.delegation_id,
            "parent_delegation_digest": parent.delegation_digest,
            "depth": child_depth,
            "workspace_root": parent.workspace_root,
            "task": normalized_task,
            "capabilities": [item.value for item in normalized_capabilities],
            "budget": normalized_budget.to_dict(),
            "limits": parent.limits.to_dict(),
        }
        return cls(
            schema_version=DELEGATION_SCHEMA_VERSION,
            delegation_id=delegation_id,
            created_at=created_at,
            root_delegation_id=parent.root_delegation_id,
            parent_delegation_id=parent.delegation_id,
            parent_delegation_digest=parent.delegation_digest,
            depth=child_depth,
            workspace_root=parent.workspace_root,
            task=normalized_task,
            capabilities=normalized_capabilities,
            budget=normalized_budget,
            limits=parent.limits,
            delegation_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        if self.schema_version != DELEGATION_SCHEMA_VERSION:
            raise InvalidDelegationError("Unsupported delegation schema version")
        _validate_uuid(self.delegation_id, "delegation_id")
        _validate_uuid(self.root_delegation_id, "root_delegation_id")
        _validate_timestamp(self.created_at, "created_at")
        if not isinstance(self.depth, int) or isinstance(self.depth, bool) or self.depth < 0:
            raise InvalidDelegationError("Delegation depth must be a non-negative integer")

        limits = _require_limits(self.limits)
        if self.depth > limits.max_depth:
            raise InvalidDelegationError("Delegation depth exceeds the root depth limit")
        if self.depth + 1 > limits.max_total_delegations:
            raise InvalidDelegationError(
                "Delegation depth is impossible under the root total-node limit"
            )
        budget = _require_budget(self.budget)
        canonical_root = _canonical_workspace_root(self.workspace_root)
        task = _bounded_text(
            self.task,
            field_name="task",
            max_chars=MAX_DELEGATION_TASK_CHARS,
        )
        capabilities = _normalize_capabilities(self.capabilities)
        object.__setattr__(self, "workspace_root", canonical_root)
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "budget", budget)
        object.__setattr__(self, "limits", limits)

        if self.parent_delegation_id is None:
            if (
                self.depth != 0
                or self.root_delegation_id != self.delegation_id
                or self.parent_delegation_digest is not None
            ):
                raise InvalidDelegationError("Root delegation lineage is invalid")
        else:
            _validate_uuid(self.parent_delegation_id, "parent_delegation_id")
            if self.depth <= 0 or self.parent_delegation_digest is None:
                raise InvalidDelegationError("Child delegation lineage is incomplete")
            _validate_sha256(self.parent_delegation_digest, "parent_delegation_digest")
            if self.delegation_id == self.parent_delegation_id:
                raise InvalidDelegationError("Child delegation cannot be its own parent")
            if self.delegation_id == self.root_delegation_id:
                raise InvalidDelegationError("Child delegation id cannot equal the root id")
            if self.depth == 1 and self.parent_delegation_id != self.root_delegation_id:
                raise InvalidDelegationError(
                    "Depth-one child parent must be the root delegation"
                )
            if self.depth > 1 and self.parent_delegation_id == self.root_delegation_id:
                raise InvalidDelegationError(
                    "Nested child parent cannot skip directly to the root delegation"
                )

        _validate_sha256(self.delegation_digest, "delegation_digest")
        expected = _digest(self._payload())
        if not hmac.compare_digest(expected, self.delegation_digest):
            raise InvalidDelegationError("Delegation digest does not match the exact envelope")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "delegation_id": self.delegation_id,
            "created_at": self.created_at,
            "root_delegation_id": self.root_delegation_id,
            "parent_delegation_id": self.parent_delegation_id,
            "parent_delegation_digest": self.parent_delegation_digest,
            "depth": self.depth,
            "workspace_root": self.workspace_root,
            "task": self.task,
            "capabilities": [item.value for item in self.capabilities],
            "budget": self.budget.to_dict(),
            "limits": self.limits.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["delegation_digest"] = self.delegation_digest
        return payload


@dataclass(frozen=True, slots=True)
class EscalationRequest:
    schema_version: int
    escalation_id: str
    created_at: str
    delegation_id: str
    delegation_digest: str
    reason: EscalationReason
    requested_capability: Capability | None
    requested_action: str | None
    summary: str
    escalation_digest: str

    @classmethod
    def create(
        cls,
        *,
        delegation: DelegationEnvelope,
        reason: EscalationReason | str,
        summary: str,
        requested_capability: Capability | str | None = None,
        requested_action: str | None = None,
        escalation_id: str | None = None,
        created_at: str | None = None,
    ) -> "EscalationRequest":
        if not isinstance(delegation, DelegationEnvelope):
            raise TypeError("delegation must be a DelegationEnvelope")
        escalation_id = escalation_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        normalized_reason = _normalize_escalation_reason(reason)
        capability = (
            None
            if requested_capability is None
            else _normalize_capability(
                requested_capability,
                field_name="requested_capability",
            )
        )
        action = _optional_bounded_text(
            requested_action,
            field_name="requested_action",
            max_chars=MAX_REQUESTED_ACTION_CHARS,
        )
        normalized_summary = _bounded_text(
            summary,
            field_name="summary",
            max_chars=MAX_ESCALATION_SUMMARY_CHARS,
        )
        payload = {
            "schema_version": DELEGATION_SCHEMA_VERSION,
            "escalation_id": escalation_id,
            "created_at": created_at,
            "delegation_id": delegation.delegation_id,
            "delegation_digest": delegation.delegation_digest,
            "reason": normalized_reason.value,
            "requested_capability": capability.value if capability is not None else None,
            "requested_action": action,
            "summary": normalized_summary,
        }
        return cls(
            schema_version=DELEGATION_SCHEMA_VERSION,
            escalation_id=escalation_id,
            created_at=created_at,
            delegation_id=delegation.delegation_id,
            delegation_digest=delegation.delegation_digest,
            reason=normalized_reason,
            requested_capability=capability,
            requested_action=action,
            summary=normalized_summary,
            escalation_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        if self.schema_version != DELEGATION_SCHEMA_VERSION:
            raise InvalidDelegationError("Unsupported escalation schema version")
        _validate_uuid(self.escalation_id, "escalation_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.delegation_id, "delegation_id")
        _validate_sha256(self.delegation_digest, "delegation_digest")
        reason = _normalize_escalation_reason(self.reason)
        capability = (
            None
            if self.requested_capability is None
            else _normalize_capability(
                self.requested_capability,
                field_name="requested_capability",
            )
        )
        action = _optional_bounded_text(
            self.requested_action,
            field_name="requested_action",
            max_chars=MAX_REQUESTED_ACTION_CHARS,
        )
        summary = _bounded_text(
            self.summary,
            field_name="summary",
            max_chars=MAX_ESCALATION_SUMMARY_CHARS,
        )
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "requested_capability", capability)
        object.__setattr__(self, "requested_action", action)
        object.__setattr__(self, "summary", summary)
        _validate_sha256(self.escalation_digest, "escalation_digest")
        expected = _digest(self._payload())
        if not hmac.compare_digest(expected, self.escalation_digest):
            raise InvalidDelegationError("Escalation digest does not match payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "escalation_id": self.escalation_id,
            "created_at": self.created_at,
            "delegation_id": self.delegation_id,
            "delegation_digest": self.delegation_digest,
            "reason": self.reason.value,
            "requested_capability": (
                self.requested_capability.value
                if self.requested_capability is not None
                else None
            ),
            "requested_action": self.requested_action,
            "summary": self.summary,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["escalation_digest"] = self.escalation_digest
        return payload


@dataclass(frozen=True, slots=True)
class OperatorContinuation:
    schema_version: int
    continuation_id: str
    created_at: str
    escalation_id: str
    escalation_digest: str
    decision: ContinuationDecision
    actor: str
    note: str | None
    continuation_digest: str

    @classmethod
    def create(
        cls,
        *,
        escalation: EscalationRequest,
        decision: ContinuationDecision | str,
        actor: str,
        note: str | None = None,
        continuation_id: str | None = None,
        created_at: str | None = None,
    ) -> "OperatorContinuation":
        if not isinstance(escalation, EscalationRequest):
            raise TypeError("escalation must be an EscalationRequest")
        continuation_id = continuation_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        normalized_decision = _normalize_continuation_decision(decision)
        normalized_actor = _bounded_text(actor, field_name="actor", max_chars=256)
        normalized_note = _optional_bounded_text(
            note,
            field_name="note",
            max_chars=MAX_OPERATOR_NOTE_CHARS,
        )
        payload = {
            "schema_version": DELEGATION_SCHEMA_VERSION,
            "continuation_id": continuation_id,
            "created_at": created_at,
            "escalation_id": escalation.escalation_id,
            "escalation_digest": escalation.escalation_digest,
            "decision": normalized_decision.value,
            "actor": normalized_actor,
            "note": normalized_note,
        }
        return cls(
            schema_version=DELEGATION_SCHEMA_VERSION,
            continuation_id=continuation_id,
            created_at=created_at,
            escalation_id=escalation.escalation_id,
            escalation_digest=escalation.escalation_digest,
            decision=normalized_decision,
            actor=normalized_actor,
            note=normalized_note,
            continuation_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        if self.schema_version != DELEGATION_SCHEMA_VERSION:
            raise InvalidDelegationError("Unsupported continuation schema version")
        _validate_uuid(self.continuation_id, "continuation_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.escalation_id, "escalation_id")
        _validate_sha256(self.escalation_digest, "escalation_digest")
        decision = _normalize_continuation_decision(self.decision)
        actor = _bounded_text(self.actor, field_name="actor", max_chars=256)
        note = _optional_bounded_text(
            self.note,
            field_name="note",
            max_chars=MAX_OPERATOR_NOTE_CHARS,
        )
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "note", note)
        _validate_sha256(self.continuation_digest, "continuation_digest")
        expected = _digest(self._payload())
        if not hmac.compare_digest(expected, self.continuation_digest):
            raise InvalidDelegationError("Continuation digest does not match payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "continuation_id": self.continuation_id,
            "created_at": self.created_at,
            "escalation_id": self.escalation_id,
            "escalation_digest": self.escalation_digest,
            "decision": self.decision.value,
            "actor": self.actor,
            "note": self.note,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["continuation_digest"] = self.continuation_digest
        return payload
