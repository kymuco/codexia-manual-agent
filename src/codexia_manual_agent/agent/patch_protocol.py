from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Mapping


from codexia_manual_agent.domain.errors import ProtocolError


MODEL_PATCH_REQUEST_SCHEMA_VERSION = 1
MAX_MODEL_PATCH_FILES = 32
MAX_MODEL_PATCH_FILE_BYTES = 1_048_576
MAX_MODEL_PATCH_TOTAL_CONTENT_BYTES = 4 * 1024 * 1024
MAX_MODEL_PATCH_REQUEST_CHARS = MAX_MODEL_PATCH_TOTAL_CONTENT_BYTES + 512 * 1024
MAX_MODEL_PATCH_TARGET_CHARS = 4096

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
_LINE_END = re.compile(r"\r\n|\n|\r")
_CLOSING_FENCE = re.compile(r"(?:\r\n|\n|\r)[ \t]*```[ \t]*\Z")


class ModelPatchOperation(StrEnum):
    CREATE = "create"
    REPLACE = "replace"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _utf8_bytes(value: str, *, label: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProtocolError(f"{label} must be valid UTF-8 text") from exc


def _request_digest(*, request_id: str, changes: tuple["ModelPatchChangeRequest", ...]) -> str:
    payload = {
        "schema_version": MODEL_PATCH_REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "changes": [change.to_digest_dict() for change in changes],
    }
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"Model patch request contains invalid JSON constant: {value}")


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError(f"Model patch request contains duplicate JSON key: {key}")
        value[key] = item
    return value


def _unwrap_json_fence(text: str) -> str:
    stripped = text.strip(" \t\r\n")
    if not stripped.startswith("```"):
        return stripped

    opening_break = _LINE_END.search(stripped)
    if opening_break is None:
        raise ProtocolError("Model patch request contains an incomplete code fence")
    opening = stripped[: opening_break.start()].strip(" \t").lower()
    if opening not in {"```", "```json"}:
        raise ProtocolError("Only a single JSON code fence is allowed")

    body_and_closing = stripped[opening_break.end() :]
    closing = _CLOSING_FENCE.search(body_and_closing)
    if closing is None:
        raise ProtocolError("Model patch request contains an incomplete code fence")
    payload = body_and_closing[: closing.start()]
    return payload.strip(" \t\r\n")


def _require_exact_keys(
    payload: Mapping[str, Any],
    expected: set[str],
    *,
    label: str,
) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProtocolError(
            f"{label} keys mismatch; missing={missing}, extra={extra}"
        )


@dataclass(frozen=True, slots=True)
class ModelPatchChangeRequest:
    operation: ModelPatchOperation
    target: str
    content: str

    def __post_init__(self) -> None:
        try:
            operation = ModelPatchOperation(self.operation)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                "Model patch operation must be 'create' or 'replace'"
            ) from exc
        if not isinstance(self.target, str) or not self.target.strip():
            raise ProtocolError("Model patch target must be non-empty text")
        if len(self.target) > MAX_MODEL_PATCH_TARGET_CHARS:
            raise ProtocolError(
                "Model patch target exceeds protocol limit "
                f"({len(self.target)} > {MAX_MODEL_PATCH_TARGET_CHARS} chars)"
            )
        if "\x00" in self.target:
            raise ProtocolError("Model patch target cannot contain NUL")
        _utf8_bytes(self.target, label="Model patch target")
        if not isinstance(self.content, str):
            raise ProtocolError("Model patch content must be UTF-8 text")
        content_bytes = _utf8_bytes(self.content, label="Model patch content")
        if len(content_bytes) > MAX_MODEL_PATCH_FILE_BYTES:
            raise ProtocolError(
                "Model patch file content exceeds protocol limit "
                f"({len(content_bytes)} > {MAX_MODEL_PATCH_FILE_BYTES} bytes)"
            )
        object.__setattr__(self, "operation", operation)

    @property
    def content_bytes(self) -> bytes:
        return _utf8_bytes(self.content, label="Model patch content")

    def to_digest_dict(self) -> dict[str, str]:
        return {
            "operation": self.operation.value,
            "target": self.target,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True)
