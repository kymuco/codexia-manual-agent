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

from codexia_manual_agent.work_core import WorkEvent, WorkSnapshot, WorkState
from codexia_manual_agent.workflow_core import (
    WorkflowCandidate,
    WorkflowRunSnapshot,
    WorkflowRunState,
)

ROLE_BINDING_SCHEMA_VERSION = 1
CONTEXT_PROJECTION_SCHEMA_VERSION = 1
ROLE_RUN_SCHEMA_VERSION = 1
COGNITION_REQUEST_SCHEMA_VERSION = 1
COGNITION_OUTCOME_SCHEMA_VERSION = 1

ROLE_STARTED_EVENT = "role.started"
COGNITION_REQUESTED_EVENT = "role.cognition-requested"
ROLE_COMPLETED_EVENT = "role.completed"
ROLE_FAILED_EVENT = "role.failed"
ROLE_OUTCOME_UNKNOWN_EVENT = "role.outcome-unknown"

MAX_ROLE_ID_CHARS = 128
MAX_ROLE_VERSION_CHARS = 128
MAX_TIMESTAMP_CHARS = 64
MAX_INSTRUCTIONS_CHARS = 65_536
MAX_CONTEXT_CHARS = 262_144
MAX_OUTPUT_CHARS = 131_072
MAX_ERROR_CHARS = 16_384

_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidRoleRecord(ValueError):
    """Raised when a G2.3 role/cognition record is structurally invalid."""


