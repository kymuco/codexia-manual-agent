from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codexia_manual_agent.domain.models import ProviderConversation, ProviderResponse
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.work import (
    BackgroundWorkDriver,
    BackgroundWorkSupervisor,
    ChatGPTPeerLoop,
    InvalidWorkRecordError,
    PilotCheckpointSource,
    SupervisorCompletion,
    SupervisorDriveStop,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)


class _Metrics:
    def to_dict(self):
        return {}


class FakeLiveChatClient:
    def __init__(self) -> None:
        self.messages: list[SimpleNamespace] = []
        self.send_calls: list[str] = []
        self._counter = 0
        self.append_user("Existing human context.")
        self.append_assistant("Ready for delegated work.")

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
        result = self.messages
        if roles:
            result = [message for message in result if message.role in roles]
        return list(result)

    def send_to_conversation(self, conversation_id: str, prompt: str, **kwargs):
        assert conversation_id == "conversation-1"
        self.send_calls.append(prompt)
        self.append_user(prompt)
        assistant = self.append_assistant(
            f"Worker evidence {len(self.send_calls)}: routine delegated step completed."
        )
        return SimpleNamespace(
            text=assistant.text,
            conversation=SimpleNamespace(
                conversation_id=conversation_id,
                message_id=assistant.message_id,
                parent_message_id=assistant.message_id,
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


class FakeCognitionProvider:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        payload = self.responses.pop(0)
        return ProviderResponse(
            text=json.dumps(payload),
            conversation=ProviderConversation(conversation_id="cognition-1"),
            model="gpt-test",
            reasoning_effort="high",
        )


def _statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def _work() -> tuple[WorkHandoff, WorkIntentInterpretation]:
    objective = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Produce a finished delegated result without asking me for routine continuation.",
    )
    handoff = WorkHandoff.create(objective=objective)
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Return a finished result after sufficient worker evidence.",
        continuation_scope="Stay within this delegated objective.",
        depth_interpretation="Use enough depth to produce a dependable result.",
    )
    return handoff, interpretation


def _registered(tmp_path):
    client = FakeLiveChatClient()
    peer = ChatGPTPeerLoop(ChatGPTWebProvider(client=client))
    handoff, interpretation = _work()
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    snapshot = supervisor.register(
        handoff=handoff,
        interpretation=interpretation,
        cursor=peer.attach("conversation-1"),
    )
    return supervisor, snapshot, peer, client


def _checkpoint(*, proposal_text, ask_human: bool = False):
    return {
        "mode": "checkpoint",
        "proposal_text": proposal_text,
        "objective_fit": "aligned",
        "constraint_fit": "aligned",
        "scope_fit": "aligned",
        "depth_fit": "aligned",
        "evidence_fit": "supported",
        "material_human_choice": ask_human,
        "admission_reason": (
            "A material human choice is genuinely required."
            if ask_human
            else "The candidate is a routine continuation inside delegated scope."
        ),
        "revision_request": None,
        "requested_human_response": (
            "Choose the material direction before continuing." if ask_human else None
        ),
        "reversibility": "reversible",
        "trajectory_impact": "material" if ask_human else "routine",
        "alternatives": "material" if ask_human else "none",
        "attention_constraint_checks": [],
        "cognitive_disposition": "keep_moving",
        "confidence_basis_points": 9400,
        "urgency": "normal" if ask_human else "none",
        "attention_reason": (
            "Human judgment is required at this material branch."
            if ask_human
            else "No human attention is needed for this routine continuation."
        ),
        "requested_response": (
            "Resolve the material branch." if ask_human else None
        ),
    }


def test_supervisor_completion_requires_codexia_authorship() -> None:
    worker = _statement(WorkActorKind.WORKER, "worker", "I think the work is done.")
    with pytest.raises(InvalidWorkRecordError):
        SupervisorCompletion(completion=worker)


