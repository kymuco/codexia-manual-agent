from __future__ import annotations

from dataclasses import replace

import pytest

from codexia_manual_agent.work import (
    AttentionAlternativeShape,
    AttentionConstraintCheck,
    AttentionConstraintStatus,
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


def statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def work(*, constrained: bool = False):
    objective = statement(
        WorkActorKind.HUMAN,
        "operator",
        "Keep delegated work moving while involving me only when my judgment matters.",
    )
    constraint = (
        statement(
            WorkActorKind.HUMAN,
            "operator",
            "Ask me before changing the core architecture direction.",
        )
        if constrained
        else None
    )
    attention_constraints = (constraint,) if constraint else ()
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
        continuation_scope="Stay inside the delegated objective and constraints.",
        depth_interpretation="Infer depth from the human request and evidence.",
    )
    return handoff, interpretation, constraint


def proposal(handoff, interpretation, *, text="Repair the routine failing gate."):
    return ContinuationProposal.create(
        handoff=handoff,
        interpretation=interpretation,
        checkpoint_digest="1" * 64,
        statement=statement(WorkActorKind.WORKER, "chatgpt", text),
    )


def admission(
    handoff,
    interpretation,
    proposed,
    *,
    scope_fit=ContinuationFit.ALIGNED,
    evidence_fit=ContinuationEvidenceFit.SUPPORTED,
    material=False,
):
    ask = scope_fit is ContinuationFit.UNCERTAIN or material
    revise = evidence_fit in {
        ContinuationEvidenceFit.UNSUPPORTED,
        ContinuationEvidenceFit.UNCERTAIN,
    }
    return ContinuationAdmission.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposed,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        objective_fit=ContinuationFit.ALIGNED,
        constraint_fit=ContinuationFit.ALIGNED,
        scope_fit=scope_fit,
        depth_fit=ContinuationFit.ALIGNED,
        evidence_fit=evidence_fit,
        material_human_choice=material,
        reason="Exact M6.2 state for the attention test.",
        revision_request="Revise inside the current delegation." if revise else None,
        requested_human_response="Choose the material direction." if ask else None,
    )


def checks(handoff, status=AttentionConstraintStatus.CLEAR):
    return tuple(
        AttentionConstraintCheck.create(
            constraint=item,
            status=status,
            reason="Exact attention constraint evaluation.",
        )
        for item in handoff.attention_constraints
    )


def context(
    handoff,
    interpretation,
    proposed,
    admitted,
    *,
    reversibility=AttentionReversibility.REVERSIBLE,
    impact=AttentionTrajectoryImpact.ROUTINE,
    alternatives=AttentionAlternativeShape.NONE,
    constraint_checks=None,
):
    if constraint_checks is None:
        constraint_checks = checks(handoff)
    evidence = statement(
        WorkActorKind.SYSTEM,
        "state",
        "The current evidence belongs to this exact delegated checkpoint.",
    )
    return DynamicAttentionContext.create(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposed,
        admission=admitted,
        context_builder_kind=WorkActorKind.CODEXIA,
        context_builder="codexia",
        basis_statements=(proposed.statement, evidence),
        reversibility=reversibility,
        trajectory_impact=impact,
        alternatives=alternatives,
        attention_constraint_checks=constraint_checks,
    )


def decide(handoff, interpretation, proposed, admitted, ctx, **overrides):
    payload = dict(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposed,
        admission=admitted,
        context=ctx,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        cognitive_disposition=AttentionDisposition.KEEP_MOVING,
        confidence_basis_points=9500,
        urgency=AttentionUrgency.NONE,
        reason="Routine delegated work does not require human judgment.",
    )
    payload.update(overrides)
    return DynamicAttentionDecision.evaluate(**payload)


def test_routine_admitted_work_keeps_moving() -> None:
    handoff, interpretation, _ = work()
    proposed = proposal(handoff, interpretation)
    admitted = admission(handoff, interpretation, proposed)
    result = decide(
        handoff,
        interpretation,
        proposed,
        admitted,
        context(handoff, interpretation, proposed, admitted),
    )
    assert not result.needs_human
    assert result.disposition is AttentionDisposition.KEEP_MOVING
    assert result.override is AttentionOverride.NONE


