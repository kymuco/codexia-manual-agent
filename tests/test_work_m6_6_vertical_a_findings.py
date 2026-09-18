from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codexia_manual_agent.domain.models import ProviderConversation, ProviderResponse
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.work import (
    BackgroundWorkDriver,
    BackgroundWorkSupervisor,
    PilotCheckpointSource,
    SupervisorDriveStop,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)
from codexia_manual_agent.work.pilot_peer_loop import PilotChatGPTPeerLoop
from codexia_manual_agent.work.pilot_runtime import answer_daily_use_pilot


class _Metrics:
    def to_dict(self):
        return {}


class _Client:
    def __init__(self, *, tool_tail: bool = False) -> None:
        self.messages: list[SimpleNamespace] = []
        self.send_calls: list[str] = []
        self._counter = 0
        self.tool_tail = tool_tail
        self.append_user("Existing human context.")
        self.append_assistant("Ready.")

    def _message(self, role: str, text: str) -> SimpleNamespace:
        self._counter += 1
        return SimpleNamespace(
            node_id=f"node-{self._counter}",
            message_id=f"message-{self._counter}",
            role=role,
            text=text,
            create_time=float(self._counter),
            recipient="all" if role == "assistant" else None,
            model="gpt-test" if role == "assistant" else None,
            finish_reason="stop" if role == "assistant" else None,
        )

    def append_user(self, text: str) -> SimpleNamespace:
        message = self._message("user", text)
        self.messages.append(message)
        return message

    def append_assistant(self, text: str) -> SimpleNamespace:
        message = self._message("assistant", text)
        self.messages.append(message)
        return message

    def get_messages(self, conversation_id: str, **kwargs):
        assert conversation_id == "conversation-1"
        roles = set(kwargs.get("roles") or ())
        messages = self.messages
        if roles:
            messages = [message for message in messages if message.role in roles]
        return list(messages)

    def send_to_conversation(self, conversation_id: str, prompt: str, **kwargs):
        assert conversation_id == "conversation-1"
        self.send_calls.append(prompt)
        self.append_user(prompt)
        if self.tool_tail:
            self.append_assistant('{"tool":"read","args":{"target":"candidate"}}')
        final = self.append_assistant("Terminal worker evidence from the logical provider turn.")
        return SimpleNamespace(
            text=final.text,
            conversation=SimpleNamespace(
                conversation_id=conversation_id,
                message_id=final.message_id,
                parent_message_id=final.message_id,
                finish_reason="stop",
            ),
            request=SimpleNamespace(
                observed_model="gpt-test",
                sent_model=None,
                observed_reasoning_effort="high",
                sent_reasoning_effort=None,
            ),
            metrics=_Metrics(),
        )


class _Cognition:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ProviderResponse(
            text=json.dumps(self.responses.pop(0)),
            conversation=ProviderConversation(conversation_id="cognition-1"),
        )


def _statement(kind: WorkActorKind, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor="operator", text=text)


def _registered(tmp_path, *, attention: bool = False, tool_tail: bool = False):
    client = _Client(tool_tail=tool_tail)
    peer = PilotChatGPTPeerLoop(ChatGPTWebProvider(client=client))
    objective = _statement(WorkActorKind.HUMAN, "Produce a dependable delegated result.")
    constraint = (
        _statement(WorkActorKind.HUMAN, "Ask me before finalizing when this rule is triggered or uncertain.")
        if attention
        else None
    )
    handoff = WorkHandoff.create(
        objective=objective,
        attention_constraints=(() if constraint is None else (constraint,)),
    )
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,) if constraint is None else (objective, constraint),
        completion_expectation="Return a finished result after exact worker evidence.",
        continuation_scope="Stay inside the delegated objective.",
        depth_interpretation="Use sufficient evidence and depth.",
    )
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    snapshot = supervisor.register(
        handoff=handoff,
        interpretation=interpretation,
        cursor=peer.attach("conversation-1"),
    )
    return supervisor, snapshot, peer, client, constraint


def _checkpoint(*, proposal_text: str | None, ask_human: bool = False) -> dict[str, object]:
    return {
        "mode": "checkpoint",
        "proposal_text": proposal_text,
        "objective_fit": "aligned",
        "constraint_fit": "aligned",
        "scope_fit": "aligned",
        "depth_fit": "aligned",
        "evidence_fit": "supported",
        "material_human_choice": ask_human,
        "admission_reason": "Human judgment is required." if ask_human else "Routine bounded continuation.",
        "revision_request": None,
        "requested_human_response": "Choose before continuing." if ask_human else None,
        "reversibility": "reversible",
        "trajectory_impact": "material" if ask_human else "routine",
        "alternatives": "material" if ask_human else "none",
        "attention_constraint_checks": [],
        "cognitive_disposition": "keep_moving",
        "confidence_basis_points": 9900,
        "urgency": "normal" if ask_human else "none",
        "attention_reason": "Human judgment is required." if ask_human else "No human attention is needed.",
        "requested_response": "Choose before continuing." if ask_human else None,
    }


