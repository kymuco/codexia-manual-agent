from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from uuid import uuid4

import pytest

from codexia_manual_agent.capability_core import project_capability_needs
from codexia_manual_agent.completion_core import (
    CompletionClaim,
    CompletionCriterionContext,
    project_work_completion,
)
from codexia_manual_agent.evidence_core import EvidenceAdmission, EvidenceRef
from codexia_manual_agent.pack_core import (
    PackBinding,
    project_workflow_pack_binding,
)
from codexia_manual_agent.standalone_host import (
    StandaloneProcessWorkBindingError,
    StandaloneProcessWorkIncompleteError,
    StandaloneProcessWorkService,
)
from codexia_manual_agent.work_core import SqliteWorkStore, WorkState
from codexia_manual_agent.workflow_core import project_workflow_runs
from codexia_manual_agent.workflow_runtime import (
    PROCESS_OUTCOME_EVIDENCE_KIND,
    StandaloneProcessCompletionCriterionV2,
    standalone_process_capability_binding,
    standalone_process_v2_capability_binding,
    standalone_process_v2_workflow_binding,
    standalone_process_workflow_binding,
)

PROVIDER_REF = "codexia:process-pack-provider@8.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _PluginService:
    def __init__(self, plugin) -> None:
        self._plugin = plugin
        self.requests: list[str] = []

    def get(self, plugin_id: str):
        self.requests.append(plugin_id)
        if plugin_id != PROVIDER_REF:
            raise KeyError(plugin_id)
        return self._plugin


def _load_plugin(monkeypatch):
    class StubBasePlugin:
        pass

    invariant_module = types.ModuleType("invariant")
    spec_module = types.ModuleType("invariant.spec")
    spec_module.BasePlugin = StubBasePlugin
    invariant_module.spec = spec_module
    monkeypatch.setitem(sys.modules, "invariant", invariant_module)
    monkeypatch.setitem(sys.modules, "invariant.spec", spec_module)

    root = Path(__file__).resolve().parents[1]
    plugin_path = root / "examples" / "invariant_process_pack_v2" / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "sv1_process_pack_v2",
        plugin_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ProcessPackProviderV2()


def test_sv1_versions_process_semantics_without_rewriting_g2_v1() -> None:
    old_workflow = standalone_process_workflow_binding()
    new_workflow = standalone_process_v2_workflow_binding()

    assert old_workflow.version == "1.0.0"
    assert new_workflow.version == "2.0.0"
    assert new_workflow.binding_digest != old_workflow.binding_digest
    assert (
        standalone_process_v2_capability_binding()
        == standalone_process_capability_binding()
    )


