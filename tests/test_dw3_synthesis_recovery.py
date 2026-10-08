from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codexia_manual_agent.research_work import (
    ResearchContextMaterialPort,
    ResearchInstructionsMaterialPort,
)
from codexia_manual_agent.role_core import (
    CognitionOutcome,
    RoleRunState,
    project_role_runs,
)
from codexia_manual_agent.role_core.transport_bridge import (
    CognitionTransportBridge,
    CognitionTransportPortError,
)
from codexia_manual_agent.role_core.transport_projection import (
    project_cognition_handoffs,
)
from codexia_manual_agent.standalone_work import (
    StandaloneWorkHost,
    StandaloneWorkSelector,
    StandaloneWorkSurface,
)
from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_orchestration.role_cognition import (
    RoleCognitionMaterializationService,
)
from codexia_manual_agent.workflow_runtime.research_v1 import (
    RESEARCH_CONTROL_START,
    STAGE_CRITIQUE,
    STAGE_INITIAL,
    STAGE_REVISION,
    STAGE_SYNTHESIS,
    SYNTHESIZER_ROLE_ID,
    ResearchRoleOutput,
    research_workflow_binding,
)
from examples.dw3_cwa_recovery_read import extract_turn
from examples.dw3_reconcile_synthesis import (
    canonicalize_synthesis,
    validate_synthesis,
    verify_recovery,
)
from examples.dw3_research_live_host import (
    PROVIDER_REF,
    _hot_research_continuation_prompt,
    _PluginService,
)


def _content(stage: str, text: str) -> str:
    return ResearchRoleOutput(
        schema_version=1,
        stage=stage,
        content=text,
        needs_human=False,
        human_question=None,
        human_reason=None,
        objective_coverage=True if stage == STAGE_SYNTHESIS else None,
        evidence_sufficiency=True if stage == STAGE_SYNTHESIS else None,
        material_unresolved_uncertainty=False if stage == STAGE_SYNTHESIS else None,
    ).to_text()


class _HandoffPort:
    port_id = "model-provider:cwa-subprocess"

    def __init__(self) -> None:
        self.calls = 0
        self.outputs = [
            _content(STAGE_INITIAL, "Initial analysis."),
            _content(STAGE_CRITIQUE, "Critic challenges storage semantics."),
            _content(STAGE_REVISION, "Revised answer with caveats."),
        ]

    def complete(self, request):
        self.calls += 1
        if self.calls == 4:
            raise RuntimeError("ambiguous external completion after handoff")
        return CognitionOutcome.succeeded(
            request.request, output_text=self.outputs[self.calls - 1]
        )


