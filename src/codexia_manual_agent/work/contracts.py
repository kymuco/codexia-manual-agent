from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

WORK_STATEMENT_SCHEMA_VERSION = 1
WORK_RESOURCE_SCHEMA_VERSION = 1
WORK_HANDOFF_SCHEMA_VERSION = 1
WORK_INTENT_INTERPRETATION_SCHEMA_VERSION = 1
ATTENTION_ASSESSMENT_SCHEMA_VERSION = 1

MAX_WORK_TEXT_CHARS = 16_384
MAX_ACTOR_CHARS = 256
MAX_RESOURCE_LOCATOR_CHARS = 8_192
MAX_RESOURCE_LABEL_CHARS = 512
MAX_HANDOFF_STATEMENTS = 128
MAX_HANDOFF_RESOURCES = 64
MAX_INTERPRETATION_BASIS = 128
MAX_ATTENTION_REASON_CHARS = 8_192
MAX_ATTENTION_RESPONSE_CHARS = 8_192
MAX_WORK_JSON_CHARS = 524_288
MAX_TIMESTAMP_CHARS = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidWorkRecordError(ValueError):
    """Raised when an M6 work-continuity record is structurally invalid."""


class WorkActorKind(StrEnum):
    HUMAN = "human"
    CODEXIA = "codexia"
    WORKER = "worker"
    SYSTEM = "system"


class WorkResourceKind(StrEnum):
    CHAT = "chat"
    REPOSITORY = "repository"
    FILE = "file"
    URL = "url"
    OTHER = "other"


class AttentionUrgency(StrEnum):
    NONE = "none"
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidWorkRecordError("Work payload is not canonical JSON") from exc
    if len(encoded) > MAX_WORK_JSON_CHARS:
        raise InvalidWorkRecordError("Work payload exceeds its structural budget")
    return encoded


