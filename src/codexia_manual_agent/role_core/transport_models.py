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

from codexia_manual_agent.role_core.models import (
    CognitionRequest,
    RoleRunSnapshot,
    RoleRunState,
)
from codexia_manual_agent.work_core import WorkEvent, WorkSnapshot, WorkState

COGNITION_HANDOFF_SCHEMA_VERSION = 1
COGNITION_HANDOFF_ADMITTED_EVENT = "role.cognition-handoff-admitted"

MAX_PORT_ID_CHARS = 128
MAX_TIMESTAMP_CHARS = 64

_PORT_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidCognitionTransportRecord(ValueError):
    """Raised when a G2.13 cognition transport record is invalid."""


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
        raise InvalidCognitionTransportRecord(
            "Cognition transport record is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    record_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise InvalidCognitionTransportRecord(
            f"{record_name} keys are not exact"
        )
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidCognitionTransportRecord(
            f"{field_name} must be a canonical UUID"
        )
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidCognitionTransportRecord(
            f"{field_name} must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise InvalidCognitionTransportRecord(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidCognitionTransportRecord(
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
        raise InvalidCognitionTransportRecord(
            f"{field_name} must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidCognitionTransportRecord(
            f"{field_name} must be canonical ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidCognitionTransportRecord(
            f"{field_name} must be canonical ISO-8601"
        )
    return value


def _canonical_port_id(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidCognitionTransportRecord("port_id must be text")
    normalized = value.strip().lower()
    if (
        normalized != value
        or len(normalized) > MAX_PORT_ID_CHARS
        or _PORT_ID_RE.fullmatch(normalized) is None
    ):
        raise InvalidCognitionTransportRecord("port_id is not canonical")
    return normalized


def _new_timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class CognitionHandoff:
    """Durable routing of one exact admitted CognitionRequest to one port.

    The handoff does not prove provider receipt, attempt start, model execution,
    response existence, or retry permission.
    """

    schema_version: int
    handoff_id: str
    created_at: str
    port_id: str
    request_id: str
    request_digest: str
    role_run_id: str
    role_run_digest: str
    work_id: str
    work_digest: str
    workflow_run_id: str
    workflow_run_digest: str
    start_revision: int
    start_event_digest: str | None
    handoff_digest: str

    @classmethod
    def create(
        cls,
        *,
        role: RoleRunSnapshot,
        snapshot: WorkSnapshot,
        port_id: str,
        handoff_id: str | None = None,
        created_at: str | None = None,
    ) -> CognitionHandoff:
        if not isinstance(role, RoleRunSnapshot):
            raise TypeError("role must be RoleRunSnapshot")
        if role.state is not RoleRunState.REQUESTED:
            raise InvalidCognitionTransportRecord(
                "CognitionHandoff requires REQUESTED RoleRun"
            )
        if role.request_id is None or role.request_digest is None:
            raise InvalidCognitionTransportRecord(
                "REQUESTED RoleRun lacks cognition request identity"
            )
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidCognitionTransportRecord(
                "CognitionHandoff cannot be admitted to terminal Work"
            )

        run = role.run
        if snapshot.work.work_id != run.work_id:
            raise InvalidCognitionTransportRecord(
                "RoleRun belongs to another Work"
            )
        if not hmac.compare_digest(
            snapshot.work.work_digest,
            run.work_digest,
        ):
            raise InvalidCognitionTransportRecord(
                "RoleRun Work binding changed"
            )

        port_id = _canonical_port_id(port_id)
        handoff_id = handoff_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(handoff_id, "handoff_id")
        _validate_timestamp(created_at, "created_at")

        base = {
            "schema_version": COGNITION_HANDOFF_SCHEMA_VERSION,
            "handoff_id": handoff_id,
            "created_at": created_at,
            "port_id": port_id,
            "request_id": role.request_id,
            "request_digest": role.request_digest,
            "role_run_id": run.role_run_id,
            "role_run_digest": run.run_digest,
            "work_id": run.work_id,
            "work_digest": run.work_digest,
            "workflow_run_id": run.workflow_run_id,
            "workflow_run_digest": run.workflow_run_digest,
            "start_revision": snapshot.revision,
            "start_event_digest": snapshot.last_event_digest,
        }
        return cls(**base, handoff_digest=_digest(base))

    def __post_init__(self) -> None:
        if self.schema_version != COGNITION_HANDOFF_SCHEMA_VERSION:
            raise InvalidCognitionTransportRecord(
                "Unsupported CognitionHandoff schema"
            )
        _validate_uuid(self.handoff_id, "handoff_id")
        _validate_timestamp(self.created_at, "created_at")
        _canonical_port_id(self.port_id)
        _validate_uuid(self.request_id, "request_id")
        _validate_digest(self.request_digest, "request_digest")
        _validate_uuid(self.role_run_id, "role_run_id")
        _validate_digest(self.role_run_digest, "role_run_digest")
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_digest(self.workflow_run_digest, "workflow_run_digest")
        if type(self.start_revision) is not int or self.start_revision < 0:
            raise InvalidCognitionTransportRecord(
                "start_revision must be non-negative integer"
            )
        if self.start_event_digest is None:
            if self.start_revision != 0:
                raise InvalidCognitionTransportRecord(
                    "nonzero start_revision requires start_event_digest"
                )
        else:
            _validate_digest(self.start_event_digest, "start_event_digest")
            if self.start_revision == 0:
                raise InvalidCognitionTransportRecord(
                    "revision zero cannot have start_event_digest"
                )
        _validate_digest(self.handoff_digest, "handoff_digest")
        if not hmac.compare_digest(
            self.handoff_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidCognitionTransportRecord(
                "CognitionHandoff digest mismatch"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "handoff_id": self.handoff_id,
            "created_at": self.created_at,
            "port_id": self.port_id,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "role_run_id": self.role_run_id,
            "role_run_digest": self.role_run_digest,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_digest": self.workflow_run_digest,
            "start_revision": self.start_revision,
            "start_event_digest": self.start_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "handoff_digest": self.handoff_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CognitionHandoff:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "handoff_id",
                "created_at",
                "port_id",
                "request_id",
                "request_digest",
                "role_run_id",
                "role_run_digest",
                "work_id",
                "work_digest",
                "workflow_run_id",
                "workflow_run_digest",
                "start_revision",
                "start_event_digest",
                "handoff_digest",
            },
            "CognitionHandoff",
        )
        return cls(
            schema_version=value["schema_version"],
            handoff_id=value["handoff_id"],
            created_at=value["created_at"],
            port_id=value["port_id"],
            request_id=value["request_id"],
            request_digest=value["request_digest"],
            role_run_id=value["role_run_id"],
            role_run_digest=value["role_run_digest"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            workflow_run_id=value["workflow_run_id"],
            workflow_run_digest=value["workflow_run_digest"],
            start_revision=value["start_revision"],
            start_event_digest=value["start_event_digest"],
            handoff_digest=value["handoff_digest"],
        )

    def to_event(self) -> WorkEvent:
        return WorkEvent.create(
            work_id=self.work_id,
            sequence=self.start_revision + 1,
            kind=COGNITION_HANDOFF_ADMITTED_EVENT,
            payload={"cognition_handoff": self.to_dict()},
            previous_event_digest=self.start_event_digest,
            event_id=self.handoff_id,
            created_at=self.created_at,
        )


@dataclass(frozen=True, slots=True)
class CognitionPortRequest:
    """Transient exact request delivered to one selected CognitionPort."""

    handoff: CognitionHandoff
    request: CognitionRequest

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, CognitionHandoff):
            raise TypeError("handoff must be CognitionHandoff")
        if not isinstance(self.request, CognitionRequest):
            raise TypeError("request must be CognitionRequest")
        if self.handoff.request_id != self.request.request_id:
            raise InvalidCognitionTransportRecord(
                "CognitionPortRequest changed request identity"
            )
        if not hmac.compare_digest(
            self.handoff.request_digest,
            self.request.request_digest,
        ):
            raise InvalidCognitionTransportRecord(
                "CognitionPortRequest changed request binding"
            )
        if self.handoff.role_run_id != self.request.role_run_id:
            raise InvalidCognitionTransportRecord(
                "CognitionPortRequest changed RoleRun identity"
            )
        if not hmac.compare_digest(
            self.handoff.role_run_digest,
            self.request.role_run_digest,
        ):
            raise InvalidCognitionTransportRecord(
                "CognitionPortRequest changed RoleRun binding"
            )
        if self.handoff.work_id != self.request.work_id:
            raise InvalidCognitionTransportRecord(
                "CognitionPortRequest changed Work identity"
            )
        if not hmac.compare_digest(
            self.handoff.work_digest,
            self.request.work_digest,
        ):
            raise InvalidCognitionTransportRecord(
                "CognitionPortRequest changed Work binding"
            )
        if self.handoff.workflow_run_id != self.request.workflow_run_id:
            raise InvalidCognitionTransportRecord(
                "CognitionPortRequest changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            self.handoff.workflow_run_digest,
            self.request.workflow_run_digest,
        ):
            raise InvalidCognitionTransportRecord(
                "CognitionPortRequest changed WorkflowRun binding"
            )
