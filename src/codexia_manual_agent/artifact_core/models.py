from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

ARTIFACT_REF_SCHEMA_VERSION = 1
ARTIFACT_REF_RECORDED_EVENT = "artifact.ref-recorded"
MAX_LOCATOR_CHARS = 4_096
MAX_MEDIA_TYPE_CHARS = 255
MAX_SIGNED_64 = (1 << 63) - 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidArtifactRef(ValueError):
    """Raised when a Gen2 ArtifactRef is structurally invalid."""


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
        raise InvalidArtifactRef("ArtifactRef is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidArtifactRef(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidArtifactRef(
            f"{field_name} must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise InvalidArtifactRef(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidArtifactRef(
            f"{field_name} must be lowercase SHA-256 hex"
        )
    return value


def _bounded_text(
    value: Any,
    field_name: str,
    *,
    max_chars: int,
) -> str:
    if not isinstance(value, str):
        raise InvalidArtifactRef(f"{field_name} must be text")
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > max_chars
    ):
        raise InvalidArtifactRef(
            f"{field_name} must be non-empty canonical trimmed text "
            f"within {max_chars} characters"
        )
    return value


def _optional_bounded_text(
    value: Any,
    field_name: str,
    *,
    max_chars: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name, max_chars=max_chars)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Portable content-bound reference to one artifact.

    The locator is an opaque storage reference, not proof that the artifact is
    currently reachable. This record grants no authority and asserts no Work
    ownership, completion, authorship, filesystem semantics, or evidence policy.

    artifact_id is stable reference identity. ref_digest binds that identity to
    exact content integrity, size, locator and type metadata.
    """

    schema_version: int
    artifact_id: str
    content_sha256: str
    size_bytes: int
    locator: str
    media_type: str | None
    ref_digest: str

    @classmethod
    def create(
        cls,
        *,
        content_sha256: str,
        size_bytes: int,
        locator: str,
        media_type: str | None = None,
        artifact_id: str | None = None,
    ) -> ArtifactRef:
        artifact_id = artifact_id or str(uuid4())
        _validate_uuid(artifact_id, "artifact_id")
        _validate_digest(content_sha256, "content_sha256")
        if (
            type(size_bytes) is not int
            or size_bytes < 0
            or size_bytes > MAX_SIGNED_64
        ):
            raise InvalidArtifactRef(
                "size_bytes must fit a non-negative signed 64-bit integer"
            )
        _bounded_text(locator, "locator", max_chars=MAX_LOCATOR_CHARS)
        _optional_bounded_text(
            media_type,
            "media_type",
            max_chars=MAX_MEDIA_TYPE_CHARS,
        )
        base = {
            "schema_version": ARTIFACT_REF_SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "content_sha256": content_sha256,
            "size_bytes": size_bytes,
            "locator": locator,
            "media_type": media_type,
        }
        return cls(**base, ref_digest=_digest(base))

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != ARTIFACT_REF_SCHEMA_VERSION
        ):
            raise InvalidArtifactRef("Unsupported ArtifactRef schema")
        _validate_uuid(self.artifact_id, "artifact_id")
        _validate_digest(self.content_sha256, "content_sha256")
        if (
            type(self.size_bytes) is not int
            or self.size_bytes < 0
            or self.size_bytes > MAX_SIGNED_64
        ):
            raise InvalidArtifactRef(
                "size_bytes must fit a non-negative signed 64-bit integer"
            )
        _bounded_text(self.locator, "locator", max_chars=MAX_LOCATOR_CHARS)
        _optional_bounded_text(
            self.media_type,
            "media_type",
            max_chars=MAX_MEDIA_TYPE_CHARS,
        )
        _validate_digest(self.ref_digest, "ref_digest")
        if not hmac.compare_digest(self.ref_digest, _digest(self._base_dict())):
            raise InvalidArtifactRef("ArtifactRef digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "locator": self.locator,
            "media_type": self.media_type,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "ref_digest": self.ref_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactRef:
        expected = {
            "schema_version",
            "artifact_id",
            "content_sha256",
            "size_bytes",
            "locator",
            "media_type",
            "ref_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise InvalidArtifactRef("ArtifactRef keys are not exact")
        return cls(
            schema_version=value["schema_version"],
            artifact_id=value["artifact_id"],
            content_sha256=value["content_sha256"],
            size_bytes=value["size_bytes"],
            locator=value["locator"],
            media_type=value["media_type"],
            ref_digest=value["ref_digest"],
        )
