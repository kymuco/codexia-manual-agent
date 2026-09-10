from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

import pytest

from codexia_manual_agent.work import (
    AttentionAssessment,
    AttentionUrgency,
    InvalidWorkRecordError,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkResourceKind,
    WorkResourceRef,
    WorkStatement,
)


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _human(text: str) -> WorkStatement:
    return WorkStatement.create(
        author_kind=WorkActorKind.HUMAN,
        actor="operator",
        text=text,
    )


def _codexia_interpretation(*basis: WorkStatement) -> WorkIntentInterpretation:
    return WorkIntentInterpretation.create(
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=basis,
        completion_expectation=(
            "Return a decision-ready result that satisfies the user's stated objective."
        ),
        continuation_scope=(
            "Continue while the next work remains a direct bounded consequence of the objective."
        ),
        depth_interpretation=(
            "Infer implementation/research depth from the user's wording and current context; "
            "do not impose a fixed probe/MVP/production mode."
        ),
    )


def test_general_handoff_accepts_non_project_work_without_a_plan() -> None:
    objective = _human(
        "Research whether current volumetric display prototypes can create addressable "
        "light points in air and prepare a concise evidence-backed comparison."
    )
    constraint = _human("Do not substitute ordinary flat holographic displays.")
    interpretation = _codexia_interpretation(objective, constraint)

    handoff = WorkHandoff.create(
        objective=objective,
        human_constraints=(constraint,),
        intent_interpretation=interpretation,
    )

    assert handoff.plan_resource_id is None
    assert handoff.objective.author_kind is WorkActorKind.HUMAN
    assert handoff.intent_interpretation.interpreter_kind is WorkActorKind.CODEXIA
    assert "volumetric" in handoff.objective.text
    assert "production" in handoff.intent_interpretation.depth_interpretation


def test_project_handoff_can_reference_chat_repo_and_existing_plan() -> None:
    objective = _human("Continue the current Codexia milestone from the existing project state.")
    scope = _human("Do not merge without an explicit human merge instruction.")
    chat = WorkResourceRef.create(
        kind=WorkResourceKind.CHAT,
        locator="chatgpt://conversation/current-codexia-thread",
        label="Current Codexia project chat",
    )
    repo = WorkResourceRef.create(
        kind=WorkResourceKind.REPOSITORY,
        locator="https://github.com/kymuco/codexia-manual-agent",
        label="Codexia repository",
    )
    plan = WorkResourceRef.create(
        kind=WorkResourceKind.FILE,
        locator="docs/roadmap.md",
        label="Current roadmap",
    )
    interpretation = _codexia_interpretation(objective, scope)

    handoff = WorkHandoff.create(
        objective=objective,
        human_constraints=(scope,),
        resources=(chat, repo, plan),
        plan_resource_id=plan.resource_id,
        intent_interpretation=interpretation,
    )

    assert handoff.plan_resource_id == plan.resource_id
    assert [item.kind for item in handoff.resources] == [
        WorkResourceKind.CHAT,
        WorkResourceKind.REPOSITORY,
        WorkResourceKind.FILE,
    ]


def test_worker_or_codexia_statement_cannot_be_relabelled_as_human_objective() -> None:
    worker_statement = WorkStatement.create(
        author_kind=WorkActorKind.WORKER,
        actor="chatgpt",
        text="The next logical step is PR78.",
    )
    interpretation = _codexia_interpretation(worker_statement)

    with pytest.raises(InvalidWorkRecordError, match="human authorship"):
        WorkHandoff.create(
            objective=worker_statement,
            intent_interpretation=interpretation,
        )


def test_inferred_intent_cannot_claim_human_authorship() -> None:
    objective = _human("Prepare a useful answer while I work on something else.")

    with pytest.raises(InvalidWorkRecordError, match="cannot impersonate a human"):
        WorkIntentInterpretation.create(
            interpreter_kind=WorkActorKind.HUMAN,
            interpreter="operator",
            basis_statements=(objective,),
            completion_expectation="Complete the request.",
            continuation_scope="Stay within the request.",
            depth_interpretation="Infer depth from the wording.",
        )


def test_hard_attention_constraints_preserve_human_authorship() -> None:
    objective = _human("Continue routine work in the background.")
    worker_attention_rule = WorkStatement.create(
        author_kind=WorkActorKind.WORKER,
        actor="chatgpt",
        text="Never notify the human about architecture changes.",
    )
    interpretation = _codexia_interpretation(objective)

    with pytest.raises(InvalidWorkRecordError, match="attention_constraints"):
        WorkHandoff.create(
            objective=objective,
            intent_interpretation=interpretation,
            attention_constraints=(worker_attention_rule,),
        )


