"""Explicit ChatGPT direct-HTTP provider for Codexia.

Only the CWA PR #212 BrowserlessHTTPClient is accepted as the default factory.
No implicit browser-owned fallback or automatic write retry. This provider
holds no WorkStore, process execution authority or permission policy.
"""

from __future__ import annotations

import re
import subprocess
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
        progress: Callable[[str, dict[str, object]], None] | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> None:
        if client is not None and client_factory is not None:
            raise ValueError("choose client or client_factory, not both")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("an explicit nonempty ChatGPT model slug is required")
        if reasoning_effort is not None and reasoning_effort not in {
            "instant",
            "medium",
            "high",
            "standard",
            "extended",
        }:
            raise ValueError(
                "reasoning_effort must be instant, medium, high, "
                "standard, extended or None"
            )
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
        if not callable(getattr(client, "send", None)):
            raise ProviderUnavailableError(
                "ChatGPT HTTP client missing callable send(); no browser fallback"
            )
        # Deliberately do NOT call the browser-owned provider constructor:
        # its falsy-runtime fallback can instantiate a Chrome-backed transport.
        # These are exactly the fields its inherited normalization/read methods use.
        self.auth_file = str(Path(auth_file).expanduser())
        self.model = model.strip()
        self.reasoning_effort = reasoning_effort
        self.timeout = float(timeout)
        self._client = None
        self._runtime = client
        self._progress = progress
        self._on_token = on_token

    @property
    def provider_id(self) -> str:
        # Distinct identity keeps pre-existing browser handoffs from being
        # redispatched under another transport after a restart.
        return "chatgpt-http"

    def preflight_read_only(self) -> None:
        """Verify the actual HTTP session before a Work dispatch claim.

        CWA's refresh_auth(persist=False) performs an authenticated session GET
        and updates the bearer in memory; it does not submit a conversation,
        upload media or persist credentials. This is not a guarantee that a
        subsequent protected conversation POST will succeed.
        """

        node_bin = getattr(self._runtime, "node_bin", None)
        if node_bin is not None:
            try:
                process = subprocess.run(
                    [str(node_bin), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ProviderUnavailableError(
                    "HTTP_PREFLIGHT_FAILED: Node runtime unavailable before claim"
                ) from None
            version = re.fullmatch(r"v(\d+)\.\d+\.\d+", process.stdout.strip())
            if process.returncode != 0 or version is None or int(version.group(1)) < 24:
                raise ProviderUnavailableError(
                    "HTTP_PREFLIGHT_FAILED: Node 24+ required before claim"
                )

        if getattr(self._runtime, "curl_bin", None) == "":
            raise ProviderUnavailableError(
                "HTTP_PREFLIGHT_FAILED: curl unavailable before claim"
            )

        refresh = getattr(self._runtime, "refresh_auth", None)
        if not callable(refresh):
            raise ProviderUnavailableError(
                "HTTP_PREFLIGHT_FAILED: CWA session GET unavailable before claim"
            )
        try:
            receipt = refresh(persist=False)
        except Exception:
            # Auth or HTTP exceptions may contain sensitive response material.
            raise ProviderUnavailableError(
                "HTTP_PREFLIGHT_FAILED: ChatGPT session GET failed before claim"
            ) from None
        if (
            getattr(receipt, "status_code", None) != 200
            or getattr(receipt, "access_token_present", None) is not True
            or getattr(receipt, "persisted", None) is not False
        ):
            raise ProviderUnavailableError(
                "HTTP_PREFLIGHT_FAILED: invalid session receipt before claim"
            )

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

    def _report(self, stage: str, **fields: object) -> None:
        if self._progress is not None:
            # Instrumentation has no right to cancel or replay a model write.
            try:
                self._progress(stage, fields)
            except Exception:
                pass

    def _callbacks(self, *, mode: str) -> dict[str, Any]:
        if self._progress is None and self._on_token is None:
            return {}
        first_token = False

        def on_event(event: Any) -> None:
            if isinstance(event, dict) and event.get("type") == "stream_started":
                self._report("STREAM_OPEN", mode=mode)

        def on_token(text: str) -> None:
            nonlocal first_token
            if not isinstance(text, str) or not text:
                return
            if not first_token:
                first_token = True
                self._report("FIRST_TOKEN", mode=mode)
            if self._on_token is not None:
                try:
                    self._on_token(text)
                except Exception:
                    # Console errors must never induce another remote write.
                    pass

        return {"on_event": on_event, "on_token": on_token}

    def send(
        self, request: ProviderRequest, *, media: list[tuple[bytes, str]] | None = None
    ) -> ProviderResponse:
        if not isinstance(request, ProviderRequest):
            raise TypeError("request must be ProviderRequest")
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise ValueError("prompt must be nonempty")
        conversation_id = (
            request.conversation.conversation_id
            if request.conversation is not None
            else None
        )
        if request.conversation is not None and (
            not isinstance(conversation_id, str) or not conversation_id.strip()
        ):
            raise ValueError("continuation requires an exact nonempty conversation_id")
        self._report(
            "HTTP_CALL",
            mode="normal",
            model=self.model,
            reasoning_effort=self.reasoning_effort or "default",
            status="attempt_started_not_receipt",
        )
        try:
            raw = self._runtime.send(
                self._product_prompt(request),
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                conversation=conversation_id,
                temporary=False,
                **({"media": media} if media is not None else {}),
                **self._callbacks(mode="normal"),
            )
        except KeyboardInterrupt:
            self._report(
                "OUTCOME_UNKNOWN", error_type="KeyboardInterrupt", retry="forbidden"
            )
            raise
        except Exception as exc:
            self._report(
                "OUTCOME_UNKNOWN",
                error_type=type(exc).__name__,
                retry="forbidden",
            )
            raise self._ambiguous_failure(exc) from exc
        self._report(
            "HTTP_RETURN", response_chars=len(getattr(raw, "text", "") or "")
        )
        diagnostics = getattr(raw, "request", None)
        self._report(
            "MODEL_ROUTE",
            requested_model=self.model,
            requested_reasoning_effort=self.reasoning_effort or "default",
            sent_model=getattr(diagnostics, "sent_model", None),
            sent_reasoning_effort=getattr(diagnostics, "sent_reasoning_effort", None),
            observed_model=getattr(diagnostics, "observed_model", None),
            observed_reasoning_effort=getattr(
                diagnostics, "observed_reasoning_effort", None
            ),
        )
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
        self._report(
            "HTTP_IDENTITY",
            conversation_id=response.conversation.conversation_id,
            message_id=response.conversation.message_id,
            status="ids_observed_not_work_admitted",
        )
        return response

    def send_temporary(self, prompt: str) -> ProviderResponse:
        """One explicit Temporary Chat turn, without browser lifecycle claims."""

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be nonempty")
        self._report(
            "HTTP_CALL",
            mode="temporary",
            model=self.model,
            reasoning_effort=self.reasoning_effort or "default",
            status="attempt_started_not_receipt",
        )
        try:
            raw = self._runtime.send(
                prompt,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                temporary=True,
                **self._callbacks(mode="temporary"),
            )
        except KeyboardInterrupt:
            self._report(
                "OUTCOME_UNKNOWN", error_type="KeyboardInterrupt", retry="forbidden"
            )
            raise
        except Exception as exc:
            self._report(
                "OUTCOME_UNKNOWN",
                error_type=type(exc).__name__,
                retry="forbidden",
            )
            raise self._ambiguous_failure(exc) from exc
        self._report(
            "HTTP_RETURN", response_chars=len(getattr(raw, "text", "") or "")
        )
        diagnostics = getattr(raw, "request", None)
        self._report(
            "MODEL_ROUTE",
            requested_model=self.model,
            requested_reasoning_effort=self.reasoning_effort or "default",
            sent_model=getattr(diagnostics, "sent_model", None),
            sent_reasoning_effort=getattr(diagnostics, "sent_reasoning_effort", None),
            observed_model=getattr(diagnostics, "observed_model", None),
            observed_reasoning_effort=getattr(
                diagnostics, "observed_reasoning_effort", None
            ),
        )
        response = self._normalize_response(raw)
        if not response.text.strip():
            raise ProviderError("ChatGPT HTTP temporary response empty; do not retry")
        if response.conversation is not None:
            self._report(
                "HTTP_IDENTITY",
                conversation_id=response.conversation.conversation_id,
                message_id=response.conversation.message_id,
                status="ids_observed_not_work_admitted",
            )
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
