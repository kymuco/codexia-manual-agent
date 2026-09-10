from __future__ import annotations

from dataclasses import replace

import pytest

from codexia_manual_agent.work import (
    AttentionAlternativeShape,
    AttentionDisposition,
    AttentionOverride,
    AttentionReversibility,
    AttentionTrajectoryImpact,
    AttentionUrgency,
    ContinuationAdmission,
    ContinuationEvidenceFit,
    ContinuationFit,
    ContinuationProposal,
    DynamicAttentionContext,
    DynamicAttentionDecision,
    InvalidWorkRecordError,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)


def _statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def _work(
    *,
    with_attention_constraint: bool = False,
) -> tuple[WorkHandoff, WorkIntentInterpretation, WorkStatement | None]:
    objective = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Keep delegated work moving while involving me only when my judgment matters.",
    )
    attention_constraint = None
    attention_constraints: tuple[WorkStatement, ...] = ()
    if with_attention_constraint:
        attention_constraint = _statement(
            WorkActorKind.HUMAN,
            "operator",
            "Ask me before changing the core architecture direction.",
        )
        attention_constraints = (attention_constraint,)
    handoff = WorkHandoff.create(
        objective=objective,
        attention_constraints=attention_constraints,
    )
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective, *attention_constraints),
        completion_expectation="Advance until a genuine human-attention boundary.",
        continuation_scope="Stay inside the delegated objective and its constraints.",
        depth_interpretation="Infer implementation depth from the human request and evidence.",
    )
    return handoff, interpretation, attention_constraint


def _proposal(
    handoff: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    *,
    checkpoint: str = "1" * 64,
    text: str = "Repair the routine failing gate and rerun validation.",
) -> ContinuationProposal:
    return ContinuationProposal.create(
        handoff=handoff,
        interpretation=interpretation,
        checkpoint_digest=checkpoint,
        statement=_statement(WorkActorKind.WORKER, "chatgpt", text),
    )


def _admission(
    handoff: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    proposal: ContinuationProposal,
    *,
    scope_fit: ContinuationFit = ContinuationFit.ALIGNED,
    depth_fit: ContinuationFit = ContinuationFit.ALIGNED,
    evidence_fit: ContinuationEvidenceFit = ContinuationEvidenceFit.SUPPORTED,
    material_human_choice: bool = False,
) -> ContinuationAdmission:
    decision_is_revise = (
        depth_fit is not ContinuationFit.ALIGNED
        or evidence_fit
        in {
            ContinuationEvidenceFit.UNSUPPORTED,
            ContinuationEvidenceFit.UNCERTAIN,
        }
    )
    decision_is_ask = scope_fit is ContinuationFit.UNCERTAIN or material_human_choice
    return ContinuationAdmission.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        objective_fit=ContinuationFit.ALIGNED,
        constraint_fit=ContinuationFit.ALIGNED,
        scope_fit=scope_fit,
        depth_fit=depth_fit,
        evidence_fit=evidence_fit,
        material_human_choice=material_human_choice,
        reason="Exact semantic admission state for the attention test.",
        revision_request=(
            "Revise the worker step inside the existing delegation."
            if decision_is_revise
            else None
        ),
        requested_human_response=(
            "Choose whether this material direction should change."
            if decision_is_ask
            else None
        ),
    )


def _context(
    handoff: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    proposal: ContinuationProposal,
    admission: ContinuationAdmission,
    *,
    reversibility: AttentionReversibility = AttentionReversibility.REVERSIBLE,
    trajectory_impact: AttentionTrajectoryImpact = AttentionTrajectoryImpact.ROUTINE,
    alternatives: AttentionAlternativeShape = AttentionAlternativeShape.NONE,
    triggered_attention_constraints: tuple[WorkStatement, ...] = (),
) -> DynamicAttentionContext:
    evidence = _statement(
        WorkActorKind.SYSTEM,
        "ci",
        "The observed failure is bounded to the already-delegated validation step.",
    )
    return DynamicAttentionContext.create(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
        basis_statements=(proposal.statement, evidence),
        reversibility=reversibility,
        trajectory_impact=trajectory_impact,
        alternatives=alternatives,
        triggered_attention_constraints=triggered_attention_constraints,
    )


