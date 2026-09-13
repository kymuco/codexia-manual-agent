from __future__ import annotations

from types import SimpleNamespace

from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.work import (
    AttentionAlternativeShape,
    AttentionDisposition,
    AttentionReversibility,
    AttentionTrajectoryImpact,
    AttentionUrgency,
    BackgroundWorkDriver,
    BackgroundWorkSupervisor,
    ChatGPTPeerLoop,
    ContinuationAdmission,
    ContinuationEvidenceFit,
    ContinuationFit,
    ContinuationProposal,
    DynamicAttentionContext,
    DynamicAttentionDecision,
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
        assistant = self.append_assistant(
            f"Worker result {len(self.send_calls)} for the delegated work."
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


def _statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def _work() -> tuple[WorkHandoff, WorkIntentInterpretation]:
    objective = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Keep this delegated work moving until my judgment is genuinely required.",
    )
    handoff = WorkHandoff.create(objective=objective)
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Advance the delegated work through routine worker turns.",
        continuation_scope="Stay inside the delegated work.",
        depth_interpretation="Use the depth implied by the human request.",
    )
    return handoff, interpretation


def _loop() -> tuple[ChatGPTPeerLoop, FakeLiveChatClient]:
    client = FakeLiveChatClient()
    return ChatGPTPeerLoop(ChatGPTWebProvider(client=client)), client


def _checkpoint(snapshot, *, ask_human: bool = False):
    proposal = ContinuationProposal.create(
        handoff=snapshot.handoff,
        interpretation=snapshot.interpretation,
        checkpoint_digest=snapshot.cursor.cursor_digest,
        statement=_statement(
            WorkActorKind.WORKER,
            "chatgpt",
            "Continue the next bounded delegated-work step.",
        ),
    )
    admission = ContinuationAdmission.evaluate(
        handoff=snapshot.handoff,
        interpretation=snapshot.interpretation,
        proposal=proposal,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        objective_fit=ContinuationFit.ALIGNED,
        constraint_fit=ContinuationFit.ALIGNED,
        scope_fit=ContinuationFit.ALIGNED,
        depth_fit=ContinuationFit.ALIGNED,
        evidence_fit=ContinuationEvidenceFit.SUPPORTED,
        material_human_choice=ask_human,
        reason=(
            "The continuation needs a material human choice."
            if ask_human
            else "This is a routine continuation inside the delegated scope."
        ),
        requested_human_response=(
            "Choose whether to continue this material direction."
            if ask_human
            else None
        ),
    )
    context = DynamicAttentionContext.create(
        handoff=snapshot.handoff,
        interpretation=snapshot.interpretation,
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
        handoff=snapshot.handoff,
        interpretation=snapshot.interpretation,
        proposal=proposal,
        admission=admission,
        context=context,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        cognitive_disposition=AttentionDisposition.KEEP_MOVING,
        confidence_basis_points=9500,
        urgency=AttentionUrgency.NORMAL if ask_human else AttentionUrgency.NONE,
        reason=(
            "The admission already requires human judgment."
            if ask_human
            else "No human attention is needed for this routine continuation."
        ),
        requested_response=(
            "Resolve the material continuation choice." if ask_human else None
        ),
    )
    return proposal, admission, attention


def _registered(tmp_path):
    peer, client = _loop()
    handoff, interpretation = _work()
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    snapshot = supervisor.register(
        handoff=handoff,
        interpretation=interpretation,
        cursor=peer.attach("conversation-1"),
    )
    return supervisor, snapshot, peer, client


def test_driver_runs_multiple_worker_turns_without_human_continue(tmp_path) -> None:
    supervisor, snapshot, peer, client = _registered(tmp_path)
    driver = BackgroundWorkDriver(supervisor)

    def source(current, _peer):
        if len(client.send_calls) >= 2:
            return None
        return _checkpoint(current)

    result = driver.drive_chat_until_blocked(
        snapshot.work_id,
        peer_loop=peer,
        checkpoint_source=source,
        max_steps=12,
    )

    assert result.stop is SupervisorDriveStop.NO_CHECKPOINT
    assert result.provider_turns == 2
    assert len(client.send_calls) == 2
    assert len(result.snapshot.cursor.message_fingerprints) == 6


def test_driver_stops_at_dynamic_human_attention_boundary(tmp_path) -> None:
    supervisor, snapshot, peer, client = _registered(tmp_path)
    driver = BackgroundWorkDriver(supervisor)

    result = driver.drive_chat_until_blocked(
        snapshot.work_id,
        peer_loop=peer,
        checkpoint_source=lambda current, _peer: _checkpoint(
            current,
            ask_human=True,
        ),
        max_steps=8,
    )

    assert result.stop is SupervisorDriveStop.WAITING_HUMAN
    assert result.snapshot.needs_human
    assert result.provider_turns == 0
    assert client.send_calls == []


