from __future__ import annotations

from typing import Protocol

from codexia_manual_agent.domain.models import ProviderRequest, ProviderResponse


class ModelProvider(Protocol):
    """Transport boundary for one model turn.

    Providers may use a remote model transport, but they do not receive direct
    access to local tools or workspace capabilities.
    """

    @property
    def provider_id(self) -> str: ...

    def send(self, request: ProviderRequest) -> ProviderResponse: ...
