from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityNeed,
)
from codexia_manual_agent.invariant_bridge import (
    InvariantPackDistributionBridge,
    InvariantWorkflowImplementationBindingError,
    InvariantWorkflowImplementationBridge,
    InvariantWorkflowImplementationError,
    InvariantWorkflowImplementationShapeError,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WORKFLOW_COMPLETED_EVENT,
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
    project_workflow_run,
)
from codexia_manual_agent.workflow_runtime import (
    StandaloneProcessWorkflowImplementation,
    WorkflowImplementationBoundary,
    standalone_process_capability_binding,
    standalone_process_workflow_binding,
)

PROVIDER_V1 = "codexia:process-pack-provider@7.3.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_member(binding: WorkflowBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.WORKFLOW,
        semantic_id=binding.workflow_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _capability_member(binding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.CAPABILITY,
        semantic_id=binding.capability_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _distribution_record(
    *,
    workflow: WorkflowBinding | None = None,
    pack_version: str = "1.0.0",
    pack_definition: str = "standalone-process-pack-v1",
) -> dict[str, object]:
    workflow = workflow or standalone_process_workflow_binding()
    capability = standalone_process_capability_binding()
    pack = PackBinding.create(
        pack_id="codexia:standalone-process-pack",
        version=pack_version,
        definition_digest=_sha(pack_definition),
        members=(
            _workflow_member(workflow),
            _capability_member(capability),
        ),
    )
    return {
        "schema_version": 1,
        "pack": pack.to_dict(),
        "workflows": [workflow.to_dict()],
        "roles": [],
        "capabilities": [capability.to_dict()],
    }


class _Plugin:
    def __init__(
        self,
        *,
        record: dict[str, object] | None = None,
        implementation=None,
    ) -> None:
        self.record = record or _distribution_record()
        self.implementation = (
            StandaloneProcessWorkflowImplementation()
            if implementation is None
            else implementation
        )
        self.implementation_requests: list[dict[str, object]] = []

    def codexia_pack_distribution(self) -> dict[str, object]:
        return self.record

    def codexia_workflow_implementation(
        self,
        workflow_binding: dict[str, object],
    ):
        self.implementation_requests.append(workflow_binding)
        return self.implementation


class _Service:
    def __init__(self, plugins: dict[str, object]) -> None:
        self.plugins = plugins
        self.requests: list[str] = []

    def get(self, plugin_id: str):
        self.requests.append(plugin_id)
        return self.plugins[plugin_id]


def _started(
    store: SqliteWorkStore,
    *,
    distribution,
    source_id: str = "g2.10",
):
    workflow_binding = distribution.workflows[0]
    work = Work.create(
        objective="Resolve one exact distributed Workflow implementation",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    run = WorkflowRun.create(
        snapshot=initial,
        binding=workflow_binding,
    )
    workflow = WorkflowAdmission(store).admit_start(run)
    pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work_id),
            pack=distribution.pack,
        )
    )
    return work, workflow, pin


def test_exact_provider_resolves_exact_implementation_without_work_mutation(
    tmp_path,
) -> None:
    plugin = _Plugin()
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)
    before = store.events(workflow.run.work_id)
    service.requests.clear()

    resolved = InvariantWorkflowImplementationBridge(service).resolve(
        provider_ref=PROVIDER_V1,
        workflow=workflow,
        pack_binding=pin,
    )

    assert resolved.provider_ref == PROVIDER_V1
    assert resolved.workflow_binding == workflow.run.binding
    assert resolved.pack_binding_digest == pin.pack.binding_digest
    assert isinstance(
        resolved.implementation,
        StandaloneProcessWorkflowImplementation,
    )
    assert store.events(workflow.run.work_id) == before
    assert service.requests == [PROVIDER_V1, PROVIDER_V1]
    assert plugin.implementation_requests == [workflow.run.binding.to_dict()]


def test_resolution_does_not_call_propose(tmp_path) -> None:
    class CountingImplementation:
        binding = standalone_process_workflow_binding()

        def __init__(self) -> None:
            self.calls = 0

        def propose(self, context):
            self.calls += 1
            return None

    implementation = CountingImplementation()
    plugin = _Plugin(implementation=implementation)
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)

    InvariantWorkflowImplementationBridge(service).resolve(
        provider_ref=PROVIDER_V1,
        workflow=workflow,
        pack_binding=pin,
    )

    assert implementation.calls == 0


def test_resolved_implementation_can_enter_g2_9_boundary_without_admission(
    tmp_path,
) -> None:
    plugin = _Plugin()
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)
    resolved = InvariantWorkflowImplementationBridge(service).resolve(
        provider_ref=PROVIDER_V1,
        workflow=workflow,
        pack_binding=pin,
    )
    before = store.events(workflow.run.work_id)

    proposal = WorkflowImplementationBoundary().prepare(
        resolved.implementation,
        context=_step_context(store, workflow, pin),
    )

    assert isinstance(proposal, CapabilityNeed)
    assert store.events(workflow.run.work_id) == before


