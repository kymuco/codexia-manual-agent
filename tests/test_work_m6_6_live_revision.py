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
        assert conversation_id == "conversation-1"
        self.send_calls.append(prompt)
        self._append("user", prompt)
        worker_text = (
            "Initial worker result is directionally useful but too shallow."
            if len(self.send_calls) == 1
            else "Revised worker result now contains the required depth and evidence."
        )
        assistant = self._append("assistant", worker_text)
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
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = list(payloads)

    def send(self, request):
        return ProviderResponse(text=json.dumps(self.payloads.pop(0)))


def _checkpoint(
    *,
    proposal_text: str | None,
    depth_fit: str = "aligned",
    revision_request: str | None = None,
) -> dict[str, object]:
    return {
        "mode": "checkpoint",
        "proposal_text": proposal_text,
        "objective_fit": "aligned",
        "constraint_fit": "aligned",
        "scope_fit": "aligned",
        "depth_fit": depth_fit,
        "evidence_fit": "supported",
        "material_human_choice": False,
        "admission_reason": (
            "The worker result needs a bounded depth revision."
            if depth_fit != "aligned"
            else "The continuation is aligned with delegated work."
        ),
        "revision_request": revision_request,
        "requested_human_response": None,
        "reversibility": "reversible",
        "trajectory_impact": "routine",
        "alternatives": "none",
        "attention_constraint_checks": [],
        "cognitive_disposition": "keep_moving",
        "confidence_basis_points": 9500,
        "urgency": "none",
        "attention_reason": "Routine worker correction does not need human judgment.",
        "requested_response": None,
    }


def test_live_cognition_revision_stays_background_and_reaches_completion(tmp_path) -> None:
    client = _LiveClient()
    peer = ChatGPTPeerLoop(ChatGPTWebProvider(client=client))
    objective = WorkStatement.create(
        author_kind=WorkActorKind.HUMAN,
        actor="operator",
        text="Produce a dependable finished result without asking me for routine corrections.",
    )
    handoff = WorkHandoff.create(objective=objective)
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Return a sufficiently deep and evidenced finished result.",
        continuation_scope="Stay within the exact delegated objective.",
        depth_interpretation="Reject shallow worker output through routine revision.",
    )
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    snapshot = supervisor.register(
        handoff=handoff,
        interpretation=interpretation,
        cursor=peer.attach("conversation-1"),
    )
    cognition = _Cognition(
        [
            _checkpoint(proposal_text="Produce the first bounded result."),
            _checkpoint(
                proposal_text=None,
                depth_fit="misaligned",
                revision_request=(
                    "Deepen the result and supply the missing evidence before treating it as complete."
                ),
            ),
            {
                "mode": "complete",
                "completion_summary": (
                    "The terminal exact worker result now satisfies the required depth and evidence."
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
    assert "[Codexia delegated-work revision]" in client.send_calls[1]
    assert "Deepen the result and supply the missing evidence" in client.send_calls[1]
    assert result.snapshot.completion is not None
