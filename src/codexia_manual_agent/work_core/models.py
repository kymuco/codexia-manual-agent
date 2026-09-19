from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

WORK_SCHEMA_VERSION = 1
WORK_INGRESS_SCHEMA_VERSION = 1
WORK_EVENT_SCHEMA_VERSION = 1

MAX_OBJECTIVE_CHARS = 16_384
MAX_SOURCE_NAMESPACE_CHARS = 128
MAX_SOURCE_ID_CHARS = 1_024
MAX_EVENT_KIND_CHARS = 128
MAX_EVENT_PAYLOAD_BYTES = 1_048_576
MAX_TIMESTAMP_CHARS = 64

WORK_COMPLETED_EVENT = "work.completed"
WORK_CANCELLED_EVENT = "work.cancelled"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_EVENT_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class InvalidWorkCoreRecord(ValueError):
    """Raised when a Gen2 work-core record is structurally invalid."""


class WorkState(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            _thaw_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidWorkCoreRecord("Work-core payload is not canonical JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        raise InvalidWorkCoreRecord("Work-core payload exceeds its byte budget")
    return encoded


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _freeze_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        _canonical_json(value)
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise InvalidWorkCoreRecord("JSON object keys must be strings")
        frozen = {key: _freeze_json(item) for key, item in value.items()}
        _canonical_json(frozen)
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, (list, tuple)):
        frozen = tuple(_freeze_json(item) for item in value)
        _canonical_json(frozen)
        return frozen
    raise InvalidWorkCoreRecord("Work-core payload must be JSON-compatible")


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidWorkCoreRecord(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidWorkCoreRecord(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidWorkCoreRecord(f"{field_name} must be lowercase hyphenated UUID")
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidWorkCoreRecord(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidWorkCoreRecord(f"{field_name} must be bounded canonical ISO-8601")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkCoreRecord(f"{field_name} must be canonical ISO-8601") from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidWorkCoreRecord(f"{field_name} must be canonical ISO-8601")
    return value


def _new_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: Any, field_name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidWorkCoreRecord(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars or "\x00" in normalized:
        raise InvalidWorkCoreRecord(f"{field_name} is empty or exceeds its text budget")
    return normalized


@dataclass(frozen=True, slots=True)
class WorkIngressBinding:
    """Exact idempotent binding from one external ingress identity to Work."""

    schema_version: int
    source_namespace: str
    source_id: str
    payload_digest: str
    binding_digest: str

    @classmethod
    def create(
        cls,
        *,
        source_namespace: str,
        source_id: str,
        payload_digest: str,
    ) -> "WorkIngressBinding":
        namespace = _bounded_text(
            source_namespace,
            "source_namespace",
            MAX_SOURCE_NAMESPACE_CHARS,
        ).lower()
        if _NAMESPACE_RE.fullmatch(namespace) is None:
            raise InvalidWorkCoreRecord("source_namespace is not canonical")
        source_id = _bounded_text(source_id, "source_id", MAX_SOURCE_ID_CHARS)
        _validate_digest(payload_digest, "payload_digest")
        base = {
            "schema_version": WORK_INGRESS_SCHEMA_VERSION,
            "source_namespace": namespace,
            "source_id": source_id,
            "payload_digest": payload_digest,
        }
        return cls(**base, binding_digest=_digest(base))

    def __post_init__(self) -> None:
        if self.schema_version != WORK_INGRESS_SCHEMA_VERSION:
            raise InvalidWorkCoreRecord("Unsupported WorkIngressBinding schema")
        namespace = _bounded_text(
            self.source_namespace,
            "source_namespace",
            MAX_SOURCE_NAMESPACE_CHARS,
        ).lower()
        if namespace != self.source_namespace or _NAMESPACE_RE.fullmatch(namespace) is None:
            raise InvalidWorkCoreRecord("source_namespace is not canonical")
        _bounded_text(self.source_id, "source_id", MAX_SOURCE_ID_CHARS)
        _validate_digest(self.payload_digest, "payload_digest")
        _validate_digest(self.binding_digest, "binding_digest")
        if not hmac.compare_digest(self.binding_digest, _digest(self._base_dict())):
            raise InvalidWorkCoreRecord("WorkIngressBinding digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_namespace": self.source_namespace,
            "source_id": self.source_id,
            "payload_digest": self.payload_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "binding_digest": self.binding_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkIngressBinding":
        return cls(
            schema_version=value["schema_version"],
            source_namespace=value["source_namespace"],
            source_id=value["source_id"],
            payload_digest=value["payload_digest"],
            binding_digest=value["binding_digest"],
        )


@dataclass(frozen=True, slots=True)
class Work:
    """Immutable origin record for one durable unit of delegated work."""

    schema_version: int
    work_id: str
    created_at: str
    objective: str
    ingress: WorkIngressBinding
    work_digest: str

    @classmethod
    def create(
        cls,
        *,
        objective: str,
        ingress: WorkIngressBinding,
        work_id: str | None = None,
        created_at: str | None = None,
    ) -> "Work":
        if not isinstance(ingress, WorkIngressBinding):
            raise TypeError("ingress must be WorkIngressBinding")
        work_id = work_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(work_id, "work_id")
        _validate_timestamp(created_at, "created_at")
        objective = _bounded_text(objective, "objective", MAX_OBJECTIVE_CHARS)
        base = {
            "schema_version": WORK_SCHEMA_VERSION,
            "work_id": work_id,
            "created_at": created_at,
            "objective": objective,
            "ingress": ingress.to_dict(),
        }
        return cls(
            schema_version=WORK_SCHEMA_VERSION,
            work_id=work_id,
            created_at=created_at,
            objective=objective,
            ingress=ingress,
            work_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != WORK_SCHEMA_VERSION:
            raise InvalidWorkCoreRecord("Unsupported Work schema")
        _validate_uuid(self.work_id, "work_id")
        _validate_timestamp(self.created_at, "created_at")
        objective = _bounded_text(self.objective, "objective", MAX_OBJECTIVE_CHARS)
        if objective != self.objective:
            raise InvalidWorkCoreRecord("objective must be canonical trimmed text")
        if not isinstance(self.ingress, WorkIngressBinding):
            raise InvalidWorkCoreRecord("ingress must be WorkIngressBinding")
        _validate_digest(self.work_digest, "work_digest")
        if not hmac.compare_digest(self.work_digest, _digest(self._base_dict())):
            raise InvalidWorkCoreRecord("Work digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "work_id": self.work_id,
            "created_at": self.created_at,
            "objective": self.objective,
            "ingress": self.ingress.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "work_digest": self.work_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Work":
        return cls(
            schema_version=value["schema_version"],
            work_id=value["work_id"],
            created_at=value["created_at"],
            objective=value["objective"],
            ingress=WorkIngressBinding.from_dict(value["ingress"]),
            work_digest=value["work_digest"],
        )

    def same_ingress_semantics(self, other: "Work") -> bool:
        return (
            isinstance(other, Work)
            and self.objective == other.objective
            and self.ingress.to_dict() == other.ingress.to_dict()
        )


@dataclass(frozen=True, slots=True)
class WorkEvent:
    """One admitted append-only fact in a Work chronology."""

    schema_version: int
    event_id: str
    work_id: str
    sequence: int
    created_at: str
    kind: str
    payload: Mapping[str, Any]
    previous_event_digest: str | None
    event_digest: str

    @classmethod
    def create(
        cls,
        *,
        work_id: str,
        sequence: int,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        previous_event_digest: str | None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> "WorkEvent":
        event_id = event_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(event_id, "event_id")
        _validate_uuid(work_id, "work_id")
        if type(sequence) is not int or sequence <= 0:
            raise InvalidWorkCoreRecord("sequence must be a positive integer")
        _validate_timestamp(created_at, "created_at")
        kind = _bounded_text(kind, "kind", MAX_EVENT_KIND_CHARS).lower()
        if _EVENT_KIND_RE.fullmatch(kind) is None:
            raise InvalidWorkCoreRecord("kind is not canonical")
        if previous_event_digest is not None:
            _validate_digest(previous_event_digest, "previous_event_digest")
        frozen_payload = _freeze_json({} if payload is None else payload)
        base = {
            "schema_version": WORK_EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "work_id": work_id,
            "sequence": sequence,
            "created_at": created_at,
            "kind": kind,
            "payload": frozen_payload,
            "previous_event_digest": previous_event_digest,
        }
        return cls(
            schema_version=WORK_EVENT_SCHEMA_VERSION,
            event_id=event_id,
            work_id=work_id,
            sequence=sequence,
            created_at=created_at,
            kind=kind,
            payload=frozen_payload,
            previous_event_digest=previous_event_digest,
            event_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != WORK_EVENT_SCHEMA_VERSION:
            raise InvalidWorkCoreRecord("Unsupported WorkEvent schema")
        _validate_uuid(self.event_id, "event_id")
        _validate_uuid(self.work_id, "work_id")
        if type(self.sequence) is not int or self.sequence <= 0:
            raise InvalidWorkCoreRecord("sequence must be a positive integer")
        _validate_timestamp(self.created_at, "created_at")
        kind = _bounded_text(self.kind, "kind", MAX_EVENT_KIND_CHARS).lower()
        if kind != self.kind or _EVENT_KIND_RE.fullmatch(kind) is None:
            raise InvalidWorkCoreRecord("kind is not canonical")
        payload = _freeze_json(self.payload)
        object.__setattr__(self, "payload", payload)
        if self.previous_event_digest is not None:
            _validate_digest(self.previous_event_digest, "previous_event_digest")
        _validate_digest(self.event_digest, "event_digest")
        if not hmac.compare_digest(self.event_digest, _digest(self._base_dict())):
            raise InvalidWorkCoreRecord("WorkEvent digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "work_id": self.work_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "kind": self.kind,
            "payload": self.payload,
            "previous_event_digest": self.previous_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**_thaw_json(self._base_dict()), "event_digest": self.event_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkEvent":
        return cls(
            schema_version=value["schema_version"],
            event_id=value["event_id"],
            work_id=value["work_id"],
            sequence=value["sequence"],
            created_at=value["created_at"],
            kind=value["kind"],
            payload=value["payload"],
            previous_event_digest=value["previous_event_digest"],
            event_digest=value["event_digest"],
        )


@dataclass(frozen=True, slots=True)
class WorkSnapshot:
    """Derived projection of one exact Work chronology revision."""

    work: Work
    state: WorkState
    revision: int
    last_event_digest: str | None
    terminal_event_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.work, Work):
            raise TypeError("work must be Work")
        object.__setattr__(self, "state", WorkState(self.state))
        if type(self.revision) is not int or self.revision < 0:
            raise InvalidWorkCoreRecord("revision must be a non-negative integer")
        if self.last_event_digest is None:
            if self.revision != 0:
                raise InvalidWorkCoreRecord("nonzero revision requires last_event_digest")
        else:
            _validate_digest(self.last_event_digest, "last_event_digest")
            if self.revision == 0:
                raise InvalidWorkCoreRecord("revision zero cannot have last_event_digest")
        if self.state is WorkState.ACTIVE:
            if self.terminal_event_id is not None:
                raise InvalidWorkCoreRecord("active Work cannot have terminal_event_id")
        else:
            if self.terminal_event_id is None:
                raise InvalidWorkCoreRecord("terminal Work requires terminal_event_id")
            _validate_uuid(self.terminal_event_id, "terminal_event_id")

    def next_event(
        self,
        *,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> WorkEvent:
        if self.state is not WorkState.ACTIVE:
            raise InvalidWorkCoreRecord("terminal Work cannot prepare another event")
        return WorkEvent.create(
            work_id=self.work.work_id,
            sequence=self.revision + 1,
            kind=kind,
            payload=payload,
            previous_event_digest=self.last_event_digest,
            event_id=event_id,
            created_at=created_at,
        )
