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


def _interpret(
    handoff: WorkHandoff,
    *basis: WorkStatement,
) -> WorkIntentInterpretation:
    return WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=basis,
        completion_expectation=(
            "Return a decision-ready result that satisfies the user's stated objective."
        ),
        continuation_scope=(
            "Continue while the next work remains a direct bounded consequence "
            "of the objective."
        ),
        depth_interpretation=(
            "Infer implementation/research depth from the user's wording and "
            "current context; do not impose a fixed probe/MVP/production mode."
        ),
    )


def test_general_handoff_accepts_non_project_work_without_a_plan() -> None:
    objective = _human(
        "Research whether current volumetric display prototypes can create "
        "addressable light points in air and prepare an evidence-backed comparison."
    )
    constraint = _human("Do not substitute ordinary flat holographic displays.")

    handoff = WorkHandoff.create(
        objective=objective,
        human_constraints=(constraint,),
    )
    interpretation = _interpret(handoff, objective, constraint)

    assert handoff.plan_resource_id is None
    assert handoff.objective.author_kind is WorkActorKind.HUMAN
    assert interpretation.interpreter_kind is WorkActorKind.CODEXIA
    assert "volumetric" in handoff.objective.text
    assert "production" in interpretation.depth_interpretation


def test_human_handoff_identity_does_not_change_when_interpretation_changes() -> None:
    objective = _human("Build the requested application while I work elsewhere.")
    handoff = WorkHandoff.create(objective=objective)

    quick = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Produce the requested working result.",
        continuation_scope="Stay inside the stated feature scope.",
        depth_interpretation=(
            "Prefer a narrow implementation because the request is exploratory."
        ),
    )
    deep = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Produce the requested working result.",
        continuation_scope="Stay inside the stated feature scope.",
        depth_interpretation="Use production-quality architecture and validation.",
    )

    assert quick.handoff_digest == handoff.handoff_digest
    assert deep.handoff_digest == handoff.handoff_digest
    assert quick.interpretation_digest != deep.interpretation_digest
    assert handoff.to_dict() == WorkHandoff.from_dict(handoff.to_dict()).to_dict()


def test_project_handoff_can_reference_chat_repo_and_existing_plan() -> None:
    objective = _human(
        "Continue the current Codexia milestone from the existing project state."
    )
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

    handoff = WorkHandoff.create(
        objective=objective,
        human_constraints=(scope,),
        resources=(chat, repo, plan),
        plan_resource_id=plan.resource_id,
    )
    interpretation = _interpret(handoff, objective, scope)

    assert handoff.plan_resource_id == plan.resource_id
    assert interpretation.handoff_id == handoff.handoff_id
    assert [item.kind for item in handoff.resources] == [
        WorkResourceKind.CHAT,
        WorkResourceKind.REPOSITORY,
        WorkResourceKind.FILE,
    ]


def test_worker_or_codexia_statement_cannot_be_human_objective() -> None:
    worker_statement = WorkStatement.create(
        author_kind=WorkActorKind.WORKER,
        actor="chatgpt",
        text="The next logical step is PR78.",
    )

    with pytest.raises(InvalidWorkRecordError, match="human authorship"):
        WorkHandoff.create(objective=worker_statement)


def test_existing_worker_statement_cannot_be_relabelled_human_without_digest_failure() -> None:
    worker_statement = WorkStatement.create(
        author_kind=WorkActorKind.WORKER,
        actor="chatgpt",
        text="Continue with the next PR.",
    )
    tampered = worker_statement.to_dict()
    tampered["author_kind"] = WorkActorKind.HUMAN.value

    with pytest.raises(InvalidWorkRecordError, match="statement digest"):
        WorkStatement.from_dict(tampered)


