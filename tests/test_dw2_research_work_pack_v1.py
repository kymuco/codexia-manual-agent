from __future__ import annotations

import ast
import importlib.util
import json
import sys
import types
from pathlib import Path

from codexia_manual_agent.completion_core import (
    project_admitted_completion_claim,
)
from codexia_manual_agent.research_work import (
    ResearchContextMaterialPort,
    ResearchInstructionsMaterialPort,
    ResearchWorkMaterializer,
)
from codexia_manual_agent.role_core import (
    CognitionOutcome,
    CognitionPortRequest,
    project_role_runs,
)
from codexia_manual_agent.standalone_work import (
    StandaloneWorkHost,
    StandaloneWorkSelector,
    StandaloneWorkSurface,
)
from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_runtime import (
    CRITIQUE_EVIDENCE_KIND,
    EVIDENCE_SUFFICIENCY_EVIDENCE_KIND,
    INITIAL_EVIDENCE_KIND,
    NO_MATERIAL_UNCERTAINTY_EVIDENCE_KIND,
    OBJECTIVE_COVERAGE_EVIDENCE_KIND,
    RESEARCH_COMPLETION_SUMMARY,
    RESEARCH_CONTROL_END,
    RESEARCH_CONTROL_START,
    RESEARCH_PACK_ID,
    RESEARCH_PACK_VERSION,
    RESEARCH_WORKFLOW_ID,
    RESEARCH_WORKFLOW_VERSION,
    REVISION_EVIDENCE_KIND,
    STAGE_CRITIQUE,
    STAGE_INITIAL,
    STAGE_REVISION,
    STAGE_SYNTHESIS,
    SYNTHESIS_EVIDENCE_KIND,
    ResearchCompletionCriterionV1,
    ResearchRoleOutput,
    ResearchWorkflowImplementationV1,
    research_pack_binding,
    research_role_bindings,
    research_role_instructions,
    research_workflow_binding,
)

PROVIDER_REF = "codexia:research-pack-provider@1.1.0"


def _output(
    stage: str,
    content: str,
    *,
    needs_human: bool = False,
    question: str | None = None,
    reason: str | None = None,
    coverage: bool | None = None,
    sufficient: bool | None = None,
    uncertainty: bool | None = None,
) -> str:
    return ResearchRoleOutput(
        schema_version=1,
        stage=stage,
        content=content,
        needs_human=needs_human,
        human_question=question,
        human_reason=reason,
        objective_coverage=coverage,
        evidence_sufficiency=sufficient,
        material_unresolved_uncertainty=uncertainty,
    ).to_text()


class _ResearchProvider:
    def codexia_pack_distribution(self):
        return {
            "schema_version": 1,
            "pack": research_pack_binding().to_dict(),
            "workflows": [research_workflow_binding().to_dict()],
            "roles": [item.to_dict() for item in research_role_bindings()],
            "capabilities": [],
        }

    def codexia_workflow_implementation(self, raw_binding):
        assert raw_binding == research_workflow_binding().to_dict()
        return ResearchWorkflowImplementationV1()

    def codexia_completion_criterion(self, raw_binding):
        assert raw_binding == research_workflow_binding().to_dict()
        return ResearchCompletionCriterionV1()


class _PluginService:
    def __init__(self) -> None:
        self.provider = _ResearchProvider()
        self.calls = 0

    def get(self, plugin_id: str):
        self.calls += 1
        assert plugin_id == PROVIDER_REF
        return self.provider


class _ScriptedCognitionPort:
    port_id = "dw2-scripted-cognition"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls = 0
        self.contexts: list[str] = []
        self.instructions: list[str] = []

    def complete(self, request: CognitionPortRequest):
        self.calls += 1
        self.contexts.append(request.request.context)
        self.instructions.append(request.request.instructions)
        if not self.outputs:
            raise AssertionError("unexpected research cognition dispatch")
        return CognitionOutcome.succeeded(
            request.request,
            output_text=self.outputs.pop(0),
        )


def _context_payload(raw: str) -> dict:
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


def _selector() -> StandaloneWorkSelector:
    return StandaloneWorkSelector(
        provider_ref=PROVIDER_REF,
        workflow_id=RESEARCH_WORKFLOW_ID,
        workflow_version=RESEARCH_WORKFLOW_VERSION,
    )


