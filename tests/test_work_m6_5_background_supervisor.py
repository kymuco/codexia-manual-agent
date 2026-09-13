from __future__ import annotations

import json
import sqlite3
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
    ContinuationAdmission,
    ContinuationEvidenceFit,
    ContinuationFit,
    ContinuationProposal,
    DynamicAttentionContext,
    DynamicAttentionDecision,
    InvalidWorkRecordError,
    SupervisorDispatch,
    SupervisorIntegrityError,
    SupervisorStateError,
    SupervisorStatus,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)
from codexia_manual_agent.work.chat_peer import ChatGPTPeerLoop


class _Metrics:
    def to_dict(self):
        return {}


class FakeLiveChatClient:
    def __init__(self, conversation_id: str = "conversation-1") -> None:
        self.conversation_id = conversation_id
        self.messages: list[SimpleNamespace] = []
        self.send_calls: list[tuple[str, str]] = []
        self.next_response = "Implemented the exact admitted background step."
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
        assert conversation_id == self.conversation_id
        roles = set(kwargs.get("roles") or ())
        result = self.messages
        if roles:
            result = [message for message in result if message.role in roles]
        return list(result)

    def send_to_conversation(self, conversation_id: str, prompt: str, **kwargs):
        assert conversation_id == self.conversation_id
        self.send_calls.append((conversation_id, prompt))
        self.append_user(prompt)
        assistant = self.append_assistant(self.next_response)
        return SimpleNamespace(
            text=self.next_response,
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


def statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def handoff() -> tuple[WorkHandoff, WorkIntentInterpretation]:
    objective = statement(
        WorkActorKind.HUMAN,
        "operator",
        "Keep this delegated work moving until my judgment is genuinely required.",
    )
    constraint = statement(
        WorkActorKind.HUMAN,
        "operator",
        "Do not leave the current delegated milestone.",
    )
    work = WorkHandoff.create(
        objective=objective,
        human_constraints=(constraint,),
    )
    interpretation = WorkIntentInterpretation.create(
        handoff=work,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective, constraint),
        completion_expectation="Advance the exact delegated milestone.",
        continuation_scope="Stay inside the delegated milestone.",
        depth_interpretation="Use the depth implied by the human request.",
    )
    return work, interpretation


def loop(
    conversation_id: str = "conversation-1",
) -> tuple[ChatGPTPeerLoop, FakeLiveChatClient]:
    client = FakeLiveChatClient(conversation_id)
    provider = ChatGPTWebProvider(client=client)
    return ChatGPTPeerLoop(provider), client


def checkpoint(
    work: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    cursor_digest: str,
    *,
    ask_human: bool = False,
):
    proposal = ContinuationProposal.create(
        handoff=work,
        interpretation=interpretation,
        checkpoint_digest=cursor_digest,
        statement=statement(
            WorkActorKind.WORKER,
            "chatgpt",
            "Continue the next exact bounded background-work step.",
        ),
    )
    admission = ContinuationAdmission.evaluate(
        handoff=work,
        interpretation=interpretation,
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
            "This choice materially needs the human."
            if ask_human
            else "The proposal is an exact routine continuation."
        ),
        requested_human_response=(
            "Choose whether this material direction should continue."
            if ask_human
            else None
        ),
    )
    context = DynamicAttentionContext.create(
        handoff=work,
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
        handoff=work,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
        context=context,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        cognitive_disposition=AttentionDisposition.KEEP_MOVING,
        confidence_basis_points=9500,
        urgency=AttentionUrgency.NORMAL if ask_human else AttentionUrgency.NONE,
        reason=(
            "The exact admission already requires human judgment."
            if ask_human
            else "Routine delegated work can keep moving."
        ),
        requested_response=(
            "Resolve the material continuation choice." if ask_human else None
        ),
    )
    return proposal, admission, attention


def prepared_supervisor(tmp_path):
    peer, client = loop()
    cursor = peer.attach("conversation-1")
    work, interpretation = handoff()
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    snapshot = supervisor.register(
        handoff=work,
        interpretation=interpretation,
        cursor=cursor,
    )
    proposal, admission, attention = checkpoint(
        work,
        interpretation,
        cursor.cursor_digest,
    )
    snapshot = supervisor.record_checkpoint(
        snapshot.work_id,
        proposal=proposal,
        admission=admission,
        attention=attention,
    )
    return supervisor, snapshot, peer, client


