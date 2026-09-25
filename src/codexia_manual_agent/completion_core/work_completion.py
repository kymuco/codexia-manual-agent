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

from codexia_manual_agent.completion_core.models import (
    COMPLETION_CLAIM_ADMITTED_EVENT,
    CompletionClaim,
)
from codexia_manual_agent.work_core import (
    WORK_COMPLETED_EVENT,
    WorkEvent,
    WorkSnapshot,
    WorkState,
)

WORK_COMPLETION_SCHEMA_VERSION = 1
MAX_WORK_COMPLETION_TIMESTAMP_CHARS = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidWorkCompletion(ValueError):
    """Raised when a Gen2 WorkCompletion is structurally invalid."""


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
        raise InvalidWorkCompletion(
            "WorkCompletion is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidWorkCompletion(
            f"{field_name} must be a canonical UUID"
        )
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidWorkCompletion(
            f"{field_name} must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise InvalidWorkCompletion(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidWorkCompletion(
            f"{field_name} must be lowercase SHA-256 hex"
        )
    return value


def _timestamp(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_WORK_COMPLETION_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidWorkCompletion(
            "created_at must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkCompletion(
            "created_at must be canonical ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidWorkCompletion(
            "created_at must be canonical ISO-8601"
        )
    return value


def _new_timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class WorkCompletion:
    """Immutable admitted semantic terminal-state record for one Work.

    WorkCompletion binds one exact durable CompletionClaim admission and is
    carried inside the resulting work.completed event. It is not cleanup proof,
    parent-HDE completion, execution authority, or a scheduler decision.
    """

    schema_version: int
    completion_id: str
    created_at: str
    work_id: str
    work_digest: str
    claim_id: str
    claim_digest: str
    claim_admission_sequence: int
    claim_admission_event_digest: str
    completion_digest: str

    @classmethod
    def create(
        cls,
        *,
        snapshot: WorkSnapshot,
        claim: CompletionClaim,
        claim_admission_event: WorkEvent,
        completion_id: str | None = None,
        created_at: str | None = None,
    ) -> WorkCompletion:
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidWorkCompletion(
                "WorkCompletion requires active Work"
            )
        if not isinstance(claim, CompletionClaim):
            raise TypeError("claim must be CompletionClaim")
        if not isinstance(claim_admission_event, WorkEvent):
            raise TypeError("claim_admission_event must be WorkEvent")
        if claim_admission_event.kind != COMPLETION_CLAIM_ADMITTED_EVENT:
            raise InvalidWorkCompletion(
                "WorkCompletion requires completion.claim-admitted event"
            )
        if snapshot.work.work_id != claim.work_id:
            raise InvalidWorkCompletion(
                "CompletionClaim belongs to another Work"
            )
        if not hmac.compare_digest(
            snapshot.work.work_digest,
            claim.work_digest,
        ):
            raise InvalidWorkCompletion(
                "CompletionClaim changed Work binding"
            )
        if claim_admission_event.work_id != claim.work_id:
            raise InvalidWorkCompletion(
                "claim admission event crossed Work identity"
            )
        if claim_admission_event.event_id != claim.claim_id:
            raise InvalidWorkCompletion(
                "claim admission event identity differs from CompletionClaim"
            )
        if claim_admission_event.sequence != claim.work_revision + 1:
            raise InvalidWorkCompletion(
                "claim admission event does not follow claimed Work revision"
            )
        if claim_admission_event.previous_event_digest != claim.work_event_digest:
            raise InvalidWorkCompletion(
                "claim admission event changed claimed Work chronology"
            )
        payload = claim_admission_event.to_dict()["payload"]
        if (
            not isinstance(payload, dict)
            or set(payload) != {"completion_claim"}
            or payload["completion_claim"] != claim.to_dict()
        ):
            raise InvalidWorkCompletion(
                "claim admission event does not contain exact CompletionClaim"
            )
        if snapshot.revision != claim_admission_event.sequence:
            raise InvalidWorkCompletion(
                "WorkCompletion snapshot is not exact claim-admission revision"
            )
        if snapshot.last_event_digest != claim_admission_event.event_digest:
            raise InvalidWorkCompletion(
                "WorkCompletion snapshot is not exact claim-admission chronology"
            )

        completion_id = completion_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(completion_id, "completion_id")
        _timestamp(created_at)

        base = {
            "schema_version": WORK_COMPLETION_SCHEMA_VERSION,
            "completion_id": completion_id,
            "created_at": created_at,
            "work_id": claim.work_id,
            "work_digest": claim.work_digest,
            "claim_id": claim.claim_id,
            "claim_digest": claim.claim_digest,
            "claim_admission_sequence": claim_admission_event.sequence,
            "claim_admission_event_digest": claim_admission_event.event_digest,
        }
        return cls(
            **base,
            completion_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != WORK_COMPLETION_SCHEMA_VERSION
        ):
            raise InvalidWorkCompletion(
                "Unsupported WorkCompletion schema"
            )
        _validate_uuid(self.completion_id, "completion_id")
        _timestamp(self.created_at)
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        _validate_uuid(self.claim_id, "claim_id")
        _validate_digest(self.claim_digest, "claim_digest")
        if (
            type(self.claim_admission_sequence) is not int
            or self.claim_admission_sequence <= 0
        ):
            raise InvalidWorkCompletion(
                "claim_admission_sequence must be a positive integer"
            )
        _validate_digest(
            self.claim_admission_event_digest,
            "claim_admission_event_digest",
        )
        _validate_digest(
            self.completion_digest,
            "completion_digest",
        )
        if not hmac.compare_digest(
            self.completion_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidWorkCompletion(
                "WorkCompletion digest mismatch"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "completion_id": self.completion_id,
            "created_at": self.created_at,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "claim_id": self.claim_id,
            "claim_digest": self.claim_digest,
            "claim_admission_sequence": self.claim_admission_sequence,
            "claim_admission_event_digest": self.claim_admission_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._base_dict(),
            "completion_digest": self.completion_digest,
        }

    def to_event(self) -> WorkEvent:
        return WorkEvent.create(
            work_id=self.work_id,
            sequence=self.claim_admission_sequence + 1,
            kind=WORK_COMPLETED_EVENT,
            payload={"work_completion": self.to_dict()},
            previous_event_digest=self.claim_admission_event_digest,
            event_id=self.completion_id,
            created_at=self.created_at,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkCompletion:
        expected = {
            "schema_version",
            "completion_id",
            "created_at",
            "work_id",
            "work_digest",
            "claim_id",
            "claim_digest",
            "claim_admission_sequence",
            "claim_admission_event_digest",
            "completion_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise InvalidWorkCompletion(
                "WorkCompletion keys are not exact"
            )
        return cls(
            schema_version=value["schema_version"],
            completion_id=value["completion_id"],
            created_at=value["created_at"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            claim_id=value["claim_id"],
            claim_digest=value["claim_digest"],
            claim_admission_sequence=value["claim_admission_sequence"],
            claim_admission_event_digest=value[
                "claim_admission_event_digest"
            ],
            completion_digest=value["completion_digest"],
        )