def _completion(constraint: WorkStatement, status: str) -> dict[str, object]:
    return {
        "mode": "complete",
        "proposal_text": None,
        "objective_fit": "aligned",
        "constraint_fit": "aligned",
        "scope_fit": "aligned",
        "depth_fit": "aligned",
        "evidence_fit": "supported",
        "material_human_choice": False,
        "admission_reason": "Terminal worker evidence satisfies the delegated objective.",
        "revision_request": None,
        "requested_human_response": None,
        "reversibility": "reversible",
        "trajectory_impact": "routine",
        "alternatives": "none",
        "attention_constraint_checks": [
            {
                "statement_digest": constraint.statement_digest,
                "status": status,
                "reason": f"The exact completion-time HUMAN rule is {status}.",
            }
        ],
        "cognitive_disposition": "keep_moving",
        "confidence_basis_points": 9900,
        "urgency": "none" if status == "clear" else "normal",
        "attention_reason": f"Completion-time HUMAN rule is {status}.",
        "requested_response": None if status == "clear" else "Resolve the completion-time HUMAN rule.",
        "completion_summary": "The governed completion candidate satisfies the objective.",
    }


@pytest.mark.parametrize("status", ["triggered", "uncertain"])
def test_completion_attention_override_never_completes(status, tmp_path) -> None:
    supervisor, snapshot, peer, _client, constraint = _registered(tmp_path, attention=True)
    assert constraint is not None
    cognition = _Cognition([
        {**_checkpoint(proposal_text="Produce one exact worker result."), "attention_constraint_checks": [
            {"statement_digest": constraint.statement_digest, "status": "clear", "reason": "The routine worker step does not finalize the work."}
        ]},
        _completion(constraint, status),
    ])
    source = PilotCheckpointSource(supervisor=supervisor, provider=cognition)

    result = BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
        snapshot.work_id, peer_loop=peer, checkpoint_source=source, max_steps=16
    )

    assert result.stop is SupervisorDriveStop.WAITING_HUMAN
    assert result.snapshot.completion is None
    assert result.snapshot.last_attention is not None
    assert result.snapshot.last_attention.needs_human is True


def test_completion_with_clear_exact_constraint_coverage_can_complete(tmp_path) -> None:
    supervisor, snapshot, peer, _client, constraint = _registered(tmp_path, attention=True)
    assert constraint is not None
    cognition = _Cognition([
        {**_checkpoint(proposal_text="Produce one exact worker result."), "attention_constraint_checks": [
            {"statement_digest": constraint.statement_digest, "status": "clear", "reason": "The routine worker step does not finalize the work."}
        ]},
        _completion(constraint, "clear"),
    ])
    source = PilotCheckpointSource(supervisor=supervisor, provider=cognition)

    result = BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
        snapshot.work_id, peer_loop=peer, checkpoint_source=source, max_steps=16
    )

    assert result.stop is SupervisorDriveStop.COMPLETED
    assert result.snapshot.completion is not None


def test_pilot_human_answer_survives_newer_external_observation_until_fresh_judgment(tmp_path) -> None:
    supervisor, snapshot, peer, client, _constraint = _registered(tmp_path)
    waiting_cognition = _Cognition([_checkpoint(proposal_text="Choose the material direction.", ask_human=True)])
    waiting = BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
        snapshot.work_id,
        peer_loop=peer,
        checkpoint_source=PilotCheckpointSource(supervisor=supervisor, provider=waiting_cognition),
        max_steps=4,
    )
    assert waiting.stop is SupervisorDriveStop.WAITING_HUMAN

    answer_daily_use_pilot(
        database_path=tmp_path / "supervisor.sqlite3",
        work_id=snapshot.work_id,
        answer="Choose B.",
        human_actor="operator",
    )
    client.append_user("Supplementary account-side note after the pilot answer.")

    resumed_cognition = _Cognition([_checkpoint(proposal_text="Continue using the exact HUMAN choice.")])
    BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
        snapshot.work_id,
        peer_loop=peer,
        checkpoint_source=PilotCheckpointSource(supervisor=supervisor, provider=resumed_cognition),
        max_steps=2,
    )

    prompt = resumed_cognition.requests[0].prompt
    assert "Choose B." in prompt
    assert "Supplementary account-side note after the pilot answer." in prompt


def test_tool_using_assistant_tail_remains_logical_worker_evidence_and_completes(tmp_path) -> None:
    supervisor, snapshot, peer, _client, _constraint = _registered(tmp_path, tool_tail=True)
    cognition = _Cognition([
        _checkpoint(proposal_text="Perform the bounded tool-using worker step."),
        {"mode": "complete", "completion_summary": "The exact logical worker result satisfies the objective."},
    ])
    source = PilotCheckpointSource(supervisor=supervisor, provider=cognition)

    result = BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
        snapshot.work_id, peer_loop=peer, checkpoint_source=source, max_steps=16
    )

    assert result.stop is SupervisorDriveStop.COMPLETED
    assert result.provider_turns == 1
    assert "Terminal worker evidence from the logical provider turn." in cognition.requests[1].prompt
    assert "Current exact logical worker evidence" in cognition.requests[1].prompt
