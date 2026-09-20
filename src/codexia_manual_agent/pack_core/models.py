from __future__ import annotations

import hmac
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from codexia_manual_agent.work_core import WorkEvent, WorkSnapshot, WorkState
from codexia_manual_agent.workflow_core import (
    WorkflowRunSnapshot,
    WorkflowRunState,
)

PACK_MEMBER_BINDING_SCHEMA_VERSION = 1
PACK_BINDING_SCHEMA_VERSION = 1
PACK_WORKFLOW_BINDING_SCHEMA_VERSION = 1

PACK_WORKFLOW_BOUND_EVENT = "pack.workflow-bound"

MAX_PACK_ID_CHARS = 128
MAX_SEMANTIC_ID_CHARS = 128
MAX_VERSION_CHARS = 128
MAX_TIMESTAMP_CHARS = 64
MAX_PACK_MEMBERS = 4096

_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidPackRecord(ValueError):
    """Raised when a G2.7 semantic Pack record is structurally invalid."""


class PackMemberKind(StrEnum):
    WORKFLOW = "workflow"
    ROLE = "role"
    CAPABILITY = "capability"


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
        raise InvalidPackRecord("Pack record is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    record_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise InvalidPackRecord(f"{record_name} keys are not exact")
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidPackRecord(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidPackRecord(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidPackRecord(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidPackRecord(
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
        raise InvalidPackRecord(
            f"{field_name} must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidPackRecord(
            f"{field_name} must be canonical ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidPackRecord(
            f"{field_name} must be canonical ISO-8601"
        )
    return value


def _bounded_id(value: Any, field_name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidPackRecord(f"{field_name} must be text")
    normalized = value.strip().lower()
    if (
        normalized != value
        or not normalized
        or len(normalized) > max_chars
        or _ID_RE.fullmatch(normalized) is None
    ):
        raise InvalidPackRecord(f"{field_name} is not canonical")
    return normalized


def _bounded_version(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidPackRecord("version must be text")
    normalized = value.strip()
    if (
        normalized != value
        or not normalized
        or len(normalized) > MAX_VERSION_CHARS
        or "\x00" in normalized
    ):
        raise InvalidPackRecord("version is not canonical")
    return normalized


def _new_timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class PackMemberBinding:
    """Exact reference to one semantic definition already owned by Codexia.

    A member reference names semantics only. It carries no source path,
    entrypoint, plugin dependency, runtime policy, authority, or lifecycle.
    """

    schema_version: int
    kind: PackMemberKind
    semantic_id: str
    version: str
    binding_digest: str

    @classmethod
    def create(
        cls,
        *,
        kind: PackMemberKind,
        semantic_id: str,
        version: str,
        binding_digest: str,
    ) -> PackMemberBinding:
        return cls(
            schema_version=PACK_MEMBER_BINDING_SCHEMA_VERSION,
            kind=PackMemberKind(kind),
            semantic_id=_bounded_id(
                semantic_id,
                "semantic_id",
                MAX_SEMANTIC_ID_CHARS,
            ),
            version=_bounded_version(version),
            binding_digest=_validate_digest(
                binding_digest,
                "binding_digest",
            ),
        )

    def __post_init__(self) -> None:
        if self.schema_version != PACK_MEMBER_BINDING_SCHEMA_VERSION:
            raise InvalidPackRecord("Unsupported PackMemberBinding schema")
        object.__setattr__(self, "kind", PackMemberKind(self.kind))
        _bounded_id(self.semantic_id, "semantic_id", MAX_SEMANTIC_ID_CHARS)
        _bounded_version(self.version)
        _validate_digest(self.binding_digest, "binding_digest")

    @property
    def identity_key(self) -> tuple[str, str]:
        return (self.kind.value, self.semantic_id)

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (
            self.kind.value,
            self.semantic_id,
            self.version,
            self.binding_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "semantic_id": self.semantic_id,
            "version": self.version,
            "binding_digest": self.binding_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PackMemberBinding:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "kind",
                "semantic_id",
                "version",
                "binding_digest",
            },
            "PackMemberBinding",
        )
        return cls(
            schema_version=value["schema_version"],
            kind=PackMemberKind(value["kind"]),
            semantic_id=value["semantic_id"],
            version=value["version"],
            binding_digest=value["binding_digest"],
        )


@dataclass(frozen=True, slots=True)
class PackBinding:
    """Exact semantic identity of one versioned Codexia Pack.

    PackBinding is a semantic bundle, not a distribution or runtime unit.
    """

    schema_version: int
    pack_id: str
    version: str
    definition_digest: str
    members: tuple[PackMemberBinding, ...]
    binding_digest: str

    @classmethod
    def create(
        cls,
        *,
        pack_id: str,
        version: str,
        definition_digest: str,
        members: Iterable[PackMemberBinding],
    ) -> PackBinding:
        normalized = tuple(members)
        if not normalized:
            raise InvalidPackRecord("PackBinding requires at least one member")
        if len(normalized) > MAX_PACK_MEMBERS:
            raise InvalidPackRecord("PackBinding exceeds its member budget")
        if any(not isinstance(item, PackMemberBinding) for item in normalized):
            raise TypeError("members must contain PackMemberBinding values")
        ordered = tuple(sorted(normalized, key=lambda item: item.sort_key))
        keys = [item.identity_key for item in ordered]
        if len(set(keys)) != len(keys):
            raise InvalidPackRecord(
                "PackBinding cannot contain multiple bindings for one semantic identity"
            )
        base = {
            "schema_version": PACK_BINDING_SCHEMA_VERSION,
            "pack_id": _bounded_id(pack_id, "pack_id", MAX_PACK_ID_CHARS),
            "version": _bounded_version(version),
            "definition_digest": _validate_digest(
                definition_digest,
                "definition_digest",
            ),
            "members": [item.to_dict() for item in ordered],
        }
        return cls(
            schema_version=PACK_BINDING_SCHEMA_VERSION,
            pack_id=base["pack_id"],
            version=base["version"],
            definition_digest=base["definition_digest"],
            members=ordered,
            binding_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != PACK_BINDING_SCHEMA_VERSION:
            raise InvalidPackRecord("Unsupported PackBinding schema")
        _bounded_id(self.pack_id, "pack_id", MAX_PACK_ID_CHARS)
        _bounded_version(self.version)
        _validate_digest(self.definition_digest, "definition_digest")
        members = tuple(self.members)
        if not members:
            raise InvalidPackRecord("PackBinding requires at least one member")
        if len(members) > MAX_PACK_MEMBERS:
            raise InvalidPackRecord("PackBinding exceeds its member budget")
        if any(not isinstance(item, PackMemberBinding) for item in members):
            raise InvalidPackRecord(
                "members must contain PackMemberBinding values"
            )
        ordered = tuple(sorted(members, key=lambda item: item.sort_key))
        if members != ordered:
            raise InvalidPackRecord("PackBinding members must be canonical sorted")
        keys = [item.identity_key for item in members]
        if len(set(keys)) != len(keys):
            raise InvalidPackRecord(
                "PackBinding cannot contain multiple bindings for one semantic identity"
            )
        _validate_digest(self.binding_digest, "binding_digest")
        if not hmac.compare_digest(
            self.binding_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidPackRecord("PackBinding digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "version": self.version,
            "definition_digest": self.definition_digest,
            "members": [item.to_dict() for item in self.members],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "binding_digest": self.binding_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PackBinding:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "pack_id",
                "version",
                "definition_digest",
                "members",
                "binding_digest",
            },
            "PackBinding",
        )
        raw_members = value["members"]
        if not isinstance(raw_members, list):
            raise InvalidPackRecord("PackBinding members must be a list")
        return cls(
            schema_version=value["schema_version"],
            pack_id=value["pack_id"],
            version=value["version"],
            definition_digest=value["definition_digest"],
            members=tuple(
                PackMemberBinding.from_dict(item)
                for item in raw_members
            ),
            binding_digest=value["binding_digest"],
        )

    def contains(self, member: PackMemberBinding) -> bool:
        if not isinstance(member, PackMemberBinding):
            raise TypeError("member must be PackMemberBinding")
        return member in self.members


@dataclass(frozen=True, slots=True)
class PackWorkflowBinding:
    """Durable pin from one exact WorkflowRun to one exact PackBinding."""

    schema_version: int
    binding_id: str
    created_at: str
    work_id: str
    work_digest: str
    workflow_run_id: str
    workflow_run_digest: str
    pack: PackBinding
    start_revision: int
    start_event_digest: str
    pin_digest: str

    @classmethod
    def create(
        cls,
        *,
        workflow: WorkflowRunSnapshot,
        snapshot: WorkSnapshot,
        pack: PackBinding,
        binding_id: str | None = None,
        created_at: str | None = None,
    ) -> PackWorkflowBinding:
        if not isinstance(workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise InvalidPackRecord(
                "Pack cannot bind a terminal WorkflowRun"
            )
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidPackRecord(
                "PackWorkflowBinding requires active Work"
            )
        if not isinstance(pack, PackBinding):
            raise TypeError("pack must be PackBinding")

        run = workflow.run
        if run.work_id != snapshot.work.work_id:
            raise InvalidPackRecord(
                "WorkflowRun belongs to another Work"
            )
        if not hmac.compare_digest(
            run.work_digest,
            snapshot.work.work_digest,
        ):
            raise InvalidPackRecord("WorkflowRun Work binding changed")

        start_event = run.to_start_event()
        if snapshot.revision != start_event.sequence:
            raise InvalidPackRecord(
                "Pack must bind immediately after workflow.started"
            )
        if snapshot.last_event_digest != start_event.event_digest:
            raise InvalidPackRecord(
                "Pack must bind exact workflow.started chronology"
            )

        workflow_member = PackMemberBinding.create(
            kind=PackMemberKind.WORKFLOW,
            semantic_id=run.binding.workflow_id,
            version=run.binding.version,
            binding_digest=run.binding.binding_digest,
        )
        if not pack.contains(workflow_member):
            raise InvalidPackRecord(
                "Pack does not contain the exact WorkflowBinding"
            )

        binding_id = binding_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(binding_id, "binding_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": PACK_WORKFLOW_BINDING_SCHEMA_VERSION,
            "binding_id": binding_id,
            "created_at": created_at,
            "work_id": run.work_id,
            "work_digest": run.work_digest,
            "workflow_run_id": run.workflow_run_id,
            "workflow_run_digest": run.run_digest,
            "pack": pack.to_dict(),
            "start_revision": snapshot.revision,
            "start_event_digest": snapshot.last_event_digest,
        }
        return cls(
            schema_version=PACK_WORKFLOW_BINDING_SCHEMA_VERSION,
            binding_id=binding_id,
            created_at=created_at,
            work_id=run.work_id,
            work_digest=run.work_digest,
            workflow_run_id=run.workflow_run_id,
            workflow_run_digest=run.run_digest,
            pack=pack,
            start_revision=snapshot.revision,
            start_event_digest=snapshot.last_event_digest or "",
            pin_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != PACK_WORKFLOW_BINDING_SCHEMA_VERSION:
            raise InvalidPackRecord(
                "Unsupported PackWorkflowBinding schema"
            )
        _validate_uuid(self.binding_id, "binding_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_digest(
            self.workflow_run_digest,
            "workflow_run_digest",
        )
        if not isinstance(self.pack, PackBinding):
            raise InvalidPackRecord("pack must be PackBinding")
        if type(self.start_revision) is not int or self.start_revision < 1:
            raise InvalidPackRecord(
                "start_revision must identify workflow.started"
            )
        _validate_digest(self.start_event_digest, "start_event_digest")
        _validate_digest(self.pin_digest, "pin_digest")
        if not hmac.compare_digest(
            self.pin_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidPackRecord("PackWorkflowBinding digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "binding_id": self.binding_id,
            "created_at": self.created_at,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_digest": self.workflow_run_digest,
            "pack": self.pack.to_dict(),
            "start_revision": self.start_revision,
            "start_event_digest": self.start_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "pin_digest": self.pin_digest}

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> PackWorkflowBinding:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "binding_id",
                "created_at",
                "work_id",
                "work_digest",
                "workflow_run_id",
                "workflow_run_digest",
                "pack",
                "start_revision",
                "start_event_digest",
                "pin_digest",
            },
            "PackWorkflowBinding",
        )
        return cls(
            schema_version=value["schema_version"],
            binding_id=value["binding_id"],
            created_at=value["created_at"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            workflow_run_id=value["workflow_run_id"],
            workflow_run_digest=value["workflow_run_digest"],
            pack=PackBinding.from_dict(value["pack"]),
            start_revision=value["start_revision"],
            start_event_digest=value["start_event_digest"],
            pin_digest=value["pin_digest"],
        )

    def to_event(self) -> WorkEvent:
        return WorkEvent.create(
            work_id=self.work_id,
            sequence=self.start_revision + 1,
            kind=PACK_WORKFLOW_BOUND_EVENT,
            payload={"pack_workflow_binding": self.to_dict()},
            previous_event_digest=self.start_event_digest,
            event_id=self.binding_id,
            created_at=self.created_at,
        )