def _activation_host(service: _PluginService):
    return lambda: StandaloneWorkHost(plugin_service=service)


def _progress_host(
    *,
    service: _PluginService,
    store: SqliteWorkStore,
    work_id: str,
    port: _ScriptedCognitionPort,
):
    return lambda: StandaloneWorkHost(
        plugin_service=service,
        cognition_port=port,
        instructions=ResearchInstructionsMaterialPort(),
        context=ResearchContextMaterialPort(store=store, work_id=work_id),
    )


def test_research_invariant_provider_and_manifest_match_exact_pack(
    monkeypatch,
) -> None:
    spec_module = types.ModuleType("invariant.spec")

    class BasePlugin:
        pass

    spec_module.BasePlugin = BasePlugin
    invariant_module = types.ModuleType("invariant")
    invariant_module.spec = spec_module
    monkeypatch.setitem(sys.modules, "invariant", invariant_module)
    monkeypatch.setitem(sys.modules, "invariant.spec", spec_module)

    root = Path(__file__).resolve().parents[1]
    plugin_path = root / "examples" / "invariant_research_pack_v1" / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "dw2_research_pack_v1",
        plugin_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provider = module.ResearchPackProviderV1()

    distribution = provider.codexia_pack_distribution()
    assert distribution["pack"] == research_pack_binding().to_dict()
    assert distribution["workflows"] == [research_workflow_binding().to_dict()]
    assert distribution["roles"] == [
        item.to_dict() for item in research_role_bindings()
    ]
    assert distribution["capabilities"] == []

    manifest = (
        root / "examples" / "invariant_research_pack_v1" / "manifest.json"
    ).read_text(encoding="utf-8")
    assert '"id": "codexia:research-pack-provider"' in manifest
    assert '"version": "1.1.0"' in manifest
    assert '"class_name": "ResearchPackProviderV1"' in manifest


def test_research_pack_distribution_is_exact() -> None:
    pack = research_pack_binding()
    assert pack.pack_id == RESEARCH_PACK_ID
    assert pack.version == RESEARCH_PACK_VERSION
    assert research_workflow_binding().workflow_id == RESEARCH_WORKFLOW_ID
    assert len(research_role_bindings()) == 4
    assert len(pack.members) == 5


def test_research_role_instructions_keep_content_free_form_and_control_minimal(
) -> None:
    researcher, critic, reviser, synthesizer = (
        research_role_instructions(binding)
        for binding in research_role_bindings()
    )

    assert "ordinary text" in researcher
    assert "Do not wrap the research content in a JSON envelope" in researcher
    assert RESEARCH_CONTROL_START in researcher
    assert RESEARCH_CONTROL_END in researcher
    assert '"needs_human":true' in researcher

    assert "ordinary text" in critic
    assert RESEARCH_CONTROL_START not in critic
    assert "ordinary text" in reviser
    assert RESEARCH_CONTROL_START not in reviser

    assert "ordinary Markdown" in synthesizer
    assert RESEARCH_CONTROL_START in synthesizer
    assert '"objective_coverage_complete":true' in synthesizer
    assert '"evidence_sufficient":true' in synthesizer
    assert '"material_uncertainty_resolved":true' in synthesizer


def test_research_role_output_accepts_rich_free_form_json_as_content() -> None:
    raw = json.dumps(
        {
            "schema": "worker-owned",
            "claims": ["SQLite has stronger transactional semantics."],
            "comparison": {"sqlite": "strong", "jsonl": "simple"},
            "recommendation": "Prefer SQLite for the durable authority log.",
        },
        sort_keys=True,
    )

    parsed = ResearchRoleOutput.parse(raw, expected_stage=STAGE_INITIAL)

    assert parsed.content == raw
    assert parsed.needs_human is False
    assert parsed.human_question is None
    assert parsed.objective_coverage is None


def test_malformed_synthesis_control_preserves_content_without_completion_signal(
) -> None:
    raw = (
        "# Final synthesis\n\nUseful research content.\n\n"
        f"{RESEARCH_CONTROL_START}\n"
        "{not valid json}\n"
        f"{RESEARCH_CONTROL_END}"
    )

    parsed = ResearchRoleOutput.parse(raw, expected_stage=STAGE_SYNTHESIS)

    assert parsed.content == "# Final synthesis\n\nUseful research content."
    assert parsed.objective_coverage is None
    assert parsed.evidence_sufficiency is None
    assert parsed.material_unresolved_uncertainty is None


