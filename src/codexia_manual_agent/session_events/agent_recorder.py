from __future__ import annotations

import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

from codexia_manual_agent.domain.errors import ProtocolError
from codexia_manual_agent.domain.models import (
    AgentBudgets,
    AgentRunResult,
    ProviderRequest,
    ProviderResponse,
    SessionManifest,
)
from codexia_manual_agent.session_events.models import (
    EventKind,
    SessionEventIntegrityError,
    SessionEventReceipt,
    SessionEventStateError,
    validate_event_payload,
)
from codexia_manual_agent.session_events.recovery import SessionRecovery
from codexia_manual_agent.session_events.sqlite_store import SqliteSessionEventStore


_REQUEST_BUDGET_ERRORS = frozenset(
    {
        "prompt exceeds the M3 event budget",
        "system exceeds the M3 event budget",
        "Event payload exceeds the M3 byte budget",
    }
)
_RESPONSE_BUDGET_ERRORS = frozenset(
    {
        "response_text exceeds the M3 event budget",
        "Event payload exceeds the M3 byte budget",
    }
)
_OBSERVATION_BUDGET_ERRORS = frozenset(
    {
        "observation_json exceeds the M3 event budget",
        "Event payload exceeds the M3 byte budget",
    }
)
_PREFLIGHT_UUID = "00000000-0000-0000-0000-000000000000"