def test_inferred_intent_cannot_claim_human_authorship() -> None:
    objective = _human("Prepare a useful answer while I work on something else.")
    handoff = WorkHandoff.create(objective=objective)

    with pytest.raises(InvalidWorkRecordError, match="cannot impersonate a human"):
        WorkIntentInterpretation.create(
            handoff=handoff,
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

    with pytest.raises(InvalidWorkRecordError, match="attention_constraints"):
        WorkHandoff.create(
            objective=objective,
            attention_constraints=(worker_attention_rule,),
        )


def test_interpretation_must_bind_exact_handoff_statements() -> None:
    objective = _human("Continue this research task.")
    handoff = WorkHandoff.create(objective=objective)
    outside = _human("Unrelated instruction from another work item.")

    with pytest.raises(InvalidWorkRecordError, match="outside the handoff"):
        _interpret(handoff, objective, outside)


def test_handoff_round_trip_and_tamper_fail_closed() -> None:
    objective = _human("Compare three approaches and recommend one.")
    constraint = _human("Use current evidence and expose important uncertainty.")
    attention = _human(
        "Ask me if the recommendation requires changing the original goal."
    )
    source = WorkResourceRef.create(
        kind=WorkResourceKind.URL,
        locator="https://example.com/reference",
    )
    handoff = WorkHandoff.create(
        objective=objective,
        human_constraints=(constraint,),
        resources=(source,),
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


def test_dynamic_attention_binds_exact_handoff_interpretation_and_checkpoint() -> None:
    objective = _human(
        "Investigate the issue and keep going through routine failures."
    )
    attention_rule = _human(
        "Ask me when evidence creates materially different project directions."
    )
    handoff = WorkHandoff.create(
        objective=objective,
        attention_constraints=(attention_rule,),
    )
    interpretation = _interpret(handoff, objective, attention_rule)
    checkpoint = _digest("exact-work-checkpoint-17")

    routine = AttentionAssessment.create(
        handoff=handoff,
        interpretation=interpretation,
        checkpoint_digest=checkpoint,
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        needs_human=False,
        confidence_basis_points=9700,
        urgency=AttentionUrgency.NONE,
        reason="The failure is a deterministic lint repair inside the current scope.",
    )
    routine.assert_binds(handoff, interpretation)

    assert routine.needs_human is False
    assert routine.requested_response is None
    assert routine.handoff_digest == handoff.handoff_digest
    assert routine.interpretation_digest == interpretation.interpretation_digest
    assert "authority" not in routine.to_dict()
    assert "permission" not in routine.to_dict()

    decision = AttentionAssessment.create(
        handoff=handoff,
        interpretation=interpretation,
        checkpoint_digest=_digest("exact-work-checkpoint-18"),
        assessor_kind=WorkActorKind.CODEXIA,
        assessor="codexia",
        needs_human=True,
        confidence_basis_points=9100,
        urgency=AttentionUrgency.NORMAL,
        reason=(
            "The evidence invalidates the assumption behind the remaining roadmap "
            "and leaves two materially different directions."
        ),
        requested_response=(
            "Choose whether to preserve the old contract or pursue the new model."
        ),
    )
    assert decision.needs_human is True
    assert decision.requested_response is not None
    assert AttentionAssessment.from_dict(decision.to_dict()) == decision


def test_attention_assessment_rejects_an_interpretation_for_another_handoff() -> None:
    first_objective = _human("Research option A.")
    first = WorkHandoff.create(objective=first_objective)
    first_interpretation = _interpret(first, first_objective)

    second_objective = _human("Research option B.")
    second = WorkHandoff.create(objective=second_objective)

    with pytest.raises(InvalidWorkRecordError, match="exact WorkHandoff"):
        AttentionAssessment.create(
            handoff=second,
            interpretation=first_interpretation,
            checkpoint_digest=_digest("checkpoint"),
            assessor_kind=WorkActorKind.CODEXIA,
            assessor="codexia",
            needs_human=False,
            confidence_basis_points=9000,
            urgency=AttentionUrgency.NONE,
            reason="Routine continuation.",
        )


def test_no_attention_assessment_cannot_smuggle_a_human_request() -> None:
    objective = _human("Keep routine work moving.")
    handoff = WorkHandoff.create(objective=objective)
    interpretation = _interpret(handoff, objective)

    with pytest.raises(
        InvalidWorkRecordError,
        match="cannot request a human response",
    ):
        AttentionAssessment.create(
            handoff=handoff,
            interpretation=interpretation,
            checkpoint_digest=_digest("checkpoint"),
            assessor_kind=WorkActorKind.CODEXIA,
            assessor="codexia",
            needs_human=False,
            confidence_basis_points=8000,
            urgency=AttentionUrgency.NONE,
            reason="Routine work can continue.",
            requested_response="Please approve anyway.",
        )


def test_human_is_not_author_of_dynamic_attention_assessment() -> None:
    objective = _human("Prepare the result.")
    handoff = WorkHandoff.create(objective=objective)
    interpretation = _interpret(handoff, objective)

    with pytest.raises(
        InvalidWorkRecordError,
        match="cannot impersonate human judgment",
    ):
        AttentionAssessment.create(
            handoff=handoff,
            interpretation=interpretation,
            checkpoint_digest=_digest("checkpoint"),
            assessor_kind=WorkActorKind.HUMAN,
            assessor="operator",
            needs_human=True,
            confidence_basis_points=10000,
            urgency=AttentionUrgency.HIGH,
            reason="Pretend the user authored the attention decision.",
        )
