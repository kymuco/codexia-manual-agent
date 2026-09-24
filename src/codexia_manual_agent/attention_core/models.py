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

from codexia_manual_agent.work_core import WorkEvent, WorkSnapshot, WorkState
from codexia_manual_agent.workflow_core import (
    WorkflowCandidate,
    WorkflowRunSnapshot,
    WorkflowRunState,
)

ATTENTION_NEED_SCHEMA_VERSION = 1
ATTENTION_NEED_DECLARED_EVENT = "attention.need-declared"

MAX_ATTENTION_TEXT_CHARS = 16_384
MAX_TIMESTAMP_CHARS = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidAttentionRecord(ValueError):
    """Raised when a Gen2 AttentionNeed is structurally invalid."""


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
        raise InvalidAttentionRecord("Attention record is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    record_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise InvalidAttentionRecord(f"{record_name} keys are not exact")
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidAttentionRecord(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidAttentionRecord(
            f"{field_name} must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise InvalidAttentionRecord(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidAttentionRecord(
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
        raise InvalidAttentionRecord(
            f"{field_name} must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidAttentionRecord(
            f"{field_name} must be canonical ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidAttentionRecord(
            f"{field_name} must be canonical ISO-8601"
        )
    return value


def _new_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _bounded_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidAttentionRecord(f"{field_name} must be text")
    if (
        not value.strip()
        or len(value) > MAX_ATTENTION_TEXT_CHARS
        or "\x00" in value
    ):
        raise InvalidAttentionRecord(
            f"{field_name} is empty or exceeds its text budget"
        )
    if value != value.strip():
        raise InvalidAttentionRecord(f"{field_name} must be canonical trimmed text")
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
class AttentionNeed:
    """Durable statement that one exact Workflow state needs human judgment.

    It is not notification policy, execution authority, approval, scheduling,
    or proof that the human has been interrupted.
    """

    schema_version: int
    attention_id: str
    created_at: str
    work_id: str
    work_digest: str
    workflow_run_id: str
    workflow_run_digest: str
    question: str
    reason: str
    start_revision: int
    start_event_digest: str | None
    need_digest: str

    @classmethod
    def create(
        cls,
        *,
        workflow: WorkflowRunSnapshot,
        snapshot: WorkSnapshot,
        question: str,
        reason: str,
        attention_id: str | None = None,
        created_at: str | None = None,
    ) -> AttentionNeed:
        if not isinstance(workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise InvalidAttentionRecord(
                "AttentionNeed cannot start under terminal WorkflowRun"
            )
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidAttentionRecord("AttentionNeed cannot start on terminal Work")
        if workflow.run.work_id != snapshot.work.work_id:
            raise InvalidAttentionRecord("WorkflowRun belongs to another Work")
        if not hmac.compare_digest(
            workflow.run.work_digest,
            snapshot.work.work_digest,
        ):
            raise InvalidAttentionRecord("WorkflowRun work binding changed")

        attention_id = attention_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(attention_id, "attention_id")
        _validate_timestamp(created_at, "created_at")
        question = _bounded_text(question, "question")
        reason = _bounded_text(reason, "reason")

        base = {
            "schema_version": ATTENTION_NEED_SCHEMA_VERSION,
            "attention_id": attention_id,
            "created_at": created_at,
            "work_id": snapshot.work.work_id,
            "work_digest": snapshot.work.work_digest,
            "workflow_run_id": workflow.run.workflow_run_id,
            "workflow_run_digest": workflow.run.run_digest,
            "question": question,
            "reason": reason,
            "start_revision": snapshot.revision,
            "start_event_digest": snapshot.last_event_digest,
        }
        return cls(**base, need_digest=_digest(base))

    def __post_init__(self) -> None:
        if self.schema_version != ATTENTION_NEED_SCHEMA_VERSION:
            raise InvalidAttentionRecord("Unsupported AttentionNeed schema")
        _validate_uuid(self.attention_id, "attention_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_digest(self.workflow_run_digest, "workflow_run_digest")
        _bounded_text(self.question, "question")
        _bounded_text(self.reason, "reason")
        if type(self.start_revision) is not int or self.start_revision < 0:
            raise InvalidAttentionRecord(
                "start_revision must be non-negative integer"
            )
        if self.start_event_digest is None:
            if self.start_revision != 0:
                raise InvalidAttentionRecord(
                    "nonzero start_revision requires start_event_digest"
                )
        else:
            _validate_digest(self.start_event_digest, "start_event_digest")
            if self.start_revision == 0:
                raise InvalidAttentionRecord(
                    "revision zero cannot have start_event_digest"
                )
        _validate_digest(self.need_digest, "need_digest")
        if not hmac.compare_digest(self.need_digest, _digest(self._base_dict())):
            raise InvalidAttentionRecord("AttentionNeed digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attention_id": self.attention_id,
            "created_at": self.created_at,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_digest": self.workflow_run_digest,
            "question": self.question,
            "reason": self.reason,
            "start_revision": self.start_revision,
            "start_event_digest": self.start_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "need_digest": self.need_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AttentionNeed:
        value = _exact_keys(
            value,
            {
                "schema_version",
                "attention_id",
                "created_at",
                "work_id",
                "work_digest",
                "workflow_run_id",
                "workflow_run_digest",
                "question",
                "reason",
                "start_revision",
                "start_event_digest",
                "need_digest",
            },
            "AttentionNeed",
        )
        return cls(
            schema_version=value["schema_version"],
            attention_id=value["attention_id"],
            created_at=value["created_at"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            workflow_run_id=value["workflow_run_id"],
            workflow_run_digest=value["workflow_run_digest"],
            question=value["question"],
            reason=value["reason"],
            start_revision=value["start_revision"],
            start_event_digest=value["start_event_digest"],
            need_digest=value["need_digest"],
        )

    def to_workflow_candidate(self) -> WorkflowCandidate:
        event = WorkEvent.create(
            work_id=self.work_id,
            sequence=self.start_revision + 1,
            kind=ATTENTION_NEED_DECLARED_EVENT,
            payload=_workflow_wrapped_payload(
                workflow_run_id=self.workflow_run_id,
                workflow_run_digest=self.workflow_run_digest,
                payload={"attention_need": self.to_dict()},
            ),
            previous_event_digest=self.start_event_digest,
            event_id=self.attention_id,
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