class SqliteAgentEventRecorder:
    """Records bounded M1.1 read-only run chronology into one M3 session."""

    def __init__(
        self,
        store: SqliteSessionEventStore,
        *,
        session_id: str,
        provider_id: str,
    ) -> None:
        if not isinstance(store, SqliteSessionEventStore):
            raise TypeError("store must be a SqliteSessionEventStore")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be a non-empty string")
        self._store = store
        self._session_id = session_id
        self._provider_id = provider_id

    @classmethod
    def start_from_manifest(
        cls,
        store: SqliteSessionEventStore,
        manifest: SessionManifest,
        *,
        provider_id: str,
    ) -> "SqliteAgentEventRecorder":
        if not isinstance(manifest, SessionManifest):
            raise TypeError("manifest must be a SessionManifest")
        store.start_session(
            session_id=manifest.session_id,
            payload=_session_payload(manifest, provider_id=provider_id),
        )
        return cls(store, session_id=manifest.session_id, provider_id=provider_id)

    @classmethod
    def recover_from_manifest(
        cls,
        store: SqliteSessionEventStore,
        manifest: SessionManifest,
    ) -> SessionRecovery:
        """Replay stable session identity without granting provider continuation."""

        if not isinstance(manifest, SessionManifest):
            raise TypeError("manifest must be a SessionManifest")
        events = store.load_events(manifest.session_id)
        first = events[0]
        if first.kind is not EventKind.SESSION_STARTED:
            raise SessionEventStateError("Persistent chronology does not start with session_started")
        expected = _session_payload(manifest, provider_id=manifest.provider)
        actual = dict(first.payload)
        for field_name in (
            "workspace",
            "prompt_version",
            "mode",
            "capabilities",
            "provider",
            "title",
        ):
            if actual[field_name] != expected[field_name]:
                raise SessionEventStateError(
                    f"Persistent session identity differs from manifest field: {field_name}"
                )
        return store.recover(manifest.session_id)

    @classmethod
    def resume_from_manifest(
        cls,
        store: SqliteSessionEventStore,
        manifest: SessionManifest,
        *,
        provider_id: str,
    ) -> tuple["SqliteAgentEventRecorder", SessionRecovery]:
        """Attach only to a replay-valid session whose exact provider may continue.

        Recovery inspection is provider-configuration independent. Actual
        continuation additionally requires the current provider id to match the
        provider bound by the original session_started event.
        """

        recovery = cls.recover_from_manifest(store, manifest)
        persisted_provider = str(recovery.events[0].payload["provider"])
        if provider_id != persisted_provider:
            raise SessionEventStateError(
                "Persistent session provider differs from the current provider"
            )
        if not recovery.can_resume_provider:
            raise SessionEventStateError(
                "Persistent session cannot resume provider work from recovery state: "
                f"{recovery.disposition.value}"
            )
        return (
            cls(store, session_id=manifest.session_id, provider_id=provider_id),
            recovery,
        )

    @property
    def session_id(self) -> str:
        return self._session_id

    def start_run(self, *, task: str, budgets: AgentBudgets) -> str:
        if not isinstance(budgets, AgentBudgets):
            raise TypeError("budgets must be AgentBudgets")
        run_id = str(uuid4())
        self._store.append(
            self._session_id,
            EventKind.RUN_STARTED,
            {
                "run_id": run_id,
                "task": task,
                "budgets": budgets.to_dict(),
            },
        )
        return run_id

    def preflight_model_request(self, *, request: ProviderRequest) -> None:
        """Validate durable request representation without publishing an event."""

        if not isinstance(request, ProviderRequest):
            raise TypeError("request must be a ProviderRequest")
        payload = {
            "run_id": _PREFLIGHT_UUID,
            "request_id": _PREFLIGHT_UUID,
            "provider": self._provider_id,
            "prompt": request.prompt,
            "system": request.system,
            "conversation": (
                request.conversation.to_dict()
                if request.conversation is not None
                else None
            ),
        }
        try:
            validate_event_payload(EventKind.MODEL_REQUEST_STARTED, payload)
        except SessionEventIntegrityError as exc:
            message = str(exc)
            if (
                "\x00" in request.prompt
                or (request.system is not None and "\x00" in request.system)
                or message not in _REQUEST_BUDGET_ERRORS
            ):
                raise
            if message == "Event payload exceeds the M3 byte budget":
                probe_payload = dict(payload)
                probe_payload["prompt"] = ""
                probe_payload["system"] = None
                validate_event_payload(EventKind.MODEL_REQUEST_STARTED, probe_payload)
            raise ProtocolError(
                f"Persistent model request exceeds event budget: {message}"
            ) from exc

    def model_request_started(
        self,
        *,
        run_id: str,
        request: ProviderRequest,
    ) -> str:
        if not isinstance(request, ProviderRequest):
            raise TypeError("request must be a ProviderRequest")
        self.preflight_model_request(request=request)
        request_id = str(uuid4())
        self._store.append(
            self._session_id,
            EventKind.MODEL_REQUEST_STARTED,
            {
                "run_id": run_id,
                "request_id": request_id,
                "provider": self._provider_id,
                "prompt": request.prompt,
                "system": request.system,
                "conversation": (
                    request.conversation.to_dict()
                    if request.conversation is not None
                    else None
                ),
            },
        )
        return request_id

    def model_response_recorded(
        self,
        *,
        run_id: str,
        request_id: str,
        response: ProviderResponse,
    ) -> None:
        if not isinstance(response, ProviderResponse):
            raise TypeError("response must be a ProviderResponse")
        conversation = (
            response.conversation.to_dict()
            if response.conversation is not None
            else None
        )
        exact_payload = {
            "run_id": run_id,
            "request_id": request_id,
            "provider": self._provider_id,
            "response_text": response.text,
            "conversation": conversation,
            "model": response.model,
            "reasoning_effort": response.reasoning_effort,
            "metrics": dict(response.metrics),
        }
        try:
            validate_event_payload(EventKind.MODEL_RESPONSE_RECORDED, exact_payload)
        except SessionEventIntegrityError as exc:
            if "\x00" in response.text or str(exc) not in _RESPONSE_BUDGET_ERRORS:
                raise
            if str(exc) == "Event payload exceeds the M3 byte budget":
                probe_payload = dict(exact_payload)
                probe_payload["response_text"] = ""
                validate_event_payload(EventKind.MODEL_RESPONSE_RECORDED, probe_payload)
            encoded = response.text.encode("utf-8")
            payload = {
                "run_id": run_id,
                "request_id": request_id,
                "provider": self._provider_id,
                "response_chars": len(response.text),
                "response_bytes": len(encoded),
                "response_digest": sha256(encoded).hexdigest(),
                "response_storage": "digest_only",
                "conversation": conversation,
                "model": response.model,
                "reasoning_effort": response.reasoning_effort,
            }
        else:
            payload = exact_payload
        self._store.append(
            self._session_id,
            EventKind.MODEL_RESPONSE_RECORDED,
            payload,
        )

    def tool_observation_recorded(
        self,
        *,
        run_id: str,
        request_id: str,
        tool: str,
        observation_json: str,
    ) -> None:
        exact_payload = {
            "run_id": run_id,
            "request_id": request_id,
            "tool": tool,
            "observation_json": observation_json,
        }
        try:
            validate_event_payload(EventKind.TOOL_OBSERVATION_RECORDED, exact_payload)
        except SessionEventIntegrityError as exc:
            if "\x00" in observation_json or str(exc) not in _OBSERVATION_BUDGET_ERRORS:
                raise
            if str(exc) == "Event payload exceeds the M3 byte budget":
                probe_payload = dict(exact_payload)
                probe_payload["observation_json"] = ""
                validate_event_payload(EventKind.TOOL_OBSERVATION_RECORDED, probe_payload)
            encoded = observation_json.encode("utf-8")
            durable_observation = json.dumps(
                {
                    "observation_storage": "digest_only",
                    "observation_chars": len(observation_json),
                    "observation_bytes": len(encoded),
                    "observation_digest": sha256(encoded).hexdigest(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            payload = {
                "run_id": run_id,
                "request_id": request_id,
                "tool": tool,
                "observation_json": durable_observation,
            }
        else:
            payload = exact_payload
        self._store.append(
            self._session_id,
            EventKind.TOOL_OBSERVATION_RECORDED,
            payload,
        )

    def run_completed(self, *, run_id: str, result: AgentRunResult) -> None:
        if not isinstance(result, AgentRunResult):
            raise TypeError("result must be an AgentRunResult")
        self._store.append(
            self._session_id,
            EventKind.RUN_COMPLETED,
            {
                "run_id": run_id,
                "status": result.status.value,
                "final_text": result.final_text,
                "turns": result.turns,
                "tool_calls": result.tool_calls,
                "model_chars": result.model_chars,
                "conversation": (
                    result.conversation.to_dict()
                    if result.conversation is not None
                    else None
                ),
                "model": result.model,
                "reasoning_effort": result.reasoning_effort,
                "error": result.error,
            },
        )

    def run_interrupted(
        self,
        *,
        run_id: str,
        reason: str,
        detail: str,
        request_id: str | None,
    ) -> None:
        self._store.append(
            self._session_id,
            EventKind.RUN_INTERRUPTED,
            {
                "run_id": run_id,
                "reason": reason,
                "detail": detail,
                "request_id": request_id,
            },
        )

    def complete_session(
        self,
        *,
        status: str = "completed",
        detail: str | None = None,
    ) -> SessionEventReceipt:
        return self._store.append(
            self._session_id,
            EventKind.SESSION_COMPLETED,
            {"status": status, "detail": detail},
        )


def _session_payload(
    manifest: SessionManifest,
    *,
    provider_id: str,
) -> dict[str, Any]:
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("provider_id must be a non-empty string")
    return {
        "workspace": manifest.workspace,
        "prompt_version": manifest.prompt_version,
        "mode": manifest.mode,
        "capabilities": tuple(manifest.capabilities),
        "provider": provider_id,
        "title": manifest.title,
        "model": manifest.model,
        "reasoning_effort": manifest.reasoning_effort,
    }
