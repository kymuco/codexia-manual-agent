from __future__ import annotations

from types import SimpleNamespace

import pytest

from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.work import (
    AttentionAlternativeShape,
    AttentionDisposition,
    AttentionReversibility,
    AttentionTrajectoryImpact,
    AttentionUrgency,
    BackgroundWorkSupervisor,
    ChatGPTPeerLoop,
    ChatPeerMessageOrigin,
    ContinuationAdmission,
    ContinuationDecision,
    ContinuationEvidenceFit,
    ContinuationFit,
    ContinuationProposal,
    DynamicAttentionContext,
    DynamicAttentionDecision,
    InvalidWorkRecordError,
    SupervisorDispatch,
    SupervisorStatus,
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
        self.send_calls: list[tuple[str, str]] = []
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
        assistant = self.append_assistant("Revised worker candidate with evidence.")
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


def _statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def _work():
    objective = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Keep the delegated work moving but require adequate evidence.",
    )
    handoff = WorkHandoff.create(objective=objective)
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Advance only when the candidate is adequately supported.",
        continuation_scope="Stay inside the delegated objective.",
        depth_interpretation="Use production-quality evidence depth.",
    )
    return handoff, interpretation


def _revision_checkpoint(handoff, interpretation, cursor, *, ask_human: bool = False):
    proposal = ContinuationProposal.create(
        handoff=handoff,
        interpretation=interpretation,
        checkpoint_digest=cursor.cursor_digest,
        statement=_statement(
            WorkActorKind.WORKER,
            "chatgpt",
            "Use an unsupported shortcut and call the work complete.",
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
        depth_fit=ContinuationFit.MISALIGNED,
        evidence_fit=ContinuationEvidenceFit.UNSUPPORTED,
        material_human_choice=False,
        reason="The worker candidate is in scope but too shallow and unsupported.",
        revision_request="Deepen the work and verify the result before continuing.",
    )
    assert admission.decision is ContinuationDecision.REVISE
    context = DynamicAttentionContext.create(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
        context_builder_kind=WorkActorKind.CODEXIA,
        context_builder="codexia",
        basis_statements=(proposal.statement,),
        reversibility=AttentionReversibility.REVERSIBLE,
        trajectory_impact=(
            AttentionTrajectoryImpact.DIRECTIONAL
            if ask_human
            else AttentionTrajectoryImpact.ROUTINE
        ),
        alternatives=(
            AttentionAlternativeShape.MATERIAL
            if ask_human
            else AttentionAlternativeShape.NONE
        ),
    )
    attention = DynamicAttentionDecision.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
        context=context,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        cognitive_disposition=(
            AttentionDisposition.ASK_HUMAN
            if ask_human
            else AttentionDisposition.KEEP_MOVING
        ),
        confidence_basis_points=9300,
        urgency=AttentionUrgency.NORMAL if ask_human else AttentionUrgency.NONE,
        reason=(
            "This revision now intersects a material human choice."
            if ask_human
            else "Routine worker-side revision does not need human attention."
        ),
        requested_response=(
            "Resolve the material direction before worker revision."
            if ask_human
            else None
        ),
    )
    return proposal, admission, attention


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


def test_revise_keep_moving_is_durable_prepared_worker_dispatch(tmp_path) -> None:
    supervisor, snapshot, _peer, _client = _registered(tmp_path)
    proposal, admission, attention = _revision_checkpoint(
        snapshot.handoff,
        snapshot.interpretation,
        snapshot.cursor,
    )

    prepared = supervisor.record_checkpoint(
        snapshot.work_id,
        proposal=proposal,
        admission=admission,
        attention=attention,
    )

    assert prepared.status is SupervisorStatus.PREPARED
    assert prepared.pending_dispatch is not None
    assert prepared.pending_dispatch.admission.decision is ContinuationDecision.REVISE

    recovered = BackgroundWorkSupervisor(
        tmp_path / "supervisor.sqlite3"
    ).recover(snapshot.work_id)
    assert recovered.status is SupervisorStatus.PREPARED
    assert recovered.pending_dispatch is not None
    assert recovered.pending_dispatch.dispatch_digest == prepared.pending_dispatch.dispatch_digest
    assert recovered.pending_dispatch.admission.decision is ContinuationDecision.REVISE


def test_revise_dispatch_strict_decode_rejects_authority_shaped_field(tmp_path) -> None:
    supervisor, snapshot, _peer, _client = _registered(tmp_path)
    proposal, admission, attention = _revision_checkpoint(
        snapshot.handoff,
        snapshot.interpretation,
        snapshot.cursor,
    )
    prepared = supervisor.record_checkpoint(
        snapshot.work_id,
        proposal=proposal,
        admission=admission,
        attention=attention,
    )
    assert prepared.pending_dispatch is not None
    payload = prepared.pending_dispatch.to_dict()
    payload["execute"] = True

    with pytest.raises(InvalidWorkRecordError):
        SupervisorDispatch.from_dict(payload)


def test_revise_attention_boundary_stays_waiting_human_without_dispatch(tmp_path) -> None:
    supervisor, snapshot, _peer, _client = _registered(tmp_path)
    proposal, admission, attention = _revision_checkpoint(
        snapshot.handoff,
        snapshot.interpretation,
        snapshot.cursor,
        ask_human=True,
    )

    waiting = supervisor.record_checkpoint(
        snapshot.work_id,
        proposal=proposal,
        admission=admission,
        attention=attention,
    )

    assert waiting.status is SupervisorStatus.WAITING_HUMAN
    assert waiting.pending_dispatch is None
    assert waiting.needs_human


def test_peer_revision_preserves_codexia_and_worker_provenance(tmp_path) -> None:
    _supervisor, snapshot, peer, client = _registered(tmp_path)
    proposal, admission, _attention = _revision_checkpoint(
        snapshot.handoff,
        snapshot.interpretation,
        snapshot.cursor,
    )

    turn = peer.revise_requested(
        cursor=snapshot.cursor,
        handoff=snapshot.handoff,
        interpretation=snapshot.interpretation,
        proposal=proposal,
        admission=admission,
    )

    assert len(client.send_calls) == 1
    assert "[Codexia delegated-work revision]" in client.send_calls[0][1]
    assert turn.codexia_message.origin is ChatPeerMessageOrigin.CODEXIA_SEND
    assert turn.codexia_message.statement.author_kind is WorkActorKind.CODEXIA
    assert turn.codexia_message.transport_role == "user"
    assert turn.worker_message.origin is ChatPeerMessageOrigin.ASSISTANT
    assert turn.worker_message.statement.author_kind is WorkActorKind.WORKER
    assert turn.worker_message.transport_role == "assistant"
