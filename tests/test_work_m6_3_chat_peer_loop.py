from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from codexia_manual_agent.domain.models import ProviderConversation
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
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
    ChatGPTPeerLoop,
    ChatPeerMessageOrigin,
    PeerConversationChangedError,
)


class _Metrics:
    def to_dict(self):
        return {}


class FakeLiveChatClient:
    def __init__(self) -> None:
        self.messages: list[SimpleNamespace] = []
        self.send_calls: list[tuple[str, str]] = []
        self.next_response = "Implemented the admitted step. Next, validate the exact gates."
        self.response_text_override: str | None = None
        self.inject_concurrent_user = False
        self._counter = 0
        self.append_user("Existing human context.")
        self.append_assistant("Ready to continue.")

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
        self.send_calls.append((conversation_id, prompt))
        self.append_user(prompt)
        if self.inject_concurrent_user:
            self.append_user("Human intervened concurrently.")
        assistant = self.append_assistant(self.next_response)
        response_text = self.response_text_override or self.next_response
        return SimpleNamespace(
            text=response_text,
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


def _statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def _handoff() -> tuple[WorkHandoff, WorkIntentInterpretation]:
    objective = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Continue the delegated milestone while preserving human/Codexia authorship.",
    )
    constraint = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Do not merge without an explicit human instruction.",
    )
    handoff = WorkHandoff.create(
        objective=objective,
        human_constraints=(constraint,),
    )
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective, constraint),
        completion_expectation="Advance the current milestone to review-ready state.",
        continuation_scope="Stay inside the current milestone.",
        depth_interpretation="Use the rigor implied by the existing project and request.",
    )
    return handoff, interpretation


def _admitted(
    cursor_digest: str,
) -> tuple[
    WorkHandoff,
    WorkIntentInterpretation,
    ContinuationProposal,
    ContinuationAdmission,
]:
    handoff, interpretation = _handoff()
    proposal = ContinuationProposal.create(
        handoff=handoff,
        interpretation=interpretation,
        checkpoint_digest=cursor_digest,
        statement=_statement(
            WorkActorKind.WORKER,
            "chatgpt",
            "Implement the next bounded M6.3 peer-loop slice and validate it.",
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
        reason="The proposal is an exact bounded continuation of the delegated milestone.",
    )
    return handoff, interpretation, proposal, admission


def _loop() -> tuple[ChatGPTPeerLoop, FakeLiveChatClient]:
    client = FakeLiveChatClient()
    provider = ChatGPTWebProvider(client=client)
    return ChatGPTPeerLoop(provider), client


def test_attach_establishes_cursor_without_relabelling_old_history() -> None:
    loop, _client = _loop()

    cursor = loop.attach("conversation-1")

    assert cursor.conversation_id == "conversation-1"
    assert len(cursor.message_fingerprints) == 2
    assert len(cursor.cursor_digest) == 64


def test_new_manual_chat_turn_is_observed_as_external_human_not_codexia() -> None:
    loop, client = _loop()
    cursor = loop.attach("conversation-1")
    client.append_user("What are you doing now?")
    client.append_assistant("We are validating the M6.3 peer boundary.")

    observation = loop.observe(cursor)

    assert len(observation.messages) == 2
    human, worker = observation.messages
    assert human.origin is ChatPeerMessageOrigin.EXTERNAL_USER
    assert human.statement.author_kind is WorkActorKind.HUMAN
    assert human.transport_role == "user"
    assert worker.origin is ChatPeerMessageOrigin.ASSISTANT
    assert worker.statement.author_kind is WorkActorKind.WORKER
    assert worker.transport_role == "assistant"
    assert observation.after_cursor.cursor_digest != cursor.cursor_digest


def test_admitted_peer_turn_preserves_codexia_and_worker_provenance() -> None:
    loop, client = _loop()
    cursor = loop.attach("conversation-1")
    handoff, interpretation, proposal, admission = _admitted(cursor.cursor_digest)

    turn = loop.continue_admitted(
        cursor=cursor,
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
    )

    assert len(client.send_calls) == 1
    assert turn.codexia_message.origin is ChatPeerMessageOrigin.CODEXIA_SEND
    assert turn.codexia_message.transport_role == "user"
    assert turn.codexia_message.statement.author_kind is WorkActorKind.CODEXIA
    assert "authored by Codexia, not a new human instruction" in (
        turn.codexia_message.statement.text
    )
    assert turn.worker_message.origin is ChatPeerMessageOrigin.ASSISTANT
    assert turn.worker_message.statement.author_kind is WorkActorKind.WORKER
    assert turn.worker_message.statement.text == client.next_response
    assert len(turn.after_cursor.message_fingerprints) == 4

    followup = turn.followup_proposal(
        handoff=handoff,
        interpretation=interpretation,
    )
    assert (
        followup.statement.statement_digest
        == turn.worker_message.statement.statement_digest
    )
    assert followup.checkpoint_digest == turn.after_cursor.cursor_digest


def test_human_turn_after_admission_makes_continuation_stale_without_sending() -> None:
    loop, client = _loop()
    cursor = loop.attach("conversation-1")
    handoff, interpretation, proposal, admission = _admitted(cursor.cursor_digest)
    client.append_user("Stop here; I want to change one constraint first.")

    with pytest.raises(
        PeerConversationChangedError,
        match="observe human/worker turns first",
    ):
        loop.continue_admitted(
            cursor=cursor,
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposal,
            admission=admission,
        )

    assert client.send_calls == []


def test_rewritten_current_branch_fails_closed_instead_of_relabelling_history() -> None:
    loop, client = _loop()
    cursor = loop.attach("conversation-1")
    client.messages[0].text = "Edited earlier message."

    with pytest.raises(PeerConversationChangedError, match="exact peer cursor prefix"):
        loop.observe(cursor)


def test_non_admitted_proposal_cannot_enter_peer_send_path() -> None:
    loop, client = _loop()
    cursor = loop.attach("conversation-1")
    handoff, interpretation = _handoff()
    proposal = ContinuationProposal.create(
        handoff=handoff,
        interpretation=interpretation,
        checkpoint_digest=cursor.cursor_digest,
        statement=_statement(
            WorkActorKind.WORKER,
            "chatgpt",
            "Merge without asking the human.",
        ),
    )
    rejected = ContinuationAdmission.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        objective_fit=ContinuationFit.ALIGNED,
        constraint_fit=ContinuationFit.MISALIGNED,
        scope_fit=ContinuationFit.ALIGNED,
        depth_fit=ContinuationFit.ALIGNED,
        evidence_fit=ContinuationEvidenceFit.SUPPORTED,
        material_human_choice=False,
        reason="The proposal conflicts with an explicit human merge constraint.",
    )

    with pytest.raises(ValueError, match="Only an ADMIT"):
        loop.continue_admitted(
            cursor=cursor,
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposal,
            admission=rejected,
        )

    assert client.send_calls == []


