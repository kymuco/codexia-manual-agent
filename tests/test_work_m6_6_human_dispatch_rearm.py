from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.work import pilot_cli
from codexia_manual_agent.work.admission import (
    ContinuationAdmission,
    ContinuationEvidenceFit,
    ContinuationFit,
    ContinuationProposal,
)
from codexia_manual_agent.work.attention import (
    AttentionAlternativeShape,
    AttentionDisposition,
    AttentionReversibility,
    AttentionTrajectoryImpact,
    AttentionUrgency,
    DynamicAttentionContext,
    DynamicAttentionDecision,
)
from codexia_manual_agent.work.chat_peer import ChatGPTPeerLoop
from codexia_manual_agent.work.contracts import (
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)
from codexia_manual_agent.work.pilot_dispatch_recovery import (
    M66RecoverableBackgroundWorkSupervisor,
)
from codexia_manual_agent.work.pilot_runtime import (
    pilot_snapshot_summary,
    rearm_daily_use_pilot_dispatch,
)
from codexia_manual_agent.work.supervisor import SupervisorStateError, SupervisorStatus


class _ReadOnlyClient:
    def __init__(self) -> None:
        self.messages = [
            SimpleNamespace(
                node_id="node-1",
                message_id="message-1",
                role="user",
                text="Existing human context.",
                create_time=1.0,
                recipient=None,
                model=None,
                finish_reason=None,
            ),
            SimpleNamespace(
                node_id="node-2",
                message_id="message-2",
                role="assistant",
                text="Ready to continue.",
                create_time=2.0,
                recipient="all",
                model="gpt-test",
                finish_reason="stop",
            ),
        ]

    def get_messages(self, conversation_id: str, **kwargs):
        assert conversation_id == "conversation-1"
        return list(self.messages)


def _statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def _handoff() -> tuple[WorkHandoff, WorkIntentInterpretation]:
    objective = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Continue the exact delegated milestone.",
    )
    constraint = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Do not leave the delegated milestone.",
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
        completion_expectation="Finish the delegated milestone.",
        continuation_scope="Stay inside the delegated milestone.",
        depth_interpretation="Use sufficient depth for a dependable result.",
    )
    return handoff, interpretation


