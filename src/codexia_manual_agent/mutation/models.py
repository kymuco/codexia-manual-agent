from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4


class MutationOperation(StrEnum):
    CREATE = "create"
    REPLACE = "replace"


class PreimageState(StrEnum):
    ABSENT = "absent"
    PRESENT = "present"


class MutationTerminationReason(StrEnum):
    APPLIED = "applied"
    PREIMAGE_CHANGED = "preimage_changed"
    TARGET_APPEARED = "target_appeared"
    TARGET_DISAPPEARED = "target_disappeared"
    BOUNDARY_CHANGED = "boundary_changed"
    WRITE_ERROR = "write_error"
    POSTIMAGE_MISMATCH = "postimage_mismatch"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: dict[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _validate_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest") from exc


@dataclass(frozen=True, slots=True)
class PreimageSnapshot:
    state: PreimageState
    size_bytes: int | None
    sha256: str | None
    mode: int | None

    @classmethod
    def absent(cls) -> "PreimageSnapshot":
        return cls(PreimageState.ABSENT, None, None, None)

    @classmethod
    def present(
        cls,
        *,
        size_bytes: int,
        digest: str,
        mode: int,
    ) -> "PreimageSnapshot":
        return cls(PreimageState.PRESENT, size_bytes, digest, mode)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", PreimageState(self.state))
        if self.state is PreimageState.ABSENT:
            if any(value is not None for value in (self.size_bytes, self.sha256, self.mode)):
                raise ValueError("Absent preimage cannot carry file metadata")
            return
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("Present preimage size_bytes must be a non-negative integer")
        if self.sha256 is None:
            raise ValueError("Present preimage requires sha256")
        _validate_sha256(self.sha256, "preimage sha256")
        if type(self.mode) is not int or self.mode < 0:
            raise ValueError("Present preimage mode must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceMutationObservation:
    schema_version: int
    observation_id: str
    created_at: str
    proposal_id: str
    proposal_digest: str
    receipt_id: str
    receipt_digest: str
    mutation_id: str
    operation: MutationOperation
    target: str
    expected_preimage: PreimageSnapshot
    observed_preimage: PreimageSnapshot
    applied: bool
    postimage_size_bytes: int | None
    postimage_sha256: str | None
    termination_reason: MutationTerminationReason
    error: str | None
    observation_digest: str

    @classmethod
    def create(
        cls,
        *,
        proposal_id: str,
        proposal_digest: str,
        receipt_id: str,
        receipt_digest: str,
        mutation_id: str,
        operation: MutationOperation,
        target: str,
        expected_preimage: PreimageSnapshot,
        observed_preimage: PreimageSnapshot,
        applied: bool,
        postimage_size_bytes: int | None,
        postimage_sha256: str | None,
        termination_reason: MutationTerminationReason,
        error: str | None = None,
        observation_id: str | None = None,
        created_at: str | None = None,
    ) -> "WorkspaceMutationObservation":
        observation_id = observation_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        payload = {
            "schema_version": 1,
            "observation_id": observation_id,
            "created_at": created_at,
            "proposal_id": proposal_id,
            "proposal_digest": proposal_digest,
            "receipt_id": receipt_id,
            "receipt_digest": receipt_digest,
            "mutation_id": mutation_id,
            "operation": MutationOperation(operation).value,
            "target": target,
            "expected_preimage": expected_preimage.to_dict(),
            "observed_preimage": observed_preimage.to_dict(),
            "applied": applied,
            "postimage_size_bytes": postimage_size_bytes,
            "postimage_sha256": postimage_sha256,
            "termination_reason": MutationTerminationReason(termination_reason).value,
            "error": error,
        }
        return cls(
            schema_version=1,
            observation_id=observation_id,
            created_at=created_at,
            proposal_id=proposal_id,
            proposal_digest=proposal_digest,
            receipt_id=receipt_id,
            receipt_digest=receipt_digest,
            mutation_id=mutation_id,
            operation=MutationOperation(operation),
            target=target,
            expected_preimage=expected_preimage,
            observed_preimage=observed_preimage,
            applied=bool(applied),
            postimage_size_bytes=postimage_size_bytes,
            postimage_sha256=postimage_sha256,
            termination_reason=MutationTerminationReason(termination_reason),
            error=error,
            observation_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Unsupported workspace mutation observation schema version")
        _validate_uuid(self.observation_id, "observation_id")
        _validate_uuid(self.proposal_id, "proposal_id")
        _validate_uuid(self.receipt_id, "receipt_id")
        _validate_sha256(self.proposal_digest, "proposal_digest")
        _validate_sha256(self.receipt_digest, "receipt_digest")
        if not isinstance(self.mutation_id, str) or not self.mutation_id.strip():
            raise ValueError("mutation_id must be non-empty")
        if not isinstance(self.target, str) or not self.target.strip():
            raise ValueError("target must be non-empty")
        if self.postimage_sha256 is not None:
            _validate_sha256(self.postimage_sha256, "postimage sha256")
        expected = _digest(self._payload())
        if not hmac.compare_digest(expected, self.observation_digest):
            raise ValueError("Workspace mutation observation digest does not match payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "created_at": self.created_at,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "receipt_id": self.receipt_id,
            "receipt_digest": self.receipt_digest,
            "mutation_id": self.mutation_id,
            "operation": self.operation.value,
            "target": self.target,
            "expected_preimage": self.expected_preimage.to_dict(),
            "observed_preimage": self.observed_preimage.to_dict(),
            "applied": self.applied,
            "postimage_size_bytes": self.postimage_size_bytes,
            "postimage_sha256": self.postimage_sha256,
            "termination_reason": self.termination_reason.value,
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["observation_digest"] = self.observation_digest
        return payload
