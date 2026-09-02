from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from codexia_manual_agent.application.run_agent import RunAgentService
from codexia_manual_agent.application.run_session import RunSessionService
from codexia_manual_agent.application.session_queries import SessionQueryService
from codexia_manual_agent.domain.models import (
    AgentBudgets,
    AgentRunResult,
    SessionManifest,
)
from codexia_manual_agent.ports.model_provider import ModelProvider
from codexia_manual_agent.ports.session_store import SessionStore
from codexia_manual_agent.session_events.agent_recorder import SqliteAgentEventRecorder
from codexia_manual_agent.session_events.recovery import SessionRecovery
from codexia_manual_agent.session_events.sqlite_store import SqliteSessionEventStore


class PersistentRunAgentService:
    """M3 composition root for durable read-only sessions.

    JSON manifests remain a small discovery/summary surface. The SQLite event
    ledger is authoritative for recovered conversation identity, counters and
    interruption state. Pure recovery never contacts or depends on the currently
    configured provider; actual continuation separately verifies exact provider
    identity.
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        manifest_store: SessionStore,
        event_store: SqliteSessionEventStore,
        budgets: AgentBudgets | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._provider = provider
        self._manifest_store = manifest_store
        self._event_store = event_store
        self._budgets = budgets
        self._model = model
        self._reasoning_effort = reasoning_effort

    def start_and_run(
        self,
        *,
        workspace: str | Path,
        task: str,
        prompt_version: str = "v0.3",
        title: str | None = None,
    ) -> tuple[AgentRunResult, SessionManifest]:
        # Construct and validate the manifest without exposing it first. The M3
        # event ledger is authoritative, so SESSION_STARTED must become durable
        # before the discovery/summary manifest is published.
        manifest = RunSessionService(self._manifest_store).prepare(
            workspace=workspace,
            prompt_version=prompt_version,
            title=title,
            provider=self._provider.provider_id,
            model=self._model,
            reasoning_effort=self._reasoning_effort,
        )
        recorder = SqliteAgentEventRecorder.start_from_manifest(
            self._event_store,
            manifest,
            provider_id=self._provider.provider_id,
        )
        self._manifest_store.save(manifest)
        return self._run(manifest=manifest, task=task, recorder=recorder)

    def resume_and_run(
        self,
        *,
        session_id: str,
        task: str,
    ) -> tuple[AgentRunResult, SessionManifest, SessionRecovery]:
        manifest = SessionQueryService(self._manifest_store).resume(session_id)
        recorder, recovery = SqliteAgentEventRecorder.resume_from_manifest(
            self._event_store,
            manifest,
            provider_id=self._provider.provider_id,
        )

        # The manifest may lag if the prior process died after durable events but
        # before its summary save. Repair only fields derivable from the ledger.
        recovered_manifest = replace(
            manifest,
            provider=self._provider.provider_id,
            conversation=recovery.latest_conversation,
            turn_count=recovery.turns,
            tool_call_count=recovery.tool_calls,
        )
        self._manifest_store.save(recovered_manifest)
        result, updated = self._run(
            manifest=recovered_manifest,
            task=task,
            recorder=recorder,
        )
        return result, updated, recovery

    def recover(self, session_id: str) -> SessionRecovery:
        """Replay durable state without contacting or binding the current provider."""

        manifest = SessionQueryService(self._manifest_store).resume(session_id)
        return SqliteAgentEventRecorder.recover_from_manifest(
            self._event_store,
            manifest,
        )

    def _run(
        self,
        *,
        manifest: SessionManifest,
        task: str,
        recorder: SqliteAgentEventRecorder,
    ) -> tuple[AgentRunResult, SessionManifest]:
        return RunAgentService(
            provider=self._provider,
            store=self._manifest_store,
            budgets=self._budgets,
            model=self._model,
            reasoning_effort=self._reasoning_effort,
            event_recorder=recorder,
        ).run(manifest=manifest, task=task)
