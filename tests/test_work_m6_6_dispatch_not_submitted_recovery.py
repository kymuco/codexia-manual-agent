from __future__ import annotations

from types import SimpleNamespace

import pytest

from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import ProviderConversation, ProviderRequest
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
    SupervisorDispatchNotSubmittedError,
)
from codexia_manual_agent.work.pilot_provider import (
    CWA_WRITE_NOT_SUBMITTED,
    M66ChatGPTWebProvider,
    ProviderWriteNotSubmittedError,
)
from codexia_manual_agent.work.supervisor import (
    SupervisorStateError,
    SupervisorStatus,
)


BrowserOwnedWriteRuntimeError = type(
    "BrowserOwnedWriteRuntimeError",
    (RuntimeError,),
    {"__module__": "chatgpt_web_adapter.browser_owned_write_runtime"},
)


def _message(index: int, role: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        node_id=f"node-{index}",
        message_id=f"message-{index}",
        role=role,
        text=text,
        create_time=float(index),
        recipient="all" if role == "assistant" else None,
        model="gpt-test" if role == "assistant" else None,
        finish_reason="stop" if role == "assistant" else None,
    )


class NoSubmitRuntime:
    def __init__(self, *, failure_kind: str = CWA_WRITE_NOT_SUBMITTED) -> None:
        self.failure_kind = failure_kind
        self.send_attempts = 0
        self.messages = [
            _message(1, "user", "Existing exact human context."),
            _message(2, "assistant", "Ready for delegated continuation."),
        ]

    def get_messages(self, conversation_id: str, **kwargs):
        assert conversation_id == "conversation-1"
        return list(self.messages)

    def send_text_observed(self, text: str, **kwargs):
        self.send_attempts += 1
        error = BrowserOwnedWriteRuntimeError("bounded browser-owned write failed")
        error.failure_kind = self.failure_kind
        error.request_stage = "prewrite_canonical_baseline"
        error.write_may_have_been_submitted = False
        error.reconciliation_required = False
        error.automatic_retry_allowed = False
        error.manual_retry_safe_after_repair = True
        raise error


class AmbiguousRuntime(NoSubmitRuntime):
    def send_text_observed(self, text: str, **kwargs):
        self.send_attempts += 1
        error = BrowserOwnedWriteRuntimeError("delegated write outcome is ambiguous")
        error.failure_kind = "BROWSER_OWNED_WRITE_DELEGATED_AMBIGUOUS"
        error.request_stage = "post_delegation"
        error.write_may_have_been_submitted = True
        error.reconciliation_required = True
        error.automatic_retry_allowed = False
        error.manual_retry_safe_after_repair = False
        raise error


def _statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def _work() -> tuple[WorkHandoff, WorkIntentInterpretation]:
    objective = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Continue the exact delegated milestone without asking for routine continuation.",
    )
    constraint = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Do not leave the current delegated milestone.",
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
        completion_expectation="Advance the exact delegated milestone.",
        continuation_scope="Stay inside the exact delegated milestone.",
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
            "Continue the next exact bounded background-work step.",
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
        reason="The proposal is a routine continuation inside exact delegated scope.",
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
        confidence_basis_points=9700,
        urgency=AttentionUrgency.NONE,
        reason="No human attention is needed for this exact routine continuation.",
    )
    return proposal, admission, attention


