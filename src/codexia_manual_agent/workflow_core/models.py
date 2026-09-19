from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from codexia_manual_agent.work_core import (
    WORK_CANCELLED_EVENT,
    WORK_COMPLETED_EVENT,
    WorkEvent,
    WorkSnapshot,
    WorkState,
)

WORKFLOW_BINDING_SCHEMA_VERSION = 1
WORKFLOW_RUN_SCHEMA_VERSION = 1
WORKFLOW_CANDIDATE_SCHEMA_VERSION = 1

WORKFLOW_STARTED_EVENT = "workflow.started"
WORKFLOW_COMPLETED_EVENT = "workflow.completed"
WORKFLOW_CANCELLED_EVENT = "workflow.cancelled"

MAX_WORKFLOW_ID_CHARS = 128
MAX_WORKFLOW_VERSION_CHARS = 128
MAX_TIMESTAMP_CHARS = 64

_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidWorkflowRecord(ValueError):
    """Raised when a Gen2 workflow record is structurally invalid."""


class WorkflowRunState(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


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
        raise InvalidWorkflowRecord("Workflow record is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    record_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise InvalidWorkflowRecord(f"{record_name} keys are not exact")
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidWorkflowRecord(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidWorkflowRecord(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidWorkflowRecord(f"{field_name} must be lowercase hyphenated UUID")
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidWorkflowRecord(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidWorkflowRecord(f"{field_name} must be bounded canonical ISO-8601")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkflowRecord(f"{field_name} must be canonical ISO-8601") from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidWorkflowRecord(f"{field_name} must be canonical ISO-8601")
    return value


def _new_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _bounded_text(value: Any, field_name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidWorkflowRecord(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars or "\x00" in normalized:
        raise InvalidWorkflowRecord(f"{field_name} is empty or exceeds its text budget")
    return normalized


def _workflow_payload(
    *,
    run_id: str,
    run_digest: str,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    body = {} if payload is None else dict(payload)
    # WorkEvent owns canonical JSON validation and freezing for the body.
    return {
        "_workflow": {
            "workflow_run_id": run_id,
            "workflow_run_digest": run_digest,
        },
        "payload": body,
    }


@dataclass(frozen=True, slots=True)
class WorkflowBinding:
    """Exact semantic identity of one workflow definition."""

    schema_version: int
    workflow_id: str
    version: str
    definition_digest: str
    binding_digest: str

    @classmethod
    def create(
        cls,
        *,
        workflow_id: str,
        version: str,
        definition_digest: str,
    ) -> WorkflowBinding:
        workflow_id = _bounded_text(
            workflow_id,
            "workflow_id",
            MAX_WORKFLOW_ID_CHARS,
        ).lower()
        if _ID_RE.fullmatch(workflow_id) is None:
            raise InvalidWorkflowRecord("workflow_id is not canonical")
        version = _bounded_text(version, "version", MAX_WORKFLOW_VERSION_CHARS)
        _validate_digest(definition_digest, "definition_digest")
        base = {
            "schema_version": WORKFLOW_BINDING_SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "version": version,
            "definition_digest": definition_digest,
        }
        return cls(**base, binding_digest=_digest(base))

    def __post_init__(self) -> None:
        if self.schema_version != WORKFLOW_BINDING_SCHEMA_VERSION:
            raise InvalidWorkflowRecord("Unsupported WorkflowBinding schema")
        workflow_id = _bounded_text(
            self.workflow_id,
            "workflow_id",
            MAX_WORKFLOW_ID_CHARS,
        ).lower()
        if workflow_id != self.workflow_id or _ID_RE.fullmatch(workflow_id) is None:
            raise InvalidWorkflowRecord("workflow_id is not canonical")
        if _bounded_text(self.version, "version", MAX_WORKFLOW_VERSION_CHARS) != self.version:
            raise InvalidWorkflowRecord("version must be canonical trimmed text")
        _validate_digest(self.definition_digest, "definition_digest")
        _validate_digest(self.binding_digest, "binding_digest")
        if not hmac.compare_digest(self.binding_digest, _digest(self._base_dict())):
            raise InvalidWorkflowRecord("WorkflowBinding digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workflow_id": self.workflow_id,
            "version": self.version,
            "definition_digest": self.definition_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "binding_digest": self.binding_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkflowBinding:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "workflow_id",
                "version",
                "definition_digest",
                "binding_digest",
            },
            "WorkflowBinding",
        )
        return cls(
            schema_version=value["schema_version"],
            workflow_id=value["workflow_id"],
            version=value["version"],
            definition_digest=value["definition_digest"],
            binding_digest=value["binding_digest"],
        )


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """Immutable origin record for one workflow application to one Work."""

    schema_version: int
    workflow_run_id: str
    created_at: str
    work_id: str
    work_digest: str
    binding: WorkflowBinding
    start_revision: int
    start_event_digest: str | None
    run_digest: str

    @classmethod
    def create(
        cls,
        *,
        snapshot: WorkSnapshot,
        binding: WorkflowBinding,
        workflow_run_id: str | None = None,
        created_at: str | None = None,
    ) -> WorkflowRun:
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidWorkflowRecord("WorkflowRun cannot start on terminal Work")
        if not isinstance(binding, WorkflowBinding):
            raise TypeError("binding must be WorkflowBinding")
        workflow_run_id = workflow_run_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(workflow_run_id, "workflow_run_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": WORKFLOW_RUN_SCHEMA_VERSION,
            "workflow_run_id": workflow_run_id,
            "created_at": created_at,
            "work_id": snapshot.work.work_id,
            "work_digest": snapshot.work.work_digest,
            "binding": binding.to_dict(),
            "start_revision": snapshot.revision,
            "start_event_digest": snapshot.last_event_digest,
        }
        return cls(
            schema_version=WORKFLOW_RUN_SCHEMA_VERSION,
            workflow_run_id=workflow_run_id,
            created_at=created_at,
            work_id=snapshot.work.work_id,
            work_digest=snapshot.work.work_digest,
            binding=binding,
            start_revision=snapshot.revision,
            start_event_digest=snapshot.last_event_digest,
            run_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != WORKFLOW_RUN_SCHEMA_VERSION:
            raise InvalidWorkflowRecord("Unsupported WorkflowRun schema")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        if not isinstance(self.binding, WorkflowBinding):
            raise InvalidWorkflowRecord("binding must be WorkflowBinding")
        if type(self.start_revision) is not int or self.start_revision < 0:
            raise InvalidWorkflowRecord("start_revision must be non-negative integer")
        if self.start_event_digest is None:
            if self.start_revision != 0:
                raise InvalidWorkflowRecord(
                    "nonzero start_revision requires start_event_digest"
                )
        else:
            _validate_digest(self.start_event_digest, "start_event_digest")
            if self.start_revision == 0:
                raise InvalidWorkflowRecord(
                    "revision zero cannot have start_event_digest"
                )
        _validate_digest(self.run_digest, "run_digest")
        if not hmac.compare_digest(self.run_digest, _digest(self._base_dict())):
            raise InvalidWorkflowRecord("WorkflowRun digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workflow_run_id": self.workflow_run_id,
            "created_at": self.created_at,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "binding": self.binding.to_dict(),
            "start_revision": self.start_revision,
            "start_event_digest": self.start_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "run_digest": self.run_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkflowRun:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "workflow_run_id",
                "created_at",
                "work_id",
                "work_digest",
                "binding",
                "start_revision",
                "start_event_digest",
                "run_digest",
            },
            "WorkflowRun",
        )
        return cls(
            schema_version=value["schema_version"],
            workflow_run_id=value["workflow_run_id"],
            created_at=value["created_at"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            binding=WorkflowBinding.from_dict(value["binding"]),
            start_revision=value["start_revision"],
            start_event_digest=value["start_event_digest"],
            run_digest=value["run_digest"],
        )

    def to_start_event(self) -> WorkEvent:
        return WorkEvent.create(
            work_id=self.work_id,
            sequence=self.start_revision + 1,
            kind=WORKFLOW_STARTED_EVENT,
            payload={"workflow_run": self.to_dict()},
            previous_event_digest=self.start_event_digest,
            event_id=self.workflow_run_id,
            created_at=self.created_at,
        )


@dataclass(frozen=True, slots=True)
class WorkflowRunSnapshot:
    """Derived lifecycle projection for one admitted WorkflowRun."""

    run: WorkflowRun
    state: WorkflowRunState
    terminal_event_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.run, WorkflowRun):
            raise TypeError("run must be WorkflowRun")
        object.__setattr__(self, "state", WorkflowRunState(self.state))
        if self.state is WorkflowRunState.ACTIVE:
            if self.terminal_event_id is not None:
                raise InvalidWorkflowRecord(
                    "active WorkflowRun cannot have terminal_event_id"
                )
        else:
            if self.terminal_event_id is None:
                raise InvalidWorkflowRecord(
                    "terminal WorkflowRun requires terminal_event_id"
                )
            _validate_uuid(self.terminal_event_id, "terminal_event_id")


@dataclass(frozen=True, slots=True)
class WorkflowCandidate:
    """Prepared WorkEvent proposed by one exact active WorkflowRun.

    A candidate is not canonical Work truth. Admission through WorkStore is the
    only operation that can make its prepared WorkEvent part of the chronology.
    """

    schema_version: int
    workflow_run_id: str
    workflow_run_digest: str
    expected_revision: int
    expected_event_digest: str | None
    event: WorkEvent

    @classmethod
    def create(
        cls,
        *,
        run_snapshot: WorkflowRunSnapshot,
        work_snapshot: WorkSnapshot,
        event_kind: str,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> WorkflowCandidate:
        if not isinstance(run_snapshot, WorkflowRunSnapshot):
            raise TypeError("run_snapshot must be WorkflowRunSnapshot")
        if run_snapshot.state is not WorkflowRunState.ACTIVE:
            raise InvalidWorkflowRecord("terminal WorkflowRun cannot propose progression")
        if not isinstance(work_snapshot, WorkSnapshot):
            raise TypeError("work_snapshot must be WorkSnapshot")
        if work_snapshot.state is not WorkState.ACTIVE:
            raise InvalidWorkflowRecord("terminal Work cannot accept workflow progression")
        run = run_snapshot.run
        if run.work_id != work_snapshot.work.work_id:
            raise InvalidWorkflowRecord("WorkflowRun belongs to another Work")
        if run.work_digest != work_snapshot.work.work_digest:
            raise InvalidWorkflowRecord("WorkflowRun work binding changed")

        normalized_kind = event_kind.strip().lower() if isinstance(event_kind, str) else event_kind
        if normalized_kind == WORKFLOW_STARTED_EVENT:
            raise InvalidWorkflowRecord(
                "WorkflowRun start must use its dedicated start admission"
            )
        if normalized_kind in {WORK_COMPLETED_EVENT, WORK_CANCELLED_EVENT}:
            raise InvalidWorkflowRecord(
                "Workflow cannot directly terminate Work in G2.2"
            )

        event = WorkEvent.create(
            work_id=run.work_id,
            sequence=work_snapshot.revision + 1,
            kind=event_kind,
            payload=_workflow_payload(
                run_id=run.workflow_run_id,
                run_digest=run.run_digest,
                payload=payload,
            ),
            previous_event_digest=work_snapshot.last_event_digest,
            event_id=event_id,
            created_at=created_at,
        )
        return cls(
            schema_version=WORKFLOW_CANDIDATE_SCHEMA_VERSION,
            workflow_run_id=run.workflow_run_id,
            workflow_run_digest=run.run_digest,
            expected_revision=work_snapshot.revision,
            expected_event_digest=work_snapshot.last_event_digest,
            event=event,
        )

    def __post_init__(self) -> None:
        if self.schema_version != WORKFLOW_CANDIDATE_SCHEMA_VERSION:
            raise InvalidWorkflowRecord("Unsupported WorkflowCandidate schema")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_digest(self.workflow_run_digest, "workflow_run_digest")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise InvalidWorkflowRecord("expected_revision must be non-negative integer")
        if self.expected_event_digest is None:
            if self.expected_revision != 0:
                raise InvalidWorkflowRecord(
                    "nonzero expected_revision requires expected_event_digest"
                )
        else:
            _validate_digest(self.expected_event_digest, "expected_event_digest")
            if self.expected_revision == 0:
                raise InvalidWorkflowRecord(
                    "revision zero cannot have expected_event_digest"
                )
        if not isinstance(self.event, WorkEvent):
            raise InvalidWorkflowRecord("event must be WorkEvent")
        if self.event.sequence != self.expected_revision + 1:
            raise InvalidWorkflowRecord(
                "candidate WorkEvent sequence does not bind expected revision"
            )
        if self.event.previous_event_digest != self.expected_event_digest:
            raise InvalidWorkflowRecord(
                "candidate WorkEvent does not bind exact prior event digest"
            )
        if self.event.kind == WORKFLOW_STARTED_EVENT:
            raise InvalidWorkflowRecord(
                "WorkflowCandidate cannot manufacture workflow.started"
            )
        if self.event.kind in {WORK_COMPLETED_EVENT, WORK_CANCELLED_EVENT}:
            raise InvalidWorkflowRecord(
                "WorkflowCandidate cannot directly terminate Work"
            )

        payload = self.event.to_dict()["payload"]
        if set(payload) != {"_workflow", "payload"}:
            raise InvalidWorkflowRecord("WorkflowCandidate payload wrapper is not exact")
        provenance = payload["_workflow"]
        if not isinstance(provenance, dict) or set(provenance) != {
            "workflow_run_id",
            "workflow_run_digest",
        }:
            raise InvalidWorkflowRecord("WorkflowCandidate provenance is not exact")
        if not isinstance(provenance["workflow_run_id"], str) or not isinstance(
            provenance["workflow_run_digest"],
            str,
        ):
            raise InvalidWorkflowRecord("WorkflowCandidate provenance types are invalid")
        if provenance["workflow_run_id"] != self.workflow_run_id:
            raise InvalidWorkflowRecord("WorkflowCandidate run identity changed")
        if not hmac.compare_digest(
            provenance["workflow_run_digest"],
            self.workflow_run_digest,
        ):
            raise InvalidWorkflowRecord("WorkflowCandidate run digest changed")
