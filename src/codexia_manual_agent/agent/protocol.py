from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from codexia_manual_agent.domain.errors import ProtocolError
from codexia_manual_agent.domain.models import ToolName, ToolRequest


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")


@dataclass(frozen=True, slots=True)
class FinalReply:
    text: str


ModelReply = ToolRequest | FinalReply


def _unwrap_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        raise ProtocolError("Model response contains an incomplete code fence")
    opening = lines[0].strip().lower()
    if opening not in {"```", "```json"}:
        raise ProtocolError("Only a single JSON code fence is allowed")
    return "\n".join(lines[1:-1]).strip()


def parse_model_reply(text: str, *, max_chars: int) -> ModelReply:
    if not isinstance(text, str):
        raise ProtocolError("Model response must be text")
    if len(text) > max_chars:
        raise ProtocolError(
            f"Model response exceeds limit ({len(text)} > {max_chars} chars)"
        )

    payload_text = _unwrap_json_fence(text)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Model response is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Model response must be one JSON object")

    reply_type = payload.get("type")
    if reply_type == "final":
        _require_exact_keys(payload, {"type", "text"})
        final_text = payload.get("text")
        if not isinstance(final_text, str) or not final_text.strip():
            raise ProtocolError("final.text must be a non-empty string")
        return FinalReply(final_text)

    if reply_type == "tool_request":
        _require_exact_keys(payload, {"type", "request_id", "tool", "arguments"})
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ProtocolError("tool_request.request_id has an invalid format")
        tool_value = payload.get("tool")
        try:
            tool = ToolName(tool_value)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"Unsupported read-only tool: {tool_value!r}") from exc
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            raise ProtocolError("tool_request.arguments must be a JSON object")
        return ToolRequest(request_id=request_id, name=tool, arguments=arguments)

    raise ProtocolError("Model response type must be 'tool_request' or 'final'")


def render_observation(observation: Mapping[str, Any]) -> str:
    """Render one exact deterministic observation without a model-context limit."""

    return json.dumps(
        dict(observation),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def serialize_observation(observation: Mapping[str, Any], *, max_chars: int) -> str:
    rendered = render_observation(observation)
    if len(rendered) > max_chars:
        raise ProtocolError(
            f"Tool observation exceeds limit ({len(rendered)} > {max_chars} chars)"
        )
    return rendered


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str]) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProtocolError(
            f"Protocol keys mismatch; missing={missing}, extra={extra}"
        )