def test_routine_admitted_work_can_keep_moving_without_human() -> None:
    handoff, interpretation, _ = _work()
    proposal = _proposal(handoff, interpretation)
    admission = _admission(handoff, interpretation, proposal)
    context = _context(handoff, interpretation, proposal, admission)

    decision = DynamicAttentionDecision.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
        context=context,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        cognitive_disposition=AttentionDisposition.KEEP_MOVING,
        confidence_basis_points=9600,
        urgency=AttentionUrgency.NONE,
        reason="Routine reversible work is already covered by the delegation.",
    )

    assert decision.needs_human is False
    assert decision.disposition is AttentionDisposition.KEEP_MOVING
    assert decision.override is AttentionOverride.NONE
    assert decision.assessment.requested_response is None


def test_cognition_can_request_human_for_material_choice_without_static_threshold() -> None:
    handoff, interpretation, _ = _work()
    proposal = _proposal(
        handoff,
        interpretation,
        text="Choose one of two materially different architecture continuations.",
    )
    admission = _admission(handoff, interpretation, proposal)
    context = _context(
        handoff,
        interpretation,
        proposal,
        admission,
        reversibility=AttentionReversibility.BOUNDED,
        trajectory_impact=AttentionTrajectoryImpact.DIRECTIONAL,
        alternatives=AttentionAlternativeShape.MATERIAL,
    )

    decision = DynamicAttentionDecision.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
        context=context,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        cognitive_disposition=AttentionDisposition.ASK_HUMAN,
        confidence_basis_points=9100,
        urgency=AttentionUrgency.NORMAL,
        reason="Both paths fit the delegation but materially change the future architecture.",
        requested_response="Choose the architecture direction to preserve.",
    )

    assert decision.needs_human is True
    assert decision.override is AttentionOverride.NONE
    assert decision.disposition is AttentionDisposition.ASK_HUMAN


def test_explicit_attention_constraint_overrides_keep_moving_recommendation() -> None:
    handoff, interpretation, constraint = _work(with_attention_constraint=True)
    assert constraint is not None
    proposal = _proposal(
        handoff,
        interpretation,
        text="Replace the core architecture with the alternate design.",
    )
    admission = _admission(handoff, interpretation, proposal)
    context = _context(
        handoff,
        interpretation,
        proposal,
        admission,
        trajectory_impact=AttentionTrajectoryImpact.DIRECTIONAL,
        triggered_attention_constraints=(constraint,),
    )

    decision = DynamicAttentionDecision.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
        context=context,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        cognitive_disposition=AttentionDisposition.KEEP_MOVING,
        confidence_basis_points=9800,
        urgency=AttentionUrgency.HIGH,
        reason="An explicit human attention constraint applies at this checkpoint.",
        requested_response="Confirm or reject the proposed core architecture change.",
    )

    assert decision.needs_human is True
    assert decision.override is AttentionOverride.EXPLICIT_HUMAN_CONSTRAINT


def test_m6_2_ask_human_cannot_be_suppressed_by_attention_cognition() -> None:
    handoff, interpretation, _ = _work()
    proposal = _proposal(
        handoff,
        interpretation,
        text="Expand the work into a materially different product direction.",
    )
    admission = _admission(
        handoff,
        interpretation,
        proposal,
        scope_fit=ContinuationFit.UNCERTAIN,
        material_human_choice=True,
    )
    context = _context(
        handoff,
        interpretation,
        proposal,
        admission,
        trajectory_impact=AttentionTrajectoryImpact.DIRECTIONAL,
        alternatives=AttentionAlternativeShape.MATERIAL,
    )

    decision = DynamicAttentionDecision.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
        context=context,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        cognitive_disposition=AttentionDisposition.KEEP_MOVING,
        confidence_basis_points=9000,
        urgency=AttentionUrgency.NORMAL,
        reason="M6.2 already determined that the direction requires human judgment.",
        requested_response=admission.requested_human_response,
    )

    assert decision.needs_human is True
    assert decision.override is AttentionOverride.ADMISSION_REQUIRES_HUMAN


def test_worker_revision_need_does_not_automatically_interrupt_human() -> None:
    handoff, interpretation, _ = _work()
    proposal = _proposal(handoff, interpretation)
    admission = _admission(
        handoff,
        interpretation,
        proposal,
        evidence_fit=ContinuationEvidenceFit.UNSUPPORTED,
    )
    context = _context(handoff, interpretation, proposal, admission)

    decision = DynamicAttentionDecision.evaluate(
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
        reason="The worker can revise the unsupported step inside the same delegation.",
    )

    assert admission.revision_request is not None
    assert decision.needs_human is False
    assert decision.override is AttentionOverride.NONE


