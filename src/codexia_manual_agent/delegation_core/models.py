from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Mapping
from uuid import UUID, uuid4

from codexia_manual_agent.work_core import (
    Work,
    WorkEvent,
    WorkIngressBinding,
    WorkSnapshot,
    WorkState,
)

DELEGATION_SCHEMA_VERSION = 1
DELEGATION_CHILD_OWNED_EVENT = "delegation.child-owned"
DELEGATION_INGRESS_NAMESPACE = "codexia.delegation"
MAX_TIMESTAMP_CHARS = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidDelegationRecord(ValueError):
    """Raised when a Gen2 durable delegation record is structurally invalid."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidDelegationRecord("Delegation record is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidDelegationRecord(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidDelegationRecord(
            f"{field_name} must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise InvalidDelegationRecord(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidDelegationRecord(
            f"{field_name} must be lowercase SHA-256 hex"
        )
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidDelegationRecord(
            f"{field_name} must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidDelegationRecord(
            f"{field_name} must be canonical ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidDelegationRecord(
            f"{field_name} must be canonical ISO-8601"
        )
    return value


def _new_timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Delegation:
    """Exact durable ownership relation from one parent Work to one child Work.

    The child has its own Work identity and lifecycle. This record grants no
    authority and carries no scheduler, budget, role, or completion semantics.
    """

    schema_version: int
    delegation_id: str
    created_at: str
    parent_work_id: str
    parent_work_digest: str
    child_work: Work
    start_revision: int
    start_event_digest: str | None
    delegation_digest: str

    @classmethod
    def create(
        cls,
        *,
        parent: WorkSnapshot,
        child_objective: str,
        delegation_id: str | None = None,
        child_work_id: str | None = None,
        created_at: str | None = None,
    ) -> Delegation:
        if not isinstance(parent, WorkSnapshot):
            raise TypeError("parent must be WorkSnapshot")
        if parent.state is not WorkState.ACTIVE:
            raise InvalidDelegationRecord(
                "Delegation requires active parent Work"
            )
        delegation_id = delegation_id or str(uuid4())
        child_work_id = child_work_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(delegation_id, "delegation_id")
        _validate_uuid(child_work_id, "child_work_id")
        _validate_timestamp(created_at, "created_at")
        if child_work_id == parent.work.work_id:
            raise InvalidDelegationRecord("Child Work cannot be its own parent")

        ingress_payload = {
            "schema_version": DELEGATION_SCHEMA_VERSION,
            "delegation_id": delegation_id,
            "parent_work_id": parent.work.work_id,
            "parent_work_digest": parent.work.work_digest,
            "child_objective": child_objective,
        }
        child_ingress = WorkIngressBinding.create(
            source_namespace=DELEGATION_INGRESS_NAMESPACE,
            source_id=delegation_id,
            payload_digest=_digest(ingress_payload),
        )
        child_work = Work.create(
            objective=child_objective,
            ingress=child_ingress,
            work_id=child_work_id,
            created_at=created_at,
        )
        base = {
            "schema_version": DELEGATION_SCHEMA_VERSION,
            "delegation_id": delegation_id,
            "created_at": created_at,
            "parent_work_id": parent.work.work_id,
            "parent_work_digest": parent.work.work_digest,
            "child_work": child_work.to_dict(),
            "start_revision": parent.revision,
            "start_event_digest": parent.last_event_digest,
        }
        return cls(**base, delegation_digest=_digest(base))

    def __post_init__(self) -> None:
        if self.schema_version != DELEGATION_SCHEMA_VERSION:
            raise InvalidDelegationRecord("Unsupported Delegation schema")
        _validate_uuid(self.delegation_id, "delegation_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.parent_work_id, "parent_work_id")
        _validate_digest(self.parent_work_digest, "parent_work_digest")
        if not isinstance(self.child_work, Work):
            raise InvalidDelegationRecord("child_work must be Work")
        if self.child_work.work_id == self.parent_work_id:
            raise InvalidDelegationRecord("Child Work cannot equal parent Work")
        if self.child_work.created_at != self.created_at:
            raise InvalidDelegationRecord(
                "child Work timestamp must equal delegation timestamp"
            )
        if self.child_work.ingress.source_namespace != DELEGATION_INGRESS_NAMESPACE:
            raise InvalidDelegationRecord(
                "child Work ingress must be Codexia delegation ingress"
            )
        if self.child_work.ingress.source_id != self.delegation_id:
            raise InvalidDelegationRecord(
                "child Work ingress identity must equal delegation_id"
            )
        expected_ingress = _digest(
            {
                "schema_version": DELEGATION_SCHEMA_VERSION,
                "delegation_id": self.delegation_id,
                "parent_work_id": self.parent_work_id,
                "parent_work_digest": self.parent_work_digest,
                "child_objective": self.child_work.objective,
            }
        )
        if not hmac.compare_digest(
            self.child_work.ingress.payload_digest,
            expected_ingress,
        ):
            raise InvalidDelegationRecord(
                "child Work ingress does not bind exact parent/objective"
            )
        if type(self.start_revision) is not int or self.start_revision < 0:
            raise InvalidDelegationRecord(
                "start_revision must be non-negative integer"
            )
        if self.start_event_digest is None:
            if self.start_revision != 0:
                raise InvalidDelegationRecord(
                    "nonzero start_revision requires start_event_digest"
                )
        else:
            _validate_digest(self.start_event_digest, "start_event_digest")
            if self.start_revision == 0:
                raise InvalidDelegationRecord(
                    "revision zero cannot have start_event_digest"
                )
        _validate_digest(self.delegation_digest, "delegation_digest")
        if not hmac.compare_digest(
            self.delegation_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidDelegationRecord("Delegation digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "delegation_id": self.delegation_id,
            "created_at": self.created_at,
            "parent_work_id": self.parent_work_id,
            "parent_work_digest": self.parent_work_digest,
            "child_work": self.child_work.to_dict(),
            "start_revision": self.start_revision,
            "start_event_digest": self.start_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "delegation_digest": self.delegation_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Delegation:
        expected = {
            "schema_version",
            "delegation_id",
            "created_at",
            "parent_work_id",
            "parent_work_digest",
            "child_work",
            "start_revision",
            "start_event_digest",
            "delegation_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise InvalidDelegationRecord("Delegation keys are not exact")
        try:
            child_work = Work.from_dict(value["child_work"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidDelegationRecord(
                "Delegation child Work is invalid"
            ) from exc
        return cls(
            schema_version=value["schema_version"],
            delegation_id=value["delegation_id"],
            created_at=value["created_at"],
            parent_work_id=value["parent_work_id"],
            parent_work_digest=value["parent_work_digest"],
            child_work=child_work,
            start_revision=value["start_revision"],
            start_event_digest=value["start_event_digest"],
            delegation_digest=value["delegation_digest"],
        )

    def to_parent_event(self) -> WorkEvent:
        return WorkEvent.create(
            work_id=self.parent_work_id,
            sequence=self.start_revision + 1,
            kind=DELEGATION_CHILD_OWNED_EVENT,
            payload={"delegation": self.to_dict()},
            previous_event_digest=self.start_event_digest,
            event_id=self.delegation_id,
            created_at=self.created_at,
        )