def _prepared(tmp_path, runtime):
    provider = M66ChatGPTWebProvider(runtime=runtime)
    peer_loop = ChatGPTPeerLoop(provider)
    handoff, interpretation = _work()
    database = tmp_path / "supervisor.sqlite3"
    supervisor = M66RecoverableBackgroundWorkSupervisor(database)
    registered = supervisor.register(
        handoff=handoff,
        interpretation=interpretation,
        cursor=peer_loop.attach("conversation-1"),
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
    assert prepared.status is SupervisorStatus.PREPARED
    assert prepared.pending_dispatch is not None
    return database, supervisor, prepared, peer_loop


def test_provider_preserves_exact_cwa_not_submitted_disposition() -> None:
    runtime = NoSubmitRuntime()
    provider = M66ChatGPTWebProvider(runtime=runtime)

    with pytest.raises(ProviderWriteNotSubmittedError) as caught:
        provider.send(
            ProviderRequest(
                prompt="continue exact admitted work",
                conversation=ProviderConversation(conversation_id="conversation-1"),
            )
        )

    error = caught.value
    assert runtime.send_attempts == 1
    assert error.failure_kind == CWA_WRITE_NOT_SUBMITTED
    assert error.request_stage == "prewrite_canonical_baseline"
    assert error.write_may_have_been_submitted is False
    assert error.reconciliation_required is False
    assert error.automatic_retry_allowed is False
    assert error.manual_retry_safe_after_repair is True


def test_claimed_not_submitted_dispatch_rearms_durably_without_retry(tmp_path) -> None:
    runtime = NoSubmitRuntime()
    database, supervisor, prepared, peer_loop = _prepared(tmp_path, runtime)
    original_dispatch = prepared.pending_dispatch
    assert original_dispatch is not None
    original_cursor = prepared.cursor
    original_proposal = prepared.last_proposal
    original_admission = prepared.last_admission
    original_attention = prepared.last_attention

    lease = supervisor.claim_dispatch(prepared.work_id)
    with pytest.raises(SupervisorDispatchNotSubmittedError, match="re-armed"):
        supervisor.execute_claimed_chat(lease, peer_loop)

    assert runtime.send_attempts == 1
    rearmed = supervisor.recover(prepared.work_id)
    assert rearmed.status is SupervisorStatus.PREPARED
    assert rearmed.in_flight_claim_id is None
    assert rearmed.pending_dispatch is not None
    assert rearmed.pending_dispatch.dispatch_digest == original_dispatch.dispatch_digest
    assert rearmed.pending_dispatch == original_dispatch
    assert rearmed.cursor == original_cursor
    assert rearmed.last_proposal == original_proposal
    assert rearmed.last_admission == original_admission
    assert rearmed.last_attention == original_attention

    restarted = M66RecoverableBackgroundWorkSupervisor(database)
    recovered = restarted.recover(prepared.work_id)
    assert recovered.status is SupervisorStatus.PREPARED
    assert recovered.pending_dispatch == original_dispatch
    assert recovered.cursor == original_cursor
    assert runtime.send_attempts == 1


def test_forged_no_submit_failure_kind_cannot_rearm(tmp_path) -> None:
    runtime = NoSubmitRuntime()
    _database, supervisor, prepared, _peer_loop = _prepared(tmp_path, runtime)
    lease = supervisor.claim_dispatch(prepared.work_id)
    forged = ProviderWriteNotSubmittedError(
        "forged boolean shape",
        failure_kind="SOME_OTHER_FAILURE",
        request_stage="prewrite_canonical_baseline",
    )

    with pytest.raises(SupervisorStateError, match="does not prove"):
        supervisor.record_dispatch_not_submitted(lease, error=forged)

    still_in_flight = supervisor.recover(prepared.work_id)
    assert still_in_flight.status is SupervisorStatus.IN_FLIGHT
    assert still_in_flight.pending_dispatch is not None
    assert still_in_flight.pending_dispatch.dispatch_digest == lease.dispatch_digest


def test_ambiguous_provider_failure_stays_in_flight(tmp_path) -> None:
    runtime = AmbiguousRuntime()
    database, supervisor, prepared, peer_loop = _prepared(tmp_path, runtime)
    original_dispatch = prepared.pending_dispatch
    assert original_dispatch is not None
    lease = supervisor.claim_dispatch(prepared.work_id)

    with pytest.raises(ProviderError):
        supervisor.execute_claimed_chat(lease, peer_loop)

    assert runtime.send_attempts == 1
    restarted = M66RecoverableBackgroundWorkSupervisor(database)
    recovered = restarted.recover(prepared.work_id)
    assert recovered.status is SupervisorStatus.IN_FLIGHT
    assert recovered.pending_dispatch == original_dispatch
    assert recovered.in_flight_claim_id == lease.claim_id

    reconciled = restarted.reconcile_in_flight_chat(
        prepared.work_id,
        peer_loop=peer_loop,
    )
    assert reconciled.status is SupervisorStatus.IN_FLIGHT
    assert runtime.send_attempts == 1