def _fixture(tmp_path: Path):
    store = SqliteWorkStore(tmp_path / "recovery.sqlite3")
    service = _PluginService()
    workflow = research_workflow_binding()
    selector = StandaloneWorkSelector(
        provider_ref=PROVIDER_REF,
        workflow_id=workflow.workflow_id,
        workflow_version=workflow.version,
    )
    surface = StandaloneWorkSurface(store)
    started = surface.start(
        objective="Choose a durable research store.",
        selector=selector,
        host_factory=lambda: StandaloneWorkHost(plugin_service=service),
        source_id="dw3-recovery-test",
    )
    work_id = started["work"]["work_id"]
    port = _HandoffPort()
    with pytest.raises(CognitionTransportPortError):
        surface.advance(
            work_id,
            selector=selector,
            host_factory=lambda: StandaloneWorkHost(
                plugin_service=service,
                cognition_port=port,
                instructions=ResearchInstructionsMaterialPort(),
                context=ResearchContextMaterialPort(store=store, work_id=work_id),
            ),
            max_steps=8,
        )
    assert port.calls == 4
    snapshot = store.snapshot(work_id)
    events = store.events(work_id)
    assert events[-1].kind == "role.cognition-handoff-admitted"
    handoff = project_cognition_handoffs(events)[-1]
    synth = next(
        role
        for role in project_role_runs(events)
        if role.run.binding.role_id == SYNTHESIZER_ROLE_ID
    )
    assert synth.state is RoleRunState.REQUESTED
    request = (
        RoleCognitionMaterializationService(
            store=store,
            instructions=ResearchInstructionsMaterialPort(),
            context=ResearchContextMaterialPort(store=store, work_id=work_id),
        )
        .rematerialize_request(
            work_id=work_id,
            role_run_id=synth.run.role_run_id,
        )
        .request
    )
    delta = _hot_research_continuation_prompt(request.context)
    assert delta is not None
    prompt = (
        "[Codexia product-runtime system context]\n"
        f"{request.instructions.strip()}\n\n"
        "[Codexia product-runtime request]\n"
        f"{delta}"
    )
    response = _content(
        STAGE_SYNTHESIS,
        "# Final synthesis\n\nSQLite recommendation with safeguards.",
    )
    filepath = tmp_path / "response.txt"
    filepath.write_bytes(response.encode("utf-8"))
    sha = hashlib.sha256(filepath.read_bytes()).hexdigest()
    return {
        "store": store,
        "work_id": work_id,
        "snapshot": snapshot,
        "handoff": handoff,
        "filepath": filepath,
        "sha": sha,
        "turn": {
            "request": {
                "message_id": "cwa-request",
                "text": prompt,
            },
            "response": {
                "message_id": "cwa-response",
                "finish_reason": "stop",
                "text": response,
            },
            "prior_revision": {
                "message_id": "cwa-prior-revision",
                "finish_reason": "stop",
                "text": _content(STAGE_REVISION, "Revised answer with caveats."),
            },
        },
    }


def _verify(fixture: dict, *, turn=None, sha=None):
    return verify_recovery(
        store=fixture["store"],
        work_id=fixture["work_id"],
        expected_revision=fixture["snapshot"].revision,
        expected_head_digest=fixture["snapshot"].last_event_digest,
        expected_handoff_id=fixture["handoff"].handoff_id,
        expected_sha256=sha or fixture["sha"],
        recovered_file=fixture["filepath"],
        turn=turn or fixture["turn"],
        request_message_id="cwa-request",
        response_message_id="cwa-response",
    )


def test_recovery_dry_run_does_not_write_and_commit_uses_existing_handoff(
    tmp_path,
) -> None:
    f = _fixture(tmp_path)
    before = len(f["store"].events(f["work_id"]))
    outcome, report = _verify(f)
    assert report["verified"] is True
    assert report["workstore_modified"] is False
    assert report["objective_coverage_complete"] is True
    assert report["response_sha256"] == f["sha"]
    assert len(f["store"].events(f["work_id"])) == before

    completed = CognitionTransportBridge(f["store"]).record_outcome(
        outcome, port_id=_HandoffPort.port_id
    )
    assert completed.state is RoleRunState.COMPLETED
    assert len(f["store"].events(f["work_id"])) == before + 1
    with pytest.raises(ValueError, match="Work head changed"):
        _verify(f)


def test_recovery_blocks_wrong_prompt_or_revision_and_wrong_file(tmp_path) -> None:
    f = _fixture(tmp_path)
    bad = json.loads(json.dumps(f["turn"]))
    bad["request"]["text"] += "extra"
    with pytest.raises(ValueError, match="prompt/response/reviser"):
        _verify(f, turn=bad)
    bad = json.loads(json.dumps(f["turn"]))
    bad["prior_revision"]["text"] += "changed"
    with pytest.raises(ValueError, match="prompt/response/reviser"):
        _verify(f, turn=bad)
    with pytest.raises(ValueError, match="SHA-256"):
        _verify(f, sha="0" * 64)
    assert f["store"].snapshot(f["work_id"]).revision == f["snapshot"].revision


def test_recovery_requires_exact_final_control(tmp_path) -> None:
    f = _fixture(tmp_path)
    text = f["turn"]["response"]["text"]
    assert validate_synthesis(text).objective_coverage is True
    with pytest.raises(ValueError, match="exactly one"):
        validate_synthesis(text.replace("<<<END_CODEXIA_CONTROL_V1>>>", ""))
    with pytest.raises(ValueError, match="exactly one"):
        validate_synthesis(text + "\n<<<CODEXIA_CONTROL_V1>>>")
    with pytest.raises(ValueError, match="boolean"):
        validate_synthesis(
            text.replace(
                '"objective_coverage_complete":true',
                '"objective_coverage_complete":"true"',
            )
        )


