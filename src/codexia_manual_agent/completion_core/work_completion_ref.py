from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidWorkCompletionRef(ValueError):
    """Raised when a WorkCompletionRef is structurally invalid."""


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidWorkCompletionRef(
            f"{field_name} must be a canonical UUID"
        )
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidWorkCompletionRef(
            f"{field_name} must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise InvalidWorkCompletionRef(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidWorkCompletionRef(
            f"{field_name} must be lowercase SHA-256 hex"
        )
    return value


@dataclass(frozen=True, slots=True)
class WorkCompletionRef:
    """Exact immutable reference to one semantic WorkCompletion terminal event.

    This value does not prove parent ownership, current reachability, completion
    quality, or that the referenced Work belongs to any particular Delegation.
    Those are admission-time judgments.
    """

    work_id: str
    completion_event_id: str
    completion_digest: str

    @classmethod
    def create(
        cls,
        *,
        work_id: str,
        completion_event_id: str,
        completion_digest: str,
    ) -> WorkCompletionRef:
        return cls(
            work_id=work_id,
            completion_event_id=completion_event_id,
            completion_digest=completion_digest,
        )

    def __post_init__(self) -> None:
        _validate_uuid(self.work_id, "work_id")
        _validate_uuid(
            self.completion_event_id,
            "completion_event_id",
        )
        _validate_digest(
            self.completion_digest,
            "completion_digest",
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "work_id": self.work_id,
            "completion_event_id": self.completion_event_id,
            "completion_digest": self.completion_digest,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> WorkCompletionRef:
        expected = {
            "work_id",
            "completion_event_id",
            "completion_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise InvalidWorkCompletionRef(
                "WorkCompletionRef keys are not exact"
            )
        return cls(
            work_id=value["work_id"],
            completion_event_id=value["completion_event_id"],
            completion_digest=value["completion_digest"],
        )