def _digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidWorkRecordError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise InvalidWorkRecordError(
            f"{label} keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidWorkRecordError(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidWorkRecordError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidWorkRecordError(
            f"{field_name} must use lowercase hyphenated UUID form"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidWorkRecordError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidWorkRecordError(
            f"{field_name} must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError(
            f"{field_name} must be canonical ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidWorkRecordError(f"{field_name} must be canonical ISO-8601")
    return value


def _bounded_text(value: Any, field_name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidWorkRecordError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars or "\x00" in normalized:
        raise InvalidWorkRecordError(
            f"{field_name} is empty or exceeds its text budget"
        )
    return normalized


def _optional_bounded_text(
    value: Any,
    field_name: str,
    max_chars: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name, max_chars)


def _normalize_actor_kind(value: WorkActorKind | str) -> WorkActorKind:
    try:
        return WorkActorKind(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported work actor kind") from exc


def _normalize_resource_kind(value: WorkResourceKind | str) -> WorkResourceKind:
    try:
        return WorkResourceKind(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported work resource kind") from exc


def _normalize_attention_urgency(value: AttentionUrgency | str) -> AttentionUrgency:
    try:
        return AttentionUrgency(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported attention urgency") from exc


def _new_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class WorkStatement:
    """Attributed text whose author kind survives orchestration."""

    schema_version: int
    statement_id: str
    created_at: str
    author_kind: WorkActorKind
    actor: str
    text: str
    statement_digest: str

    @classmethod
    def create(
        cls,
        *,
        author_kind: WorkActorKind | str,
        actor: str,
        text: str,
        statement_id: str | None = None,
        created_at: str | None = None,
    ) -> "WorkStatement":
        statement_id = statement_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        kind = _normalize_actor_kind(author_kind)
        actor = _bounded_text(actor, "actor", MAX_ACTOR_CHARS)
        text = _bounded_text(text, "text", MAX_WORK_TEXT_CHARS)
        base = {
            "schema_version": WORK_STATEMENT_SCHEMA_VERSION,
            "statement_id": statement_id,
            "created_at": created_at,
            "author_kind": kind.value,
            "actor": actor,
            "text": text,
        }
        return cls(
            schema_version=WORK_STATEMENT_SCHEMA_VERSION,
            statement_id=statement_id,
            created_at=created_at,
            author_kind=kind,
            actor=actor,
            text=text,
            statement_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != WORK_STATEMENT_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported work statement schema")
        _validate_uuid(self.statement_id, "statement_id")
        _validate_timestamp(self.created_at, "created_at")
        object.__setattr__(self, "author_kind", _normalize_actor_kind(self.author_kind))
        object.__setattr__(
            self,
            "actor",
            _bounded_text(self.actor, "actor", MAX_ACTOR_CHARS),
        )
        object.__setattr__(
            self,
            "text",
            _bounded_text(self.text, "text", MAX_WORK_TEXT_CHARS),
        )
        _validate_digest(self.statement_digest, "statement_digest")
        if not hmac.compare_digest(self.statement_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "Work statement digest does not match its exact attributed text"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "statement_id": self.statement_id,
            "created_at": self.created_at,
            "author_kind": self.author_kind.value,
            "actor": self.actor,
            "text": self.text,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "statement_digest": self.statement_digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkStatement":
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "statement_id",
                "created_at",
                "author_kind",
                "actor",
                "text",
                "statement_digest",
            },
            "WorkStatement",
        )
        return cls(
            schema_version=value["schema_version"],
            statement_id=value["statement_id"],
            created_at=value["created_at"],
            author_kind=value["author_kind"],
            actor=value["actor"],
            text=value["text"],
            statement_digest=value["statement_digest"],
        )


@dataclass(frozen=True, slots=True)
class WorkResourceRef:
    """Context locator only; a reference grants no access or authority."""

    schema_version: int
    resource_id: str
    kind: WorkResourceKind
    locator: str
    label: str | None
    resource_digest: str

    @classmethod
    def create(
        cls,
        *,
        kind: WorkResourceKind | str,
        locator: str,
        label: str | None = None,
        resource_id: str | None = None,
    ) -> "WorkResourceRef":
        resource_id = resource_id or str(uuid4())
        kind = _normalize_resource_kind(kind)
        locator = _bounded_text(
            locator,
            "locator",
            MAX_RESOURCE_LOCATOR_CHARS,
        )
        label = _optional_bounded_text(
            label,
            "label",
            MAX_RESOURCE_LABEL_CHARS,
        )
        base = {
            "schema_version": WORK_RESOURCE_SCHEMA_VERSION,
            "resource_id": resource_id,
            "kind": kind.value,
            "locator": locator,
            "label": label,
        }
        return cls(
            schema_version=WORK_RESOURCE_SCHEMA_VERSION,
            resource_id=resource_id,
            kind=kind,
            locator=locator,
            label=label,
            resource_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != WORK_RESOURCE_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported work resource schema")
        _validate_uuid(self.resource_id, "resource_id")
        object.__setattr__(self, "kind", _normalize_resource_kind(self.kind))
        object.__setattr__(
            self,
            "locator",
            _bounded_text(
                self.locator,
                "locator",
                MAX_RESOURCE_LOCATOR_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "label",
            _optional_bounded_text(
                self.label,
                "label",
                MAX_RESOURCE_LABEL_CHARS,
            ),
        )
        _validate_digest(self.resource_digest, "resource_digest")
        if not hmac.compare_digest(self.resource_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "Work resource digest does not match its exact locator"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "resource_id": self.resource_id,
            "kind": self.kind.value,
            "locator": self.locator,
            "label": self.label,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "resource_digest": self.resource_digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkResourceRef":
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "resource_id",
                "kind",
                "locator",
                "label",
                "resource_digest",
            },
            "WorkResourceRef",
        )
        return cls(
            schema_version=value["schema_version"],
            resource_id=value["resource_id"],
            kind=value["kind"],
            locator=value["locator"],
            label=value["label"],
            resource_digest=value["resource_digest"],
        )


@dataclass(frozen=True, slots=True)
class WorkHandoff:
    """Exact human-authored delegated intent; never an execution authority grant."""

    schema_version: int
    handoff_id: str
    created_at: str
    objective: WorkStatement
    context: tuple[WorkStatement, ...]
    human_constraints: tuple[WorkStatement, ...]
    resources: tuple[WorkResourceRef, ...]
    plan_resource_id: str | None
    attention_constraints: tuple[WorkStatement, ...]
    handoff_digest: str

    @classmethod
    def create(
        cls,
        *,
        objective: WorkStatement,
        context: Iterable[WorkStatement] = (),
        human_constraints: Iterable[WorkStatement] = (),
        resources: Iterable[WorkResourceRef] = (),
        plan_resource_id: str | None = None,
        attention_constraints: Iterable[WorkStatement] = (),
        handoff_id: str | None = None,
        created_at: str | None = None,
    ) -> "WorkHandoff":
        if not isinstance(objective, WorkStatement):
            raise InvalidWorkRecordError("objective must be a WorkStatement")
        context = tuple(context)
        human_constraints = tuple(human_constraints)
        resources = tuple(resources)
        attention_constraints = tuple(attention_constraints)
        cls._validate_members(
            objective=objective,
            context=context,
            human_constraints=human_constraints,
            resources=resources,
            plan_resource_id=plan_resource_id,
            attention_constraints=attention_constraints,
        )
        handoff_id = handoff_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        base = {
            "schema_version": WORK_HANDOFF_SCHEMA_VERSION,
            "handoff_id": handoff_id,
            "created_at": created_at,
            "objective": objective.to_dict(),
            "context": [item.to_dict() for item in context],
            "human_constraints": [item.to_dict() for item in human_constraints],
            "resources": [item.to_dict() for item in resources],
            "plan_resource_id": plan_resource_id,
            "attention_constraints": [
                item.to_dict() for item in attention_constraints
            ],
        }
        return cls(
            schema_version=WORK_HANDOFF_SCHEMA_VERSION,
            handoff_id=handoff_id,
            created_at=created_at,
            objective=objective,
            context=context,
            human_constraints=human_constraints,
            resources=resources,
            plan_resource_id=plan_resource_id,
            attention_constraints=attention_constraints,
            handoff_digest=_digest(base),
        )

    @staticmethod
    def _validate_members(
        *,
        objective: WorkStatement,
        context: tuple[WorkStatement, ...],
        human_constraints: tuple[WorkStatement, ...],
        resources: tuple[WorkResourceRef, ...],
        plan_resource_id: str | None,
        attention_constraints: tuple[WorkStatement, ...],
    ) -> None:
        if objective.author_kind is not WorkActorKind.HUMAN:
            raise InvalidWorkRecordError(
                "A WorkHandoff objective must preserve explicit human authorship"
            )
        all_statements = (
            objective,
            *context,
            *human_constraints,
            *attention_constraints,
        )
        if len(all_statements) > MAX_HANDOFF_STATEMENTS:
            raise InvalidWorkRecordError("WorkHandoff exceeds its statement budget")
        if any(not isinstance(item, WorkStatement) for item in all_statements):
            raise InvalidWorkRecordError(
                "WorkHandoff statements must be WorkStatement records"
            )
        if any(
            item.author_kind is not WorkActorKind.HUMAN
            for item in human_constraints
        ):
            raise InvalidWorkRecordError(
                "human_constraints must preserve explicit human authorship"
            )
        if any(
            item.author_kind is not WorkActorKind.HUMAN
            for item in attention_constraints
        ):
            raise InvalidWorkRecordError(
                "attention_constraints must preserve explicit human authorship"
            )
        statement_ids = [item.statement_id for item in all_statements]
        if len(set(statement_ids)) != len(statement_ids):
            raise InvalidWorkRecordError(
                "WorkHandoff must not contain duplicate statement ids"
            )
        if len(resources) > MAX_HANDOFF_RESOURCES:
            raise InvalidWorkRecordError("WorkHandoff exceeds its resource budget")
        if any(not isinstance(item, WorkResourceRef) for item in resources):
            raise InvalidWorkRecordError(
                "WorkHandoff resources must be WorkResourceRef records"
            )
        resource_ids = [item.resource_id for item in resources]
        if len(set(resource_ids)) != len(resource_ids):
            raise InvalidWorkRecordError(
                "WorkHandoff must not contain duplicate resource ids"
            )
        if plan_resource_id is not None:
            _validate_uuid(plan_resource_id, "plan_resource_id")
            if plan_resource_id not in set(resource_ids):
                raise InvalidWorkRecordError(
                    "plan_resource_id must reference an exact handoff resource"
                )

    def __post_init__(self) -> None:
        if self.schema_version != WORK_HANDOFF_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported WorkHandoff schema")
        _validate_uuid(self.handoff_id, "handoff_id")
        _validate_timestamp(self.created_at, "created_at")
        if not isinstance(self.objective, WorkStatement):
            raise InvalidWorkRecordError("objective must be a WorkStatement")
        if not isinstance(self.context, tuple):
            raise InvalidWorkRecordError("context must be a tuple")
        if not isinstance(self.human_constraints, tuple):
            raise InvalidWorkRecordError("human_constraints must be a tuple")
        if not isinstance(self.resources, tuple):
            raise InvalidWorkRecordError("resources must be a tuple")
        if not isinstance(self.attention_constraints, tuple):
            raise InvalidWorkRecordError("attention_constraints must be a tuple")
        self._validate_members(
            objective=self.objective,
            context=self.context,
            human_constraints=self.human_constraints,
            resources=self.resources,
            plan_resource_id=self.plan_resource_id,
            attention_constraints=self.attention_constraints,
        )
        _validate_digest(self.handoff_digest, "handoff_digest")
        if not hmac.compare_digest(self.handoff_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "WorkHandoff digest does not match its exact delegated intent"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "handoff_id": self.handoff_id,
            "created_at": self.created_at,
            "objective": self.objective.to_dict(),
            "context": [item.to_dict() for item in self.context],
            "human_constraints": [
                item.to_dict() for item in self.human_constraints
            ],
            "resources": [item.to_dict() for item in self.resources],
            "plan_resource_id": self.plan_resource_id,
            "attention_constraints": [
                item.to_dict() for item in self.attention_constraints
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "handoff_digest": self.handoff_digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkHandoff":
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "handoff_id",
                "created_at",
                "objective",
                "context",
                "human_constraints",
                "resources",
                "plan_resource_id",
                "attention_constraints",
                "handoff_digest",
            },
            "WorkHandoff",
        )
        for field_name in (
            "context",
            "human_constraints",
            "resources",
            "attention_constraints",
        ):
            if not isinstance(value[field_name], list):
                raise InvalidWorkRecordError(
                    f"{field_name} must decode from a JSON array"
                )
        return cls(
            schema_version=value["schema_version"],
            handoff_id=value["handoff_id"],
            created_at=value["created_at"],
            objective=WorkStatement.from_dict(value["objective"]),
            context=tuple(
                WorkStatement.from_dict(item) for item in value["context"]
            ),
            human_constraints=tuple(
                WorkStatement.from_dict(item)
                for item in value["human_constraints"]
            ),
            resources=tuple(
                WorkResourceRef.from_dict(item) for item in value["resources"]
            ),
            plan_resource_id=value["plan_resource_id"],
            attention_constraints=tuple(
                WorkStatement.from_dict(item)
                for item in value["attention_constraints"]
            ),
            handoff_digest=value["handoff_digest"],
        )


@dataclass(frozen=True, slots=True)
class WorkIntentInterpretation:
    """Derived reading of a handoff; changing it never rewrites human intent."""

    schema_version: int
    interpretation_id: str
    created_at: str
    handoff_id: str
    handoff_digest: str
    interpreter_kind: WorkActorKind
    interpreter: str
    basis_statement_digests: tuple[str, ...]
    completion_expectation: str
    continuation_scope: str
    depth_interpretation: str
    interpretation_digest: str

    @classmethod
    def create(
        cls,
        *,
        handoff: WorkHandoff,
        interpreter_kind: WorkActorKind | str,
        interpreter: str,
        basis_statements: Iterable[WorkStatement],
        completion_expectation: str,
        continuation_scope: str,
        depth_interpretation: str,
        interpretation_id: str | None = None,
        created_at: str | None = None,
    ) -> "WorkIntentInterpretation":
        if not isinstance(handoff, WorkHandoff):
            raise InvalidWorkRecordError("handoff must be a WorkHandoff")
        kind = _normalize_actor_kind(interpreter_kind)
        if kind is WorkActorKind.HUMAN:
            raise InvalidWorkRecordError(
                "Intent interpretation cannot impersonate a human source"
            )
        basis = tuple(basis_statements)
        cls._validate_basis(handoff, basis)
        interpretation_id = interpretation_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        interpreter = _bounded_text(
            interpreter,
            "interpreter",
            MAX_ACTOR_CHARS,
        )
        completion_expectation = _bounded_text(
            completion_expectation,
            "completion_expectation",
            MAX_WORK_TEXT_CHARS,
        )
        continuation_scope = _bounded_text(
            continuation_scope,
            "continuation_scope",
            MAX_WORK_TEXT_CHARS,
        )
        depth_interpretation = _bounded_text(
            depth_interpretation,
            "depth_interpretation",
            MAX_WORK_TEXT_CHARS,
        )
        digests = tuple(item.statement_digest for item in basis)
        base = {
            "schema_version": WORK_INTENT_INTERPRETATION_SCHEMA_VERSION,
            "interpretation_id": interpretation_id,
            "created_at": created_at,
            "handoff_id": handoff.handoff_id,
            "handoff_digest": handoff.handoff_digest,
            "interpreter_kind": kind.value,
            "interpreter": interpreter,
            "basis_statement_digests": list(digests),
            "completion_expectation": completion_expectation,
            "continuation_scope": continuation_scope,
            "depth_interpretation": depth_interpretation,
        }
        return cls(
            schema_version=WORK_INTENT_INTERPRETATION_SCHEMA_VERSION,
            interpretation_id=interpretation_id,
            created_at=created_at,
            handoff_id=handoff.handoff_id,
            handoff_digest=handoff.handoff_digest,
            interpreter_kind=kind,
            interpreter=interpreter,
            basis_statement_digests=digests,
            completion_expectation=completion_expectation,
            continuation_scope=continuation_scope,
            depth_interpretation=depth_interpretation,
            interpretation_digest=_digest(base),
        )

    @staticmethod
    def _validate_basis(
        handoff: WorkHandoff,
        basis: tuple[WorkStatement, ...],
    ) -> None:
        if not basis or len(basis) > MAX_INTERPRETATION_BASIS:
            raise InvalidWorkRecordError(
                "Intent interpretation requires a bounded non-empty statement basis"
            )
        if any(not isinstance(item, WorkStatement) for item in basis):
            raise InvalidWorkRecordError(
                "Intent interpretation basis must contain WorkStatement records"
            )
        digests = tuple(item.statement_digest for item in basis)
        if len(set(digests)) != len(digests):
            raise InvalidWorkRecordError(
                "Intent interpretation basis must not contain duplicate statements"
            )
        handoff_statements = (
            handoff.objective,
            *handoff.context,
            *handoff.human_constraints,
            *handoff.attention_constraints,
        )
        handoff_digests = {item.statement_digest for item in handoff_statements}
        if handoff.objective.statement_digest not in digests:
            raise InvalidWorkRecordError(
                "Intent interpretation must bind the exact human objective"
            )
        if not set(digests).issubset(handoff_digests):
            raise InvalidWorkRecordError(
                "Intent interpretation basis references statements outside the handoff"
            )

    def __post_init__(self) -> None:
        if self.schema_version != WORK_INTENT_INTERPRETATION_SCHEMA_VERSION:
            raise InvalidWorkRecordError(
                "Unsupported work intent interpretation schema"
            )
        _validate_uuid(self.interpretation_id, "interpretation_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.handoff_id, "handoff_id")
        _validate_digest(self.handoff_digest, "handoff_digest")
        kind = _normalize_actor_kind(self.interpreter_kind)
        if kind is WorkActorKind.HUMAN:
            raise InvalidWorkRecordError(
                "Intent interpretation cannot impersonate a human source"
            )
        object.__setattr__(self, "interpreter_kind", kind)
        object.__setattr__(
            self,
            "interpreter",
            _bounded_text(self.interpreter, "interpreter", MAX_ACTOR_CHARS),
        )
        if (
            not isinstance(self.basis_statement_digests, tuple)
            or not self.basis_statement_digests
            or len(self.basis_statement_digests) > MAX_INTERPRETATION_BASIS
        ):
            raise InvalidWorkRecordError(
                "basis_statement_digests must be a bounded non-empty tuple"
            )
        for item in self.basis_statement_digests:
            _validate_digest(item, "basis_statement_digest")
        if len(set(self.basis_statement_digests)) != len(
            self.basis_statement_digests
        ):
            raise InvalidWorkRecordError(
                "basis_statement_digests must not contain duplicates"
            )
        object.__setattr__(
            self,
            "completion_expectation",
            _bounded_text(
                self.completion_expectation,
                "completion_expectation",
                MAX_WORK_TEXT_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "continuation_scope",
            _bounded_text(
                self.continuation_scope,
                "continuation_scope",
                MAX_WORK_TEXT_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "depth_interpretation",
            _bounded_text(
                self.depth_interpretation,
                "depth_interpretation",
                MAX_WORK_TEXT_CHARS,
            ),
        )
        _validate_digest(self.interpretation_digest, "interpretation_digest")
        if not hmac.compare_digest(
            self.interpretation_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidWorkRecordError(
                "Intent interpretation digest does not match its exact payload"
            )

    def assert_binds(self, handoff: WorkHandoff) -> None:
        if (
            self.handoff_id != handoff.handoff_id
            or not hmac.compare_digest(
                self.handoff_digest,
                handoff.handoff_digest,
            )
        ):
            raise InvalidWorkRecordError(
                "Intent interpretation does not bind the exact WorkHandoff"
            )
        handoff_digests = {
            item.statement_digest
            for item in (
                handoff.objective,
                *handoff.context,
                *handoff.human_constraints,
                *handoff.attention_constraints,
            )
        }
        if handoff.objective.statement_digest not in self.basis_statement_digests:
            raise InvalidWorkRecordError(
                "Intent interpretation does not bind the exact human objective"
            )
        if not set(self.basis_statement_digests).issubset(handoff_digests):
            raise InvalidWorkRecordError(
                "Intent interpretation basis no longer belongs to the handoff"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "interpretation_id": self.interpretation_id,
            "created_at": self.created_at,
            "handoff_id": self.handoff_id,
            "handoff_digest": self.handoff_digest,
            "interpreter_kind": self.interpreter_kind.value,
            "interpreter": self.interpreter,
            "basis_statement_digests": list(self.basis_statement_digests),
            "completion_expectation": self.completion_expectation,
            "continuation_scope": self.continuation_scope,
            "depth_interpretation": self.depth_interpretation,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._base_dict(),
            "interpretation_digest": self.interpretation_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkIntentInterpretation":
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "interpretation_id",
                "created_at",
                "handoff_id",
                "handoff_digest",
                "interpreter_kind",
                "interpreter",
                "basis_statement_digests",
                "completion_expectation",
                "continuation_scope",
                "depth_interpretation",
                "interpretation_digest",
            },
            "WorkIntentInterpretation",
        )
        digests = value["basis_statement_digests"]
        if not isinstance(digests, list):
            raise InvalidWorkRecordError(
                "basis_statement_digests must decode from a JSON array"
            )
        return cls(
            schema_version=value["schema_version"],
            interpretation_id=value["interpretation_id"],
            created_at=value["created_at"],
            handoff_id=value["handoff_id"],
            handoff_digest=value["handoff_digest"],
            interpreter_kind=value["interpreter_kind"],
            interpreter=value["interpreter"],
            basis_statement_digests=tuple(digests),
            completion_expectation=value["completion_expectation"],
            continuation_scope=value["continuation_scope"],
            depth_interpretation=value["depth_interpretation"],
            interpretation_digest=value["interpretation_digest"],
        )


@dataclass(frozen=True, slots=True)
class AttentionAssessment:
    """Dynamic attention judgment; never an execution or permission grant."""

    schema_version: int
    assessment_id: str
    created_at: str
    handoff_id: str
    handoff_digest: str
    interpretation_id: str
    interpretation_digest: str
    checkpoint_digest: str
    assessor_kind: WorkActorKind
    assessor: str
    needs_human: bool
    confidence_basis_points: int
    urgency: AttentionUrgency
    reason: str
    requested_response: str | None
    assessment_digest: str

    @classmethod
    def create(
        cls,
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        checkpoint_digest: str,
        assessor_kind: WorkActorKind | str,
        assessor: str,
        needs_human: bool,
        confidence_basis_points: int,
        urgency: AttentionUrgency | str,
        reason: str,
        requested_response: str | None = None,
        assessment_id: str | None = None,
        created_at: str | None = None,
    ) -> "AttentionAssessment":
        if not isinstance(handoff, WorkHandoff):
            raise InvalidWorkRecordError("handoff must be a WorkHandoff")
        if not isinstance(interpretation, WorkIntentInterpretation):
            raise InvalidWorkRecordError(
                "interpretation must be a WorkIntentInterpretation"
            )
        interpretation.assert_binds(handoff)
        kind = _normalize_actor_kind(assessor_kind)
        if kind is WorkActorKind.HUMAN:
            raise InvalidWorkRecordError(
                "Dynamic attention assessment cannot impersonate human judgment"
            )
        assessment_id = assessment_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_digest(checkpoint_digest, "checkpoint_digest")
        urgency = _normalize_attention_urgency(urgency)
        assessor = _bounded_text(assessor, "assessor", MAX_ACTOR_CHARS)
        reason = _bounded_text(
            reason,
            "reason",
            MAX_ATTENTION_REASON_CHARS,
        )
        requested_response = _optional_bounded_text(
            requested_response,
            "requested_response",
            MAX_ATTENTION_RESPONSE_CHARS,
        )
        cls._validate_decision_shape(
            needs_human=needs_human,
            confidence_basis_points=confidence_basis_points,
            urgency=urgency,
            requested_response=requested_response,
        )
        base = {
            "schema_version": ATTENTION_ASSESSMENT_SCHEMA_VERSION,
            "assessment_id": assessment_id,
            "created_at": created_at,
            "handoff_id": handoff.handoff_id,
            "handoff_digest": handoff.handoff_digest,
            "interpretation_id": interpretation.interpretation_id,
            "interpretation_digest": interpretation.interpretation_digest,
            "checkpoint_digest": checkpoint_digest,
            "assessor_kind": kind.value,
            "assessor": assessor,
            "needs_human": needs_human,
            "confidence_basis_points": confidence_basis_points,
            "urgency": urgency.value,
            "reason": reason,
            "requested_response": requested_response,
        }
        return cls(
            schema_version=ATTENTION_ASSESSMENT_SCHEMA_VERSION,
            assessment_id=assessment_id,
            created_at=created_at,
            handoff_id=handoff.handoff_id,
            handoff_digest=handoff.handoff_digest,
            interpretation_id=interpretation.interpretation_id,
            interpretation_digest=interpretation.interpretation_digest,
            checkpoint_digest=checkpoint_digest,
            assessor_kind=kind,
            assessor=assessor,
            needs_human=needs_human,
            confidence_basis_points=confidence_basis_points,
            urgency=urgency,
            reason=reason,
            requested_response=requested_response,
            assessment_digest=_digest(base),
        )

    @staticmethod
    def _validate_decision_shape(
        *,
        needs_human: Any,
        confidence_basis_points: Any,
        urgency: AttentionUrgency,
        requested_response: str | None,
    ) -> None:
        if type(needs_human) is not bool:
            raise InvalidWorkRecordError("needs_human must be boolean")
        if (
            type(confidence_basis_points) is not int
            or not 0 <= confidence_basis_points <= 10_000
        ):
            raise InvalidWorkRecordError(
                "confidence_basis_points must be an integer in [0, 10000]"
            )
        if not needs_human:
            if urgency is not AttentionUrgency.NONE:
                raise InvalidWorkRecordError(
                    "No-attention assessment must use urgency=none"
                )
            if requested_response is not None:
                raise InvalidWorkRecordError(
                    "No-attention assessment cannot request a human response"
                )
        elif urgency is AttentionUrgency.NONE:
            raise InvalidWorkRecordError(
                "Human-attention assessment must use non-none urgency"
            )

    def __post_init__(self) -> None:
        if self.schema_version != ATTENTION_ASSESSMENT_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported attention assessment schema")
        _validate_uuid(self.assessment_id, "assessment_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.handoff_id, "handoff_id")
        _validate_digest(self.handoff_digest, "handoff_digest")
        _validate_uuid(self.interpretation_id, "interpretation_id")
        _validate_digest(self.interpretation_digest, "interpretation_digest")
        _validate_digest(self.checkpoint_digest, "checkpoint_digest")
        kind = _normalize_actor_kind(self.assessor_kind)
        if kind is WorkActorKind.HUMAN:
            raise InvalidWorkRecordError(
                "Dynamic attention assessment cannot impersonate human judgment"
            )
        object.__setattr__(self, "assessor_kind", kind)
        object.__setattr__(
            self,
            "assessor",
            _bounded_text(self.assessor, "assessor", MAX_ACTOR_CHARS),
        )
        urgency = _normalize_attention_urgency(self.urgency)
        object.__setattr__(self, "urgency", urgency)
        object.__setattr__(
            self,
            "reason",
            _bounded_text(
                self.reason,
                "reason",
                MAX_ATTENTION_REASON_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "requested_response",
            _optional_bounded_text(
                self.requested_response,
                "requested_response",
                MAX_ATTENTION_RESPONSE_CHARS,
            ),
        )
        self._validate_decision_shape(
            needs_human=self.needs_human,
            confidence_basis_points=self.confidence_basis_points,
            urgency=self.urgency,
            requested_response=self.requested_response,
        )
        _validate_digest(self.assessment_digest, "assessment_digest")
        if not hmac.compare_digest(
            self.assessment_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidWorkRecordError(
                "Attention assessment digest does not match its exact judgment"
            )

    def assert_binds(
        self,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
    ) -> None:
        interpretation.assert_binds(handoff)
        if (
            self.handoff_id != handoff.handoff_id
            or not hmac.compare_digest(
                self.handoff_digest,
                handoff.handoff_digest,
            )
            or self.interpretation_id != interpretation.interpretation_id
            or not hmac.compare_digest(
                self.interpretation_digest,
                interpretation.interpretation_digest,
            )
        ):
            raise InvalidWorkRecordError(
                "Attention assessment does not bind the exact handoff interpretation"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assessment_id": self.assessment_id,
            "created_at": self.created_at,
            "handoff_id": self.handoff_id,
            "handoff_digest": self.handoff_digest,
            "interpretation_id": self.interpretation_id,
            "interpretation_digest": self.interpretation_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "assessor_kind": self.assessor_kind.value,
            "assessor": self.assessor,
            "needs_human": self.needs_human,
            "confidence_basis_points": self.confidence_basis_points,
            "urgency": self.urgency.value,
            "reason": self.reason,
            "requested_response": self.requested_response,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._base_dict(),
            "assessment_digest": self.assessment_digest,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AttentionAssessment":
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "assessment_id",
                "created_at",
                "handoff_id",
                "handoff_digest",
                "interpretation_id",
                "interpretation_digest",
                "checkpoint_digest",
                "assessor_kind",
                "assessor",
                "needs_human",
                "confidence_basis_points",
                "urgency",
                "reason",
                "requested_response",
                "assessment_digest",
            },
            "AttentionAssessment",
        )
        return cls(
            schema_version=value["schema_version"],
            assessment_id=value["assessment_id"],
            created_at=value["created_at"],
            handoff_id=value["handoff_id"],
            handoff_digest=value["handoff_digest"],
            interpretation_id=value["interpretation_id"],
            interpretation_digest=value["interpretation_digest"],
            checkpoint_digest=value["checkpoint_digest"],
            assessor_kind=value["assessor_kind"],
            assessor=value["assessor"],
            needs_human=value["needs_human"],
            confidence_basis_points=value["confidence_basis_points"],
            urgency=value["urgency"],
            reason=value["reason"],
            requested_response=value["requested_response"],
            assessment_digest=value["assessment_digest"],
        )
