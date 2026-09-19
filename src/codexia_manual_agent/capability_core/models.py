from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from codexia_manual_agent.work_core import WorkEvent, WorkSnapshot, WorkState
from codexia_manual_agent.workflow_core import (
    WorkflowCandidate,
    WorkflowRunSnapshot,
    WorkflowRunState,
)

CAPABILITY_BINDING_SCHEMA_VERSION = 1
CAPABILITY_NEED_SCHEMA_VERSION = 1
CAPABILITY_OUTCOME_SCHEMA_VERSION = 1

CAPABILITY_NEED_DECLARED_EVENT = "capability.need-declared"
CAPABILITY_OUTCOME_RECORDED_EVENT = "capability.outcome-recorded"

MAX_CAPABILITY_ID_CHARS = 128
MAX_CAPABILITY_VERSION_CHARS = 128
MAX_OPERATION_CHARS = 128
MAX_ATTEMPT_ID_CHARS = 512
MAX_ERROR_CHARS = 16_384
MAX_JSON_BYTES = 262_144
MAX_TIMESTAMP_CHARS = 64

_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidCapabilityRecord(ValueError):
    """Raised when a G2.4 capability record is structurally invalid."""


class CapabilityNeedState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class CapabilityOutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


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
        raise InvalidCapabilityRecord(
            "Capability record is not canonical JSON"
        ) from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise InvalidCapabilityRecord("Capability JSON exceeds its byte budget")
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
            raise InvalidCapabilityRecord("JSON object keys must be strings")
        frozen = {key: _freeze_json(item) for key, item in value.items()}
        _canonical_json(frozen)
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, (list, tuple)):
        frozen = tuple(_freeze_json(item) for item in value)
        _canonical_json(frozen)
        return frozen
    raise InvalidCapabilityRecord(
        f"Capability JSON must be compatible, got {type(value).__name__}"
    )


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    record_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise InvalidCapabilityRecord(f"{record_name} keys are not exact")
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidCapabilityRecord(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidCapabilityRecord(
            f"{field_name} must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise InvalidCapabilityRecord(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidCapabilityRecord(
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
        raise InvalidCapabilityRecord(
            f"{field_name} must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidCapabilityRecord(
            f"{field_name} must be canonical ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidCapabilityRecord(f"{field_name} must be canonical ISO-8601")
    return value


def _new_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _bounded_text(
    value: Any,
    field_name: str,
    max_chars: int,
    *,
    canonical_trimmed: bool = True,
) -> str:
    if not isinstance(value, str):
        raise InvalidCapabilityRecord(f"{field_name} must be text")
    if "\x00" in value or len(value) > max_chars:
        raise InvalidCapabilityRecord(f"{field_name} exceeds its text budget")
    normalized = value.strip()
    if not normalized:
        raise InvalidCapabilityRecord(f"{field_name} must be non-empty text")
    if canonical_trimmed and normalized != value:
        raise InvalidCapabilityRecord(f"{field_name} must be canonical trimmed text")
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
class CapabilityBinding:
    """Exact semantic identity of one capability contract.

    This is not authority and does not imply that any host exposes, admits, or
    permits the capability.
    """

    schema_version: int
    capability_id: str
    version: str
    contract_digest: str
    binding_digest: str

    @classmethod
    def create(
        cls,
        *,
        capability_id: str,
        version: str,
        contract_digest: str,
    ) -> CapabilityBinding:
        capability_id = _bounded_text(
            capability_id,
            "capability_id",
            MAX_CAPABILITY_ID_CHARS,
        ).lower()
        if _ID_RE.fullmatch(capability_id) is None:
            raise InvalidCapabilityRecord("capability_id is not canonical")
        version = _bounded_text(
            version,
            "version",
            MAX_CAPABILITY_VERSION_CHARS,
        )
        _validate_digest(contract_digest, "contract_digest")
        base = {
            "schema_version": CAPABILITY_BINDING_SCHEMA_VERSION,
            "capability_id": capability_id,
            "version": version,
            "contract_digest": contract_digest,
        }
        return cls(**base, binding_digest=_digest(base))

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_BINDING_SCHEMA_VERSION:
            raise InvalidCapabilityRecord("Unsupported CapabilityBinding schema")
        capability_id = _bounded_text(
            self.capability_id,
            "capability_id",
            MAX_CAPABILITY_ID_CHARS,
        ).lower()
        if (
            capability_id != self.capability_id
            or _ID_RE.fullmatch(capability_id) is None
        ):
            raise InvalidCapabilityRecord("capability_id is not canonical")
        _bounded_text(self.version, "version", MAX_CAPABILITY_VERSION_CHARS)
        _validate_digest(self.contract_digest, "contract_digest")
        _validate_digest(self.binding_digest, "binding_digest")
        if not hmac.compare_digest(
            self.binding_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidCapabilityRecord("CapabilityBinding digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capability_id": self.capability_id,
            "version": self.version,
            "contract_digest": self.contract_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "binding_digest": self.binding_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityBinding:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "capability_id",
                "version",
                "contract_digest",
                "binding_digest",
            },
            "CapabilityBinding",
        )
        return cls(
            schema_version=value["schema_version"],
            capability_id=value["capability_id"],
            version=value["version"],
            contract_digest=value["contract_digest"],
            binding_digest=value["binding_digest"],
        )


@dataclass(frozen=True, slots=True)
class CapabilityNeed:
    """One exact external-world requirement declared by a WorkflowRun.

    Parameters are durable semantic inputs. They must not contain ambient secret
    material; hosts should resolve secrets through opaque references outside this
    record.
    """

    schema_version: int
    need_id: str
    created_at: str
    work_id: str
    work_digest: str
    workflow_run_id: str
    workflow_run_digest: str
    binding: CapabilityBinding
    operation: str
    parameters: Mapping[str, Any]
    start_revision: int
    start_event_digest: str | None
    need_digest: str

    @classmethod
    def create(
        cls,
        *,
        workflow: WorkflowRunSnapshot,
        snapshot: WorkSnapshot,
        binding: CapabilityBinding,
        operation: str,
        parameters: Mapping[str, Any] | None = None,
        need_id: str | None = None,
        created_at: str | None = None,
    ) -> CapabilityNeed:
        if not isinstance(workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise InvalidCapabilityRecord(
                "CapabilityNeed cannot be declared by terminal WorkflowRun"
            )
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidCapabilityRecord(
                "CapabilityNeed cannot be declared on terminal Work"
            )
        if workflow.run.work_id != snapshot.work.work_id:
            raise InvalidCapabilityRecord("WorkflowRun belongs to another Work")
        if not hmac.compare_digest(
            workflow.run.work_digest,
            snapshot.work.work_digest,
        ):
            raise InvalidCapabilityRecord("WorkflowRun work binding changed")
        if not isinstance(binding, CapabilityBinding):
            raise TypeError("binding must be CapabilityBinding")

        operation = _bounded_text(
            operation,
            "operation",
            MAX_OPERATION_CHARS,
        ).lower()
        if _OPERATION_RE.fullmatch(operation) is None:
            raise InvalidCapabilityRecord("operation is not canonical")
        frozen_parameters = _freeze_json({} if parameters is None else parameters)
        if not isinstance(frozen_parameters, Mapping):
            raise InvalidCapabilityRecord("parameters must be a JSON object")

        need_id = need_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(need_id, "need_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": CAPABILITY_NEED_SCHEMA_VERSION,
            "need_id": need_id,
            "created_at": created_at,
            "work_id": snapshot.work.work_id,
            "work_digest": snapshot.work.work_digest,
            "workflow_run_id": workflow.run.workflow_run_id,
            "workflow_run_digest": workflow.run.run_digest,
            "binding": binding.to_dict(),
            "operation": operation,
            "parameters": frozen_parameters,
            "start_revision": snapshot.revision,
            "start_event_digest": snapshot.last_event_digest,
        }
        return cls(
            schema_version=CAPABILITY_NEED_SCHEMA_VERSION,
            need_id=need_id,
            created_at=created_at,
            work_id=snapshot.work.work_id,
            work_digest=snapshot.work.work_digest,
            workflow_run_id=workflow.run.workflow_run_id,
            workflow_run_digest=workflow.run.run_digest,
            binding=binding,
            operation=operation,
            parameters=frozen_parameters,
            start_revision=snapshot.revision,
            start_event_digest=snapshot.last_event_digest,
            need_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_NEED_SCHEMA_VERSION:
            raise InvalidCapabilityRecord("Unsupported CapabilityNeed schema")
        _validate_uuid(self.need_id, "need_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_digest(self.workflow_run_digest, "workflow_run_digest")
        if not isinstance(self.binding, CapabilityBinding):
            raise InvalidCapabilityRecord("binding must be CapabilityBinding")
        operation = _bounded_text(
            self.operation,
            "operation",
            MAX_OPERATION_CHARS,
        ).lower()
        if operation != self.operation or _OPERATION_RE.fullmatch(operation) is None:
            raise InvalidCapabilityRecord("operation is not canonical")
        frozen_parameters = _freeze_json(self.parameters)
        if not isinstance(frozen_parameters, Mapping):
            raise InvalidCapabilityRecord("parameters must be a JSON object")
        object.__setattr__(self, "parameters", frozen_parameters)
        if type(self.start_revision) is not int or self.start_revision < 0:
            raise InvalidCapabilityRecord(
                "start_revision must be non-negative integer"
            )
        if self.start_event_digest is None:
            if self.start_revision != 0:
                raise InvalidCapabilityRecord(
                    "nonzero start_revision requires start_event_digest"
                )
        else:
            _validate_digest(self.start_event_digest, "start_event_digest")
            if self.start_revision == 0:
                raise InvalidCapabilityRecord(
                    "revision zero cannot have start_event_digest"
                )
        _validate_digest(self.need_digest, "need_digest")
        if not hmac.compare_digest(self.need_digest, _digest(self._base_dict())):
            raise InvalidCapabilityRecord("CapabilityNeed digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "need_id": self.need_id,
            "created_at": self.created_at,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_digest": self.workflow_run_digest,
            "binding": self.binding.to_dict(),
            "operation": self.operation,
            "parameters": self.parameters,
            "start_revision": self.start_revision,
            "start_event_digest": self.start_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**_thaw_json(self._base_dict()), "need_digest": self.need_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityNeed:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "need_id",
                "created_at",
                "work_id",
                "work_digest",
                "workflow_run_id",
                "workflow_run_digest",
                "binding",
                "operation",
                "parameters",
                "start_revision",
                "start_event_digest",
                "need_digest",
            },
            "CapabilityNeed",
        )
        return cls(
            schema_version=value["schema_version"],
            need_id=value["need_id"],
            created_at=value["created_at"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            workflow_run_id=value["workflow_run_id"],
            workflow_run_digest=value["workflow_run_digest"],
            binding=CapabilityBinding.from_dict(value["binding"]),
            operation=value["operation"],
            parameters=value["parameters"],
            start_revision=value["start_revision"],
            start_event_digest=value["start_event_digest"],
            need_digest=value["need_digest"],
        )

    def to_workflow_candidate(self) -> WorkflowCandidate:
        event = WorkEvent.create(
            work_id=self.work_id,
            sequence=self.start_revision + 1,
            kind=CAPABILITY_NEED_DECLARED_EVENT,
            payload=_workflow_wrapped_payload(
                workflow_run_id=self.workflow_run_id,
                workflow_run_digest=self.workflow_run_digest,
                payload={"capability_need": self.to_dict()},
            ),
            previous_event_digest=self.start_event_digest,
            event_id=self.need_id,
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
class CapabilityOutcome:
    """Observed result of one concrete host-side attempt for one exact Need.

    attempt_id/attempt_digest are opaque host-owned correlation facts. They are
    not authority and Codexia does not define how the attempt was admitted.
    """

    schema_version: int
    outcome_id: str
    created_at: str
    need_id: str
    need_digest: str
    work_id: str
    work_digest: str
    workflow_run_id: str
    workflow_run_digest: str
    attempt_id: str
    attempt_digest: str
    status: CapabilityOutcomeStatus
    observation: Mapping[str, Any]
    error: str | None
    outcome_digest: str

    @classmethod
    def succeeded(
        cls,
        need: CapabilityNeedSnapshot,
        *,
        attempt_id: str,
        attempt_digest: str,
        observation: Mapping[str, Any] | None = None,
        outcome_id: str | None = None,
        created_at: str | None = None,
    ) -> CapabilityOutcome:
        return cls._create(
            need,
            attempt_id=attempt_id,
            attempt_digest=attempt_digest,
            status=CapabilityOutcomeStatus.SUCCEEDED,
            observation={} if observation is None else observation,
            error=None,
            outcome_id=outcome_id,
            created_at=created_at,
        )

    @classmethod
    def failed(
        cls,
        need: CapabilityNeedSnapshot,
        *,
        attempt_id: str,
        attempt_digest: str,
        error: str,
        observation: Mapping[str, Any] | None = None,
        outcome_id: str | None = None,
        created_at: str | None = None,
    ) -> CapabilityOutcome:
        return cls._create(
            need,
            attempt_id=attempt_id,
            attempt_digest=attempt_digest,
            status=CapabilityOutcomeStatus.FAILED,
            observation={} if observation is None else observation,
            error=error,
            outcome_id=outcome_id,
            created_at=created_at,
        )

    @classmethod
    def unknown(
        cls,
        need: CapabilityNeedSnapshot,
        *,
        attempt_id: str,
        attempt_digest: str,
        detail: str,
        observation: Mapping[str, Any] | None = None,
        outcome_id: str | None = None,
        created_at: str | None = None,
    ) -> CapabilityOutcome:
        return cls._create(
            need,
            attempt_id=attempt_id,
            attempt_digest=attempt_digest,
            status=CapabilityOutcomeStatus.UNKNOWN,
            observation={} if observation is None else observation,
            error=detail,
            outcome_id=outcome_id,
            created_at=created_at,
        )

    @classmethod
    def _create(
        cls,
        need: CapabilityNeedSnapshot,
        *,
        attempt_id: str,
        attempt_digest: str,
        status: CapabilityOutcomeStatus,
        observation: Mapping[str, Any],
        error: str | None,
        outcome_id: str | None,
        created_at: str | None,
    ) -> CapabilityOutcome:
        if not isinstance(need, CapabilityNeedSnapshot):
            raise TypeError("need must be CapabilityNeedSnapshot")
        if need.state is not CapabilityNeedState.PENDING:
            raise InvalidCapabilityRecord(
                "terminal CapabilityNeed cannot receive another outcome"
            )
        attempt_id = _bounded_text(
            attempt_id,
            "attempt_id",
            MAX_ATTEMPT_ID_CHARS,
        )
        _validate_digest(attempt_digest, "attempt_digest")
        frozen_observation = _freeze_json(observation)
        if not isinstance(frozen_observation, Mapping):
            raise InvalidCapabilityRecord("observation must be a JSON object")
        if status is CapabilityOutcomeStatus.SUCCEEDED:
            if error is not None:
                raise InvalidCapabilityRecord(
                    "successful CapabilityOutcome cannot carry error"
                )
        else:
            error = _bounded_text(
                error,
                "error",
                MAX_ERROR_CHARS,
                canonical_trimmed=False,
            )

        outcome_id = outcome_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(outcome_id, "outcome_id")
        _validate_timestamp(created_at, "created_at")
        source = need.need
        base = {
            "schema_version": CAPABILITY_OUTCOME_SCHEMA_VERSION,
            "outcome_id": outcome_id,
            "created_at": created_at,
            "need_id": source.need_id,
            "need_digest": source.need_digest,
            "work_id": source.work_id,
            "work_digest": source.work_digest,
            "workflow_run_id": source.workflow_run_id,
            "workflow_run_digest": source.workflow_run_digest,
            "attempt_id": attempt_id,
            "attempt_digest": attempt_digest,
            "status": status.value,
            "observation": frozen_observation,
            "error": error,
        }
        return cls(
            schema_version=CAPABILITY_OUTCOME_SCHEMA_VERSION,
            outcome_id=outcome_id,
            created_at=created_at,
            need_id=source.need_id,
            need_digest=source.need_digest,
            work_id=source.work_id,
            work_digest=source.work_digest,
            workflow_run_id=source.workflow_run_id,
            workflow_run_digest=source.workflow_run_digest,
            attempt_id=attempt_id,
            attempt_digest=attempt_digest,
            status=status,
            observation=frozen_observation,
            error=error,
            outcome_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != CAPABILITY_OUTCOME_SCHEMA_VERSION:
            raise InvalidCapabilityRecord("Unsupported CapabilityOutcome schema")
        _validate_uuid(self.outcome_id, "outcome_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.need_id, "need_id")
        _validate_digest(self.need_digest, "need_digest")
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_digest(self.workflow_run_digest, "workflow_run_digest")
        _bounded_text(self.attempt_id, "attempt_id", MAX_ATTEMPT_ID_CHARS)
        _validate_digest(self.attempt_digest, "attempt_digest")
        object.__setattr__(self, "status", CapabilityOutcomeStatus(self.status))
        frozen_observation = _freeze_json(self.observation)
        if not isinstance(frozen_observation, Mapping):
            raise InvalidCapabilityRecord("observation must be a JSON object")
        object.__setattr__(self, "observation", frozen_observation)
        if self.status is CapabilityOutcomeStatus.SUCCEEDED:
            if self.error is not None:
                raise InvalidCapabilityRecord(
                    "successful CapabilityOutcome cannot carry error"
                )
        else:
            _bounded_text(
                self.error,
                "error",
                MAX_ERROR_CHARS,
                canonical_trimmed=False,
            )
        _validate_digest(self.outcome_digest, "outcome_digest")
        if not hmac.compare_digest(
            self.outcome_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidCapabilityRecord("CapabilityOutcome digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outcome_id": self.outcome_id,
            "created_at": self.created_at,
            "need_id": self.need_id,
            "need_digest": self.need_digest,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_digest": self.workflow_run_digest,
            "attempt_id": self.attempt_id,
            "attempt_digest": self.attempt_digest,
            "status": self.status.value,
            "observation": self.observation,
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **_thaw_json(self._base_dict()),
            "outcome_digest": self.outcome_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapabilityOutcome:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "outcome_id",
                "created_at",
                "need_id",
                "need_digest",
                "work_id",
                "work_digest",
                "workflow_run_id",
                "workflow_run_digest",
                "attempt_id",
                "attempt_digest",
                "status",
                "observation",
                "error",
                "outcome_digest",
            },
            "CapabilityOutcome",
        )
        return cls(
            schema_version=value["schema_version"],
            outcome_id=value["outcome_id"],
            created_at=value["created_at"],
            need_id=value["need_id"],
            need_digest=value["need_digest"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            workflow_run_id=value["workflow_run_id"],
            workflow_run_digest=value["workflow_run_digest"],
            attempt_id=value["attempt_id"],
            attempt_digest=value["attempt_digest"],
            status=CapabilityOutcomeStatus(value["status"]),
            observation=value["observation"],
            error=value["error"],
            outcome_digest=value["outcome_digest"],
        )

    def to_event(self, snapshot: WorkSnapshot) -> WorkEvent:
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidCapabilityRecord(
                "CapabilityOutcome cannot be admitted to terminal Work"
            )
        if snapshot.work.work_id != self.work_id:
            raise InvalidCapabilityRecord("CapabilityOutcome belongs to another Work")
        if not hmac.compare_digest(
            snapshot.work.work_digest,
            self.work_digest,
        ):
            raise InvalidCapabilityRecord("CapabilityOutcome Work binding changed")
        return WorkEvent.create(
            work_id=self.work_id,
            sequence=snapshot.revision + 1,
            kind=CAPABILITY_OUTCOME_RECORDED_EVENT,
            payload={"capability_outcome": self.to_dict()},
            previous_event_digest=snapshot.last_event_digest,
            event_id=self.outcome_id,
            created_at=self.created_at,
        )


@dataclass(frozen=True, slots=True)
class CapabilityNeedSnapshot:
    """Derived lifecycle projection for one admitted CapabilityNeed."""

    need: CapabilityNeed
    state: CapabilityNeedState
    outcome: CapabilityOutcome | None

    def __post_init__(self) -> None:
        if not isinstance(self.need, CapabilityNeed):
            raise TypeError("need must be CapabilityNeed")
        object.__setattr__(self, "state", CapabilityNeedState(self.state))
        if self.state is CapabilityNeedState.PENDING:
            if self.outcome is not None:
                raise InvalidCapabilityRecord(
                    "pending CapabilityNeed cannot have outcome"
                )
            return
        if not isinstance(self.outcome, CapabilityOutcome):
            raise InvalidCapabilityRecord(
                "terminal CapabilityNeed requires CapabilityOutcome"
            )
        expected_state = {
            CapabilityOutcomeStatus.SUCCEEDED: CapabilityNeedState.SUCCEEDED,
            CapabilityOutcomeStatus.FAILED: CapabilityNeedState.FAILED,
            CapabilityOutcomeStatus.UNKNOWN: CapabilityNeedState.OUTCOME_UNKNOWN,
        }[self.outcome.status]
        if self.state is not expected_state:
            raise InvalidCapabilityRecord(
                "CapabilityNeed state does not match CapabilityOutcome"
            )
