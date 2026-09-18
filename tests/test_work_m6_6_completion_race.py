from __future__ import annotations

import json
from types import SimpleNamespace

from codexia_manual_agent.domain.models import ProviderResponse
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.work import (
    BackgroundWorkDriver,
    BackgroundWorkSupervisor,
    ChatGPTPeerLoop,
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
        result = self.messages
        if roles:
            result = [message for message in result if message.role in roles]
        return list(result)

    def send_to_conversation(self, conversation_id: str, prompt: str, **kwargs):
        assert conversation_id == "conversation-1"
        self.send_calls.append(prompt)
        self.append_user(prompt)
        assistant = self.append_assistant("Worker result that appears complete.")
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


class _RacingCognition:
    def __init__(self, client: _LiveClient) -> None:
        self.client = client
        self.calls = 0

    def send(self, request):
        self.calls += 1
        if self.calls == 1:
            payload = {
                "mode": "checkpoint",
                "proposal_text": "Produce the bounded delegated result.",
                "objective_fit": "aligned",
                "constraint_fit": "aligned",
                "scope_fit": "aligned",
                "depth_fit": "aligned",
                "evidence_fit": "supported",
                "material_human_choice": False,
                "admission_reason": "The initial bounded step is aligned.",
                "revision_request": None,
                "requested_human_response": None,
                "reversibility": "reversible",
                "trajectory_impact": "routine",
                "alternatives": "none",
                "attention_constraint_checks": [],
                "cognitive_disposition": "keep_moving",
                "confidence_basis_points": 9500,
                "urgency": "none",
                "attention_reason": "No human attention is required.",
                "requested_response": None,
            }
        else:
            # This arrives after the terminal worker turn was captured but while
            # Codexia is still deciding whether that turn completes the work.
            self.client.append_user(
                "Human update during completion cognition: do not close this yet."
            )
            payload = {
                "mode": "complete",
                "completion_summary": "Stale completion that must not be committed.",
            }
        return ProviderResponse(text=json.dumps(payload))


def test_intervening_human_activity_invalidates_cognition_completion(tmp_path) -> None:
    client = _LiveClient()
    peer = ChatGPTPeerLoop(ChatGPTWebProvider(client=client))
    objective = WorkStatement.create(
        author_kind=WorkActorKind.HUMAN,
        actor="operator",
        text="Produce a finished delegated result while preserving human precedence.",
    )
    handoff = WorkHandoff.create(objective=objective)
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Complete only from current exact worker evidence.",
        continuation_scope="Stay inside the delegated objective.",
        depth_interpretation="Use sufficient evidence and preserve intervening human activity.",
    )
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    snapshot = supervisor.register(
        handoff=handoff,
        interpretation=interpretation,
        cursor=peer.attach("conversation-1"),
    )
    source = PilotCheckpointSource(
        supervisor=supervisor,
        provider=_RacingCognition(client),
    )

    result = BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
        snapshot.work_id,
        peer_loop=peer,
        checkpoint_source=source,
        max_steps=3,
    )

    assert result.stop is SupervisorDriveStop.STEP_BUDGET
    assert result.provider_turns == 1
    assert result.snapshot.status.value == "ready"
    assert result.snapshot.completion is None
    assert len(result.snapshot.cursor.message_fingerprints) == 5
    assert client.messages[-1].text == (
        "Human update during completion cognition: do not close this yet."
    )