def test_escaped_cwa_trailer_recovers_exact_false_uncertainty_without_retry(
    tmp_path,
) -> None:
    f = _fixture(tmp_path)
    response = f["turn"]["response"]["text"].replace(
        '"material_uncertainty_resolved":true',
        '"material_uncertainty_resolved":false',
    )
    assert '"material_uncertainty_resolved":false' in response
    wrapped = (
        response.replace(
            RESEARCH_CONTROL_START,
            "<escape>\n" + RESEARCH_CONTROL_START,
        )
        + "\n</escape>"
    )
    f["filepath"].write_bytes(wrapped.encode("utf-8"))
    f["turn"]["response"]["text"] = wrapped
    f["sha"] = hashlib.sha256(wrapped.encode("utf-8")).hexdigest()

    before = f["store"].snapshot(f["work_id"]).revision
    outcome, report = _verify(f)
    canonical, mode = canonicalize_synthesis(wrapped)
    assert mode == "cwa-final-control-escape-unwrapped-v1"
    assert outcome.output_text == canonical
    assert outcome.output_text != wrapped
    assert report["response_sha256"] == f["sha"]
    assert report["admitted_output_sha256"] != f["sha"]
    assert report["normalization"] == mode
    assert report["objective_coverage_complete"] is True
    assert report["evidence_sufficient"] is True
    assert report["material_uncertainty_resolved"] is False
    assert validate_synthesis(outcome.output_text).material_unresolved_uncertainty is True
    assert f["store"].snapshot(f["work_id"]).revision == before


def test_escaped_cwa_trailer_rejects_ambiguous_wrappers(tmp_path) -> None:
    f = _fixture(tmp_path)
    text = f["turn"]["response"]["text"]
    wrapped = (
        text.replace(RESEARCH_CONTROL_START, "<escape>\n" + RESEARCH_CONTROL_START)
        + "\n</escape>"
    )
    with pytest.raises(ValueError, match="exactly one"):
        validate_synthesis(wrapped.replace("</escape>", "</escape>\nextra"))
    with pytest.raises(ValueError, match="standalone"):
        validate_synthesis(wrapped.replace("<escape>\n", "<escape>junk\n"))
    with pytest.raises(ValueError, match="exactly one"):
        validate_synthesis(
            wrapped.replace(
                RESEARCH_CONTROL_START,
                RESEARCH_CONTROL_START + "\n" + RESEARCH_CONTROL_START,
            )
        )


def test_cwa_readonly_probe_selects_one_completed_response() -> None:
    messages = [
        SimpleNamespace(
            role="user",
            message_id="rev-user",
            text="Revise",
            finish_reason=None,
        ),
        SimpleNamespace(
            role="assistant",
            message_id="rev-answer",
            text="Revision",
            finish_reason="stop",
        ),
        SimpleNamespace(
            role="user",
            message_id="syn-user",
            text="Synthesize",
            finish_reason=None,
        ),
        SimpleNamespace(
            role="assistant",
            message_id="interim",
            text="Working",
            finish_reason=None,
        ),
        SimpleNamespace(
            role="assistant",
            message_id="syn-answer",
            text="Final",
            finish_reason="stop",
        ),
    ]
    evidence = extract_turn(
        messages, request_message_id="syn-user", response_message_id="syn-answer"
    )
    assert evidence["request"]["text"] == "Synthesize"
    assert evidence["prior_revision"]["message_id"] == "rev-answer"
    with pytest.raises(ValueError, match="unique"):
        extract_turn(
            messages + [messages[-1]],
            request_message_id="syn-user",
            response_message_id="syn-answer",
        )
    with pytest.raises(ValueError, match="intervening"):
        extract_turn(
            messages[:3] + [messages[0]] + messages[3:],
            request_message_id="syn-user",
            response_message_id="syn-answer",
        )
