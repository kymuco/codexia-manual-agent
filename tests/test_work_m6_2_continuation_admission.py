from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

import pytest

from codexia_manual_agent.work import (
    ContinuationAdmission,
    ContinuationDecision,
    ContinuationEvidenceFit,
    ContinuationFit,
    ContinuationProposal,
    InvalidWorkRecordError,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _statement(kind: WorkActorKind, actor: str, text: str) -> WorkStatement:
    return WorkStatement.create(author_kind=kind, actor=actor, text=text)


def _handoff() -> tuple[WorkHandoff, WorkIntentInterpretation]:
    objective = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Continue Codexia toward delegated background work without widening authority.",
    )
    constraint = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Do not merge without an explicit human merge instruction.",
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
        completion_expectation=(
            "Advance the current milestone to a validated review-ready result."
        ),
        continuation_scope=(
            "Continue only with work directly required by the current milestone."
        ),
        depth_interpretation=(
            "Use the level of rigor implied by the user's request and existing codebase."
        ),
    )
    return handoff, interpretation


def _proposal(
    handoff: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    text: str,
    *,
    checkpoint: str = "checkpoint-17",
) -> ContinuationProposal:
    statement = _statement(WorkActorKind.WORKER, "chatgpt", text)
    return ContinuationProposal.create(
        handoff=handoff,
        interpretation=interpretation,
        checkpoint_digest=_digest(checkpoint),
        statement=statement,
    )


def _evaluate(
    handoff: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    proposal: ContinuationProposal,
    *,
    objective_fit: ContinuationFit = ContinuationFit.ALIGNED,
    constraint_fit: ContinuationFit = ContinuationFit.ALIGNED,
    scope_fit: ContinuationFit = ContinuationFit.ALIGNED,
    depth_fit: ContinuationFit = ContinuationFit.ALIGNED,
    evidence_fit: ContinuationEvidenceFit = ContinuationEvidenceFit.SUPPORTED,
    material_human_choice: bool = False,
    revision_request: str | None = None,
    requested_human_response: str | None = None,
) -> ContinuationAdmission:
    return ContinuationAdmission.evaluate(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        objective_fit=objective_fit,
        constraint_fit=constraint_fit,
        scope_fit=scope_fit,
        depth_fit=depth_fit,
        evidence_fit=evidence_fit,
        material_human_choice=material_human_choice,
        reason="Bounded M6.2 admission assessment.",
        revision_request=revision_request,
        requested_human_response=requested_human_response,
    )


def test_routine_worker_next_pr_can_be_admitted_without_becoming_authority() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(
        handoff,
        interpretation,
        "Implement M6.2 continuation admission and validate it before review.",
    )

    admission = _evaluate(handoff, interpretation, proposal)

    assert admission.decision is ContinuationDecision.ADMIT
    assert admission.admitted is True
    assert admission.proposal_digest == proposal.proposal_digest
    assert admission.checkpoint_digest == proposal.checkpoint_digest
    assert "authority" not in admission.to_dict()
    assert "permission" not in admission.to_dict()
    assert "authorization" not in admission.to_dict()


def test_planless_research_continuation_can_use_same_admission_contract() -> None:
    objective = _statement(
        WorkActorKind.HUMAN,
        "operator",
        "Research current volumetric display approaches and prepare a useful comparison.",
    )
    handoff = WorkHandoff.create(objective=objective)
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Return an evidence-backed comparison.",
        continuation_scope="Deepen the research until the comparison is decision-ready.",
        depth_interpretation="Prefer primary evidence over a superficial list of examples.",
    )
    proposal = _proposal(
        handoff,
        interpretation,
        "Inspect the strongest physical prototypes and compare their actual voxel mechanism.",
    )

    admission = _evaluate(handoff, interpretation, proposal)

    assert handoff.plan_resource_id is None
    assert admission.decision is ContinuationDecision.ADMIT


def test_depth_mismatch_requests_worker_revision_instead_of_human_attention() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(
        handoff,
        interpretation,
        "Hard-code a shortcut and skip regression coverage to finish immediately.",
    )

    admission = _evaluate(
        handoff,
        interpretation,
        proposal,
        depth_fit=ContinuationFit.MISALIGNED,
        revision_request=(
            "Revise the proposal to match the required implementation rigor and validation."
        ),
    )

    assert admission.decision is ContinuationDecision.REVISE
    assert admission.admitted is False
    assert admission.revision_request is not None
    assert admission.requested_human_response is None


def test_insufficient_evidence_requests_revision_before_continuation() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(
        handoff,
        interpretation,
        "Declare the milestone complete from the first unverified worker answer.",
    )

    admission = _evaluate(
        handoff,
        interpretation,
        proposal,
        evidence_fit=ContinuationEvidenceFit.UNSUPPORTED,
        revision_request=(
            "Verify the claimed result against the exact current repository and gates first."
        ),
    )

    assert admission.decision is ContinuationDecision.REVISE