def test_sv1_process_pack_v2_manifest_is_exact() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (
            root
            / "examples"
            / "invariant_process_pack_v2"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert manifest == {
        "manifest_version": 1,
        "id": "codexia:process-pack-provider",
        "version": "8.0.0",
        "source": {
            "code_path": "plugin.py",
            "class_name": "ProcessPackProviderV2",
        },
    }


def test_sv1_real_standalone_process_reaches_work_completion(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    service = _PluginService(plugin)
    store = SqliteWorkStore(tmp_path / "sv1.sqlite")

    result = StandaloneProcessWorkService(
        store=store,
        plugin_service=service,
        provider_ref=PROVIDER_REF,
        workspace=tmp_path,
    ).run(
        objective="Run the standalone process vertical proof.",
        source_namespace="standalone.api",
        source_id="sv1-success",
        payload_digest=_sha("sv1-success-payload"),
        approved=True,
        actor="sv1-test-human",
        reason="exercise the first standalone Gen2 vertical",
    )

    assert result.snapshot.state is WorkState.COMPLETED
    assert result.snapshot.terminal_event_id == result.completion.completion_id
    assert result.capability.outcome is not None
    assert result.capability.outcome.observation["authorization"]["source"] == "human"
    assert result.capability.outcome.observation["execution"]["started"] is True
    assert result.capability.outcome.observation["execution"]["exit_code"] == 0
    assert result.evidence.evidence_kind == PROCESS_OUTCOME_EVIDENCE_KIND
    assert result.evidence.evidence_id == result.capability.outcome.outcome_id
    assert (
        result.evidence.evidence_digest
        == result.capability.outcome.outcome_digest
    )
    assert result.claim.evidence_refs == (result.evidence,)

    events = store.events(result.snapshot.work.work_id)
    kinds = tuple(event.kind for event in events)
    assert "capability.need-declared" in kinds
    assert "capability.handoff-admitted" in kinds
    assert "capability.outcome-recorded" in kinds
    assert "evidence.ref-recorded" in kinds
    assert "completion.claim-admitted" in kinds
    assert kinds[-1] == "work.completed"
    assert "workflow.completed" not in kinds
    assert project_work_completion(events) == result.completion
    assert service.requests
    assert set(service.requests) == {PROVIDER_REF}


def test_sv1_denied_process_never_fabricates_completion(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    store = SqliteWorkStore(tmp_path / "sv1-denied.sqlite")

    with pytest.raises(StandaloneProcessWorkIncompleteError) as exc_info:
        StandaloneProcessWorkService(
            store=store,
            plugin_service=_PluginService(plugin),
            provider_ref=PROVIDER_REF,
            workspace=tmp_path,
        ).run(
            objective="Do not complete when process authority is denied.",
            source_namespace="standalone.api",
            source_id="sv1-denied",
            payload_digest=_sha("sv1-denied-payload"),
            approved=False,
            actor="sv1-test-human",
            reason="deny the process effect",
        )

    work_id = exc_info.value.work_id
    assert exc_info.value.capability_state.value == "failed"
    assert store.snapshot(work_id).state is WorkState.ACTIVE

    kinds = tuple(event.kind for event in store.events(work_id))
    assert "capability.outcome-recorded" in kinds
    assert "evidence.ref-recorded" not in kinds
    assert "completion.claim-admitted" not in kinds
    assert "work.completed" not in kinds


def test_sv1_host_composition_is_finite_and_owns_no_generic_scheduler() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "standalone_host" / "process_work.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    assert "Scheduler" not in imported_names
    assert "Queue" not in imported_names
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))


class _ChangedPackPlugin:
    def __init__(self, base) -> None:
        self._base = base

    def codexia_pack_distribution(self):
        raw = self._base.codexia_pack_distribution()
        pack = PackBinding.from_dict(raw["pack"])
        changed = PackBinding.create(
            pack_id=pack.pack_id,
            version="2.0.1",
            definition_digest=_sha("changed-sv1-pack"),
            members=pack.members,
        )
        return {**raw, "pack": changed.to_dict()}

    def codexia_workflow_implementation(self, workflow_binding):
        return self._base.codexia_workflow_implementation(workflow_binding)

    def codexia_completion_criterion(self, workflow_binding):
        return self._base.codexia_completion_criterion(workflow_binding)


def test_sv1_rejects_noncanonical_pack_before_work_creation(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _ChangedPackPlugin(_load_plugin(monkeypatch))
    store = SqliteWorkStore(tmp_path / "changed-pack.sqlite")

    with pytest.raises(
        StandaloneProcessWorkBindingError,
        match="exact standalone process v2 Pack",
    ):
        StandaloneProcessWorkService(
            store=store,
            plugin_service=_PluginService(plugin),
            provider_ref=PROVIDER_REF,
            workspace=tmp_path,
        ).run(
            objective="Reject a provider that changes Pack semantics.",
            source_namespace="standalone.api",
            source_id="sv1-changed-pack",
            payload_digest=_sha("sv1-changed-pack-payload"),
            approved=True,
            actor="sv1-test-human",
        )


def test_sv1_criterion_rejects_forged_evidence_after_failed_capability(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    store = SqliteWorkStore(tmp_path / "forged-evidence.sqlite")
    service = StandaloneProcessWorkService(
        store=store,
        plugin_service=_PluginService(plugin),
        provider_ref=PROVIDER_REF,
        workspace=tmp_path,
    )

    with pytest.raises(StandaloneProcessWorkIncompleteError) as exc_info:
        service.run(
            objective="Do not confuse forged evidence with process success.",
            source_namespace="standalone.api",
            source_id="sv1-forged-evidence",
            payload_digest=_sha("sv1-forged-evidence-payload"),
            approved=False,
            actor="sv1-test-human",
        )

    work_id = exc_info.value.work_id
    evidence_id = str(uuid4())
    forged = EvidenceRef.create(
        evidence_id=evidence_id,
        evidence_digest=_sha("invented-success-outcome"),
        evidence_kind=PROCESS_OUTCOME_EVIDENCE_KIND,
        locator=f"work-event://{work_id}/{evidence_id}",
    )
    EvidenceAdmission(store).record(
        store.snapshot(work_id),
        forged,
    )

    events = store.events(work_id)
    workflows = project_workflow_runs(events)
    assert len(workflows) == 1
    workflow = workflows[0]
    pin = project_workflow_pack_binding(
        events,
        workflow.run.workflow_run_id,
    )
    assert pin is not None
    claim = CompletionClaim.create(
        snapshot=store.snapshot(work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="Standalone process capability completed successfully.",
        evidence_refs=(forged,),
    )
    context = CompletionCriterionContext(
        claim=claim,
        work=store.snapshot(work_id),
        workflow=workflow,
        pack_binding=pin,
        capabilities=tuple(
            capability
            for capability in project_capability_needs(events)
            if capability.need.workflow_run_id == workflow.run.workflow_run_id
        ),
    )

    result = StandaloneProcessCompletionCriterionV2().evaluate(context)

    assert result.accepted is False
    assert "succeeded CapabilityOutcome" in result.reason
