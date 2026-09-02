from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .capabilities import READ_ONLY_CAPABILITIES


class ToolName(StrEnum):
    READ_FILE = "read_file"
    LIST_FILES = "list_files"
    SEARCH_TEXT = "search_text"
    GIT_STATUS = "git_status"


class AgentRunStatus(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PROTOCOL_ERROR = "protocol_error"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class ToolRequest:
    request_id: str
    name: ToolName
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolObservation:
    request_id: str
    tool: ToolName
    success: bool
    data: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool": self.tool.value,
            "success": self.success,
            "data": self.data,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    path: str
    kind: str
    size_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SearchMatch:
    path: str
    line_number: int
    line: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GitStatus:
    branch_line: str | None
    entries: tuple[str, ...]
    clean: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_line": self.branch_line,
            "entries": list(self.entries),
            "clean": self.clean,
        }


@dataclass(frozen=True, slots=True)
class ProviderConversation:
    conversation_id: str | None = None
    message_id: str | None = None
    parent_message_id: str | None = None
    finish_reason: str | None = None

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any] | None,
    ) -> "ProviderConversation | None":
        if not isinstance(data, Mapping):
            return None
        return cls(
            conversation_id=_optional_string(data.get("conversation_id")),
            message_id=_optional_string(data.get("message_id")),
            parent_message_id=_optional_string(data.get("parent_message_id")),
            finish_reason=_optional_string(data.get("finish_reason")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "parent_message_id": self.parent_message_id,
            "finish_reason": self.finish_reason,
        }


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    prompt: str
    system: str | None = None
    conversation: ProviderConversation | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    conversation: ProviderConversation | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentBudgets:
    max_turns: int = 8
    max_tool_calls: int = 8
    max_response_chars: int = 32_768
    max_total_model_chars: int = 131_072
    max_observation_chars: int = 131_072

    def __post_init__(self) -> None:
        for field_name in (
            "max_turns",
            "max_tool_calls",
            "max_response_chars",
            "max_total_model_chars",
            "max_observation_chars",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: AgentRunStatus
    final_text: str | None
    turns: int
    tool_calls: int
    model_chars: int
    conversation: ProviderConversation | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    error: str | None = None

    @property
    def completed(self) -> bool:
        return self.status is AgentRunStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "final_text": self.final_text,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "model_chars": self.model_chars,
            "conversation": (
                self.conversation.to_dict() if self.conversation is not None else None
            ),
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class SessionManifest:
    schema_version: int
    session_id: str
    created_at: str
    workspace: str
    prompt_version: str
    mode: str
    capabilities: tuple[str, ...]
    provider: str
    title: str | None = None
    conversation: ProviderConversation | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    turn_count: int = 0
    tool_call_count: int = 0
    last_status: str | None = None

    @classmethod
    def create(
        cls,
        *,
        workspace: Path,
        prompt_version: str,
        title: str | None = None,
        provider: str = "unconfigured",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> "SessionManifest":
        return cls(
            schema_version=2,
            session_id=str(uuid4()),
            created_at=datetime.now(timezone.utc).isoformat(),
            workspace=str(workspace.resolve()),
            prompt_version=prompt_version,
            mode="read-only",
            capabilities=tuple(capability.value for capability in READ_ONLY_CAPABILITIES),
            provider=provider,
            title=title,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SessionManifest":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            session_id=str(data["session_id"]),
            created_at=str(data["created_at"]),
            workspace=str(data["workspace"]),
            prompt_version=str(data["prompt_version"]),
            mode=str(data["mode"]),
            capabilities=tuple(str(item) for item in data["capabilities"]),
            provider=str(data["provider"]),
            title=None if data.get("title") is None else str(data["title"]),
            conversation=ProviderConversation.from_dict(data.get("conversation")),
            model=_optional_string(data.get("model")),
            reasoning_effort=_optional_string(data.get("reasoning_effort")),
            turn_count=_non_negative_int(data.get("turn_count", 0), "turn_count"),
            tool_call_count=_non_negative_int(
                data.get("tool_call_count", 0),
                "tool_call_count",
            ),
            last_status=_optional_string(data.get("last_status")),
        )

    def with_run_result(
        self,
        result: AgentRunResult,
        *,
        provider: str,
        model: str | None,
        reasoning_effort: str | None,
    ) -> "SessionManifest":
        return replace(
            self,
            schema_version=2,
            provider=provider,
            conversation=result.conversation,
            model=result.model or model,
            reasoning_effort=result.reasoning_effort or reasoning_effort,
            turn_count=self.turn_count + result.turns,
            tool_call_count=self.tool_call_count + result.tool_calls,
            last_status=result.status.value,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "workspace": self.workspace,
            "prompt_version": self.prompt_version,
            "mode": self.mode,
            "capabilities": list(self.capabilities),
            "provider": self.provider,
            "title": self.title,
            "conversation": (
                self.conversation.to_dict() if self.conversation is not None else None
            ),
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "turn_count": self.turn_count,
            "tool_call_count": self.tool_call_count,
            "last_status": self.last_status,
        }


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value