def test_objective_conflict_is_rejected_not_rewritten_into_a_new_goal() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(
        handoff,
        interpretation,
        "Pause M6 and redesign Codexia as an unrelated social-media automation product.",
    )

    admission = _evaluate(
        handoff,
        interpretation,
        proposal,
        objective_fit=ContinuationFit.MISALIGNED,
    )

    assert admission.decision is ContinuationDecision.REJECT
    assert admission.revision_request is None
    assert admission.requested_human_response is None


def test_human_constraint_violation_is_rejected_even_if_worker_prefers_it() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(
        handoff,
        interpretation,
        "Merge the current PR automatically after CI succeeds.",
    )

    admission = _evaluate(
        handoff,
        interpretation,
        proposal,
        constraint_fit=ContinuationFit.MISALIGNED,
    )

    assert admission.decision is ContinuationDecision.REJECT


def test_material_new_direction_requires_human_judgment() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(
        handoff,
        interpretation,
        "Replace the current work-continuity architecture with a new incompatible model.",
    )

    admission = _evaluate(
        handoff,
        interpretation,
        proposal,
        scope_fit=ContinuationFit.MISALIGNED,
        material_human_choice=True,
        requested_human_response=(
            "Choose whether to preserve the current architecture or authorize the new direction."
        ),
    )

    assert admission.decision is ContinuationDecision.ASK_HUMAN
    assert admission.admitted is False
    assert admission.requested_human_response is not None


def test_uncertain_human_constraint_requires_attention_instead_of_guessing() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(
        handoff,
        interpretation,
        "Change the public contract in a way whose compatibility impact is unclear.",
    )

    admission = _evaluate(
        handoff,
        interpretation,
        proposal,
        constraint_fit=ContinuationFit.UNCERTAIN,
        requested_human_response=(
            "Clarify whether preserving the existing public contract is required here."
        ),
    )

    assert admission.decision is ContinuationDecision.ASK_HUMAN


def test_human_or_system_statement_cannot_be_downcast_into_worker_proposal() -> None:
    handoff, interpretation = _handoff()

    for kind in (WorkActorKind.HUMAN, WorkActorKind.SYSTEM):
        statement = _statement(kind, kind.value, "Continue with the next step.")
        with pytest.raises(InvalidWorkRecordError, match="worker or Codexia"):
            ContinuationProposal.create(
                handoff=handoff,
                interpretation=interpretation,
                checkpoint_digest=_digest("checkpoint"),
                statement=statement,
            )


def test_worker_cannot_author_its_own_admission_decision() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(handoff, interpretation, "Continue with the bounded next step.")

    with pytest.raises(InvalidWorkRecordError, match="Codexia authorship"):
        ContinuationAdmission.evaluate(
            handoff=handoff,
            interpretation=interpretation,
            proposal=proposal,
            assessor_kind=WorkActorKind.WORKER,
            assessor="chatgpt",
            objective_fit=ContinuationFit.ALIGNED,
            constraint_fit=ContinuationFit.ALIGNED,
            scope_fit=ContinuationFit.ALIGNED,
            depth_fit=ContinuationFit.ALIGNED,
            evidence_fit=ContinuationEvidenceFit.SUPPORTED,
            material_human_choice=False,
            reason="The worker tries to admit its own proposal.",
        )


def test_admission_is_bound_to_exact_interpretation_and_checkpoint() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(handoff, interpretation, "Continue with M6.2.")
    admission = _evaluate(handoff, interpretation, proposal)
    admission.assert_binds(handoff, interpretation, proposal)

    objective = handoff.objective
    different_interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Produce a different completion standard.",
        continuation_scope="Stay inside the same milestone.",
        depth_interpretation="Use a different depth interpretation.",
    )

    with pytest.raises(InvalidWorkRecordError, match="exact work interpretation"):
        proposal.assert_binds(handoff, different_interpretation)


def test_decision_payload_shape_fails_closed() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(handoff, interpretation, "Use a shortcut.")

    with pytest.raises(InvalidWorkRecordError, match="requires a bounded revision"):
        _evaluate(
            handoff,
            interpretation,
            proposal,
            depth_fit=ContinuationFit.MISALIGNED,
        )

    with pytest.raises(InvalidWorkRecordError, match="requires a bounded human"):
        _evaluate(
            handoff,
            interpretation,
            proposal,
            material_human_choice=True,
        )


def test_round_trip_and_authority_field_tamper_fail_closed() -> None:
    handoff, interpretation = _handoff()
    proposal = _proposal(handoff, interpretation, "Continue with the admitted next step.")
    admission = _evaluate(handoff, interpretation, proposal)

    assert ContinuationProposal.from_dict(proposal.to_dict()) == proposal
    assert ContinuationAdmission.from_dict(admission.to_dict()) == admission

    tampered = deepcopy(admission.to_dict())
    tampered["decision"] = ContinuationDecision.REJECT.value
    with pytest.raises(InvalidWorkRecordError, match="does not match"):
        ContinuationAdmission.from_dict(tampered)

    extra = deepcopy(admission.to_dict())
    extra["execution_authority"] = True
    with pytest.raises(InvalidWorkRecordError, match="keys mismatch"):
        ContinuationAdmission.from_dict(extra)