def test_registration_and_recovery_preserve_exact_work_cursor(tmp_path) -> None:
    peer, _client = loop()
    cursor = peer.attach("conversation-1")
    work, interpretation = handoff()
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")

    registered = supervisor.register(
        handoff=work,
        interpretation=interpretation,
        cursor=cursor,
    )
    recovered = BackgroundWorkSupervisor(
        tmp_path / "supervisor.sqlite3"
    ).recover(registered.work_id)

    assert recovered.status is SupervisorStatus.READY
    assert recovered.handoff == work
    assert recovered.interpretation == interpretation
    assert recovered.cursor == cursor
    assert recovered.last_sequence == 0


def test_keep_moving_admit_becomes_prepared_not_authorized_execution(tmp_path) -> None:
    supervisor, snapshot, _peer, client = prepared_supervisor(tmp_path)

    assert snapshot.status is SupervisorStatus.PREPARED
    assert snapshot.pending_dispatch is not None
    assert client.send_calls == []

    lease = supervisor.claim_dispatch(snapshot.work_id)
    in_flight = supervisor.recover(snapshot.work_id)
    assert in_flight.status is SupervisorStatus.IN_FLIGHT
    assert in_flight.requires_reconciliation
    assert lease.dispatch_digest == in_flight.pending_dispatch.dispatch_digest
    assert client.send_calls == []


def test_claimed_dispatch_executes_once_and_returns_to_ready(tmp_path) -> None:
    supervisor, snapshot, peer, client = prepared_supervisor(tmp_path)
    lease = supervisor.claim_dispatch(snapshot.work_id)

    result = supervisor.execute_claimed_chat(lease, peer)

    assert len(client.send_calls) == 1
    assert result.status is SupervisorStatus.READY
    assert result.pending_dispatch is None
    assert len(result.cursor.message_fingerprints) == 4

    with pytest.raises(SupervisorStateError, match="not live"):
        supervisor.execute_claimed_chat(lease, peer)
    assert len(client.send_calls) == 1


def test_restart_after_claim_cannot_replay_ambiguous_provider_effect(tmp_path) -> None:
    supervisor, snapshot, peer, client = prepared_supervisor(tmp_path)
    lease = supervisor.claim_dispatch(snapshot.work_id)

    restarted = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    recovered = restarted.recover(snapshot.work_id)

    assert recovered.status is SupervisorStatus.IN_FLIGHT
    with pytest.raises(SupervisorStateError, match="PREPARED"):
        restarted.claim_dispatch(snapshot.work_id)
    with pytest.raises(SupervisorStateError, match="not live"):
        restarted.execute_claimed_chat(lease, peer)
    assert client.send_calls == []


def test_post_crash_reconciliation_recovers_exact_turn_without_resend(tmp_path) -> None:
    supervisor, snapshot, peer, client = prepared_supervisor(tmp_path)
    supervisor.claim_dispatch(snapshot.work_id)
    in_flight = supervisor.recover(snapshot.work_id)
    dispatch = in_flight.pending_dispatch
    assert dispatch is not None
    expected_prompt = ChatGPTPeerLoop._render_codexia_continuation(
        handoff=in_flight.handoff,
        interpretation=in_flight.interpretation,
        proposal=dispatch.proposal,
        admission=dispatch.admission,
    )

    client.send_to_conversation("conversation-1", expected_prompt)
    restarted = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")

    reconciled = restarted.reconcile_in_flight_chat(
        snapshot.work_id,
        peer_loop=peer,
    )

    assert len(client.send_calls) == 1
    assert reconciled.status is SupervisorStatus.READY
    assert reconciled.pending_dispatch is None
    assert len(reconciled.cursor.message_fingerprints) == 4


def test_no_visible_post_crash_effect_stays_in_flight_without_retry(tmp_path) -> None:
    supervisor, snapshot, peer, client = prepared_supervisor(tmp_path)
    supervisor.claim_dispatch(snapshot.work_id)
    restarted = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")

    still_ambiguous = restarted.reconcile_in_flight_chat(
        snapshot.work_id,
        peer_loop=peer,
    )

    assert still_ambiguous.status is SupervisorStatus.IN_FLIGHT
    assert client.send_calls == []


