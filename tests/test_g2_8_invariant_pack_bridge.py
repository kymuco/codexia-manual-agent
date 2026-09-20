from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityBinding,
    CapabilityNeed,
    CapabilityNeedState,
)
from codexia_manual_agent.invariant_bridge import (
    InvariantPackDistributionBridge,
    InvariantPackDistributionError,
)
from codexia_manual_agent.pack_core import (
    InvalidPackRecord,
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
    project_workflow_pack_binding,
)
from codexia_manual_agent.standalone_host import StandaloneProcessCapabilityPort
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)

EXAMPLE_PROVIDER_REF = "codexia:process-pack-provider@7.3.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_binding(
    *,
    version: str = "1.0.0",
    definition: str = "standalone-process-workflow-v1",
) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:standalone-process",
        version=version,
        definition_digest=_sha(definition),
    )


def _capability_binding(
    *,
    version: str = "1.0.0",
    contract: str = "process-run-contract-v1",
) -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id="process",
        version=version,
        contract_digest=_sha(contract),
    )


def _member_for_workflow(binding: WorkflowBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.WORKFLOW,
        semantic_id=binding.workflow_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _member_for_capability(binding: CapabilityBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.CAPABILITY,
        semantic_id=binding.capability_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _distribution_record(
    *,
    pack_version: str = "1.0.0",
    pack_definition: str = "standalone-process-pack-v1",
    workflow: WorkflowBinding | None = None,
    capability: CapabilityBinding | None = None,
) -> dict[str, object]:
    workflow = workflow or _workflow_binding()
    capability = capability or _capability_binding()
    pack = PackBinding.create(
        pack_id="codexia:standalone-process-pack",
        version=pack_version,
        definition_digest=_sha(pack_definition),
        members=(
            _member_for_workflow(workflow),
            _member_for_capability(capability),
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
    def __init__(self, record: dict[str, object]) -> None:
        self._record = record
        self.calls = 0

    def codexia_pack_distribution(self) -> dict[str, object]:
        self.calls += 1
        return self._record


class _Service:
    def __init__(self, plugins: dict[str, object]) -> None:
        self._plugins = plugins
        self.requests: list[str] = []

    def get(self, plugin_id: str):
        self.requests.append(plugin_id)
        return self._plugins[plugin_id]


def _started_workflow(
    store: SqliteWorkStore,
    workflow_binding: WorkflowBinding,
):
    work = Work.create(
        objective="Resolve one exact Pack through Invariant distribution",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="g2.8",
            payload_digest=_sha("g2.8-payload"),
        ),
    )
    initial = store.create(work)
    run = WorkflowRun.create(
        snapshot=initial,
        binding=workflow_binding,
    )
    workflow = WorkflowAdmission(store).admit_start(run)
    return work, workflow


@pytest.mark.parametrize(
    "provider_ref",
    [
        "codexia:process-pack-provider",
        "codexia:process-pack-provider@latest",
        " codexia:process-pack-provider@7.3.0",
        "codexia:process-pack-provider@",
        "codexia:process@pack-provider@7.3.0",
        "codexia:process-pack-provider@7.3.0 beta",
    ],
)
def test_bridge_requires_exact_versioned_provider_ref(provider_ref: str) -> None:
    bridge = InvariantPackDistributionBridge(_Service({}))

    with pytest.raises(InvariantPackDistributionError):
        bridge.resolve(provider_ref)


def test_bridge_uses_only_exact_service_get_reference() -> None:
    plugin = _Plugin(_distribution_record())
    service = _Service({EXAMPLE_PROVIDER_REF: plugin})

    resolved = InvariantPackDistributionBridge(service).resolve(
        EXAMPLE_PROVIDER_REF
    )

    assert service.requests == [EXAMPLE_PROVIDER_REF]
    assert plugin.calls == 1
    assert resolved.provider_ref == EXAMPLE_PROVIDER_REF


def test_plugin_version_is_independent_from_pack_version() -> None:
    plugin = _Plugin(_distribution_record(pack_version="1.0.0"))
    service = _Service({EXAMPLE_PROVIDER_REF: plugin})

    resolved = InvariantPackDistributionBridge(service).resolve(
        EXAMPLE_PROVIDER_REF
    )

    assert resolved.provider_ref.endswith("@7.3.0")
    assert resolved.pack.version == "1.0.0"


def test_distribution_rejects_extra_runtime_or_authority_fields() -> None:
    record = _distribution_record()
    record["authority"] = {"approved": True}
    plugin = _Plugin(record)
    bridge = InvariantPackDistributionBridge(
        _Service({EXAMPLE_PROVIDER_REF: plugin})
    )

    with pytest.raises(InvariantPackDistributionError):
        bridge.resolve(EXAMPLE_PROVIDER_REF)


def test_distribution_requires_exact_pack_membership() -> None:
    record = _distribution_record()
    record["capabilities"] = []
    plugin = _Plugin(record)
    bridge = InvariantPackDistributionBridge(
        _Service({EXAMPLE_PROVIDER_REF: plugin})
    )

    with pytest.raises(InvariantPackDistributionError):
        bridge.resolve(EXAMPLE_PROVIDER_REF)


def test_distribution_rejects_extra_semantic_definition() -> None:
    record = _distribution_record()
    extra = CapabilityBinding.create(
        capability_id="filesystem",
        version="1.0.0",
        contract_digest=_sha("filesystem-v1"),
    )
    record["capabilities"] = [
        *record["capabilities"],
        extra.to_dict(),
    ]
    plugin = _Plugin(record)
    bridge = InvariantPackDistributionBridge(
        _Service({EXAMPLE_PROVIDER_REF: plugin})
    )

    with pytest.raises(InvariantPackDistributionError):
        bridge.resolve(EXAMPLE_PROVIDER_REF)


def test_distribution_rejects_same_identity_changed_digest() -> None:
    record = _distribution_record()
    changed = _capability_binding(contract="changed-contract")
    record["capabilities"] = [changed.to_dict()]
    plugin = _Plugin(record)
    bridge = InvariantPackDistributionBridge(
        _Service({EXAMPLE_PROVIDER_REF: plugin})
    )

    with pytest.raises(InvariantPackDistributionError):
        bridge.resolve(EXAMPLE_PROVIDER_REF)


def test_plugin_without_distribution_export_is_rejected() -> None:
    bridge = InvariantPackDistributionBridge(
        _Service({EXAMPLE_PROVIDER_REF: object()})
    )

    with pytest.raises(InvariantPackDistributionError):
        bridge.resolve(EXAMPLE_PROVIDER_REF)


def test_plugin_activation_does_not_admit_pack(tmp_path) -> None:
    record = _distribution_record()
    workflow_binding = WorkflowBinding.from_dict(record["workflows"][0])
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store, workflow_binding)
    before = store.events(workflow.run.work_id)

    resolved = InvariantPackDistributionBridge(
        _Service({EXAMPLE_PROVIDER_REF: _Plugin(record)})
    ).resolve(EXAMPLE_PROVIDER_REF)

    assert store.events(workflow.run.work_id) == before
    assert project_workflow_pack_binding(
        store.events(workflow.run.work_id),
        workflow.run.workflow_run_id,
    ) is None
    assert resolved.pack.binding_digest


def test_pack_admission_remains_explicit_after_plugin_resolution(tmp_path) -> None:
    record = _distribution_record()
    bridge = InvariantPackDistributionBridge(
        _Service({EXAMPLE_PROVIDER_REF: _Plugin(record)})
    )
    resolved = bridge.resolve(EXAMPLE_PROVIDER_REF)
    workflow_binding = resolved.workflows[0]

    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store, workflow_binding)
    pin = PackWorkflowBinding.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow.run.work_id),
        pack=resolved.pack,
    )

    admitted = PackAdmission(store).admit_workflow_binding(pin)

    assert admitted.pack == resolved.pack