class RoleRunState(StrEnum):
    ACTIVE = "active"
    REQUESTED = "requested"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class CognitionOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


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
        raise InvalidRoleRecord("Role/cognition record is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    record_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise InvalidRoleRecord(f"{record_name} keys are not exact")
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidRoleRecord(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidRoleRecord(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidRoleRecord(f"{field_name} must be lowercase hyphenated UUID")
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidRoleRecord(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidRoleRecord(f"{field_name} must be bounded canonical ISO-8601")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRoleRecord(f"{field_name} must be canonical ISO-8601") from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidRoleRecord(f"{field_name} must be canonical ISO-8601")
    return value


def _new_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _bounded_text(
    value: Any,
    field_name: str,
    max_chars: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise InvalidRoleRecord(f"{field_name} must be text")
    if "\x00" in value or len(value) > max_chars:
        raise InvalidRoleRecord(f"{field_name} exceeds its text budget")
    if not allow_empty and not value.strip():
        raise InvalidRoleRecord(f"{field_name} must be non-empty text")
    return value


def _workflow_wrapped_payload(
    *,
    workflow_run_id: str,
    workflow_run_digest: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "_workflow": {
            "workflow_run_id": workflow_run_id,
            "workflow_run_digest": workflow_run_digest,
        },
        "payload": dict(payload),
    }


@dataclass(frozen=True, slots=True)
class RoleBinding:
    """Exact semantic identity of one temporary cognition specialization."""

    schema_version: int
    role_id: str
    version: str
    instructions_digest: str
    binding_digest: str

    @classmethod
    def create(
        cls,
        *,
        role_id: str,
        version: str,
        instructions_digest: str,
    ) -> RoleBinding:
        role_id = _bounded_text(role_id, "role_id", MAX_ROLE_ID_CHARS).lower()
        if _ID_RE.fullmatch(role_id) is None:
            raise InvalidRoleRecord("role_id is not canonical")
        version = _bounded_text(version, "version", MAX_ROLE_VERSION_CHARS)
        if version != version.strip():
            raise InvalidRoleRecord("version must be canonical trimmed text")
        _validate_digest(instructions_digest, "instructions_digest")
        base = {
            "schema_version": ROLE_BINDING_SCHEMA_VERSION,
            "role_id": role_id,
            "version": version,
            "instructions_digest": instructions_digest,
        }
        return cls(**base, binding_digest=_digest(base))

    def __post_init__(self) -> None:
        if self.schema_version != ROLE_BINDING_SCHEMA_VERSION:
            raise InvalidRoleRecord("Unsupported RoleBinding schema")
        role_id = _bounded_text(self.role_id, "role_id", MAX_ROLE_ID_CHARS).lower()
        if role_id != self.role_id or _ID_RE.fullmatch(role_id) is None:
            raise InvalidRoleRecord("role_id is not canonical")
        version = _bounded_text(self.version, "version", MAX_ROLE_VERSION_CHARS)
        if version != self.version or version != version.strip():
            raise InvalidRoleRecord("version must be canonical trimmed text")
        _validate_digest(self.instructions_digest, "instructions_digest")
        _validate_digest(self.binding_digest, "binding_digest")
        if not hmac.compare_digest(self.binding_digest, _digest(self._base_dict())):
            raise InvalidRoleRecord("RoleBinding digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role_id": self.role_id,
            "version": self.version,
            "instructions_digest": self.instructions_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "binding_digest": self.binding_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RoleBinding:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "role_id",
                "version",
                "instructions_digest",
                "binding_digest",
            },
            "RoleBinding",
        )
        return cls(
            schema_version=value["schema_version"],
            role_id=value["role_id"],
            version=value["version"],
            instructions_digest=value["instructions_digest"],
            binding_digest=value["binding_digest"],
        )


@dataclass(frozen=True, slots=True)
class ContextProjection:
    """Durable binding to the exact bounded context supplied to one RoleRun.

    The context bytes are intentionally not stored here. A host may resolve or
    construct them outside the canonical Work chronology, but CognitionRequest
    must prove that the supplied bytes match this exact digest.
    """

    schema_version: int
    projection_id: str
    content_digest: str
    projection_digest: str

    @classmethod
    def create(
        cls,
        *,
        content_digest: str,
        projection_id: str | None = None,
    ) -> ContextProjection:
        projection_id = projection_id or str(uuid4())
        _validate_uuid(projection_id, "projection_id")
        _validate_digest(content_digest, "content_digest")
        base = {
            "schema_version": CONTEXT_PROJECTION_SCHEMA_VERSION,
            "projection_id": projection_id,
            "content_digest": content_digest,
        }
        return cls(**base, projection_digest=_digest(base))

    def __post_init__(self) -> None:
        if self.schema_version != CONTEXT_PROJECTION_SCHEMA_VERSION:
            raise InvalidRoleRecord("Unsupported ContextProjection schema")
        _validate_uuid(self.projection_id, "projection_id")
        _validate_digest(self.content_digest, "content_digest")
        _validate_digest(self.projection_digest, "projection_digest")
        if not hmac.compare_digest(
            self.projection_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidRoleRecord("ContextProjection digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "projection_id": self.projection_id,
            "content_digest": self.content_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "projection_digest": self.projection_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextProjection:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "projection_id",
                "content_digest",
                "projection_digest",
            },
            "ContextProjection",
        )
        return cls(
            schema_version=value["schema_version"],
            projection_id=value["projection_id"],
            content_digest=value["content_digest"],
            projection_digest=value["projection_digest"],
        )


@dataclass(frozen=True, slots=True)
class RoleRun:
    """One bounded cognition operation owned by one exact WorkflowRun."""

    schema_version: int
    role_run_id: str
    created_at: str
    work_id: str
    work_digest: str
    workflow_run_id: str
    workflow_run_digest: str
    binding: RoleBinding
    context: ContextProjection
    start_revision: int
    start_event_digest: str | None
    run_digest: str

    @classmethod
    def create(
        cls,
        *,
        workflow: WorkflowRunSnapshot,
        snapshot: WorkSnapshot,
        binding: RoleBinding,
        context: ContextProjection,
        role_run_id: str | None = None,
        created_at: str | None = None,
    ) -> RoleRun:
        if not isinstance(workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise InvalidRoleRecord("RoleRun cannot start under terminal WorkflowRun")
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidRoleRecord("RoleRun cannot start on terminal Work")
        if workflow.run.work_id != snapshot.work.work_id:
            raise InvalidRoleRecord("WorkflowRun belongs to another Work")
        if not hmac.compare_digest(
            workflow.run.work_digest,
            snapshot.work.work_digest,
        ):
            raise InvalidRoleRecord("WorkflowRun work binding changed")
        if not isinstance(binding, RoleBinding):
            raise TypeError("binding must be RoleBinding")
        if not isinstance(context, ContextProjection):
            raise TypeError("context must be ContextProjection")

        role_run_id = role_run_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(role_run_id, "role_run_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": ROLE_RUN_SCHEMA_VERSION,
            "role_run_id": role_run_id,
            "created_at": created_at,
            "work_id": snapshot.work.work_id,
            "work_digest": snapshot.work.work_digest,
            "workflow_run_id": workflow.run.workflow_run_id,
            "workflow_run_digest": workflow.run.run_digest,
            "binding": binding.to_dict(),
            "context": context.to_dict(),
            "start_revision": snapshot.revision,
            "start_event_digest": snapshot.last_event_digest,
        }
        return cls(
            schema_version=ROLE_RUN_SCHEMA_VERSION,
            role_run_id=role_run_id,
            created_at=created_at,
            work_id=snapshot.work.work_id,
            work_digest=snapshot.work.work_digest,
            workflow_run_id=workflow.run.workflow_run_id,
            workflow_run_digest=workflow.run.run_digest,
            binding=binding,
            context=context,
            start_revision=snapshot.revision,
            start_event_digest=snapshot.last_event_digest,
            run_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != ROLE_RUN_SCHEMA_VERSION:
            raise InvalidRoleRecord("Unsupported RoleRun schema")
        _validate_uuid(self.role_run_id, "role_run_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_digest(self.workflow_run_digest, "workflow_run_digest")
        if not isinstance(self.binding, RoleBinding):
            raise InvalidRoleRecord("binding must be RoleBinding")
        if not isinstance(self.context, ContextProjection):
            raise InvalidRoleRecord("context must be ContextProjection")
        if type(self.start_revision) is not int or self.start_revision < 0:
            raise InvalidRoleRecord("start_revision must be non-negative integer")
        if self.start_event_digest is None:
            if self.start_revision != 0:
                raise InvalidRoleRecord(
                    "nonzero start_revision requires start_event_digest"
                )
        else:
            _validate_digest(self.start_event_digest, "start_event_digest")
            if self.start_revision == 0:
                raise InvalidRoleRecord(
                    "revision zero cannot have start_event_digest"
                )
        _validate_digest(self.run_digest, "run_digest")
        if not hmac.compare_digest(self.run_digest, _digest(self._base_dict())):
            raise InvalidRoleRecord("RoleRun digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role_run_id": self.role_run_id,
            "created_at": self.created_at,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_digest": self.workflow_run_digest,
            "binding": self.binding.to_dict(),
            "context": self.context.to_dict(),
            "start_revision": self.start_revision,
            "start_event_digest": self.start_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "run_digest": self.run_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RoleRun:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "role_run_id",
                "created_at",
                "work_id",
                "work_digest",
                "workflow_run_id",
                "workflow_run_digest",
                "binding",
                "context",
                "start_revision",
                "start_event_digest",
                "run_digest",
            },
            "RoleRun",
        )
        return cls(
            schema_version=value["schema_version"],
            role_run_id=value["role_run_id"],
            created_at=value["created_at"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            workflow_run_id=value["workflow_run_id"],
            workflow_run_digest=value["workflow_run_digest"],
            binding=RoleBinding.from_dict(value["binding"]),
            context=ContextProjection.from_dict(value["context"]),
            start_revision=value["start_revision"],
            start_event_digest=value["start_event_digest"],
            run_digest=value["run_digest"],
        )

    def to_start_candidate(self) -> WorkflowCandidate:
        event = WorkEvent.create(
            work_id=self.work_id,
            sequence=self.start_revision + 1,
            kind=ROLE_STARTED_EVENT,
            payload=_workflow_wrapped_payload(
                workflow_run_id=self.workflow_run_id,
                workflow_run_digest=self.workflow_run_digest,
                payload={"role_run": self.to_dict()},
            ),
            previous_event_digest=self.start_event_digest,
            event_id=self.role_run_id,
            created_at=self.created_at,
        )
        return WorkflowCandidate(
            schema_version=1,
            workflow_run_id=self.workflow_run_id,
            workflow_run_digest=self.workflow_run_digest,
            expected_revision=self.start_revision,
            expected_event_digest=self.start_event_digest,
            event=event,
        )


@dataclass(frozen=True, slots=True)
class RoleRunSnapshot:
    run: RoleRun
    state: RoleRunState
    request_id: str | None
    request_digest: str | None
    terminal_event_id: str | None
    output_text: str | None
    error: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.run, RoleRun):
            raise TypeError("run must be RoleRun")
        object.__setattr__(self, "state", RoleRunState(self.state))
        if self.state is RoleRunState.ACTIVE:
            if any(
                value is not None
                for value in (
                    self.request_id,
                    self.request_digest,
                    self.terminal_event_id,
                    self.output_text,
                    self.error,
                )
            ):
                raise InvalidRoleRecord("active RoleRun cannot have cognition state")
            return

        if self.request_id is None or self.request_digest is None:
            raise InvalidRoleRecord("non-active RoleRun requires cognition request")
        _validate_uuid(self.request_id, "request_id")
        _validate_digest(self.request_digest, "request_digest")

        if self.state is RoleRunState.REQUESTED:
            if any(
                value is not None
                for value in (self.terminal_event_id, self.output_text, self.error)
            ):
                raise InvalidRoleRecord("requested RoleRun cannot have terminal output")
            return

        if self.terminal_event_id is None:
            raise InvalidRoleRecord("terminal RoleRun requires terminal_event_id")
        _validate_uuid(self.terminal_event_id, "terminal_event_id")
        if self.state is RoleRunState.COMPLETED:
            _bounded_text(self.output_text, "output_text", MAX_OUTPUT_CHARS)
            if self.error is not None:
                raise InvalidRoleRecord("completed RoleRun cannot carry error")
        else:
            _bounded_text(self.error, "error", MAX_ERROR_CHARS)
            if self.output_text is not None:
                raise InvalidRoleRecord("failed/unknown RoleRun cannot carry output")


@dataclass(frozen=True, slots=True)
class CognitionRequest:
    """Transient exact input to one cognition transport call.

    This value intentionally contains no WorkStore, tool, workspace, capability,
    permission, conversation, or provider handle.
    """

    schema_version: int
    request_id: str
    created_at: str
    role_run_id: str
    role_run_digest: str
    work_id: str
    work_digest: str
    workflow_run_id: str
    workflow_run_digest: str
    expected_revision: int
    expected_event_digest: str | None
    instructions_digest: str
    context_digest: str
    instructions: str
    context: str
    request_digest: str

    @classmethod
    def create(
        cls,
        *,
        role: RoleRunSnapshot,
        snapshot: WorkSnapshot,
        instructions: str,
        context: str,
        request_id: str | None = None,
        created_at: str | None = None,
    ) -> CognitionRequest:
        if not isinstance(role, RoleRunSnapshot):
            raise TypeError("role must be RoleRunSnapshot")
        if role.state is not RoleRunState.ACTIVE:
            raise InvalidRoleRecord("terminal RoleRun cannot request cognition")
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidRoleRecord("terminal Work cannot request cognition")
        run = role.run
        if run.work_id != snapshot.work.work_id:
            raise InvalidRoleRecord("RoleRun belongs to another Work")
        if not hmac.compare_digest(run.work_digest, snapshot.work.work_digest):
            raise InvalidRoleRecord("RoleRun work binding changed")

        instructions = _bounded_text(
            instructions,
            "instructions",
            MAX_INSTRUCTIONS_CHARS,
        )
        context = _bounded_text(
            context,
            "context",
            MAX_CONTEXT_CHARS,
            allow_empty=True,
        )
        if not hmac.compare_digest(
            _text_digest(instructions),
            run.binding.instructions_digest,
        ):
            raise InvalidRoleRecord("instructions do not match RoleBinding")
        if not hmac.compare_digest(
            _text_digest(context),
            run.context.content_digest,
        ):
            raise InvalidRoleRecord("context does not match ContextProjection")

        request_id = request_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(request_id, "request_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": COGNITION_REQUEST_SCHEMA_VERSION,
            "request_id": request_id,
            "created_at": created_at,
            "role_run_id": run.role_run_id,
            "role_run_digest": run.run_digest,
            "work_id": run.work_id,
            "work_digest": run.work_digest,
            "workflow_run_id": run.workflow_run_id,
            "workflow_run_digest": run.workflow_run_digest,
            "expected_revision": snapshot.revision,
            "expected_event_digest": snapshot.last_event_digest,
            "instructions_digest": _text_digest(instructions),
            "context_digest": _text_digest(context),
            "instructions": instructions,
            "context": context,
        }
        return cls(**base, request_digest=_digest(base))

    def __post_init__(self) -> None:
        if self.schema_version != COGNITION_REQUEST_SCHEMA_VERSION:
            raise InvalidRoleRecord("Unsupported CognitionRequest schema")
        _validate_uuid(self.request_id, "request_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.role_run_id, "role_run_id")
        _validate_digest(self.role_run_digest, "role_run_digest")
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_digest(self.workflow_run_digest, "workflow_run_digest")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise InvalidRoleRecord("expected_revision must be non-negative integer")
        if self.expected_event_digest is None:
            if self.expected_revision != 0:
                raise InvalidRoleRecord(
                    "nonzero expected_revision requires expected_event_digest"
                )
        else:
            _validate_digest(self.expected_event_digest, "expected_event_digest")
            if self.expected_revision == 0:
                raise InvalidRoleRecord(
                    "revision zero cannot have expected_event_digest"
                )
        _validate_digest(self.instructions_digest, "instructions_digest")
        _validate_digest(self.context_digest, "context_digest")
        instructions = _bounded_text(
            self.instructions,
            "instructions",
            MAX_INSTRUCTIONS_CHARS,
        )
        context = _bounded_text(
            self.context,
            "context",
            MAX_CONTEXT_CHARS,
            allow_empty=True,
        )
        if not hmac.compare_digest(
            self.instructions_digest,
            _text_digest(instructions),
        ):
            raise InvalidRoleRecord("CognitionRequest instructions digest mismatch")
        if not hmac.compare_digest(
            self.context_digest,
            _text_digest(context),
        ):
            raise InvalidRoleRecord("CognitionRequest context digest mismatch")
        _validate_digest(self.request_digest, "request_digest")
        if not hmac.compare_digest(self.request_digest, _digest(self._base_dict())):
            raise InvalidRoleRecord("CognitionRequest digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "role_run_id": self.role_run_id,
            "role_run_digest": self.role_run_digest,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_digest": self.workflow_run_digest,
            "expected_revision": self.expected_revision,
            "expected_event_digest": self.expected_event_digest,
            "instructions_digest": self.instructions_digest,
            "context_digest": self.context_digest,
            "instructions": self.instructions,
            "context": self.context,
        }

    def durable_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "role_run_id": self.role_run_id,
            "role_run_digest": self.role_run_digest,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_digest": self.workflow_run_digest,
            "expected_revision": self.expected_revision,
            "expected_event_digest": self.expected_event_digest,
            "instructions_digest": self.instructions_digest,
            "context_digest": self.context_digest,
            "request_digest": self.request_digest,
        }

    def to_workflow_candidate(self) -> WorkflowCandidate:
        event = WorkEvent.create(
            work_id=self.work_id,
            sequence=self.expected_revision + 1,
            kind=COGNITION_REQUESTED_EVENT,
            payload=_workflow_wrapped_payload(
                workflow_run_id=self.workflow_run_id,
                workflow_run_digest=self.workflow_run_digest,
                payload={"cognition_request": self.durable_dict()},
            ),
            previous_event_digest=self.expected_event_digest,
            event_id=self.request_id,
            created_at=self.created_at,
        )
        return WorkflowCandidate(
            schema_version=1,
            workflow_run_id=self.workflow_run_id,
            workflow_run_digest=self.workflow_run_digest,
            expected_revision=self.expected_revision,
            expected_event_digest=self.expected_event_digest,
            event=event,
        )


@dataclass(frozen=True, slots=True)
class CognitionOutcome:
    """Transient exact observation of one cognition request outcome."""

    schema_version: int
    outcome_id: str
    created_at: str
    request_id: str
    request_digest: str
    role_run_id: str
    role_run_digest: str
    work_id: str
    work_digest: str
    workflow_run_id: str
    workflow_run_digest: str
    expected_revision: int
    expected_event_digest: str | None
    status: CognitionOutcomeStatus
    output_text: str | None
    output_digest: str | None
    error: str | None
    outcome_digest: str

    @classmethod
    def succeeded(
        cls,
        request: CognitionRequest,
        *,
        output_text: str,
        outcome_id: str | None = None,
        created_at: str | None = None,
    ) -> CognitionOutcome:
        output_text = _bounded_text(output_text, "output_text", MAX_OUTPUT_CHARS)
        return cls._create(
            request,
            status=CognitionOutcomeStatus.SUCCEEDED,
            output_text=output_text,
            output_digest=_text_digest(output_text),
            error=None,
            outcome_id=outcome_id,
            created_at=created_at,
        )

    @classmethod
    def failed(
        cls,
        request: CognitionRequest,
        *,
        error: str,
        outcome_id: str | None = None,
        created_at: str | None = None,
    ) -> CognitionOutcome:
        error = _bounded_text(error, "error", MAX_ERROR_CHARS)
        return cls._create(
            request,
            status=CognitionOutcomeStatus.FAILED,
            output_text=None,
            output_digest=None,
            error=error,
            outcome_id=outcome_id,
            created_at=created_at,
        )

    @classmethod
    def unknown(
        cls,
        request: CognitionRequest,
        *,
        detail: str,
        outcome_id: str | None = None,
        created_at: str | None = None,
    ) -> CognitionOutcome:
        detail = _bounded_text(detail, "detail", MAX_ERROR_CHARS)
        return cls._create(
            request,
            status=CognitionOutcomeStatus.UNKNOWN,
            output_text=None,
            output_digest=None,
            error=detail,
            outcome_id=outcome_id,
            created_at=created_at,
        )

    @classmethod
    def _create(
        cls,
        request: CognitionRequest,
        *,
        status: CognitionOutcomeStatus,
        output_text: str | None,
        output_digest: str | None,
        error: str | None,
        outcome_id: str | None,
        created_at: str | None,
    ) -> CognitionOutcome:
        if not isinstance(request, CognitionRequest):
            raise TypeError("request must be CognitionRequest")
        outcome_id = outcome_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(outcome_id, "outcome_id")
        _validate_timestamp(created_at, "created_at")
        request_event = request.to_workflow_candidate().event
        base = {
            "schema_version": COGNITION_OUTCOME_SCHEMA_VERSION,
            "outcome_id": outcome_id,
            "created_at": created_at,
            "request_id": request.request_id,
            "request_digest": request.request_digest,
            "role_run_id": request.role_run_id,
            "role_run_digest": request.role_run_digest,
            "work_id": request.work_id,
            "work_digest": request.work_digest,
            "workflow_run_id": request.workflow_run_id,
            "workflow_run_digest": request.workflow_run_digest,
            "expected_revision": request.expected_revision + 1,
            "expected_event_digest": request_event.event_digest,
            "status": status.value,
            "output_text": output_text,
            "output_digest": output_digest,
            "error": error,
        }
        return cls(
            schema_version=COGNITION_OUTCOME_SCHEMA_VERSION,
            outcome_id=outcome_id,
            created_at=created_at,
            request_id=request.request_id,
            request_digest=request.request_digest,
            role_run_id=request.role_run_id,
            role_run_digest=request.role_run_digest,
            work_id=request.work_id,
            work_digest=request.work_digest,
            workflow_run_id=request.workflow_run_id,
            workflow_run_digest=request.workflow_run_digest,
            expected_revision=request.expected_revision + 1,
            expected_event_digest=request_event.event_digest,
            status=status,
            output_text=output_text,
            output_digest=output_digest,
            error=error,
            outcome_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != COGNITION_OUTCOME_SCHEMA_VERSION:
            raise InvalidRoleRecord("Unsupported CognitionOutcome schema")
        _validate_uuid(self.outcome_id, "outcome_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.request_id, "request_id")
        _validate_digest(self.request_digest, "request_digest")
        _validate_uuid(self.role_run_id, "role_run_id")
        _validate_digest(self.role_run_digest, "role_run_digest")
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_digest(self.workflow_run_digest, "workflow_run_digest")
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise InvalidRoleRecord("expected_revision must be non-negative integer")
        if self.expected_event_digest is None:
            if self.expected_revision != 0:
                raise InvalidRoleRecord(
                    "nonzero expected_revision requires expected_event_digest"
                )
        else:
            _validate_digest(self.expected_event_digest, "expected_event_digest")
            if self.expected_revision == 0:
                raise InvalidRoleRecord(
                    "revision zero cannot have expected_event_digest"
                )
        object.__setattr__(self, "status", CognitionOutcomeStatus(self.status))
        if self.status is CognitionOutcomeStatus.SUCCEEDED:
            output = _bounded_text(self.output_text, "output_text", MAX_OUTPUT_CHARS)
            _validate_digest(self.output_digest, "output_digest")
            if not hmac.compare_digest(_text_digest(output), self.output_digest):
                raise InvalidRoleRecord("CognitionOutcome output digest mismatch")
            if self.error is not None:
                raise InvalidRoleRecord("successful CognitionOutcome cannot carry error")
        else:
            if self.output_text is not None or self.output_digest is not None:
                raise InvalidRoleRecord("failed/unknown outcome cannot carry output")
            _bounded_text(self.error, "error", MAX_ERROR_CHARS)
        _validate_digest(self.outcome_digest, "outcome_digest")
        if not hmac.compare_digest(self.outcome_digest, _digest(self._base_dict())):
            raise InvalidRoleRecord("CognitionOutcome digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outcome_id": self.outcome_id,
            "created_at": self.created_at,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "role_run_id": self.role_run_id,
            "role_run_digest": self.role_run_digest,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_digest": self.workflow_run_digest,
            "expected_revision": self.expected_revision,
            "expected_event_digest": self.expected_event_digest,
            "status": self.status.value,
            "output_text": self.output_text,
            "output_digest": self.output_digest,
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "outcome_digest": self.outcome_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CognitionOutcome:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "outcome_id",
                "created_at",
                "request_id",
                "request_digest",
                "role_run_id",
                "role_run_digest",
                "work_id",
                "work_digest",
                "workflow_run_id",
                "workflow_run_digest",
                "expected_revision",
                "expected_event_digest",
                "status",
                "output_text",
                "output_digest",
                "error",
                "outcome_digest",
            },
            "CognitionOutcome",
        )
        return cls(
            schema_version=value["schema_version"],
            outcome_id=value["outcome_id"],
            created_at=value["created_at"],
            request_id=value["request_id"],
            request_digest=value["request_digest"],
            role_run_id=value["role_run_id"],
            role_run_digest=value["role_run_digest"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            workflow_run_id=value["workflow_run_id"],
            workflow_run_digest=value["workflow_run_digest"],
            expected_revision=value["expected_revision"],
            expected_event_digest=value["expected_event_digest"],
            status=CognitionOutcomeStatus(value["status"]),
            output_text=value["output_text"],
            output_digest=value["output_digest"],
            error=value["error"],
            outcome_digest=value["outcome_digest"],
        )

    def to_workflow_candidate(self) -> WorkflowCandidate:
        kind_by_status = {
            CognitionOutcomeStatus.SUCCEEDED: ROLE_COMPLETED_EVENT,
            CognitionOutcomeStatus.FAILED: ROLE_FAILED_EVENT,
            CognitionOutcomeStatus.UNKNOWN: ROLE_OUTCOME_UNKNOWN_EVENT,
        }
        event = WorkEvent.create(
            work_id=self.work_id,
            sequence=self.expected_revision + 1,
            kind=kind_by_status[self.status],
            payload=_workflow_wrapped_payload(
                workflow_run_id=self.workflow_run_id,
                workflow_run_digest=self.workflow_run_digest,
                payload={"cognition_outcome": self.to_dict()},
            ),
            previous_event_digest=self.expected_event_digest,
            event_id=self.outcome_id,
            created_at=self.created_at,
        )
        return WorkflowCandidate(
            schema_version=1,
            workflow_run_id=self.workflow_run_id,
            workflow_run_digest=self.workflow_run_digest,
            expected_revision=self.expected_revision,
            expected_event_digest=self.expected_event_digest,
            event=event,
        )
