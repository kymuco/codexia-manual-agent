from __future__ import annotations

from codexia_manual_agent.adapters.filesystem_workspace import FilesystemWorkspace
from codexia_manual_agent.agent.loop import ReadOnlyAgentLoop
from codexia_manual_agent.application.inspect_workspace import InspectWorkspaceService
from codexia_manual_agent.domain.models import (
    AgentBudgets,
    AgentRunResult,
    SessionManifest,
)
from codexia_manual_agent.ports.model_provider import ModelProvider
from codexia_manual_agent.ports.session_event_recorder import AgentEventRecorder
from codexia_manual_agent.ports.session_store import SessionStore


class RunAgentService:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        store: SessionStore,
        budgets: AgentBudgets | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        event_recorder: AgentEventRecorder | None = None,
    ) -> None:
        self._provider = provider
        self._store = store
        self._budgets = budgets
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._event_recorder = event_recorder

    def run(
        self,
        *,
        manifest: SessionManifest,
        task: str,
    ) -> tuple[AgentRunResult, SessionManifest]:
        inspector = InspectWorkspaceService(FilesystemWorkspace(manifest.workspace))
        loop = ReadOnlyAgentLoop(
            provider=self._provider,
            inspector=inspector,
            prompt_version=manifest.prompt_version,
            budgets=self._budgets,
            event_recorder=self._event_recorder,
        )
        result = loop.run(task, conversation=manifest.conversation)
        updated = manifest.with_run_result(
            result,
            provider=self._provider.provider_id,
            model=self._model,
            reasoning_effort=self._reasoning_effort,
        )
        self._store.save(updated)
        return result, updated