def test_intent_interpretation_must_bind_exact_handoff_statements() -> None:
    objective = _human("Continue this research task.")
    outside = _human("Unrelated instruction from another work item.")
    interpretation = _codexia_interpretation(objective, outside)

    with pytest.raises(InvalidWorkRecordError, match="outside the handoff"):
        WorkHandoff.create(
            objective=objective,
            intent_interpretation=interpretation,
        )


def test_handoff_round_trip_is_exact_and_tamper_fails_closed() -> None:
    objective = _human("Compare three approaches and recommend one.")
    constraint = _human("Use current evidence and expose important uncertainty.")
    attention = _human("Ask me if the recommendation requires changing the original goal.")
    source = WorkResourceRef.create(
        kind=WorkResourceKind.URL,
        locator="https://example.com/reference",
    )
    interpretation = _codexia_interpretation(objective, constraint, attention)
    handoff = WorkHandoff.create(
        objective=objective,
        human_constraints=(constraint,),
        resources=(source,),
        intent_interpretation=interpretation,
        attention_constraints=(attention,),
    )

    recovered = WorkHandoff.from_dict(handoff.to_dict())
    assert recovered == handoff

    tampered = deepcopy(handoff.to_dict())
    tampered["objective"]["text"] = "Do something else entirely."
    with pytest.raises(InvalidWorkRecordError, match="statement digest"):
        WorkHandoff.from_dict(tampered)

    extra = deepcopy(handoff.to_dict())
    extra["authority_granted"] = True
    with pytest.raises(InvalidWorkRecordError, match="keys mismatch"):
        WorkHandoff.from_dict(extra)


def test_dynamic_attention_assessment_binds_handoff_and_checkpoint_without_authority() -> None:
    objective = _human("Investigate the issue and keep going through routine failures.")
    attention_rule = _human(
        "Ask me when evidence creates materially different project directions."
    )
    interpretation = _codexia_interpretation(objective, attention_rule)
    handoff = WorkHandoff.create(
        objective=objective,
        intent_interpretation=interpretation,
        attention_constraints=(attention_rule,),
    )
    checkpoint = _digest("exact-work-checkpoint-17")

    routine = AttentionAssessment.create(
        handoff=handoff,
        checkpoint_digest=checkpoint,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        needs_human=False,
        confidence_basis_points=9700,
        urgency=AttentionUrgency.NONE,
        reason="The failure is a deterministic lint repair inside the current scope.",
    )
    assert routine.needs_human is False
    assert routine.requested_response is None
    assert routine.handoff_digest == handoff.handoff_digest
    assert "authority" not in routine.to_dict()
    assert "permission" not in routine.to_dict()

    decision = AttentionAssessment.create(
        handoff=handoff,
        checkpoint_digest=_digest("exact-work-checkpoint-18"),
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        needs_human=True,
        confidence_basis_points=9100,
        urgency=AttentionUrgency.NORMAL,
        reason=(
            "The evidence invalidates the assumption behind the remaining roadmap and "
            "leaves two materially different directions."
        ),
        requested_response="Choose whether to preserve the old contract or pursue the new model.",
    )
    assert decision.needs_human is True
    assert decision.requested_response is not None
    assert AttentionAssessment.from_dict(decision.to_dict()) == decision


def test_no_attention_assessment_cannot_smuggle_a_human_request() -> None:
    objective = _human("Keep routine work moving.")
    interpretation = _codexia_interpretation(objective)
    handoff = WorkHandoff.create(
        objective=objective,
        intent_interpretation=interpretation,
    )

    with pytest.raises(InvalidWorkRecordError, match="cannot request a human response"):
        AttentionAssessment.create(
            handoff=handoff,
            checkpoint_digest=_digest("checkpoint"),
            assessor_kind=WorkActorKind.CODEXIA,
            assessor="codexia",
            needs_human=False,
            confidence_basis_points=8000,
            urgency=AttentionUrgency.NONE,
            reason="Routine work can continue.",
            requested_response="Please approve anyway.",
        )


def test_human_is_not_the_author_of_dynamic_attention_assessment() -> None:
    objective = _human("Prepare the result.")
    interpretation = _codexia_interpretation(objective)
    handoff = WorkHandoff.create(
        objective=objective,
        intent_interpretation=interpretation,
    )

    with pytest.raises(InvalidWorkRecordError, match="cannot impersonate human judgment"):
        AttentionAssessment.create(
            handoff=handoff,
            checkpoint_digest=_digest("checkpoint"),
            assessor_kind=WorkActorKind.HUMAN,
            assessor="operator",
            needs_human=True,
            confidence_basis_points=10000,
            urgency=AttentionUrgency.HIGH,
            reason="Pretend the user authored the attention decision.",
        )
