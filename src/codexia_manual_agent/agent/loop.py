from __future__ import annotations

from codexia_manual_agent.agent.prompting import (
    build_initial_task_prompt,
    build_observation_prompt,
    build_runtime_system_prompt,
)
from codexia_manual_agent.agent.protocol import (
    FinalReply,
    parse_model_reply,
    render_observation,
    serialize_observation,
)
from codexia_manual_agent.agent.tool_policy import ReadOnlyAgentToolPolicy
from codexia_manual_agent.application.inspect_workspace import InspectWorkspaceService
from codexia_manual_agent.domain.errors import (
    InvalidToolArgumentsError,
    ProtocolError,
    ProviderError,
)
from codexia_manual_agent.domain.models import (
    AgentBudgets,
    AgentRunResult,
    AgentRunStatus,
    ProviderConversation,
    ProviderRequest,
    ToolObservation,
)
from codexia_manual_agent.ports.model_provider import ModelProvider
from codexia_manual_agent.ports.session_event_recorder import AgentEventRecorder


class ReadOnlyAgentLoop:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        inspector: InspectWorkspaceService,
        prompt_version: str = "v0.3",
        budgets: AgentBudgets | None = None,
        tool_policy: ReadOnlyAgentToolPolicy | None = None,
        event_recorder: AgentEventRecorder | None = None,
    ) -> None:
        self._provider = provider
        self._inspector = inspector
        self._prompt_version = prompt_version
        self._budgets = budgets or AgentBudgets()
        self._tool_policy = tool_policy or ReadOnlyAgentToolPolicy()
        self._event_recorder = event_recorder

    def run(
        self,
        task: str,
        *,
        conversation: ProviderConversation | None = None,
    ) -> AgentRunResult:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string")

        prompt = build_initial_task_prompt(task)
        system = (
            None
            if conversation is not None and conversation.conversation_id
            else build_runtime_system_prompt(self._prompt_version)
        )
        recorder = self._event_recorder
        if recorder is not None:
            initial_request = ProviderRequest(
                prompt=prompt,
                system=system,
                conversation=conversation,
            )
            try:
                recorder.preflight_model_request(request=initial_request)
            except ProtocolError as exc:
                # No durable run and no provider side effect exist yet. Reject the
                # request at the persistence budget boundary instead of publishing
                # RUN_STARTED that can never advance to MODEL_REQUEST_STARTED.
                return self._result(
                    AgentRunStatus.BUDGET_EXHAUSTED,
                    0,
                    0,
                    0,
                    conversation,
                    None,
                    None,
                    error=str(exc),
                )
            run_id = recorder.start_run(task=task, budgets=self._budgets)
        else:
            run_id = None

        turns = 0
        tool_calls = 0
        model_chars = 0
        seen_request_ids: set[str] = set()
        current_conversation = conversation
        current_model: str | None = None
        current_reasoning_effort: str | None = None

        while True:
            if turns >= self._budgets.max_turns:
                return self._finish(
                    self._result(
                        AgentRunStatus.BUDGET_EXHAUSTED,
                        turns,
                        tool_calls,
                        model_chars,
                        current_conversation,
                        current_model,
                        current_reasoning_effort,
                        error=f"Turn budget exhausted ({self._budgets.max_turns})",
                    ),
                    recorder=recorder,
                    run_id=run_id,
                )

            provider_request = ProviderRequest(
                prompt=prompt,
                system=system,
                conversation=current_conversation,
            )
            if recorder is not None and run_id is not None:
                try:
                    recorder.preflight_model_request(request=provider_request)
                except ProtocolError as exc:
                    # No provider side effect has occurred. Close the already-open
                    # durable run instead of leaving it stranded when a later
                    # rendered request cannot satisfy the persistence budget.
                    return self._finish(
                        self._result(
                            AgentRunStatus.BUDGET_EXHAUSTED,
                            turns,
                            tool_calls,
                            model_chars,
                            current_conversation,
                            current_model,
                            current_reasoning_effort,
                            error=str(exc),
                        ),
                        recorder=recorder,
                        run_id=run_id,
                    )
            provider_request_id = (
                recorder.model_request_started(run_id=run_id, request=provider_request)
                if recorder is not None and run_id is not None
                else None
            )
            try:
                response = self._provider.send(provider_request)
            except ProviderError as exc:
                if recorder is not None and run_id is not None:
                    recorder.run_interrupted(
                        run_id=run_id,
                        reason="provider_error",
                        detail=str(exc),
                        request_id=provider_request_id,
                    )
                return self._result(
                    AgentRunStatus.PROVIDER_ERROR,
                    turns,
                    tool_calls,
                    model_chars,
                    current_conversation,
                    current_model,
                    current_reasoning_effort,
                    error=str(exc),
                )
            except Exception as exc:
                if recorder is not None and run_id is not None:
                    recorder.run_interrupted(
                        run_id=run_id,
                        reason="unexpected_provider_error",
                        detail=str(exc),
                        request_id=provider_request_id,
                    )
                return self._result(
                    AgentRunStatus.PROVIDER_ERROR,
                    turns,
                    tool_calls,
                    model_chars,
                    current_conversation,
                    current_model,
                    current_reasoning_effort,
                    error=f"Unexpected provider failure: {exc}",
                )

            if recorder is not None and run_id is not None and provider_request_id is not None:
                recorder.model_response_recorded(
                    run_id=run_id,
                    request_id=provider_request_id,
                    response=response,
                )

            turns += 1
            current_conversation = response.conversation or current_conversation
            current_model = response.model or current_model
            current_reasoning_effort = response.reasoning_effort or current_reasoning_effort
            model_chars += len(response.text)
            if model_chars > self._budgets.max_total_model_chars:
                return self._finish(
                    self._result(
                        AgentRunStatus.BUDGET_EXHAUSTED,
                        turns,
                        tool_calls,
                        model_chars,
                        current_conversation,
                        current_model,
                        current_reasoning_effort,
                        error=(
                            "Cumulative model output budget exhausted "
                            f"({model_chars} > {self._budgets.max_total_model_chars})"
                        ),
                    ),
                    recorder=recorder,
                    run_id=run_id,
                )

            try:
                reply = parse_model_reply(
                    response.text,
                    max_chars=self._budgets.max_response_chars,
                )
            except ProtocolError as exc:
                return self._finish(
                    self._result(
                        AgentRunStatus.PROTOCOL_ERROR,
                        turns,
                        tool_calls,
                        model_chars,
                        current_conversation,
                        current_model,
                        current_reasoning_effort,
                        error=str(exc),
                    ),
                    recorder=recorder,
                    run_id=run_id,
                )

            if isinstance(reply, FinalReply):
                return self._finish(
                    AgentRunResult(
                        status=AgentRunStatus.COMPLETED,
                        final_text=reply.text,
                        turns=turns,
                        tool_calls=tool_calls,
                        model_chars=model_chars,
                        conversation=current_conversation,
                        model=current_model,
                        reasoning_effort=current_reasoning_effort,
                    ),
                    recorder=recorder,
                    run_id=run_id,
                )

            if reply.request_id in seen_request_ids:
                return self._finish(
                    self._result(
                        AgentRunStatus.PROTOCOL_ERROR,
                        turns,
                        tool_calls,
                        model_chars,
                        current_conversation,
                        current_model,
                        current_reasoning_effort,
                        error=f"Duplicate tool request id: {reply.request_id}",
                    ),
                    recorder=recorder,
                    run_id=run_id,
                )
            seen_request_ids.add(reply.request_id)

            if tool_calls >= self._budgets.max_tool_calls:
                return self._finish(
                    self._result(
                        AgentRunStatus.BUDGET_EXHAUSTED,
                        turns,
                        tool_calls,
                        model_chars,
                        current_conversation,
                        current_model,
                        current_reasoning_effort,
                        error=f"Tool-call budget exhausted ({self._budgets.max_tool_calls})",
                    ),
                    recorder=recorder,
                    run_id=run_id,
                )

            try:
                validated = self._tool_policy.validate(reply)
                observation = self._inspector.execute(validated)
            except InvalidToolArgumentsError as exc:
                observation = ToolObservation(
                    request_id=reply.request_id,
                    tool=reply.name,
                    success=False,
                    error=str(exc),
                )
            tool_calls += 1

            # A tool call has already happened at this point. Persist the exact
            # deterministic observation before applying the smaller model-context
            # observation budget, so terminal counters can never claim a tool call
            # that durable recovery cannot see.
            if recorder is not None and run_id is not None:
                recorder.tool_observation_recorded(
                    run_id=run_id,
                    request_id=reply.request_id,
                    tool=reply.name.value,
                    observation_json=render_observation(observation.to_dict()),
                )

            try:
                observation_json = serialize_observation(
                    observation.to_dict(),
                    max_chars=self._budgets.max_observation_chars,
                )
            except ProtocolError as exc:
                return self._finish(
                    self._result(
                        AgentRunStatus.BUDGET_EXHAUSTED,
                        turns,
                        tool_calls,
                        model_chars,
                        current_conversation,
                        current_model,
                        current_reasoning_effort,
                        error=str(exc),
                    ),
                    recorder=recorder,
                    run_id=run_id,
                )

            prompt = build_observation_prompt(observation_json)
            system = None

    @staticmethod
    def _finish(
        result: AgentRunResult,
        *,
        recorder: AgentEventRecorder | None,
        run_id: str | None,
    ) -> AgentRunResult:
        if recorder is not None and run_id is not None:
            recorder.run_completed(run_id=run_id, result=result)
        return result

    @staticmethod
    def _result(
        status: AgentRunStatus,
        turns: int,
        tool_calls: int,
        model_chars: int,
        conversation: ProviderConversation | None,
        model: str | None,
        reasoning_effort: str | None,
        *,
        error: str,
    ) -> AgentRunResult:
        return AgentRunResult(
            status=status,
            final_text=None,
            turns=turns,
            tool_calls=tool_calls,
            model_chars=model_chars,
            conversation=conversation,
            model=model,
            reasoning_effort=reasoning_effort,
            error=error,
        )
