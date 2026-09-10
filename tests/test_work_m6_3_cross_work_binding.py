from __future__ import annotations

import pytest

from codexia_manual_agent.providers.chatgpt_web import ChatGPTConversationMessage
from codexia_manual_agent.work import (
    ContinuationAdmission,
    ContinuationEvidenceFit,
    ContinuationFit,
    ContinuationProposal,
    InvalidWorkRecordError,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)
from codexia_manual_agent.work.chat_peer import (
    CapturedChatPeerMessage,
    ChatPeerCursor,
    ChatPeerMessageOrigin,
    ChatPeerTurn,
)


def _statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def _work(label: str) -> tuple[WorkHandoff, WorkIntentInterpretation]:
    objective = _statement(
        WorkActorKind.HUMAN,
        "operator",
        f"Continue exact delegated work {label}.",
    )
    handoff = WorkHandoff.create(objective=objective)
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation=f"Complete bounded work {label}.",
        continuation_scope=f"Stay inside delegated work {label}.",
        depth_interpretation="Use the rigor required by the existing project.",
    )
    return handoff, interpretation


def _message(index: int, role: str, text: str) -> ChatGPTConversationMessage:
    return ChatGPTConversationMessage(
        node_id=f"node-{index}",
        message_id=f"message-{index}",
        role=role,
        text=text,
    )


def test_peer_turn_followup_cannot_be_rebound_to_different_delegated_work() -> None:
    handoff, interpretation = _work("alpha")
    before_messages = (
        _message(1, "user", "Existing context."),
        _message(2, "assistant", "Ready."),
    )
    before = ChatPeerCursor.from_messages("conversation-1", before_messages)

    proposal = ContinuationProposal.create(
        handoff=handoff,
        interpretation=interpretation,
        checkpoint_digest=before.cursor_digest,
        statement=_statement(
            WorkActorKind.WORKER,
            "chatgpt",
            "Continue the bounded alpha step.",
        ),
    )
    admission = ContinuationAdmission.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        objective_fit=ContinuationFit.ALIGNED,
        constraint_fit=ContinuationFit.ALIGNED,
        scope_fit=ContinuationFit.ALIGNED,
        depth_fit=ContinuationFit.ALIGNED,
        evidence_fit=ContinuationEvidenceFit.SUPPORTED,
        material_human_choice=False,
        reason="Exact bounded continuation remains inside delegated work alpha.",
    )

    sent = _message(3, "user", "[Codexia] Continue alpha.")
    worker = _message(4, "assistant", "Alpha step complete; validate next gate.")
    captured_codexia = CapturedChatPeerMessage.capture(
        conversation_id="conversation-1",
        message=sent,
        origin=ChatPeerMessageOrigin.CODEXIA_SEND,
        actor="codexia",
    )
    captured_worker = CapturedChatPeerMessage.capture(
        conversation_id="conversation-1",
        message=worker,
        origin=ChatPeerMessageOrigin.ASSISTANT,
        actor="chatgpt",
    )
    after = ChatPeerCursor.from_messages(
        "conversation-1",
        (*before_messages, sent, worker),
    )
    turn = ChatPeerTurn.create(
        admission=admission,
        before=before,
        codexia_message=captured_codexia,
        worker_message=captured_worker,
        after=after,
    )

    same_work = turn.followup_proposal(
        handoff=handoff,
        interpretation=interpretation,
    )
    assert same_work.statement.statement_digest == captured_worker.statement.statement_digest
    assert same_work.checkpoint_digest == after.cursor_digest

    other_handoff, other_interpretation = _work("beta")
    with pytest.raises(InvalidWorkRecordError, match="exact delegated work"):
        turn.followup_proposal(
            handoff=other_handoff,
            interpretation=other_interpretation,
        )
