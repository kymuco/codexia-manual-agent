from __future__ import annotations

import json

import pytest

from codexia_manual_agent.domain.models import ProviderResponse
from codexia_manual_agent.work.admission import ContinuationDecision
from codexia_manual_agent.work.chat_peer import ChatGPTPeerLoop, ChatPeerCursor
from codexia_manual_agent.work.contracts import (
    InvalidWorkRecordError,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)
from codexia_manual_agent.work.pilot_checkpoint_hardening import PilotCheckpointSource
from codexia_manual_agent.work.supervisor import BackgroundWorkSupervisor


class _Cognition:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ProviderResponse(text=json.dumps(self.payload))


def _registered(tmp_path):
    objective = WorkStatement.create(
        author_kind=WorkActorKind.HUMAN,
        actor="operator",
        text="Perform the bounded read-only validation.",
    )
    handoff = WorkHandoff.create(objective=objective)
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter="codexia",
        basis_statements=(objective,),
        completion_expectation="Return a finished evidence-backed validation.",
        continuation_scope="Stay inside the bounded read-only validation.",
        depth_interpretation="Use enough evidence to support the result.",
    )
    supervisor = BackgroundWorkSupervisor(tmp_path / "supervisor.sqlite3")
    snapshot = supervisor.register(
        handoff=handoff,
        interpretation=interpretation,
        cursor=ChatPeerCursor.from_messages("conversation-1", ()),
    )
    return supervisor, snapshot


def _initial_payload(*, evidence_fit: str) -> dict[str, object]:
    return {
        "mode": "checkpoint",
        "proposal_text": "Perform the first bounded read-only inspection step.",
        "objective_fit": "aligned",
        "constraint_fit": "aligned",
        "scope_fit": "aligned",
        "depth_fit": "aligned",
        "evidence_fit": evidence_fit,
        "material_human_choice": False,
        "admission_reason": "The bounded initial step stays inside delegated scope.",
        "revision_request": None,
        "requested_human_response": None,
        "reversibility": "reversible",
        "trajectory_impact": "routine",
        "alternatives": "none",
        "attention_constraint_checks": [],
        "cognitive_disposition": "keep_moving",
        "confidence_basis_points": 9900,
        "urgency": "none",
        "attention_reason": "No human attention is required for this routine step.",
        "requested_response": None,
    }


def test_initial_checkpoint_contract_uses_not_required_without_worker_evidence(tmp_path) -> None:
    supervisor, snapshot = _registered(tmp_path)
    cognition = _Cognition(_initial_payload(evidence_fit="not_required"))
    source = PilotCheckpointSource(supervisor=supervisor, provider=cognition)

    proposal, admission, attention = source(
        snapshot,
        ChatGPTPeerLoop(object()),
        None,
    )

    assert proposal.statement.author_kind is WorkActorKind.CODEXIA
    assert admission.decision is ContinuationDecision.ADMIT
    assert attention.needs_human is False
    prompt = cognition.requests[0].prompt
    assert "evidence_fit evaluates whether evidence required to admit the CURRENT proposal is adequate" in prompt
    assert "Do NOT mark evidence_fit=unsupported merely because current exact logical worker evidence is null" in prompt
    assert "use evidence_fit=not_required" in prompt
    assert "evidence_fit=unsupported or evidence_fit=uncertain deterministically derives REVISE" in prompt
    assert "revision_request MUST contain a bounded non-null worker correction" in prompt


def test_unsupported_evidence_without_revision_request_still_fails_closed(tmp_path) -> None:
    supervisor, snapshot = _registered(tmp_path)
    source = PilotCheckpointSource(
        supervisor=supervisor,
        provider=_Cognition(_initial_payload(evidence_fit="unsupported")),
    )

    with pytest.raises(
        InvalidWorkRecordError,
        match="REVISE admission requires a bounded revision request",
    ):
        source(snapshot, ChatGPTPeerLoop(object()), None)
