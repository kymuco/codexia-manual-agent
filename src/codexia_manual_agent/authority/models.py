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

from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import ActionIntegrityError


class ApprovalMode(StrEnum):
    ALWAYS = "always"
    RISKY = "risky"
    NEVER = "never"


class ActionRisk(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_MUTATION = "workspace_mutation"
    PROCESS_EXECUTION = "process_execution"
    NETWORK_ACCESS = "network_access"
    EXTERNAL_GIT = "external_git"
    DESTRUCTIVE = "destructive"
    OUTSIDE_WORKSPACE = "outside_workspace"


class ApprovalRequirement(StrEnum):
    AUTO_AUTHORIZE = "auto_authorize"
    REQUIRE_HUMAN = "require_human"
    DENY = "deny"


class AuthorizationDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class AuthorizationSource(StrEnum):
    POLICY = "policy"
    HUMAN = "human"


class ActionPhase(StrEnum):
    PROPOSED = "proposed"
    AUTHORIZED = "authorized"
    EXECUTED = "executed"
    OBSERVED = "observed"
    DENIED = "denied"


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ActionIntegrityError("Action parameters cannot contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ActionIntegrityError("Action parameter object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ActionIntegrityError(
        f"Action parameters must be JSON-compatible, got {type(value).__name__}"
    )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest_payload(payload: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ActionIntegrityError(f"{field_name} must be a UUID") from exc


def _validate_timestamp(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ActionIntegrityError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ActionIntegrityError(f"{field_name} must include a timezone")


def _validate_sha256_hex(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ActionIntegrityError(f"{field_name} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ActionIntegrityError(
            f"{field_name} must be a SHA-256 hex digest"
        ) from exc


@dataclass(frozen=True, slots=True)
class ActionProposal:
    schema_version: int
    proposal_id: str
    created_at: str
    capability: Capability
    action: str
    workspace_root: str
    parameters: Mapping[str, Any]
    summary: str | None
    proposal_digest: str

    @classmethod
    def create(
        cls,
        *,
        capability: Capability,
        action: str,
        workspace_root: str,
        parameters: Mapping[str, Any] | None = None,
        summary: str | None = None,
        proposal_id: str | None = None,
        created_at: str | None = None,
    ) -> "ActionProposal":
        proposal_id = proposal_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        frozen_parameters = _freeze_json(parameters or {})
        payload = {
            "schema_version": 1,
            "proposal_id": proposal_id,
            "created_at": created_at,
            "capability": Capability(capability).value,
            "action": action,
            "workspace_root": workspace_root,
            "parameters": frozen_parameters,
            "summary": summary,
        }
        digest = _digest_payload(payload)
        return cls(
            schema_version=1,
            proposal_id=proposal_id,
            created_at=created_at,
            capability=Capability(capability),
            action=action,
            workspace_root=workspace_root,
            parameters=frozen_parameters,
            summary=summary,
            proposal_digest=digest,
        )

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ActionIntegrityError("Unsupported action proposal schema version")
        _validate_uuid(self.proposal_id, "proposal_id")
        _validate_timestamp(self.created_at, "created_at")
        if not isinstance(self.action, str) or not self.action.strip():
            raise ActionIntegrityError("action must be a non-empty string")
        if not isinstance(self.workspace_root, str) or not self.workspace_root.strip():
            raise ActionIntegrityError("workspace_root must be a non-empty string")
        if self.summary is not None and not isinstance(self.summary, str):
            raise ActionIntegrityError("summary must be a string or null")

        capability = Capability(self.capability)
        frozen_parameters = _freeze_json(self.parameters)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "parameters", frozen_parameters)

        _validate_sha256_hex(self.proposal_digest, "proposal_digest")
        expected = _digest_payload(self._payload())
        if not hmac.compare_digest(expected, self.proposal_digest):
            raise ActionIntegrityError("Action proposal digest does not match payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "created_at": self.created_at,
            "capability": self.capability.value,
            "action": self.action,
            "workspace_root": self.workspace_root,
            "parameters": self.parameters,
            "summary": self.summary,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = _thaw_json(self._payload())
        payload["proposal_digest"] = self.proposal_digest
        return payload


@dataclass(frozen=True, slots=True)
class AuthorizationReceipt:
    schema_version: int
    receipt_id: str
    created_at: str
    proposal_id: str
    proposal_digest: str
    decision: AuthorizationDecision
    mode: ApprovalMode
    source: AuthorizationSource
    actor: str
    reason: str | None
    single_use: bool
    receipt_digest: str

    @classmethod
    def issue(
        cls,
        *,
        proposal: ActionProposal,
        decision: AuthorizationDecision,
        mode: ApprovalMode,
        source: AuthorizationSource,
        actor: str,
        reason: str | None = None,
        receipt_id: str | None = None,
        created_at: str | None = None,
    ) -> "AuthorizationReceipt":
        receipt_id = receipt_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        payload = {
            "schema_version": 1,
            "receipt_id": receipt_id,
            "created_at": created_at,
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "decision": AuthorizationDecision(decision).value,
            "mode": ApprovalMode(mode).value,
            "source": AuthorizationSource(source).value,
            "actor": actor,
            "reason": reason,
            "single_use": True,
        }
        digest = _digest_payload(payload)
        return cls(
            schema_version=1,
            receipt_id=receipt_id,
            created_at=created_at,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            decision=AuthorizationDecision(decision),
            mode=ApprovalMode(mode),
            source=AuthorizationSource(source),
            actor=actor,
            reason=reason,
            single_use=True,
            receipt_digest=digest,
        )

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ActionIntegrityError("Unsupported authorization receipt schema version")
        _validate_uuid(self.receipt_id, "receipt_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.proposal_id, "proposal_id")
        _validate_sha256_hex(self.proposal_digest, "proposal_digest")
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ActionIntegrityError("actor must be a non-empty string")
        if self.reason is not None and not isinstance(self.reason, str):
            raise ActionIntegrityError("reason must be a string or null")
        if self.single_use is not True:
            raise ActionIntegrityError("M2.0 authorization receipts must be single-use")

        object.__setattr__(self, "decision", AuthorizationDecision(self.decision))
        object.__setattr__(self, "mode", ApprovalMode(self.mode))
        object.__setattr__(self, "source", AuthorizationSource(self.source))

        _validate_sha256_hex(self.receipt_digest, "receipt_digest")
        expected = _digest_payload(self._payload())
        if not hmac.compare_digest(expected, self.receipt_digest):
            raise ActionIntegrityError("Authorization receipt digest does not match payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "created_at": self.created_at,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "decision": self.decision.value,
            "mode": self.mode.value,
            "source": self.source.value,
            "actor": self.actor,
            "reason": self.reason,
            "single_use": self.single_use,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["receipt_digest"] = self.receipt_digest
        return payload