def test_research_work_runs_critique_revision_materialization_and_completion(
    tmp_path,
) -> None:
    path = tmp_path / "research.sqlite"
    store = SqliteWorkStore(path)
    service = _PluginService()
    surface = StandaloneWorkSurface(store)
    started = surface.start(
        objective=(
            "Compare two candidate system designs and produce an evidence-aware "
            "recommendation with explicit uncertainty."
        ),
        selector=_selector(),
        host_factory=_activation_host(service),
        source_id="dw2-research",
    )
    work_id = started["work"]["work_id"]

    initial = _output(
        STAGE_INITIAL,
        "Initial finding: design A is simpler, but support is incomplete.",
    )
    critique = _output(
        STAGE_CRITIQUE,
        "Challenge: the initial finding underweights failure isolation in B.",
    )
    revision = _output(
        STAGE_REVISION,
        "Revision: A is simpler; B has stronger isolation; qualify the tradeoff.",
    )
    synthesis = _output(
        STAGE_SYNTHESIS,
        "# Recommendation\n\nPrefer B when failure isolation dominates; "
        "prefer A when operational simplicity dominates.",
        coverage=True,
        sufficient=True,
        uncertainty=False,
    )
    port = _ScriptedCognitionPort([initial, critique, revision, synthesis])

    progressed = surface.advance(
        work_id,
        selector=_selector(),
        host_factory=_progress_host(
            service=service,
            store=store,
            work_id=work_id,
            port=port,
        ),
        max_steps=8,
    )
    assert progressed["progression"]["status"] == "bound_exhausted"
    assert port.calls == 4
    assert len(project_role_runs(store.events(work_id))) == 4
    critic_context = _context_payload(port.contexts[1])
    reviser_context = _context_payload(port.contexts[2])
    synthesis_context = _context_payload(port.contexts[3])
    assert critic_context["prior_outputs"] == [initial]
    assert reviser_context["prior_outputs"] == [initial, critique]
    assert synthesis_context["prior_outputs"] == [
        initial,
        critique,
        revision,
    ]

    restarted = SqliteWorkStore(path)
    before_material = len(restarted.events(work_id))
    materialized = ResearchWorkMaterializer(restarted).materialize(work_id)
    first_after = len(restarted.events(work_id))
    repeated = ResearchWorkMaterializer(restarted).materialize(work_id)
    assert len(restarted.events(work_id)) == first_after
    assert first_after > before_material
    assert materialized == repeated
    assert len(materialized.artifact_refs) == 1
    artifact = materialized.artifact_refs[0]
    assert artifact.media_type == "text/markdown"
    assert artifact.content_sha256

    kinds = {item.evidence_kind for item in materialized.evidence_refs}
    assert {
        INITIAL_EVIDENCE_KIND,
        CRITIQUE_EVIDENCE_KIND,
        REVISION_EVIDENCE_KIND,
        SYNTHESIS_EVIDENCE_KIND,
        OBJECTIVE_COVERAGE_EVIDENCE_KIND,
        EVIDENCE_SUFFICIENCY_EVIDENCE_KIND,
        NO_MATERIAL_UNCERTAINTY_EVIDENCE_KIND,
    }.issubset(kinds)

    no_more_cognition = _ScriptedCognitionPort([])
    completed = StandaloneWorkSurface(restarted).advance(
        work_id,
        selector=_selector(),
        host_factory=_progress_host(
            service=service,
            store=restarted,
            work_id=work_id,
            port=no_more_cognition,
        ),
        max_steps=2,
    )
    assert completed["progression"]["status"] == "yielded"
    assert completed["progression"]["yield"]["kind"] == "completion"
    completion = completed["status"]["yield"]["completion"]
    admitted_claim = project_admitted_completion_claim(
        restarted.events(work_id),
        completion["claim_id"],
    )
    assert admitted_claim.summary == RESEARCH_COMPLETION_SUMMARY
    assert admitted_claim.claim_digest == completion["claim_digest"]
    assert no_more_cognition.calls == 0