def test_distributed_process_binding_matches_g2_6_host_contract(tmp_path) -> None:
    resolved = InvariantPackDistributionBridge(
        _Service(
            {
                EXAMPLE_PROVIDER_REF: _Plugin(
                    _distribution_record()
                )
            }
        )
    ).resolve(EXAMPLE_PROVIDER_REF)

    port = StandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=resolved.capabilities[0],
        approved=False,
    )

    assert port.binding == resolved.capabilities[0]


def test_pack_availability_does_not_create_capability_authority(tmp_path) -> None:
    record = _distribution_record()
    resolved = InvariantPackDistributionBridge(
        _Service({EXAMPLE_PROVIDER_REF: _Plugin(record)})
    ).resolve(EXAMPLE_PROVIDER_REF)
    workflow_binding = resolved.workflows[0]
    capability_binding = resolved.capabilities[0]

    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store, workflow_binding)
    PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(workflow.run.work_id),
            pack=resolved.pack,
        )
    )
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow.run.work_id),
        binding=capability_binding,
        operation="run",
        parameters={
            "argv": ["python", "-V"],
            "cwd_ref": "workspace",
        },
    )

    pending = CapabilityAdmission(store).admit_need(need)

    assert pending.state is CapabilityNeedState.PENDING
    assert pending.outcome is None