def _step_context(store, workflow, pin):
    from codexia_manual_agent.capability_core import project_capability_needs
    from codexia_manual_agent.workflow_runtime import WorkflowStepContext

    events = store.events(workflow.run.work_id)
    capabilities = tuple(
        item
        for item in project_capability_needs(events)
        if item.need.workflow_run_id == workflow.run.workflow_run_id
    )
    return WorkflowStepContext(
        work=store.snapshot(workflow.run.work_id),
        workflow=project_workflow_run(
            events,
            workflow.run.workflow_run_id,
        ),
        pack_binding=pin,
        capabilities=capabilities,
    )


def test_g2_10_resolution_still_requires_explicit_capability_admission(
    tmp_path,
) -> None:
    plugin = _Plugin()
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)
    resolved = InvariantWorkflowImplementationBridge(service).resolve(
        provider_ref=PROVIDER_V1,
        workflow=workflow,
        pack_binding=pin,
    )
    proposal = WorkflowImplementationBoundary().prepare(
        resolved.implementation,
        _step_context(store, workflow, pin),
    )
    assert isinstance(proposal, CapabilityNeed)
    before = store.events(workflow.run.work_id)

    pending = CapabilityAdmission(store).admit_need(proposal)

    assert pending.need == proposal
    assert len(store.events(workflow.run.work_id)) == len(before) + 1


@pytest.mark.parametrize(
    "provider_ref",
    [
        "codexia:process-pack-provider",
        "codexia:process-pack-provider@latest",
        " codexia:process-pack-provider@7.3.0",
    ],
)
def test_resolver_inherits_exact_provider_ref_requirement(
    tmp_path,
    provider_ref: str,
) -> None:
    plugin = _Plugin()
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)

    with pytest.raises(InvariantWorkflowImplementationError):
        InvariantWorkflowImplementationBridge(service).resolve(
            provider_ref=provider_ref,
            workflow=workflow,
            pack_binding=pin,
        )


def test_provider_with_different_pack_is_rejected_for_running_workflow(
    tmp_path,
) -> None:
    original = _Plugin()
    upgraded_ref = "codexia:process-pack-provider@8.0.0"
    upgraded = _Plugin(
        record=_distribution_record(
            pack_version="2.0.0",
            pack_definition="pack-v2",
        )
    )
    service = _Service(
        {
            PROVIDER_V1: original,
            upgraded_ref: upgraded,
        }
    )
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)

    with pytest.raises(InvariantWorkflowImplementationBindingError):
        InvariantWorkflowImplementationBridge(service).resolve(
            provider_ref=upgraded_ref,
            workflow=workflow,
            pack_binding=pin,
        )


def test_new_technical_build_may_supply_exact_same_semantics(tmp_path) -> None:
    upgraded_ref = "codexia:process-pack-provider@8.0.0"
    old_plugin = _Plugin()
    new_plugin = _Plugin()
    service = _Service(
        {
            PROVIDER_V1: old_plugin,
            upgraded_ref: new_plugin,
        }
    )
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)

    resolved = InvariantWorkflowImplementationBridge(service).resolve(
        provider_ref=upgraded_ref,
        workflow=workflow,
        pack_binding=pin,
    )

    assert resolved.provider_ref == upgraded_ref
    assert resolved.workflow_binding == workflow.run.binding
    assert resolved.pack_binding_digest == pin.pack.binding_digest


def test_same_workflow_id_version_with_changed_semantic_digest_fails_closed(
    tmp_path,
) -> None:
    changed = WorkflowBinding.create(
        workflow_id="codexia:standalone-process",
        version="1.0.0",
        definition_digest=_sha("changed-implementation-semantics"),
    )
    changed_ref = "codexia:process-pack-provider@9.0.0"
    service = _Service(
        {
            PROVIDER_V1: _Plugin(),
            changed_ref: _Plugin(
                record=_distribution_record(
                    workflow=changed,
                    pack_version="2.0.0",
                    pack_definition="changed-pack",
                ),
                implementation=_ImplementationFor(changed),
            ),
        }
    )
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)

    with pytest.raises(InvariantWorkflowImplementationBindingError):
        InvariantWorkflowImplementationBridge(service).resolve(
            provider_ref=changed_ref,
            workflow=workflow,
            pack_binding=pin,
        )


class _ImplementationFor:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding

    def propose(self, context):
        return None


def test_provider_returning_class_instead_of_instance_is_rejected(tmp_path) -> None:
    plugin = _Plugin(implementation=StandaloneProcessWorkflowImplementation)
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)

    with pytest.raises(InvariantWorkflowImplementationShapeError):
        InvariantWorkflowImplementationBridge(service).resolve(
            provider_ref=PROVIDER_V1,
            workflow=workflow,
            pack_binding=pin,
        )


def test_provider_returning_arbitrary_object_is_rejected(tmp_path) -> None:
    plugin = _Plugin(implementation=object())
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)

    with pytest.raises(InvariantWorkflowImplementationShapeError):
        InvariantWorkflowImplementationBridge(service).resolve(
            provider_ref=PROVIDER_V1,
            workflow=workflow,
            pack_binding=pin,
        )