def _checkpoint(
    handoff: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    cursor_digest: str,
):
    proposal = ContinuationProposal.create(
        handoff=handoff,
        interpretation=interpretation,
        checkpoint_digest=cursor_digest,
        statement=_statement(
            WorkActorKind.WORKER,
            "chatgpt",
            "Continue the exact bounded next step.",
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
        reason="This is an exact routine continuation.",
    )
    context = DynamicAttentionContext.create(
        handoff=handoff,
        interpretation=interpretation,
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
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
        context=context,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        cognitive_disposition=AttentionDisposition.KEEP_MOVING,
        confidence_basis_points=9900,
        urgency=AttentionUrgency.NONE,
        reason="No human attention is required for the admitted routine step.",
    )
    return proposal, admission, attention


def _claimed(tmp_path):
    database = tmp_path / "supervisor.sqlite3"
    provider = ChatGPTWebProvider(client=_ReadOnlyClient())
    peer = ChatGPTPeerLoop(provider)
    handoff, interpretation = _handoff()
    supervisor = M66RecoverableBackgroundWorkSupervisor(database)
    registered = supervisor.register(
        handoff=handoff,
        interpretation=interpretation,
        cursor=peer.attach("conversation-1"),
    )
    proposal, admission, attention = _checkpoint(
        handoff,
        interpretation,
        registered.cursor.cursor_digest,
    )
    prepared = supervisor.record_checkpoint(
        registered.work_id,
        proposal=proposal,
        admission=admission,
        attention=attention,
    )
    dispatch = prepared.pending_dispatch
    assert dispatch is not None
    lease = supervisor.claim_dispatch(prepared.work_id)
    in_flight = supervisor.recover(prepared.work_id)
    assert in_flight.status is SupervisorStatus.IN_FLIGHT
    return database, supervisor, in_flight, lease


def _authorization(text: str = "Re-arm this exact historical dispatch.") -> WorkStatement:
    return _statement(WorkActorKind.HUMAN, "operator", text)


def test_human_rearm_preserves_exact_dispatch_and_survives_restart(tmp_path) -> None:
    database, supervisor, in_flight, lease = _claimed(tmp_path)
    dispatch = in_flight.pending_dispatch
    assert dispatch is not None
    original_cursor = in_flight.cursor
    original_proposal = in_flight.last_proposal
    original_admission = in_flight.last_admission
    original_attention = in_flight.last_attention

    rearmed = supervisor.record_human_dispatch_rearm(
        in_flight.work_id,
        expected_dispatch_digest=dispatch.dispatch_digest,
        expected_claim_id=lease.claim_id,
        authorization=_authorization(),
    )

    assert rearmed.status is SupervisorStatus.PREPARED
    assert rearmed.in_flight_claim_id is None
    assert rearmed.pending_dispatch == dispatch
    assert rearmed.cursor == original_cursor
    assert rearmed.last_proposal == original_proposal
    assert rearmed.last_admission == original_admission
    assert rearmed.last_attention == original_attention

    restarted = M66RecoverableBackgroundWorkSupervisor(database)
    recovered = restarted.recover(in_flight.work_id)
    assert recovered.status is SupervisorStatus.PREPARED
    assert recovered.pending_dispatch == dispatch
    assert recovered.cursor == original_cursor

    next_lease = restarted.claim_dispatch(in_flight.work_id)
    assert next_lease.dispatch_digest == dispatch.dispatch_digest
    assert next_lease.claim_id != lease.claim_id


def test_human_rearm_fails_closed_on_wrong_dispatch_digest(tmp_path) -> None:
    _database, supervisor, in_flight, lease = _claimed(tmp_path)

    with pytest.raises(SupervisorStateError, match="digest"):
        supervisor.record_human_dispatch_rearm(
            in_flight.work_id,
            expected_dispatch_digest="0" * 64,
            expected_claim_id=lease.claim_id,
            authorization=_authorization(),
        )

    assert supervisor.recover(in_flight.work_id).status is SupervisorStatus.IN_FLIGHT


def test_human_rearm_fails_closed_on_stale_claim_id(tmp_path) -> None:
    _database, supervisor, in_flight, _lease = _claimed(tmp_path)
    dispatch = in_flight.pending_dispatch
    assert dispatch is not None

    with pytest.raises(SupervisorStateError, match="claim id"):
        supervisor.record_human_dispatch_rearm(
            in_flight.work_id,
            expected_dispatch_digest=dispatch.dispatch_digest,
            expected_claim_id=str(uuid4()),
            authorization=_authorization(),
        )

    assert supervisor.recover(in_flight.work_id).status is SupervisorStatus.IN_FLIGHT


def test_human_rearm_rejects_non_human_authorization(tmp_path) -> None:
    _database, supervisor, in_flight, lease = _claimed(tmp_path)
    dispatch = in_flight.pending_dispatch
    assert dispatch is not None
    codexia_statement = _statement(
        WorkActorKind.CODEXIA,
        "codexia",
        "Re-arm this exact historical dispatch.",
    )

    with pytest.raises(SupervisorStateError, match="HUMAN"):
        supervisor.record_human_dispatch_rearm(
            in_flight.work_id,
            expected_dispatch_digest=dispatch.dispatch_digest,
            expected_claim_id=lease.claim_id,
            authorization=codexia_statement,
        )

    assert supervisor.recover(in_flight.work_id).status is SupervisorStatus.IN_FLIGHT


def test_runtime_rearm_requires_nonempty_human_reason(tmp_path) -> None:
    database, _supervisor, in_flight, lease = _claimed(tmp_path)
    dispatch = in_flight.pending_dispatch
    assert dispatch is not None

    with pytest.raises(ValueError):
        rearm_daily_use_pilot_dispatch(
            database_path=database,
            work_id=in_flight.work_id,
            expected_dispatch_digest=dispatch.dispatch_digest,
            expected_claim_id=lease.claim_id,
            reason="   ",
        )

    recovered = M66RecoverableBackgroundWorkSupervisor(database).recover(in_flight.work_id)
    assert recovered.status is SupervisorStatus.IN_FLIGHT


def test_cli_rearm_performs_zero_provider_writes(tmp_path, monkeypatch, capsys) -> None:
    database, _supervisor, in_flight, lease = _claimed(tmp_path)
    dispatch = in_flight.pending_dispatch
    assert dispatch is not None

    def forbidden_provider(_args):
        raise AssertionError("rearm-dispatch must not construct a provider")

    monkeypatch.setattr(pilot_cli, "_provider", forbidden_provider)
    code = pilot_cli.main(
        [
            "rearm-dispatch",
            in_flight.work_id,
            "--database",
            str(database),
            "--dispatch-digest",
            dispatch.dispatch_digest,
            "--claim-id",
            lease.claim_id,
            "--reason",
            "Authorize re-arm of this exact historical dispatch after bounded repair.",
            "--human-actor",
            "operator",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "rearm-dispatch"
    assert payload["snapshot"]["status"] == "prepared"
    assert payload["snapshot"]["in_flight_claim_id"] is None
    assert (
        payload["snapshot"]["pending_dispatch"]["dispatch_digest"]
        == dispatch.dispatch_digest
    )


def test_status_summary_exposes_exact_in_flight_claim_id(tmp_path) -> None:
    _database, _supervisor, in_flight, lease = _claimed(tmp_path)

    summary = pilot_snapshot_summary(in_flight)

    assert summary["status"] == "in_flight"
    assert summary["in_flight_claim_id"] == lease.claim_id