def test_foreign_attention_constraint_cannot_be_smuggled_into_context() -> None:
    handoff, interpretation, _ = _work()
    proposal = _proposal(handoff, interpretation)
    admission = _admission(handoff, interpretation, proposal)
    foreign = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Ask me before every single routine action.",
    )

    with pytest.raises(InvalidWorkRecordError, match="exact handoff"):
        _context(
            handoff,
            interpretation,
            proposal,
            admission,
            triggered_attention_constraints=(foreign,),
        )


def test_attention_basis_must_include_exact_proposal_statement() -> None:
    handoff, interpretation, _ = _work()
    proposal = _proposal(handoff, interpretation)
    admission = _admission(handoff, interpretation, proposal)
    unrelated = _statement(WorkActorKind.SYSTEM, "ci", "A different observation.")

    with pytest.raises(InvalidWorkRecordError, match="exact continuation proposal"):
        DynamicAttentionContext.create(
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposal,
            admission=admission,
            basis_statements=(unrelated,),
            reversibility=AttentionReversibility.REVERSIBLE,
            trajectory_impact=AttentionTrajectoryImpact.ROUTINE,
            alternatives=AttentionAlternativeShape.NONE,
        )


def test_dynamic_attention_decision_must_be_codexia_authored() -> None:
    handoff, interpretation, _ = _work()
    proposal = _proposal(handoff, interpretation)
    admission = _admission(handoff, interpretation, proposal)
    context = _context(handoff, interpretation, proposal, admission)

    with pytest.raises(InvalidWorkRecordError, match="Codexia authorship"):
        DynamicAttentionDecision.evaluate(
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposal,
            admission=admission,
            context=context,
            assessor_kind=WorkActorKind.WORKER,
            assessor="chatgpt",
            cognitive_disposition=AttentionDisposition.KEEP_MOVING,
            confidence_basis_points=9000,
            urgency=AttentionUrgency.NONE,
            reason="Worker must not own the attention decision.",
        )


def test_no_attention_outcome_cannot_carry_urgency_or_human_request() -> None:
    handoff, interpretation, _ = _work()
    proposal = _proposal(handoff, interpretation)
    admission = _admission(handoff, interpretation, proposal)
    context = _context(handoff, interpretation, proposal, admission)

    with pytest.raises(InvalidWorkRecordError, match="urgency=none"):
        DynamicAttentionDecision.evaluate(
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposal,
            admission=admission,
            context=context,
            assessor_kind=WorkActorKind.CODEXIA,
            assessor="codexia",
            cognitive_disposition=AttentionDisposition.KEEP_MOVING,
            confidence_basis_points=9000,
            urgency=AttentionUrgency.LOW,
            reason="Invalid attention payload.",
        )


def test_context_and_decision_round_trip_and_reject_tamper() -> None:
    handoff, interpretation, _ = _work()
    proposal = _proposal(handoff, interpretation)
    admission = _admission(handoff, interpretation, proposal)
    context = _context(
        handoff,
        interpretation,
        proposal,
        admission,
        reversibility=AttentionReversibility.COSTLY,
        trajectory_impact=AttentionTrajectoryImpact.LOCAL,
        alternatives=AttentionAlternativeShape.EQUIVALENT,
    )
    decision = DynamicAttentionDecision.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
        context=context,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        cognitive_disposition=AttentionDisposition.KEEP_MOVING,
        confidence_basis_points=8800,
        urgency=AttentionUrgency.NONE,
        reason="Cost alone does not require interruption when the bounded choice is clear.",
    )

    assert DynamicAttentionContext.from_dict(context.to_dict()) == context
    assert DynamicAttentionDecision.from_dict(decision.to_dict()) == decision

    with pytest.raises(InvalidWorkRecordError, match="context digest"):
        replace(context, alternatives=AttentionAlternativeShape.MATERIAL)
    with pytest.raises(InvalidWorkRecordError, match="decision digest"):
        replace(decision, cognitive_disposition=AttentionDisposition.ASK_HUMAN)


def test_attention_context_cannot_be_rebound_to_different_work() -> None:
    handoff, interpretation, _ = _work()
    proposal = _proposal(handoff, interpretation)
    admission = _admission(handoff, interpretation, proposal)
    context = _context(handoff, interpretation, proposal, admission)

    other_handoff, other_interpretation, _ = _work()
    other_proposal = _proposal(other_handoff, other_interpretation)
    other_admission = _admission(
        other_handoff,
        other_interpretation,
        other_proposal,
    )

    with pytest.raises(InvalidWorkRecordError, match="exact admitted work checkpoint"):
        context.assert_binds(
            other_handoff,
            other_interpretation,
            other_proposal,
            other_admission,
        )