def test_research_pack_uses_attention_only_for_explicit_human_judgment(
    tmp_path,
) -> None:
    path = tmp_path / "attention.sqlite"
    store = SqliteWorkStore(path)
    service = _PluginService()
    surface = StandaloneWorkSurface(store)
    started = surface.start(
        objective="Choose a research scope where one boundary is user-owned.",
        selector=_selector(),
        host_factory=_activation_host(service),
        source_id="dw2-attention",
    )
    work_id = started["work"]["work_id"]
    initial = _output(
        STAGE_INITIAL,
        "Two defensible scopes remain and the delegated objective cannot choose.",
        needs_human=True,
        question="Should the analysis optimize for latency or robustness?",
        reason="The preference is normative and absent from delegated context.",
    )
    port = _ScriptedCognitionPort([initial])

    first = surface.advance(
        work_id,
        selector=_selector(),
        host_factory=_progress_host(
            service=service,
            store=store,
            work_id=work_id,
            port=port,
        ),
        max_steps=3,
    )
    assert first["progression"]["status"] == "yielded"
    assert first["progression"]["yield"]["kind"] == "attention"
    attention = first["progression"]["yield"]["attention"]
    assert attention["question"].startswith("Should the analysis")
    assert port.calls == 1

    answered = surface.answer(
        work_id,
        response_text="Optimize for robustness.",
    )
    assert answered["status"]["yield"]["kind"] == "none"
    assert answered["status"]["work"]["work_id"] == work_id

    remaining = _ScriptedCognitionPort(
        [
            _output(STAGE_CRITIQUE, "Critique after robustness clarification."),
            _output(STAGE_REVISION, "Revision incorporates robustness preference."),
            _output(
                STAGE_SYNTHESIS,
                "# Robustness-focused synthesis",
                coverage=True,
                sufficient=True,
                uncertainty=False,
            ),
        ]
    )
    resumed = StandaloneWorkSurface(SqliteWorkStore(path)).advance(
        work_id,
        selector=_selector(),
        host_factory=_progress_host(
            service=service,
            store=SqliteWorkStore(path),
            work_id=work_id,
            port=remaining,
        ),
        max_steps=6,
    )
    assert resumed["progression"]["status"] == "bound_exhausted"
    assert remaining.calls == 3
    resumed_context = _context_payload(remaining.contexts[0])
    assert resumed_context["prior_outputs"] == [
        "Two defensible scopes remain and the delegated objective cannot choose."
    ]
    assert resumed_context["human_responses"] == ["Optimize for robustness."]


def test_restart_does_not_repeat_completed_research_roles(tmp_path) -> None:
    path = tmp_path / "restart.sqlite"
    store = SqliteWorkStore(path)
    service = _PluginService()
    surface = StandaloneWorkSurface(store)
    started = surface.start(
        objective="Research with a restart between critique and revision.",
        selector=_selector(),
        host_factory=_activation_host(service),
        source_id="dw2-restart",
    )
    work_id = started["work"]["work_id"]
    first_port = _ScriptedCognitionPort(
        [
            _output(STAGE_INITIAL, "Initial before restart."),
            _output(STAGE_CRITIQUE, "Critique before restart."),
        ]
    )
    first = surface.advance(
        work_id,
        selector=_selector(),
        host_factory=_progress_host(
            service=service,
            store=store,
            work_id=work_id,
            port=first_port,
        ),
        max_steps=4,
    )
    assert first["progression"]["status"] == "bound_exhausted"
    assert first_port.calls == 2

    restarted = SqliteWorkStore(path)
    second_port = _ScriptedCognitionPort(
        [
            _output(STAGE_REVISION, "Revision after restart."),
            _output(
                STAGE_SYNTHESIS,
                "# Synthesis after restart",
                coverage=True,
                sufficient=True,
                uncertainty=False,
            ),
        ]
    )
    second = StandaloneWorkSurface(restarted).advance(
        work_id,
        selector=_selector(),
        host_factory=_progress_host(
            service=service,
            store=restarted,
            work_id=work_id,
            port=second_port,
        ),
        max_steps=4,
    )
    assert second["progression"]["status"] == "bound_exhausted"
    assert second_port.calls == 2
    roles = project_role_runs(restarted.events(work_id))
    assert len(roles) == 4


