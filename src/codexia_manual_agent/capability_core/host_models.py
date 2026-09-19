from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from codexia_manual_agent.capability_core.models import (
    CapabilityNeedSnapshot,
    CapabilityNeedState,
)
from codexia_manual_agent.work_core import WorkEvent, WorkSnapshot, WorkState

CAPABILITY_HANDOFF_SCHEMA_VERSION = 1
CAPABILITY_HANDOFF_ADMITTED_EVENT = "capability.handoff-admitted"

MAX_HOST_ID_CHARS = 128
MAX_TIMESTAMP_CHARS = 64

_HOST_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidCapabilityHostRecord(ValueError):
    """Raised when a G2.5 host-boundary record is structurally invalid."""


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
        raise InvalidCapabilityHostRecord(
            "Capability host record is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    record_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise InvalidCapabilityHostRecord(f"{record_name} keys are not exact")
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidCapabilityHostRecord(
            f"{field_name} must be a canonical UUID"
        )
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidCapabilityHostRecord(
            f"{field_name} must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise InvalidCapabilityHostRecord(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidCapabilityHostRecord(
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
        raise InvalidCapabilityHostRecord(
            f"{field_name} must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidCapabilityHostRecord(
            f"{field_name} must be canonical ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidCapabilityHostRecord(
            f"{field_name} must be canonical ISO-8601"
        )
    return value


def _canonical_host_id(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidCapabilityHostRecord("host_id must be text")
    normalized = value.strip().lower()
    if (
        normalized != value
        or len(normalized) > MAX_HOST_ID_CHARS
        or _HOST_ID_RE.fullmatch(normalized) is None
    ):
        raise InvalidCapabilityHostRecord("host_id is not canonical")
    return normalized


def _new_timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class CapabilityHandoff:
    """Durable admission of one exact CapabilityNeed to one selected host.

    A handoff records routing intent only. It does not prove that the host
    received the request, that authority existed, that an attempt started, or
    that any effect occurred.
    """

    schema_version: int
    handoff_id: str
    created_at: str
    host_id: str
    need_id: str
    need_digest: str
    work_id: str
    work_digest: str
    start_revision: int
    start_event_digest: str | None
    handoff_digest: str

    @classmethod
    def create(
        cls,
        *,
        need: CapabilityNeedSnapshot,
        snapshot: WorkSnapshot,
        host_id: str,
        handoff_id: str | None = None,
        created_at: str | None = None,
    ) -> CapabilityHandoff:
        if not isinstance(need, CapabilityNeedSnapshot):
            raise TypeError("need must be CapabilityNeedSnapshot")
        if need.state is not CapabilityNeedState.PENDING:
            raise InvalidCapabilityHostRecord(
                "terminal CapabilityNeed cannot receive a host handoff"
            )
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidCapabilityHostRecord(
                "CapabilityHandoff cannot be admitted to terminal Work"
            )
        source = need.need
        if snapshot.work.work_id != source.work_id:
            raise InvalidCapabilityHostRecord(
                "CapabilityNeed belongs to another Work"
            )
        if not hmac.compare_digest(
            snapshot.work.work_digest,
            source.work_digest,
        ):
            raise InvalidCapabilityHostRecord(
                "CapabilityNeed Work binding changed"
            )

        host_id = _canonical_host_id(host_id)
        handoff_id = handoff_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(handoff_id, "handoff_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": CAPABILITY_HANDOFF_SCHEMA_VERSION,
            "handoff_id": handoff_id,
            "created_at": created_at,
            "host_id": host_id,
            "need_id": source.need_id,
            "need_digest": source.need_digest,
            "work_id": source.work_id,
            "work_digest": source.work_digest,
            "start_revision": snapshot.revision,
            "start_event_digest": snapshot.last_event_digest,
        }
        return cls(**base, handoff_digest=_digest(base))

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_HANDOFF_SCHEMA_VERSION:
            raise InvalidCapabilityHostRecord("Unsupported CapabilityHandoff schema")
        _validate_uuid(self.handoff_id, "handoff_id")
        _validate_timestamp(self.created_at, "created_at")
        _canonical_host_id(self.host_id)
        _validate_uuid(self.need_id, "need_id")
        _validate_digest(self.need_digest, "need_digest")
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        if type(self.start_revision) is not int or self.start_revision < 0:
            raise InvalidCapabilityHostRecord(
                "start_revision must be non-negative integer"
            )
        if self.start_event_digest is None:
            if self.start_revision != 0:
                raise InvalidCapabilityHostRecord(
                    "nonzero start_revision requires start_event_digest"
                )
        else:
            _validate_digest(self.start_event_digest, "start_event_digest")
            if self.start_revision == 0:
                raise InvalidCapabilityHostRecord(
                    "revision zero cannot have start_event_digest"
                )
        _validate_digest(self.handoff_digest, "handoff_digest")
        if not hmac.compare_digest(
            self.handoff_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidCapabilityHostRecord("CapabilityHandoff digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "handoff_id": self.handoff_id,
            "created_at": self.created_at,
            "host_id": self.host_id,
            "need_id": self.need_id,
            "need_digest": self.need_digest,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "start_revision": self.start_revision,
            "start_event_digest": self.start_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "handoff_digest": self.handoff_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityHandoff:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "handoff_id",
                "created_at",
                "host_id",
                "need_id",
                "need_digest",
                "work_id",
                "work_digest",
                "start_revision",
                "start_event_digest",
                "handoff_digest",
            },
            "CapabilityHandoff",
        )
        return cls(
            schema_version=value["schema_version"],
            handoff_id=value["handoff_id"],
            created_at=value["created_at"],
            host_id=value["host_id"],
            need_id=value["need_id"],
            need_digest=value["need_digest"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            start_revision=value["start_revision"],
            start_event_digest=value["start_event_digest"],
            handoff_digest=value["handoff_digest"],
        )

    def to_event(self) -> WorkEvent:
        return WorkEvent.create(
            work_id=self.work_id,
            sequence=self.start_revision + 1,
            kind=CAPABILITY_HANDOFF_ADMITTED_EVENT,
            payload={"capability_handoff": self.to_dict()},
            previous_event_digest=self.start_event_digest,
            event_id=self.handoff_id,
            created_at=self.created_at,
        )


@dataclass(frozen=True, slots=True)
class CapabilityHostRequest:
    """Transient exact request delivered to one host port.

    The request contains semantic Need data and routing identity only. It carries
    no authorization object, permission token, execution handle, or retry grant.
    """

    handoff: CapabilityHandoff
    need: CapabilityNeedSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, CapabilityHandoff):
            raise TypeError("handoff must be CapabilityHandoff")
        if not isinstance(self.need, CapabilityNeedSnapshot):
            raise TypeError("need must be CapabilityNeedSnapshot")
        if self.need.state is not CapabilityNeedState.PENDING:
            raise InvalidCapabilityHostRecord(
                "CapabilityHostRequest requires pending CapabilityNeed"
            )
        source = self.need.need
        if self.handoff.need_id != source.need_id:
            raise InvalidCapabilityHostRecord(
                "CapabilityHostRequest changed Need identity"
            )
        if not hmac.compare_digest(
            self.handoff.need_digest,
            source.need_digest,
        ):
            raise InvalidCapabilityHostRecord(
                "CapabilityHostRequest changed Need binding"
            )
        if self.handoff.work_id != source.work_id:
            raise InvalidCapabilityHostRecord(
                "CapabilityHostRequest changed Work identity"
            )
        if not hmac.compare_digest(
            self.handoff.work_digest,
            source.work_digest,
        ):
            raise InvalidCapabilityHostRecord(
                "CapabilityHostRequest changed Work binding"
            )
