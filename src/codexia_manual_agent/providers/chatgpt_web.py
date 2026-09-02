from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from codexia_manual_agent.domain.errors import ProviderError, ProviderUnavailableError
from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderRequest,
    ProviderResponse,
)


class ChatGPTWebProvider:
    """`chatgpt-web-adapter` stable-core transport for Codexia.

    The provider only sends and continues text conversations. It never uses the
    SDK's experimental approval helpers and never receives a local tool handle.
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
