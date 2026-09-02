from __future__ import annotations

from typing import Protocol

from codexia_manual_agent.domain.models import (
    AgentBudgets,
    AgentRunResult,
    ProviderRequest,
    ProviderResponse,
)


class AgentEventRecorder(Protocol):
    """Non-authority event sink used by the read-only agent loop."""

    def start_run(self, *, task: str, budgets: AgentBudgets) -> str: ...

    def preflight_model_request(self, *, request: ProviderRequest) -> None: ...

    def model_request_started(
        self,
        *,
        run_id: str,
        request: ProviderRequest,
    ) -> str: ...

    def model_response_recorded(
        self,
        *,
        run_id: str,
        request_id: str,
        response: ProviderResponse,
    ) -> None: ...

    def tool_observation_recorded(
        self,
        *,
        run_id: str,
        request_id: str,
        tool: str,
        observation_json: str,
    ) -> None: ...

    def run_completed(self, *, run_id: str, result: AgentRunResult) -> None: ...

    def run_interrupted(
        self,
        *,
        run_id: str,
        reason: str,
        detail: str,
        request_id: str | None,
    ) -> None: ...