def test_admission_bound_to_different_chat_checkpoint_cannot_be_reused() -> None:
    loop, client = _loop()
    cursor = loop.attach("conversation-1")
    handoff, interpretation, proposal, admission = _admitted("0" * 64)

    with pytest.raises(PeerConversationChangedError, match="stale"):
        loop.continue_admitted(
            cursor=cursor,
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposal,
            admission=admission,
        )

    assert client.send_calls == []


def test_concurrent_user_message_during_codexia_send_fails_reconciliation() -> None:
    loop, client = _loop()
    cursor = loop.attach("conversation-1")
    handoff, interpretation, proposal, admission = _admitted(cursor.cursor_digest)
    client.inject_concurrent_user = True

    with pytest.raises(
        PeerConversationChangedError,
        match="exact user/assistant branch delta",
    ):
        loop.continue_admitted(
            cursor=cursor,
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposal,
            admission=admission,
        )


def test_provider_response_must_match_observed_worker_message() -> None:
    loop, client = _loop()
    cursor = loop.attach("conversation-1")
    handoff, interpretation, proposal, admission = _admitted(cursor.cursor_digest)
    client.response_text_override = "A different response body."

    with pytest.raises(
        PeerConversationChangedError,
        match="differs from the provider response",
    ):
        loop.continue_admitted(
            cursor=cursor,
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposal,
            admission=admission,
        )


def test_observation_record_rejects_post_capture_message_tamper() -> None:
    loop, client = _loop()
    cursor = loop.attach("conversation-1")
    client.append_user("A fresh manual turn.")

    observation = loop.observe(cursor)

    with pytest.raises(InvalidWorkRecordError, match="observation digest"):
        replace(observation, messages=())


def test_peer_turn_record_rejects_admission_binding_tamper() -> None:
    loop, _client = _loop()
    cursor = loop.attach("conversation-1")
    handoff, interpretation, proposal, admission = _admitted(cursor.cursor_digest)
    turn = loop.continue_admitted(
        cursor=cursor,
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
    )

    with pytest.raises(InvalidWorkRecordError, match="turn digest"):
        replace(turn, admission_digest="0" * 64)
