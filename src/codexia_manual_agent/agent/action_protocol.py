from __future__ import annotations

import json
import re
from typing import Any, Mapping

from codexia_manual_agent.admission.models import CommandFamily, ModelProcessRequest
from codexia_manual_agent.domain.errors import ProtocolError


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")


def _unwrap_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        raise ProtocolError("Model process request contains an incomplete code fence")
    opening = lines[0].strip().lower()
    if opening not in {"```", "```json"}:
        raise ProtocolError("Only a single JSON code fence is allowed")
    return "\n".join(lines[1:-1]).strip()


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str]) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProtocolError(
            f"Process request keys mismatch; missing={missing}, extra={extra}"
        )


def parse_model_process_request(text: str, *, max_chars: int = 16_384) -> ModelProcessRequest:
    """Parse the M2.2 admission protocol without enabling execution in the agent loop."""

    if not isinstance(text, str):
        raise ProtocolError("Model process request must be text")
    if len(text) > max_chars:
        raise ProtocolError(
            f"Model process request exceeds limit ({len(text)} > {max_chars} chars)"
        )

    payload_text = _unwrap_json_fence(text)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Model process request is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Model process request must be one JSON object")

    _require_exact_keys(payload, {"type", "request_id", "family", "arguments"})
    if payload.get("type") != "process_request":
        raise ProtocolError("Process request type must be 'process_request'")

    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise ProtocolError("process_request.request_id has an invalid format")

    try:
        family = CommandFamily(payload.get("family"))
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"Unsupported process command family: {payload.get('family')!r}") from exc

    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        raise ProtocolError("process_request.arguments must be a JSON object")

    return ModelProcessRequest(
        request_id=request_id,
        family=family,
        arguments=arguments,
    )
