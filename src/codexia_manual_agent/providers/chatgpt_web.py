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

_PRODUCT_TRANSPORT = "browser-owned"
_SEMANTIC_MODEL_PROFILES = {"FAST", "BALANCED", "DEEP"}
_REASONING_PROFILE_MAP = {
    "minimal": "FAST",
    "low": "FAST",
    "instant": "FAST",
    "fast": "FAST",
    "medium": "BALANCED",
    "standard": "BALANCED",
    "balanced": "BALANCED",
    "high": "DEEP",
    "extended": "DEEP",
    "deep": "DEEP",
}


@dataclass(frozen=True, slots=True)
class ChatGPTConversationStatus:
    status: str
    message_id: str | None = None
    finish_reason: str | None = None


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
    """ChatGPT product-runtime transport for Codexia.

    The default live path uses chatgpt-web-adapter's production
    ``ChatGPTProductRuntime`` with the browser-owned write transport and canonical
    readback. Codexia owns semantic provenance and orchestration; CWA owns product
    write, conversation identity, finality, and canonical observation.

    ``client=`` / ``client_factory=`` remain only as explicit compatibility seams
    for deterministic tests and historical injected callers. They are never chosen
    by the default live constructor.
    """

    def __init__(
        self,
        *,
        auth_file: str | Path = "auth_data.json",
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = 90.0,
        runtime: Any | None = None,
        runtime_factory: Callable[..., Any] | None = None,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.auth_file = str(Path(auth_file).expanduser())
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if runtime is not None and (client is not None or client_factory is not None):
            raise ValueError("runtime and legacy client injection are mutually exclusive")
        if runtime_factory is not None and (
            client is not None or client_factory is not None
        ):
            raise ValueError(
                "runtime_factory and legacy client injection are mutually exclusive"
            )

        self._client = client
        if self._client is None and client_factory is not None:
            self._client = self._create_legacy_client(client_factory)
        self._runtime = None
        if self._client is None:
            self._runtime = runtime or self._create_runtime(runtime_factory)

    @property
    def provider_id(self) -> str:
        return "chatgpt-web"

    def _create_runtime(self, runtime_factory: Callable[..., Any] | None) -> Any:
        if runtime_factory is None:
            try:
                from chatgpt_web_adapter import assemble_product_runtime
            except ImportError as exc:
                raise ProviderUnavailableError(
                    "chatgpt-web-adapter product runtime is not installed; install "
                    "with `python -m pip install -e .[web]`"
                ) from exc
            runtime_factory = assemble_product_runtime
        try:
            return runtime_factory(
                transport=_PRODUCT_TRANSPORT,
                auth_file=self.auth_file,
                client_timeout=max(1, int(self.timeout)),
            )
        except Exception as exc:
            raise ProviderError(
                f"Failed to initialize chatgpt product runtime: {exc}"
            ) from exc

    def _create_legacy_client(self, client_factory: Callable[..., Any]) -> Any:
        try:
            return client_factory(auth_file=self.auth_file, timeout=self.timeout)
        except Exception as exc:
            raise ProviderError(
                f"Failed to initialize injected legacy chatgpt-web client: {exc}"
            ) from exc

    def send(self, request: ProviderRequest) -> ProviderResponse:
        if self._client is not None:
            return self._send_legacy(request)
        if self._runtime is None:
            raise ProviderUnavailableError("chatgpt product runtime is unavailable")

        try:
            conversation_id = (
                request.conversation.conversation_id
                if request.conversation is not None
                else None
            )
            kwargs: dict[str, Any] = {
                "conversation": conversation_id,
                "timeout": self.timeout,
            }
            model_profile = self._model_profile()
            if model_profile is not None:
                kwargs["model_profile"] = model_profile
            execution = self._runtime.send_text_observed(
                self._product_prompt(request),
                **kwargs,
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"chatgpt product-runtime request failed: {exc}") from exc

        transport = getattr(execution, "transport", None)
        if transport != _PRODUCT_TRANSPORT:
            raise ProviderError(
                "chatgpt product runtime returned unexpected transport identity"
            )
        raw = getattr(execution, "response", None)
        if raw is None:
            raise ProviderError("chatgpt product runtime did not return a response")
        return self._normalize_response(raw)

    def send_temporary(self, prompt: str) -> ProviderResponse:
        """Send one turn through CWA's live process-local Temporary Chat session."""

        text = prompt.strip()
        if not text:
            raise ValueError("temporary prompt must be non-empty")
        if self._client is not None:
            raise ProviderUnavailableError(
                "temporary Simple Work requires the browser-owned product runtime"
            )
        if self._runtime is None:
            raise ProviderUnavailableError("chatgpt product runtime is unavailable")

        try:
            kwargs: dict[str, Any] = {
                "conversation": None,
                "conversation_mode": "temporary",
                "timeout": self.timeout,
            }
            model_profile = self._model_profile()
            if model_profile is not None:
                kwargs["model_profile"] = model_profile
            execution = self._runtime.send_text_observed(text, **kwargs)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"chatgpt temporary product-runtime request failed: {exc}"
            ) from exc

        transport = getattr(execution, "transport", None)
        if transport != _PRODUCT_TRANSPORT:
            raise ProviderError(
                "chatgpt temporary runtime returned unexpected transport identity"
            )
        raw = getattr(execution, "response", None)
        if raw is None:
            raise ProviderError("chatgpt temporary runtime did not return a response")
        return self._normalize_response(raw)

    def end_temporary_chat(self) -> bool:
        """Explicitly end the current CWA Temporary Chat lifecycle if one exists."""

        if self._client is not None:
            raise ProviderUnavailableError(
                "temporary Simple Work requires the browser-owned product runtime"
            )
        if self._runtime is None:
            raise ProviderUnavailableError("chatgpt product runtime is unavailable")
        try:
            return bool(self._runtime.end_temporary_chat())
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"failed to end chatgpt temporary lifecycle: {exc}"
            ) from exc

    def _send_legacy(self, request: ProviderRequest) -> ProviderResponse:
        """Compatibility-only injected client seam; never the default live path."""

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

    def _product_prompt(self, request: ProviderRequest) -> str:
        """Represent ProviderRequest.system without claiming hidden system authority.

        The production browser-owned runtime intentionally exposes product-visible
        text turns rather than the legacy ``system=`` backend field. A Codexia
        system contract is therefore carried explicitly inside the cognition turn
        instead of being silently dropped or routed through an obsolete backend
        payload. Worker peer-loop sends have ``system=None`` and remain byte-for-byte
        unchanged for M6.3 exact-message reconciliation.
        """

        if request.system is None or not request.system.strip():
            return request.prompt
        return (
            "[Codexia product-runtime system context]\n"
            f"{request.system.strip()}\n\n"
            "[Codexia product-runtime request]\n"
            f"{request.prompt}"
        )

    def _model_profile(self) -> str | None:
        model_profile = None
        if self.model is not None:
            candidate = self.model.strip().upper()
            if candidate not in _SEMANTIC_MODEL_PROFILES:
                raise ProviderError(
                    "browser-owned product runtime does not accept raw model slugs; "
                    "use semantic model profile FAST, BALANCED, or DEEP"
                )
            model_profile = candidate

        reasoning_profile = None
        if self.reasoning_effort is not None:
            key = self.reasoning_effort.strip().lower()
            reasoning_profile = _REASONING_PROFILE_MAP.get(key)
            if reasoning_profile is None:
                raise ProviderError(
                    "unsupported reasoning effort for browser-owned product runtime"
                )

        if (
            model_profile is not None
            and reasoning_profile is not None
            and model_profile != reasoning_profile
        ):
            raise ProviderError(
                "model profile and reasoning effort resolve to conflicting product modes"
            )
        return model_profile or reasoning_profile

    def read_status(self, conversation_id: str) -> ChatGPTConversationStatus:
        """Read canonical conversation finality without performing a write."""

        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id is required")
        conversation_id = conversation_id.strip()
        reader = self._client if self._client is not None else self._runtime
        if reader is None:
            raise ProviderUnavailableError("chatgpt product runtime is unavailable")
        try:
            raw = reader.get_status(conversation_id)
        except Exception as exc:
            raise ProviderError(f"chatgpt-web status read failed: {exc}") from exc
        status = getattr(raw, "status", None)
        if not isinstance(status, str) or not status.strip():
            raise ProviderError("chatgpt-web status did not contain canonical status")
        return ChatGPTConversationStatus(
            status=status.strip(),
            message_id=_optional_attr(raw, "message_id"),
            finish_reason=_optional_attr(raw, "finish_reason"),
        )

    def read_messages(
        self, conversation_id: str
    ) -> tuple[ChatGPTConversationMessage, ...]:
        """Read the complete visible user/assistant current branch canonically."""

        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id is required")
        conversation_id = conversation_id.strip()
        reader = self._client if self._client is not None else self._runtime
        if reader is None:
            raise ProviderUnavailableError("chatgpt product runtime is unavailable")
        try:
            raw_messages = reader.get_messages(
                conversation_id,
                roles=("user", "assistant"),
                include_empty=False,
                limit=None,
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
