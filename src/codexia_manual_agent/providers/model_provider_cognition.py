from __future__ import annotations

import re

from codexia_manual_agent.domain.models import ProviderRequest, ProviderResponse
from codexia_manual_agent.ports.model_provider import ModelProvider
from codexia_manual_agent.role_core import CognitionOutcome, CognitionPortRequest

_PORT_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


class ModelProviderCognitionPortError(RuntimeError):
    """Legacy ModelProvider cannot safely satisfy one Gen2 cognition request."""


class ModelProviderCognitionPort:
    """Adapt one existing ModelProvider to the Gen2 CognitionPort contract.

    The adapter is intentionally transport-only. It receives no WorkStore,
    admission service, workspace, tools, capabilities, or authority. G2.13 has
    already durably recorded the CognitionHandoff before complete() is called.

    Provider exceptions are deliberately allowed to escape. The G2.13 bridge
    therefore preserves the durable handoff and treats the transport result as
    ambiguous instead of manufacturing FAILED, UNKNOWN, or retry permission.
    """

    def __init__(
        self,
        provider: ModelProvider,
        *,
        port_id: str | None = None,
    ) -> None:
        provider_id = _provider_id(provider)
        resolved_port_id = port_id or f"model-provider:{provider_id}"
        _validate_port_id(resolved_port_id)

        self._provider = provider
        self._provider_id = provider_id
        self._port_id = resolved_port_id

    @property
    def port_id(self) -> str:
        return self._port_id

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def complete(self, request: CognitionPortRequest) -> CognitionOutcome:
        if not isinstance(request, CognitionPortRequest):
            raise TypeError("request must be CognitionPortRequest")
        if _provider_id(self._provider) != self._provider_id:
            raise ModelProviderCognitionPortError(
                "ModelProvider identity changed after cognition port construction"
            )

        response = self._provider.send(self._provider_request(request))
        if not isinstance(response, ProviderResponse):
            raise ModelProviderCognitionPortError(
                "ModelProvider returned a non-ProviderResponse value"
            )

        return CognitionOutcome.succeeded(
            request.request,
            output_text=response.text,
        )

    @staticmethod
    def _provider_request(request: CognitionPortRequest) -> ProviderRequest:
        cognition = request.request

        # Context is the ordinary model turn and instructions are the role
        # contract. An empty ContextProjection is legal in Gen2; in that narrow
        # case the role instructions themselves become the visible turn rather
        # than dispatching an avoidably empty provider request after handoff.
        if cognition.context:
            return ProviderRequest(
                prompt=cognition.context,
                system=cognition.instructions,
                conversation=None,
            )
        return ProviderRequest(
            prompt=cognition.instructions,
            system=None,
            conversation=None,
        )


def _provider_id(provider: ModelProvider) -> str:
    candidate = getattr(provider, "provider_id", None)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ModelProviderCognitionPortError(
            "ModelProvider must expose a non-empty provider_id"
        )
    return candidate.strip()


def _validate_port_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or value.strip() != value
        or _PORT_ID_RE.fullmatch(value) is None
    ):
        raise ValueError("port_id must be canonical Gen2 cognition port identity")