def test_cognition_can_ask_human_without_static_threshold() -> None:
    handoff, interpretation, _ = work()
    proposed = proposal(handoff, interpretation, text="Choose between two valid architectures.")
    admitted = admission(handoff, interpretation, proposed)
    ctx = context(
        handoff,
        interpretation,
        proposed,
        admitted,
        reversibility=AttentionReversibility.BOUNDED,
        impact=AttentionTrajectoryImpact.DIRECTIONAL,
        alternatives=AttentionAlternativeShape.MATERIAL,
    )
    result = decide(
        handoff,
        interpretation,
        proposed,
        admitted,
        ctx,
        cognitive_disposition=AttentionDisposition.ASK_HUMAN,
        urgency=AttentionUrgency.NORMAL,
        reason="Both options fit, but the choice materially changes the future trajectory.",
        requested_response="Choose which architecture direction to preserve.",
    )
    assert result.needs_human
    assert result.override is AttentionOverride.NONE


@pytest.mark.parametrize(
    "status",
    [AttentionConstraintStatus.TRIGGERED, AttentionConstraintStatus.UNCERTAIN],
)
def test_explicit_attention_rule_blocks_model_keep_moving(status) -> None:
    handoff, interpretation, constraint = work(constrained=True)
    assert constraint is not None
    proposed = proposal(handoff, interpretation)
    admitted = admission(handoff, interpretation, proposed)
    ctx = context(
        handoff,
        interpretation,
        proposed,
        admitted,
        constraint_checks=checks(handoff, status),
    )
    result = decide(
        handoff,
        interpretation,
        proposed,
        admitted,
        ctx,
        urgency=AttentionUrgency.NORMAL,
        reason="The explicit human attention rule is triggered or cannot be resolved safely.",
        requested_response="Resolve the explicit attention boundary.",
    )
    assert result.needs_human
    assert result.override is AttentionOverride.EXPLICIT_HUMAN_CONSTRAINT


def test_every_explicit_attention_rule_must_be_checked_exactly_once() -> None:
    handoff, interpretation, _ = work(constrained=True)
    proposed = proposal(handoff, interpretation)
    admitted = admission(handoff, interpretation, proposed)
    with pytest.raises(InvalidWorkRecordError, match="every exact attention constraint"):
        context(
            handoff,
            interpretation,
            proposed,
            admitted,
            constraint_checks=(),
        )


def test_foreign_attention_rule_cannot_substitute_for_handoff_rule() -> None:
    handoff, interpretation, _ = work(constrained=True)
    proposed = proposal(handoff, interpretation)
    admitted = admission(handoff, interpretation, proposed)
    foreign = statement(WorkActorKind.HUMAN, "operator", "Ask before every action.")
    foreign_check = AttentionConstraintCheck.create(
        constraint=foreign,
        status=AttentionConstraintStatus.TRIGGERED,
        reason="Foreign rule.",
    )
    with pytest.raises(InvalidWorkRecordError, match="every exact attention constraint"):
        context(
            handoff,
            interpretation,
            proposed,
            admitted,
            constraint_checks=(foreign_check,),
        )


def test_m6_2_ask_human_cannot_be_suppressed() -> None:
    handoff, interpretation, _ = work()
    proposed = proposal(handoff, interpretation)
    admitted = admission(
        handoff,
        interpretation,
        proposed,
        scope_fit=ContinuationFit.UNCERTAIN,
        material=True,
    )
    ctx = context(handoff, interpretation, proposed, admitted)
    result = decide(
        handoff,
        interpretation,
        proposed,
        admitted,
        ctx,
        urgency=AttentionUrgency.NORMAL,
        reason="M6.2 already requires human judgment.",
        requested_response=admitted.requested_human_response,
    )
    assert result.needs_human
    assert result.override is AttentionOverride.ADMISSION_REQUIRES_HUMAN


def test_routine_revise_need_does_not_automatically_interrupt_human() -> None:
    handoff, interpretation, _ = work()
    proposed = proposal(handoff, interpretation)
    admitted = admission(
        handoff,
        interpretation,
        proposed,
        evidence_fit=ContinuationEvidenceFit.UNSUPPORTED,
    )
    result = decide(
        handoff,
        interpretation,
        proposed,
        admitted,
        context(handoff, interpretation, proposed, admitted),
    )
    assert admitted.revision_request
    assert not result.needs_human