def test_returned_implementation_must_match_exact_workflow_binding(tmp_path) -> None:
    changed = WorkflowBinding.create(
        workflow_id="codexia:standalone-process",
        version="1.0.0",
        definition_digest=_sha("wrong-binding"),
    )
    plugin = _Plugin(implementation=_ImplementationFor(changed))
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)

    with pytest.raises(InvariantWorkflowImplementationBindingError):
        InvariantWorkflowImplementationBridge(service).resolve(
            provider_ref=PROVIDER_V1,
            workflow=workflow,
            pack_binding=pin,
        )


def test_provider_without_implementation_export_is_rejected(tmp_path) -> None:
    class DistributionOnlyPlugin:
        def codexia_pack_distribution(self):
            return _distribution_record()

    plugin = DistributionOnlyPlugin()
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)

    with pytest.raises(InvariantWorkflowImplementationShapeError):
        InvariantWorkflowImplementationBridge(service).resolve(
            provider_ref=PROVIDER_V1,
            workflow=workflow,
            pack_binding=pin,
        )


def test_terminal_workflow_cannot_resolve_new_implementation(tmp_path) -> None:
    plugin = _Plugin()
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)
    completion = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=store.snapshot(workflow.run.work_id),
        event_kind=WORKFLOW_COMPLETED_EVENT,
    )
    completed = WorkflowAdmission(store).admit_candidate(completion)

    with pytest.raises(InvariantWorkflowImplementationError):
        InvariantWorkflowImplementationBridge(service).resolve(
            provider_ref=PROVIDER_V1,
            workflow=completed,
            pack_binding=pin,
        )


def test_pack_binding_for_another_workflow_is_rejected(tmp_path) -> None:
    plugin = _Plugin()
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, first_pin = _started(
        store,
        distribution=distribution,
        source_id="first",
    )
    _, second, _ = _started(
        store,
        distribution=distribution,
        source_id="second",
    )

    with pytest.raises(InvariantWorkflowImplementationBindingError):
        InvariantWorkflowImplementationBridge(service).resolve(
            provider_ref=PROVIDER_V1,
            workflow=second,
            pack_binding=first_pin,
        )


def test_provider_ref_is_not_persisted_into_semantic_pack_pin(tmp_path) -> None:
    plugin = _Plugin()
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pin = _started(store, distribution=distribution)

    record = pin.to_dict()

    assert "provider_ref" not in record
    assert PROVIDER_V1 not in str(record)


def test_actual_example_plugin_resolves_g2_9_implementation(monkeypatch, tmp_path) -> None:
    class StubBasePlugin:
        pass

    invariant_module = types.ModuleType("invariant")
    spec_module = types.ModuleType("invariant.spec")
    spec_module.BasePlugin = StubBasePlugin
    invariant_module.spec = spec_module
    monkeypatch.setitem(sys.modules, "invariant", invariant_module)
    monkeypatch.setitem(sys.modules, "invariant.spec", spec_module)

    root = Path(__file__).resolve().parents[1]
    plugin_path = root / "examples" / "invariant_process_pack" / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "g2_10_example_process_pack",
        plugin_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    plugin = module.ProcessPackProvider()
    service = _Service({PROVIDER_V1: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(PROVIDER_V1)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store, distribution=distribution)

    resolved = InvariantWorkflowImplementationBridge(service).resolve(
        provider_ref=PROVIDER_V1,
        workflow=workflow,
        pack_binding=pin,
    )

    assert isinstance(
        resolved.implementation,
        StandaloneProcessWorkflowImplementation,
    )
    assert resolved.implementation.binding == workflow.run.binding


def test_actual_example_plugin_rejects_non_exact_workflow_request(monkeypatch) -> None:
    class StubBasePlugin:
        pass

    invariant_module = types.ModuleType("invariant")
    spec_module = types.ModuleType("invariant.spec")
    spec_module.BasePlugin = StubBasePlugin
    invariant_module.spec = spec_module
    monkeypatch.setitem(sys.modules, "invariant", invariant_module)
    monkeypatch.setitem(sys.modules, "invariant.spec", spec_module)

    root = Path(__file__).resolve().parents[1]
    plugin_path = root / "examples" / "invariant_process_pack" / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "g2_10_example_process_pack_negative",
        plugin_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    changed = standalone_process_workflow_binding().to_dict()
    changed["version"] = "2.0.0"

    with pytest.raises(ValueError):
        module.ProcessPackProvider().codexia_workflow_implementation(changed)


def test_resolution_bridge_has_no_store_admission_executor_or_scheduler_imports() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "invariant_bridge" / "workflow_implementation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)

    forbidden = {
        "WorkStore",
        "WorkflowAdmission",
        "RoleAdmission",
        "CapabilityAdmission",
        "ProcessExecutor",
        "CapabilityHostBridge",
        "CognitionPort",
        "Scheduler",
    }
    assert forbidden.isdisjoint(imported_names)
    assert not any(
        module.endswith((".admission", ".authority", ".execution"))
        for module in imported_modules
    )