def test_driver_observes_human_before_claim_and_invalidates_prepared(tmp_path) -> None:
    supervisor, snapshot, peer, client = _registered(tmp_path)
    prepared = supervisor.record_checkpoint(
        snapshot.work_id,
        proposal=_checkpoint(snapshot)[0],
        admission=_checkpoint(snapshot)[1],
        attention=_checkpoint(snapshot)[2],
    )
    client.append_user("Change direction before the background continuation.")
    client.append_assistant("Understood; I will wait for the revised direction.")
    driver = BackgroundWorkDriver(supervisor)

    result = driver.drive_chat_until_blocked(
        prepared.work_id,
        peer_loop=peer,
        checkpoint_source=lambda _current, _peer: None,
        max_steps=4,
    )

    assert result.stop is SupervisorDriveStop.NO_CHECKPOINT
    assert result.provider_turns == 0
    assert client.send_calls == []
    assert result.snapshot.pending_dispatch is None


def test_driver_reconciles_in_flight_turn_without_second_send(tmp_path) -> None:
    supervisor, snapshot, peer, client = _registered(tmp_path)
    proposal, admission, attention = _checkpoint(snapshot)
    prepared = supervisor.record_checkpoint(
        snapshot.work_id,
        proposal=proposal,
        admission=admission,
        attention=attention,
    )
    supervisor.claim_dispatch(prepared.work_id)
    in_flight = supervisor.recover(prepared.work_id)
    dispatch = in_flight.pending_dispatch
    assert dispatch is not None
    prompt = ChatGPTPeerLoop._render_codexia_continuation(
        handoff=in_flight.handoff,
        interpretation=in_flight.interpretation,
        proposal=dispatch.proposal,
        admission=dispatch.admission,
    )
    client.send_to_conversation("conversation-1", prompt)

    restarted = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    result = BackgroundWorkDriver(restarted).drive_chat_until_blocked(
        prepared.work_id,
        peer_loop=peer,
        checkpoint_source=lambda _current, _peer: None,
        max_steps=4,
    )

    assert result.stop is SupervisorDriveStop.NO_CHECKPOINT
    assert result.provider_turns == 0
    assert len(client.send_calls) == 1


def test_driver_step_budget_bounds_replanning_without_provider_send(tmp_path) -> None:
    supervisor, snapshot, peer, client = _registered(tmp_path)
    driver = BackgroundWorkDriver(supervisor)

    def revise_source(current, _peer):
        proposal = ContinuationProposal.create(
            handoff=current.handoff,
            interpretation=current.interpretation,
            checkpoint_digest=current.cursor.cursor_digest,
            statement=_statement(
                WorkActorKind.WORKER,
                "chatgpt",
                "A candidate that needs deeper work before it can be admitted.",
            ),
        )
        admission = ContinuationAdmission.evaluate(
            handoff=current.handoff,
            interpretation=current.interpretation,
            proposal=proposal,
            assessor_kind=WorkActorKind.CODEXIA,
            assessor="codexia",
            objective_fit=ContinuationFit.ALIGNED,
            constraint_fit=ContinuationFit.ALIGNED,
            scope_fit=ContinuationFit.ALIGNED,
            depth_fit=ContinuationFit.MISALIGNED,
            evidence_fit=ContinuationEvidenceFit.SUPPORTED,
            material_human_choice=False,
            reason="The worker needs to deepen the candidate before continuation.",
            revision_request="Deepen the candidate and return a better-supported next step.",
        )
        context = DynamicAttentionContext.create(
            handoff=current.handoff,
            interpretation=current.interpretation,
            proposal=proposal,
            admission=admission,
            context_builder_kind=WorkActorKind.CODEXIA,
            context_builder="codexia",
            basis_statements=(proposal.statement,),
            reversibility=AttentionReversibility.REVERSIBLE,
            trajectory_impact=AttentionTrajectoryImpact.ROUTINE,
            alternatives=AttentionAlternativeShape.NONE,
        )
        attention = DynamicAttentionDecision.evaluate(
            handoff=current.handoff,
            interpretation=current.interpretation,
            proposal=proposal,
            admission=admission,
            context=context,
            assessor_kind=WorkActorKind.CODEXIA,
            assessor="codexia",
            cognitive_disposition=AttentionDisposition.KEEP_MOVING,
            confidence_basis_points=9000,
            urgency=AttentionUrgency.NONE,
            reason="Routine revision does not need the human.",
        )
        return proposal, admission, attention

    result = driver.drive_chat_until_blocked(
        snapshot.work_id,
        peer_loop=peer,
        checkpoint_source=revise_source,
        max_steps=3,
    )

    assert result.stop is SupervisorDriveStop.STEP_BUDGET
    assert result.provider_turns == 0
    assert client.send_calls == []
