"""Explicit ChatGPT direct-HTTP provider for Codexia.

Only the CWA PR #212 BrowserlessHTTPClient is accepted as the default factory.
No implicit browser-owned fallback or automatic write retry. This provider
holds no WorkStore, process execution authority or permission policy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from codexia_manual_agent.domain.errors import ProviderError, ProviderUnavailableError
from codexia_manual_agent.domain.models import ProviderRequest, ProviderResponse
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider

_SAFE_STAGE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class ChatGPTHttpProvider(ChatGPTWebProvider):
    """ModelProvider using the separate experimental CWA HTTP SDK.

    A caller must select this provider explicitly. The inherited canonical
    read_status/read_messages methods reuse the HTTP client's read-only API.
    Requested model slugs are not attestation of the model actually used.
    """

    def __init__(
        self,
        *,
        auth_file: str | Path = "auth_data.json",
        model: str = "gpt-6-instant",
        reasoning_effort: str | None = None,
        timeout: float = 120.0,
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if client is not None and client_factory is not None:
            raise ValueError("choose client or client_factory, not both")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("an explicit nonempty ChatGPT model slug is required")
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
        ):
            raise ValueError("reasoning_effort must be a nonempty string or None")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if client is None:
            if client_factory is None:
                try:
                    from chatgpt_web_adapter.browserless_http import (
                        BrowserlessHTTPClient,
                    )
                except ImportError as exc:
                    raise ProviderUnavailableError(
                        "CWA PR #212 BrowserlessHTTPClient is not installed; "
                        "use the isolated browserless-http source checkout"
                    ) from exc
                client_factory = BrowserlessHTTPClient
            try:
                client = client_factory(
                    auth_file=str(Path(auth_file).expanduser()),
                    timeout=max(1, int(timeout)),
                )
            except Exception as exc:
                raise ProviderError(
                    "ChatGPT HTTP client initialization failed; no message was sent"
                ) from exc
        # Supplying runtime= avoids ChatGPTWebProvider's browser-owned factory.
        super().__init__(
            auth_file=auth_file,
            model=model.strip(),
            reasoning_effort=reasoning_effort,
            timeout=timeout,
            runtime=client,
        )

    @property
    def provider_id(self) -> str:
        # Distinct identity keeps pre-existing browser handoffs from being
        # redispatched under another transport after a restart.
        return "chatgpt-http"

    @staticmethod
    def _ambiguous_failure(exc: Exception) -> ProviderError:
        stage = getattr(exc, "request_stage", None)
        safe_stage = (
            stage
            if isinstance(stage, str) and _SAFE_STAGE.fullmatch(stage)
            else "unknown"
        )
        return ProviderError(
            f"ChatGPT HTTP call failed ({type(exc).__name__}; stage={safe_stage}); "
            "write outcome may be ambiguous; do not retry without read-only "
            "reconciliation"
        )

    def send(self, request: ProviderRequest) -> ProviderResponse:
        if not isinstance(request, ProviderRequest):
            raise TypeError("request must be ProviderRequest")
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise ValueError("prompt must be nonempty")
        conversation_id = (
            request.conversation.conversation_id
            if request.conversation is not None
            else None
        )
        try:
            raw = self._runtime.send(
                self._product_prompt(request),
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                conversation=conversation_id,
                temporary=False,
            )
        except Exception as exc:
            raise self._ambiguous_failure(exc) from exc
        response = self._normalize_response(raw)
        if not response.text.strip() or response.conversation is None:
            raise ProviderError(
                "ChatGPT HTTP returned incomplete response; do not retry"
            )
        if (
            not response.conversation.conversation_id
            or not response.conversation.message_id
        ):
            raise ProviderError(
                "ChatGPT HTTP response lacks canonical IDs; do not retry"
            )
        if conversation_id and response.conversation.conversation_id != conversation_id:
            raise ProviderError(
                "ChatGPT HTTP continuation switched conversation identity; do not retry"
            )
        return response

    def send_temporary(self, prompt: str) -> ProviderResponse:
        """One explicit Temporary Chat turn, without browser lifecycle claims."""

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be nonempty")
        try:
            raw = self._runtime.send(
                prompt,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                temporary=True,
            )
        except Exception as exc:
            raise self._ambiguous_failure(exc) from exc
        response = self._normalize_response(raw)
        if not response.text.strip():
            raise ProviderError("ChatGPT HTTP temporary response empty; do not retry")
        return response

    def end_temporary_chat(self) -> bool:
        raise ProviderUnavailableError(
            "HTTP client does not implement browser-owned Temporary lifecycle disposal"
        )

    def handoff_generated_artifact(
        self,
        conversation_id: str,
        *,
        filename: str,
        destination: str | Path,
        overwrite: bool = False,
    ):
        raise ProviderUnavailableError(
            "CWA HTTP generated-artifact handoff is not implemented"
        )