def test_live_pilot_crosses_two_worker_turns_then_codexia_completes(tmp_path) -> None:
    supervisor, snapshot, peer, client = _registered(tmp_path)
    cognition = FakeCognitionProvider(
        [
            _checkpoint(proposal_text="Start the next bounded step toward the objective."),
            _checkpoint(proposal_text=None),
            {
                "mode": "complete",
                "completion_summary": (
                    "The objective is complete after two exact worker turns supplied "
                    "sufficient evidence for the requested finished result."
                ),
            },
        ]
    )
    source = PilotCheckpointSource(supervisor=supervisor, provider=cognition)

    result = BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
        snapshot.work_id,
        peer_loop=peer,
        checkpoint_source=source,
        max_steps=16,
    )

    assert result.stop is SupervisorDriveStop.COMPLETED
    assert result.provider_turns == 2
    assert len(client.send_calls) == 2
    assert result.snapshot.completion is not None
    assert result.snapshot.completion.author_kind is WorkActorKind.CODEXIA
    assert len(cognition.requests) == 3
    assert cognition.requests[0].conversation is None
    assert cognition.requests[1].conversation is not None
    assert cognition.requests[1].conversation.conversation_id == "cognition-1"


def test_pilot_stops_for_human_and_uses_exact_answer_to_resume(tmp_path) -> None:
    supervisor, snapshot, peer, client = _registered(tmp_path)
    cognition = FakeCognitionProvider(
        [
            _checkpoint(
                proposal_text="Choose between two materially different directions.",
                ask_human=True,
            ),
            _checkpoint(
                proposal_text="Apply the human's exact choice and continue the bounded work."
            ),
            {
                "mode": "complete",
                "completion_summary": (
                    "The human choice was observed as exact external evidence and the "
                    "worker completed the resulting bounded step."
                ),
            },
        ]
    )
    source = PilotCheckpointSource(supervisor=supervisor, provider=cognition)
    driver = BackgroundWorkDriver(supervisor)

    first = driver.drive_chat_until_blocked(
        snapshot.work_id,
        peer_loop=peer,
        checkpoint_source=source,
        max_steps=8,
    )
    assert first.stop is SupervisorDriveStop.WAITING_HUMAN
    assert first.provider_turns == 0
    assert client.send_calls == []

    client.append_user("Use direction B; preserve the original scope.")
    resumed = driver.drive_chat_until_blocked(
        snapshot.work_id,
        peer_loop=peer,
        checkpoint_source=source,
        max_steps=12,
    )

    assert resumed.stop is SupervisorDriveStop.COMPLETED
    assert resumed.provider_turns == 1
    assert len(client.send_calls) == 1
    second_prompt = cognition.requests[1].prompt
    assert "Use direction B; preserve the original scope." in second_prompt
    assert "external_user" in second_prompt


def test_pilot_cannot_replace_exact_worker_followup(tmp_path) -> None:
    supervisor, snapshot, peer, _client = _registered(tmp_path)
    cognition = FakeCognitionProvider(
        [
            _checkpoint(proposal_text="Start the delegated step."),
            _checkpoint(proposal_text="Replace the worker output with this text."),
        ]
    )
    source = PilotCheckpointSource(supervisor=supervisor, provider=cognition)

    with pytest.raises(InvalidWorkRecordError):
        BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
            snapshot.work_id,
            peer_loop=peer,
            checkpoint_source=source,
            max_steps=8,
        )


def test_pilot_cannot_complete_before_worker_evidence(tmp_path) -> None:
    supervisor, snapshot, peer, _client = _registered(tmp_path)
    cognition = FakeCognitionProvider(
        [
            {
                "mode": "complete",
                "completion_summary": "Pretend the untouched objective is already complete.",
            }
        ]
    )
    source = PilotCheckpointSource(supervisor=supervisor, provider=cognition)

    with pytest.raises(InvalidWorkRecordError):
        BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
            snapshot.work_id,
            peer_loop=peer,
            checkpoint_source=source,
            max_steps=4,
        )