def test_attention_boundary_waits_for_human_and_exact_observation_resumes(tmp_path) -> None:
    peer, client = loop()
    cursor = peer.attach("conversation-1")
    work, interpretation = handoff()
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    snapshot = supervisor.register(
        handoff=work,
        interpretation=interpretation,
        cursor=cursor,
    )
    proposal, admission, attention = checkpoint(
        work,
        interpretation,
        cursor.cursor_digest,
        ask_human=True,
    )

    waiting = supervisor.record_checkpoint(
        snapshot.work_id,
        proposal=proposal,
        admission=admission,
        attention=attention,
    )

    assert waiting.status is SupervisorStatus.WAITING_HUMAN
    assert waiting.needs_human
    assert waiting.pending_dispatch is None

    client.append_user("Keep the API stable and use option B.")
    client.append_assistant("Understood. I will preserve the API and use option B.")
    observation = peer.observe(waiting.cursor)
    resumed = supervisor.record_external_observation(
        waiting.work_id,
        observation=observation,
    )
    assert resumed.status is SupervisorStatus.READY
    assert resumed.cursor == observation.after_cursor


def test_human_activity_invalidates_prepared_background_dispatch(tmp_path) -> None:
    supervisor, prepared, peer, client = prepared_supervisor(tmp_path)
    client.append_user("Change direction before continuing.")
    client.append_assistant("I will wait for the updated direction.")
    observation = peer.observe(prepared.cursor)

    updated = supervisor.record_external_observation(
        prepared.work_id,
        observation=observation,
    )

    assert updated.status is SupervisorStatus.READY
    assert updated.pending_dispatch is None
    with pytest.raises(SupervisorStateError, match="PREPARED"):
        supervisor.claim_dispatch(prepared.work_id)


def test_persisted_event_tamper_fails_recovery(tmp_path) -> None:
    peer, _client = loop()
    cursor = peer.attach("conversation-1")
    work, interpretation = handoff()
    database = tmp_path / "supervisor.sqlite3"
    supervisor = BackgroundWorkSupervisor(database)
    registered = supervisor.register(
        handoff=work,
        interpretation=interpretation,
        cursor=cursor,
    )

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """
            SELECT payload_json FROM work_supervisor_events
            WHERE work_id = ? AND sequence = 0
            """,
            (registered.work_id,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["execute"] = True
        connection.execute(
            """
            UPDATE work_supervisor_events
            SET payload_json = ?
            WHERE work_id = ? AND sequence = 0
            """,
            (json.dumps(payload, sort_keys=True), registered.work_id),
        )

    with pytest.raises(SupervisorIntegrityError):
        BackgroundWorkSupervisor(database).recover(registered.work_id)


def test_dispatch_strict_decoding_rejects_authority_shaped_field(tmp_path) -> None:
    _supervisor, prepared, _peer, _client = prepared_supervisor(tmp_path)
    dispatch = prepared.pending_dispatch
    assert dispatch is not None
    payload = dispatch.to_dict()
    payload["execute"] = True

    with pytest.raises(InvalidWorkRecordError, match="keys mismatch"):
        SupervisorDispatch.from_dict(payload)


def test_worker_output_cannot_declare_supervisor_completion(tmp_path) -> None:
    peer, _client = loop()
    cursor = peer.attach("conversation-1")
    work, interpretation = handoff()
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    registered = supervisor.register(
        handoff=work,
        interpretation=interpretation,
        cursor=cursor,
    )

    with pytest.raises(InvalidWorkRecordError, match="Codexia authorship"):
        supervisor.complete(
            registered.work_id,
            completion=statement(
                WorkActorKind.WORKER,
                "chatgpt",
                "I think this work is complete.",
            ),
        )


def test_multiple_delegated_works_recover_independently(tmp_path) -> None:
    database = tmp_path / "supervisor.sqlite3"
    supervisor = BackgroundWorkSupervisor(database)

    peer_a, _ = loop("conversation-a")
    peer_b, _ = loop("conversation-b")
    work_a, interpretation_a = handoff()
    work_b, interpretation_b = handoff()
    snapshot_a = supervisor.register(
        handoff=work_a,
        interpretation=interpretation_a,
        cursor=peer_a.attach("conversation-a"),
    )
    snapshot_b = supervisor.register(
        handoff=work_b,
        interpretation=interpretation_b,
        cursor=peer_b.attach("conversation-b"),
    )

    active = supervisor.list_active()

    assert {item.work_id for item in active} == {
        snapshot_a.work_id,
        snapshot_b.work_id,
    }