def test_attention_basis_must_include_exact_proposal() -> None:
    handoff, interpretation, _ = work()
    proposed = proposal(handoff, interpretation)
    admitted = admission(handoff, interpretation, proposed)
    unrelated = statement(WorkActorKind.SYSTEM, "state", "Unrelated evidence.")
    with pytest.raises(InvalidWorkRecordError, match="exact continuation proposal"):
        DynamicAttentionContext.create(
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposed,
            admission=admitted,
            context_builder_kind=WorkActorKind.CODEXIA,
            context_builder="codexia",
            basis_statements=(unrelated,),
            reversibility=AttentionReversibility.REVERSIBLE,
            trajectory_impact=AttentionTrajectoryImpact.ROUTINE,
            alternatives=AttentionAlternativeShape.NONE,
        )


def test_worker_cannot_own_attention_context_or_decision() -> None:
    handoff, interpretation, _ = work()
    proposed = proposal(handoff, interpretation)
    admitted = admission(handoff, interpretation, proposed)
    with pytest.raises(InvalidWorkRecordError, match="Codexia authorship"):
        DynamicAttentionContext.create(
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposed,
            admission=admitted,
            context_builder_kind=WorkActorKind.WORKER,
            context_builder="chatgpt",
            basis_statements=(proposed.statement,),
            reversibility=AttentionReversibility.REVERSIBLE,
            trajectory_impact=AttentionTrajectoryImpact.ROUTINE,
            alternatives=AttentionAlternativeShape.NONE,
        )
    ctx = context(handoff, interpretation, proposed, admitted)
    with pytest.raises(InvalidWorkRecordError, match="Codexia authorship"):
        decide(
            handoff,
            interpretation,
            proposed,
            admitted,
            ctx,
            assessor_kind=WorkActorKind.WORKER,
            assessor="chatgpt",
        )


def test_no_attention_result_cannot_smuggle_urgency() -> None:
    handoff, interpretation, _ = work()
    proposed = proposal(handoff, interpretation)
    admitted = admission(handoff, interpretation, proposed)
    with pytest.raises(InvalidWorkRecordError, match="urgency=none"):
        decide(
            handoff,
            interpretation,
            proposed,
            admitted,
            context(handoff, interpretation, proposed, admitted),
            urgency=AttentionUrgency.LOW,
        )


def test_round_trip_and_tamper_are_fail_closed() -> None:
    handoff, interpretation, _ = work()
    proposed = proposal(handoff, interpretation)
    admitted = admission(handoff, interpretation, proposed)
    ctx = context(
        handoff,
        interpretation,
        proposed,
        admitted,
        reversibility=AttentionReversibility.COSTLY,
        impact=AttentionTrajectoryImpact.LOCAL,
        alternatives=AttentionAlternativeShape.EQUIVALENT,
    )
    result = decide(handoff, interpretation, proposed, admitted, ctx)

    assert DynamicAttentionContext.from_dict(ctx.to_dict()) == ctx
    assert DynamicAttentionDecision.from_dict(result.to_dict()) == result
    with pytest.raises(InvalidWorkRecordError):
        replace(ctx, alternatives=AttentionAlternativeShape.MATERIAL)
    with pytest.raises(InvalidWorkRecordError):
        replace(result, cognitive_disposition=AttentionDisposition.ASK_HUMAN)


def test_strict_decoding_rejects_authority_shaped_extra_fields() -> None:
    handoff, interpretation, _ = work()
    proposed = proposal(handoff, interpretation)
    admitted = admission(handoff, interpretation, proposed)
    ctx = context(handoff, interpretation, proposed, admitted)
    payload = ctx.to_dict()
    payload["execute"] = True
    with pytest.raises(InvalidWorkRecordError, match="keys mismatch"):
        DynamicAttentionContext.from_dict(payload)


def test_context_cannot_be_rebound_to_other_work() -> None:
    handoff, interpretation, _ = work()
    proposed = proposal(handoff, interpretation)
    admitted = admission(handoff, interpretation, proposed)
    ctx = context(handoff, interpretation, proposed, admitted)

    other_handoff, other_interpretation, _ = work()
    other_proposed = proposal(other_handoff, other_interpretation)
    other_admitted = admission(other_handoff, other_interpretation, other_proposed)
    with pytest.raises(InvalidWorkRecordError, match="exact admitted work checkpoint"):
        ctx.assert_binds(other_handoff, other_interpretation, other_proposed, other_admitted)
