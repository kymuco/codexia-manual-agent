from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

EVIDENCE_REF_SCHEMA_VERSION = 1
MAX_EVIDENCE_KIND_CHARS = 128
MAX_LOCATOR_CHARS = 4_096

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")


class InvalidEvidenceRef(ValueError):
    """Raised when a Gen2 EvidenceRef is structurally invalid."""


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
        raise InvalidEvidenceRef("EvidenceRef is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidEvidenceRef(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidEvidenceRef(
            f"{field_name} must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise InvalidEvidenceRef(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidEvidenceRef(
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
        raise InvalidEvidenceRef(f"{field_name} must be text")
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > max_chars
    ):
        raise InvalidEvidenceRef(
            f"{field_name} must be non-empty canonical trimmed text "
            f"within {max_chars} characters"
        )
    return value


def _evidence_kind(value: Any) -> str:
    value = _bounded_text(
        value,
        "evidence_kind",
        max_chars=MAX_EVIDENCE_KIND_CHARS,
    )
    if _EVIDENCE_KIND_RE.fullmatch(value) is None:
        raise InvalidEvidenceRef(
            "evidence_kind must be canonical lowercase semantic type text"
        )
    return value


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Portable integrity-bound reference to one evidence record.

    The referenced evidence owns its domain-specific payload, provenance,
    subject bindings and validity rules. EvidenceRef deliberately does not
    standardize statuses, observations, human authorship, execution receipts,
    metrics or scientific conclusions.

    The locator is opaque and grants no access or authority. Recording this
    reference does not assert that the evidence is currently reachable, true,
    sufficient for a claim, or adequate for Work completion.

    evidence_id is the stable identity of the referenced evidence record.
    ref_digest binds that identity to exact evidence integrity, semantic kind
    and locator.
    """

    schema_version: int
    evidence_id: str
    evidence_digest: str
    evidence_kind: str
    locator: str
    ref_digest: str

    @classmethod
    def create(
        cls,
        *,
        evidence_id: str,
        evidence_digest: str,
        evidence_kind: str,
        locator: str,
    ) -> EvidenceRef:
        _validate_uuid(evidence_id, "evidence_id")
        _validate_digest(evidence_digest, "evidence_digest")
        _evidence_kind(evidence_kind)
        _bounded_text(locator, "locator", max_chars=MAX_LOCATOR_CHARS)
        base = {
            "schema_version": EVIDENCE_REF_SCHEMA_VERSION,
            "evidence_id": evidence_id,
            "evidence_digest": evidence_digest,
            "evidence_kind": evidence_kind,
            "locator": locator,
        }
        return cls(**base, ref_digest=_digest(base))

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != EVIDENCE_REF_SCHEMA_VERSION
        ):
            raise InvalidEvidenceRef("Unsupported EvidenceRef schema")
        _validate_uuid(self.evidence_id, "evidence_id")
        _validate_digest(self.evidence_digest, "evidence_digest")
        _evidence_kind(self.evidence_kind)
        _bounded_text(self.locator, "locator", max_chars=MAX_LOCATOR_CHARS)
        _validate_digest(self.ref_digest, "ref_digest")
        if not hmac.compare_digest(self.ref_digest, _digest(self._base_dict())):
            raise InvalidEvidenceRef("EvidenceRef digest mismatch")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "evidence_digest": self.evidence_digest,
            "evidence_kind": self.evidence_kind,
            "locator": self.locator,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "ref_digest": self.ref_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceRef:
        expected = {
            "schema_version",
            "evidence_id",
            "evidence_digest",
            "evidence_kind",
            "locator",
            "ref_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise InvalidEvidenceRef("EvidenceRef keys are not exact")
        return cls(
            schema_version=value["schema_version"],
            evidence_id=value["evidence_id"],
            evidence_digest=value["evidence_digest"],
            evidence_kind=value["evidence_kind"],
            locator=value["locator"],
            ref_digest=value["ref_digest"],
        )
