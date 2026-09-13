from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codexia_manual_agent.domain.models import ProviderResponse
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.work import (
    BackgroundWorkDriver,
    BackgroundWorkSupervisor,
    ChatGPTPeerLoop,
    InvalidWorkRecordError,
    PilotCheckpointSource,
    SupervisorDriveStop,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)


class _Metrics:
    def to_dict(self):
        return {}


class _LiveClient:
    def __init__(self) -> None:
        self.messages: list[SimpleNamespace] = []
        self.send_calls: list[str] = []
        self._counter = 0
        self._append("user", "Existing human context.")
        self._append("assistant", "Ready.")

    def _append(self, role: str, text: str) -> SimpleNamespace:
        self._counter += 1
        message = SimpleNamespace(
            node_id=f"node-{self._counter}",
            message_id=f"message-{self._counter}",
            role=role,
            text=text,
            create_time=float(self._counter),
            recipient="all" if role == "assistant" else None,
            model="gpt-test" if role == "assistant" else None,
            finish_reason="stop" if role == "assistant" else None,
        )
        self.messages.append(message)
        return message

    def get_messages(self, conversation_id: str, **kwargs):
        assert conversation_id == "conversation-1"
        roles = set(kwargs.get("roles") or ())
        if not roles:
            return list(self.messages)
        return [message for message in self.messages if message.role in roles]

    def send_to_conversation(self, conversation_id: str, prompt: str, **kwargs):
        self.send_calls.append(prompt)
        self._append("user", prompt)
        assistant = self._append("assistant", "Unexpected worker send.")
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


class _Cognition:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def send(self, request):
        return ProviderResponse(text=json.dumps(self.payload))


def _registered(tmp_path):
    client = _LiveClient()
    peer = ChatGPTPeerLoop(ChatGPTWebProvider(client=client))
    objective = WorkStatement.create(
        author_kind=WorkActorKind.HUMAN,
        actor="operator",
        text="Produce the delegated result.",
    )
    constraint = WorkStatement.create(
        author_kind=WorkActorKind.HUMAN,
        actor="operator",
        text="Ask me before choosing a materially different direction.",
    )
    handoff = WorkHandoff.create(
        objective=objective,
        attention_constraints=(constraint,),
    )
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective, constraint),
        completion_expectation="Return a finished result.",
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


def _payload(constraint: WorkStatement, *, urgency: str) -> dict[str, object]:
    return {
        "mode": "checkpoint",
        "proposal_text": "Consider the materially different direction.",
        "objective_fit": "aligned",
        "constraint_fit": "aligned",
        "scope_fit": "aligned",
        "depth_fit": "aligned",
        "evidence_fit": "supported",
        "material_human_choice": False,
        "admission_reason": "The step is otherwise aligned with delegated work.",
        "revision_request": None,
        "requested_human_response": None,
        "reversibility": "reversible",
        "trajectory_impact": "material",
        "alternatives": "material",
        "attention_constraint_checks": [
            {
                "statement_digest": constraint.statement_digest,
                "status": "triggered",
                "reason": "The proposed step chooses a materially different direction.",
            }
        ],
        "cognitive_disposition": "keep_moving",
        "confidence_basis_points": 9500,
        "urgency": urgency,
        "attention_reason": "The explicit human attention constraint is triggered.",
        "requested_response": "Choose whether to take the materially different direction.",
    }


def test_explicit_attention_constraint_overrides_keep_moving(tmp_path) -> None:
    supervisor, snapshot, peer, client, constraint = _registered(tmp_path)
    source = PilotCheckpointSource(
        supervisor=supervisor,
        provider=_Cognition(_payload(constraint, urgency="normal")),
    )

    result = BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
        snapshot.work_id,
        peer_loop=peer,
        checkpoint_source=source,
        max_steps=4,
    )

    assert result.stop is SupervisorDriveStop.WAITING_HUMAN
    assert result.provider_turns == 0
    assert client.send_calls == []
    assert result.snapshot.last_attention is not None
    assert result.snapshot.last_attention.needs_human is True
    assert result.snapshot.last_attention.cognitive_disposition.value == "keep_moving"
    assert result.snapshot.last_attention.override.value == "explicit_human_constraint"


def test_hard_attention_override_with_none_urgency_fails_closed(tmp_path) -> None:
    supervisor, snapshot, peer, _client, constraint = _registered(tmp_path)
    source = PilotCheckpointSource(
        supervisor=supervisor,
        provider=_Cognition(_payload(constraint, urgency="none")),
    )

    with pytest.raises(
        InvalidWorkRecordError,
        match="Human-attention assessment must use non-none urgency",
    ):
        BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
            snapshot.work_id,
            peer_loop=peer,
            checkpoint_source=source,
            max_steps=4,
        )