def test_plugin_upgrade_does_not_silently_repin_running_workflow(tmp_path) -> None:
    first_ref = "codexia:process-pack-provider@7.3.0"
    second_ref = "codexia:process-pack-provider@8.0.0"
    first_record = _distribution_record(
        pack_version="1.0.0",
        pack_definition="pack-v1",
    )
    second_record = _distribution_record(
        pack_version="2.0.0",
        pack_definition="pack-v2",
    )
    service = _Service(
        {
            first_ref: _Plugin(first_record),
            second_ref: _Plugin(second_record),
        }
    )
    bridge = InvariantPackDistributionBridge(service)
    first = bridge.resolve(first_ref)

    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store, first.workflows[0])
    original_pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(workflow.run.work_id),
            pack=first.pack,
        )
    )
    event_count = len(store.events(workflow.run.work_id))

    second = bridge.resolve(second_ref)
    recovered = project_workflow_pack_binding(
        store.events(workflow.run.work_id),
        workflow.run.workflow_run_id,
    )

    assert second.pack.version == "2.0.0"
    assert recovered == original_pin
    assert recovered.pack.version == "1.0.0"
    assert len(store.events(workflow.run.work_id)) == event_count


def test_running_workflow_cannot_be_rebound_to_new_pack_after_upgrade(tmp_path) -> None:
    first = InvariantPackDistributionBridge(
        _Service(
            {
                EXAMPLE_PROVIDER_REF: _Plugin(
                    _distribution_record(pack_version="1.0.0")
                )
            }
        )
    ).resolve(EXAMPLE_PROVIDER_REF)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store, first.workflows[0])
    PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(workflow.run.work_id),
            pack=first.pack,
        )
    )

    upgraded = _distribution_record(
        pack_version="2.0.0",
        pack_definition="pack-v2",
    )
    new_pack = PackBinding.from_dict(upgraded["pack"])

    with pytest.raises(InvalidPackRecord):
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(workflow.run.work_id),
            pack=new_pack,
        )


def test_bridge_has_no_mandatory_invariant_import() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    bridge_source = (
        root / "invariant_bridge" / "pack_distribution.py"
    ).read_text(encoding="utf-8")

    assert "from invariant" not in bridge_source
    assert "import invariant" not in bridge_source


def test_example_manifest_pins_technical_plugin_version() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (
            root / "examples" / "invariant_process_pack" / "manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert manifest == {
        "manifest_version": 1,
        "id": "codexia:process-pack-provider",
        "version": "7.3.0",
        "source": {
            "code_path": "plugin.py",
            "class_name": "ProcessPackProvider",
        },
    }


def test_example_plugin_exports_valid_independent_distribution(
    monkeypatch,
) -> None:
    class StubBasePlugin:
        pass

    invariant_module = types.ModuleType("invariant")
    spec_module = types.ModuleType("invariant.spec")
    spec_module.BasePlugin = StubBasePlugin
    invariant_module.spec = spec_module
    monkeypatch.setitem(sys.modules, "invariant", invariant_module)
    monkeypatch.setitem(sys.modules, "invariant.spec", spec_module)

    root = Path(__file__).resolve().parents[1]
    plugin_path = (
        root / "examples" / "invariant_process_pack" / "plugin.py"
    )
    spec = importlib.util.spec_from_file_location(
        "g2_8_example_process_pack",
        plugin_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    plugin = module.ProcessPackProvider()
    service = _Service({EXAMPLE_PROVIDER_REF: plugin})
    resolved = InvariantPackDistributionBridge(service).resolve(
        EXAMPLE_PROVIDER_REF
    )

    assert resolved.pack.pack_id == "codexia:standalone-process-pack"
    assert resolved.pack.version == "1.0.0"
    assert resolved.provider_ref == EXAMPLE_PROVIDER_REF
    assert resolved.workflows[0].workflow_id == "codexia:standalone-process"
    assert resolved.capabilities[0].capability_id == "process"
