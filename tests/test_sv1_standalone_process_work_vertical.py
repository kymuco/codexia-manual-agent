from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from codexia_manual_agent.completion_core import project_work_completion
from codexia_manual_agent.standalone_host import (
    StandaloneProcessWorkIncompleteError,
    StandaloneProcessWorkService,
)
from codexia_manual_agent.work_core import SqliteWorkStore, WorkState
from codexia_manual_agent.workflow_runtime import (
    PROCESS_OUTCOME_EVIDENCE_KIND,
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
