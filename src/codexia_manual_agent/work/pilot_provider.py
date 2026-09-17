from __future__ import annotations

from typing import Any

from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import ProviderRequest, ProviderResponse
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider

CWA_WRITE_NOT_SUBMITTED = "BROWSER_OWNED_WRITE_NOT_SUBMITTED"
_CWA_RUNTIME_ERROR_MODULE = "chatgpt_web_adapter.browser_owned_write_runtime"
_CWA_RUNTIME_ERROR_NAME = "BrowserOwnedWriteRuntimeError"


class ProviderWriteNotSubmittedError(ProviderError):
    """Exact provider evidence that no product write crossed delegation boundary."""

    def __init__(
        self,
        message: str,
        *,
        failure_kind: str,
        request_stage: str | None,
    ) -> None:
        self.failure_kind = failure_kind
        self.request_stage = request_stage
        self.write_may_have_been_submitted = False
        self.reconciliation_required = False
        self.automatic_retry_allowed = False
        self.manual_retry_safe_after_repair = True
        super().__init__(message)


class M66ChatGPTWebProvider(ChatGPTWebProvider):
    """Preserve CWA's exact no-submit disposition for M6.6 recovery only."""

    @staticmethod
    def _proven_not_submitted(error: BaseException) -> tuple[str, str | None] | None:
        cause: BaseException | None = error.__cause__
        if cause is None:
            return None
        if (
            type(cause).__module__ != _CWA_RUNTIME_ERROR_MODULE
            or type(cause).__name__ != _CWA_RUNTIME_ERROR_NAME
        ):
            return None
        if getattr(cause, "failure_kind", None) != CWA_WRITE_NOT_SUBMITTED:
            return None
        if getattr(cause, "write_may_have_been_submitted", None) is not False:
            return None
        if getattr(cause, "reconciliation_required", None) is not False:
            return None
        if getattr(cause, "automatic_retry_allowed", None) is not False:
            return None
        if getattr(cause, "manual_retry_safe_after_repair", None) is not True:
            return None
        stage = getattr(cause, "request_stage", None)
        if stage is not None and not isinstance(stage, str):
            return None
        return CWA_WRITE_NOT_SUBMITTED, stage

    def send(self, request: ProviderRequest) -> ProviderResponse:
        try:
            return super().send(request)
        except ProviderError as error:
            proof = self._proven_not_submitted(error)
            if proof is None:
                raise
            failure_kind, request_stage = proof
            raise ProviderWriteNotSubmittedError(
                str(error),
                failure_kind=failure_kind,
                request_stage=request_stage,
            ) from error


__all__ = [
    "CWA_WRITE_NOT_SUBMITTED",
    "M66ChatGPTWebProvider",
    "ProviderWriteNotSubmittedError",
]