def test_incomplete_synthesis_cannot_reach_completion(tmp_path) -> None:
    path = tmp_path / "incomplete.sqlite"
    store = SqliteWorkStore(path)
    service = _PluginService()
    surface = StandaloneWorkSurface(store)
    started = surface.start(
        objective="Keep material uncertainty explicit.",
        selector=_selector(),
        host_factory=_activation_host(service),
        source_id="dw2-incomplete",
    )
    work_id = started["work"]["work_id"]
    port = _ScriptedCognitionPort(
        [
            _output(STAGE_INITIAL, "Initial."),
            _output(STAGE_CRITIQUE, "Critique."),
            _output(STAGE_REVISION, "Revision."),
            _output(
                STAGE_SYNTHESIS,
                "# Draft with unresolved uncertainty",
                coverage=True,
                sufficient=False,
                uncertainty=True,
            ),
        ]
    )
    surface.advance(
        work_id,
        selector=_selector(),
        host_factory=_progress_host(
            service=service,
            store=store,
            work_id=work_id,
            port=port,
        ),
        max_steps=8,
    )
    ResearchWorkMaterializer(store).materialize(work_id)
    result = surface.advance(
        work_id,
        selector=_selector(),
        host_factory=_progress_host(
            service=service,
            store=store,
            work_id=work_id,
            port=_ScriptedCognitionPort([]),
        ),
        max_steps=1,
    )
    assert result["progression"]["status"] == "quiescent"
    assert result["status"]["yield"]["kind"] == "none"


def test_malformed_synthesis_control_materializes_artifact_but_cannot_complete(
    tmp_path,
) -> None:
    path = tmp_path / "malformed-control.sqlite"
    store = SqliteWorkStore(path)
    service = _PluginService()
    surface = StandaloneWorkSurface(store)
    started = surface.start(
        objective="Preserve useful synthesis when workflow control is malformed.",
        selector=_selector(),
        host_factory=_activation_host(service),
        source_id="dw3-malformed-control",
    )
    work_id = started["work"]["work_id"]
    synthesis_content = "# Final synthesis\n\nUseful durable research."
    malformed_synthesis = (
        f"{synthesis_content}\n\n{RESEARCH_CONTROL_START}\n"
        "{not valid json}\n"
        f"{RESEARCH_CONTROL_END}"
    )
    port = _ScriptedCognitionPort(
        [
            _output(STAGE_INITIAL, "Initial."),
            _output(STAGE_CRITIQUE, "Critique."),
            _output(STAGE_REVISION, "Revision."),
            malformed_synthesis,
        ]
    )

    progressed = surface.advance(
        work_id,
        selector=_selector(),
        host_factory=_progress_host(
            service=service,
            store=store,
            work_id=work_id,
            port=port,
        ),
        max_steps=8,
    )
    assert progressed["progression"]["status"] == "bound_exhausted"

    materialized = ResearchWorkMaterializer(store).materialize(work_id)
    assert len(materialized.artifact_refs) == 1
    assert materialized.artifact_refs[0].size_bytes == len(
        synthesis_content.encode("utf-8")
    )
    kinds = {item.evidence_kind for item in materialized.evidence_refs}
    assert SYNTHESIS_EVIDENCE_KIND in kinds
    assert OBJECTIVE_COVERAGE_EVIDENCE_KIND not in kinds
    assert EVIDENCE_SUFFICIENCY_EVIDENCE_KIND not in kinds
    assert NO_MATERIAL_UNCERTAINTY_EVIDENCE_KIND not in kinds

    result = surface.advance(
        work_id,
        selector=_selector(),
        host_factory=_progress_host(
            service=service,
            store=store,
            work_id=work_id,
            port=_ScriptedCognitionPort([]),
        ),
        max_steps=1,
    )
    assert result["progression"]["status"] == "quiescent"
    assert result["status"]["yield"]["kind"] == "none"


def test_research_pack_has_no_hde_irr_or_scheduler_ownership() -> None:
    root = Path(__file__).resolve().parents[1]
    sources = [
        root / "src" / "codexia_manual_agent" / "workflow_runtime" / "research_v1.py",
        root / "src" / "codexia_manual_agent" / "research_work" / "materialization.py",
    ]
    for path in sources:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
        assert "intent_resolution_runtime" not in source
        assert "hde_" not in source
        assert "Scheduler" not in source
