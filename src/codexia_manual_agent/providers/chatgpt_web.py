from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codexia_manual_agent.domain.errors import ProviderError, ProviderUnavailableError
from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderRequest,
    ProviderResponse,
)


@dataclass(frozen=True, slots=True)
class ChatGPTConversationMessage:
    """Stable normalized visible message from one ChatGPT current branch."""

    node_id: str
    message_id: str
    role: str
    text: str
    create_time: float | None = None
    recipient: str | None = None
    model: str | None = None
    finish_reason: str | None = None


class ChatGPTWebProvider:
    """`chatgpt-web-adapter` stable-core transport for Codexia.

    The provider sends and continues text conversations and can read the visible
    user/assistant messages on the current branch. It never uses the SDK's
    experimental approval helpers and never receives a local tool handle.
    """

    def __init__(
        self,
        *,
        auth_file: str | Path = "auth_data.json",
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = 90.0,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.auth_file = str(Path(auth_file).expanduser())
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self._client = client or self._create_client(client_factory)

    @property
    def provider_id(self) -> str:
        return "chatgpt-web"

    def _create_client(self, client_factory: Callable[..., Any] | None) -> Any:
        if client_factory is None:
            try:
                from chatgpt_web_adapter import ChatGPTWebClient
            except ImportError as exc:
                raise ProviderUnavailableError(
                    "chatgpt-web-adapter is not installed; install with "
                    "`python -m pip install -e .[web]`"
                ) from exc
            client_factory = ChatGPTWebClient
        try:
            return client_factory(auth_file=self.auth_file, timeout=self.timeout)
        except Exception as exc:  # SDK exposes several changing error types
            raise ProviderError(f"Failed to initialize chatgpt-web provider: {exc}") from exc

    def send(self, request: ProviderRequest) -> ProviderResponse:
        try:
            if request.conversation and request.conversation.conversation_id:
                raw = self._client.send_to_conversation(
                    request.conversation.conversation_id,
                    request.prompt,
                    preserve_model=self.model is None,
                    model=self.model,
                    system=request.system,
                    web_search=False,
                    temporary=False,
                    reasoning_effort=self.reasoning_effort,
                )
            else:
                raw = self._client.send(
                    request.prompt,
                    model=self.model,
                    system=request.system,
                    web_search=False,
                    temporary=False,
                    reasoning_effort=self.reasoning_effort,
                )
        except Exception as exc:
            raise ProviderError(f"chatgpt-web request failed: {exc}") from exc
        return self._normalize_response(raw)

    def read_messages(self, conversation_id: str) -> tuple[ChatGPTConversationMessage, ...]:
        """Read visible user/assistant messages from the exact current branch."""

        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id is required")
        conversation_id = conversation_id.strip()
        try:
            raw_messages = self._client.get_messages(
                conversation_id,
                roles=("user", "assistant"),
                include_empty=False,
            )
        except Exception as exc:
            raise ProviderError(f"chatgpt-web history read failed: {exc}") from exc
        if not isinstance(raw_messages, (list, tuple)):
            raise ProviderError("chatgpt-web history did not contain a message sequence")
        normalized: list[ChatGPTConversationMessage] = []
        seen_nodes: set[str] = set()
        seen_messages: set[str] = set()
        for raw in raw_messages:
            node_id = _required_attr(raw, "node_id")
            message_id = _required_attr(raw, "message_id")
            role = _required_attr(raw, "role")
            if role not in {"user", "assistant"}:
                raise ProviderError(f"unsupported visible chatgpt-web role: {role!r}")
            if node_id in seen_nodes or message_id in seen_messages:
                raise ProviderError("chatgpt-web history contains duplicate message identity")
            seen_nodes.add(node_id)
            seen_messages.add(message_id)
            text = getattr(raw, "text", None)
            if not isinstance(text, str) or not text:
                raise ProviderError("chatgpt-web visible message did not contain text")
            create_time = getattr(raw, "create_time", None)
            if create_time is not None:
                try:
                    create_time = float(create_time)
                except (TypeError, ValueError) as exc:
                    raise ProviderError("chatgpt-web message create_time is invalid") from exc
            normalized.append(
                ChatGPTConversationMessage(
                    node_id=node_id,
                    message_id=message_id,
                    role=role,
                    text=text,
                    create_time=create_time,
                    recipient=_optional_attr(raw, "recipient"),
                    model=_optional_attr(raw, "model"),
                    finish_reason=_optional_attr(raw, "finish_reason"),
                )
            )
        return tuple(normalized)

    @staticmethod
    def _normalize_response(raw: Any) -> ProviderResponse:
        text = getattr(raw, "text", None)
        if not isinstance(text, str):
            raise ProviderError("chatgpt-web response did not contain text")

        raw_conversation = getattr(raw, "conversation", None)
        conversation = None
        if raw_conversation is not None:
            conversation = ProviderConversation(
                conversation_id=_optional_attr(raw_conversation, "conversation_id"),
                message_id=_optional_attr(raw_conversation, "message_id"),
                parent_message_id=_optional_attr(raw_conversation, "parent_message_id"),
                finish_reason=_optional_attr(raw_conversation, "finish_reason"),
            )

        request = getattr(raw, "request", None)
        model = _optional_attr(request, "observed_model") or _optional_attr(
            request, "sent_model"
        )
        reasoning_effort = _optional_attr(
            request, "observed_reasoning_effort"
        ) or _optional_attr(request, "sent_reasoning_effort")

        metrics_obj = getattr(raw, "metrics", None)
        metrics: dict[str, Any] = {}
        if metrics_obj is not None:
            to_dict = getattr(metrics_obj, "to_dict", None)
            if callable(to_dict):
                candidate = to_dict()
                if isinstance(candidate, dict):
                    metrics = candidate

        return ProviderResponse(
            text=text,
            conversation=conversation,
            model=model,
            reasoning_effort=reasoning_effort,
            metrics=metrics,
        )


def _optional_attr(value: Any, name: str) -> str | None:
    candidate = getattr(value, name, None)
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip()
    return candidate or None


def _required_attr(value: Any, name: str) -> str:
    candidate = _optional_attr(value, name)
    if candidate is None:
        raise ProviderError(f"chatgpt-web visible message is missing {name}")
    return candidate