class ModelPatchRequest:
    request_id: str
    changes: tuple[ModelPatchChangeRequest, ...]
    request_digest: str

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        changes: tuple[ModelPatchChangeRequest, ...],
    ) -> "ModelPatchRequest":
        normalized = tuple(changes)
        return cls(
            request_id=request_id,
            changes=normalized,
            request_digest=_request_digest(
                request_id=request_id,
                changes=normalized,
            ),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not _REQUEST_ID.fullmatch(
            self.request_id
        ):
            raise ProtocolError("patch_request.request_id has an invalid format")
        normalized = tuple(self.changes)
        if not 1 <= len(normalized) <= MAX_MODEL_PATCH_FILES:
            raise ProtocolError(
                "Model patch request must contain "
                f"1..{MAX_MODEL_PATCH_FILES} changes"
            )
        if any(not isinstance(item, ModelPatchChangeRequest) for item in normalized):
            raise TypeError(
                "Model patch request changes must be ModelPatchChangeRequest instances"
            )
        total = sum(len(item.content_bytes) for item in normalized)
        if total > MAX_MODEL_PATCH_TOTAL_CONTENT_BYTES:
            raise ProtocolError(
                "Model patch total content exceeds protocol limit "
                f"({total} > {MAX_MODEL_PATCH_TOTAL_CONTENT_BYTES} bytes)"
            )
        if not isinstance(self.request_digest, str) or len(self.request_digest) != 64:
            raise ProtocolError("Model patch request digest must be SHA-256 hex")
        try:
            int(self.request_digest, 16)
        except ValueError as exc:
            raise ProtocolError("Model patch request digest must be SHA-256 hex") from exc
        expected = _request_digest(
            request_id=self.request_id,
            changes=normalized,
        )
        if expected != self.request_digest:
            raise ProtocolError("Model patch request digest does not match payload")
        object.__setattr__(self, "changes", normalized)

    def to_digest_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MODEL_PATCH_REQUEST_SCHEMA_VERSION,
            "request_id": self.request_id,
            "changes": [change.to_digest_dict() for change in self.changes],
        }


def parse_model_patch_request(
    text: str,
    *,
    max_chars: int = MAX_MODEL_PATCH_REQUEST_CHARS,
) -> ModelPatchRequest:
    """Parse model patch intent without granting authority or applying a mutation."""

    if not isinstance(text, str):
        raise ProtocolError("Model patch request must be text")
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if len(text) > max_chars:
        raise ProtocolError(
            f"Model patch request exceeds limit ({len(text)} > {max_chars} chars)"
        )

    payload_text = _unwrap_json_fence(text)
    try:
        payload = json.loads(
            payload_text,
            object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ProtocolError(
            f"Model patch request is not valid JSON: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Model patch request must be one JSON object")

    _require_exact_keys(
        payload,
        {"type", "request_id", "changes"},
        label="Model patch request",
    )
    if payload.get("type") != "patch_request":
        raise ProtocolError("Model patch request type must be 'patch_request'")

    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise ProtocolError("patch_request.request_id has an invalid format")

    raw_changes = payload.get("changes")
    if not isinstance(raw_changes, list):
        raise ProtocolError("patch_request.changes must be a JSON array")
    if not 1 <= len(raw_changes) <= MAX_MODEL_PATCH_FILES:
        raise ProtocolError(
            f"patch_request.changes must contain 1..{MAX_MODEL_PATCH_FILES} items"
        )

    changes: list[ModelPatchChangeRequest] = []
    for index, raw in enumerate(raw_changes):
        if not isinstance(raw, dict):
            raise ProtocolError(f"patch_request.changes[{index}] must be an object")
        _require_exact_keys(
            raw,
            {"operation", "target", "content"},
            label=f"patch_request.changes[{index}]",
        )
        changes.append(
            ModelPatchChangeRequest(
                operation=raw.get("operation"),
                target=raw.get("target"),
                content=raw.get("content"),
            )
        )

    return ModelPatchRequest.create(
        request_id=request_id,
        changes=tuple(changes),
    )
