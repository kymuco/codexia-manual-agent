from __future__ import annotations

import pytest

from codexia_manual_agent.providers.chatgpt_web import ChatGPTConversationMessage
from codexia_manual_agent.work.chat_peer import ChatPeerCursor
from codexia_manual_agent.work.contracts import (
    InvalidWorkRecordError,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)
from codexia_manual_agent.work.pilot_checkpoint import (
    MAX_PILOT_COGNITION_PROMPT_CHARS,
    PilotCheckpointSource,
)
from codexia_manual_agent.work.supervisor import BackgroundWorkSupervisor


def _statement(text: str) -> WorkStatement:
    return WorkStatement.create(
        author_kind=WorkActorKind.HUMAN,
        actor="operator",
        text=text,
    )


def _snapshot(tmp_path, *, context=(), cursor: ChatPeerCursor):
    objective = _statement("Produce a dependable delegated result.")
    handoff = WorkHandoff.create(objective=objective, context=context)
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Return a finished result with sufficient evidence.",
        continuation_scope="Stay inside the delegated objective.",
        depth_interpretation="Use enough depth for a dependable result.",
    )
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    return supervisor.register(
        handoff=handoff,
        interpretation=interpretation,
        cursor=cursor,
    )


def test_cognition_projection_omits_full_cursor_fingerprint_array(tmp_path) -> None:
    message = ChatGPTConversationMessage(
        node_id="node-1",
        message_id="message-1",
        role="user",
        text="existing context",
    )
    cursor = ChatPeerCursor.from_messages(
        "conversation-1",
        [message] * 4_096,
    )
    snapshot = _snapshot(tmp_path, cursor=cursor)

    prompt = PilotCheckpointSource._render_prompt(
        snapshot=snapshot,
        latest_turn=None,
        external_observation=None,
        human_answer=None,
    )

    assert len(prompt) < MAX_PILOT_COGNITION_PROMPT_CHARS
    assert '"message_count": 4096' in prompt
    assert cursor.cursor_digest in prompt
    assert '"message_fingerprints"' not in prompt


def test_cognition_projection_fails_closed_when_semantic_evidence_is_too_large(
    tmp_path,
) -> None:
    cursor = ChatPeerCursor.from_messages("conversation-1", ())
    context = tuple(
        _statement(f"context-{index}:" + ("x" * 16_000))
        for index in range(8)
    )
    snapshot = _snapshot(tmp_path, context=context, cursor=cursor)

    with pytest.raises(
        InvalidWorkRecordError,
        match="semantic projection exceeds its prompt budget",
    ):
        PilotCheckpointSource._render_prompt(
            snapshot=snapshot,
            latest_turn=None,
            external_observation=None,
            human_answer=None,
        )
