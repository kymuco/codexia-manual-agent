from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityNeed,
    CapabilityOutcome,
    CapabilityNeedState,
)
from codexia_manual_agent.invariant_bridge import (
    InvariantPackDistributionBridge,
    InvariantWorkflowImplementationBridge,
    ResolvedWorkflowImplementation,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WORKFLOW_COMPLETED_EVENT,
    WorkflowAdmission,
    WorkflowCandidate,
    WorkflowRun,
    WorkflowRunState,
    project_workflow_run,
)
from codexia_manual_agent.workflow_orchestration import (
    WorkflowStepPackRequiredError,
    WorkflowStepReadConflictError,
    WorkflowStepService,
)
from codexia_manual_agent.workflow_runtime import (
    StandaloneProcessWorkflowImplementation,
    WorkflowImplementationStateError,
    standalone_process_capability_binding,
)

PROVIDER_REF = "codexia:process-pack-provider@7.3.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Service:
    def __init__(self, plugins: dict[str, object]) -> None:
        self._plugins = plugins

    def get(self, plugin_id: str):
        return self._plugins[plugin_id]


def _load_example_plugin(monkeypatch):
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
        "g2_11_example_process_pack",
        plugin_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ProcessPackProvider()


def _distribution_and_resolver(monkeypatch):
    plugin = _load_example_plugin(monkeypatch)
    service = _Service({PROVIDER_REF: plugin})
    distribution = InvariantPackDistributionBridge(service).resolve(
        PROVIDER_REF
    )
    resolver = InvariantWorkflowImplementationBridge(service)
    return distribution, resolver


def _start_pinned(
    store: SqliteWorkStore,
    distribution,
    *,
    source_id: str = "g2.11",
):
    work = Work.create(
        objective="Compute one bounded Workflow step",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    run = WorkflowRun.create(
        snapshot=initial,
        binding=distribution.workflows[0],
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


def _stepper(store, resolver) -> WorkflowStepService:
    return WorkflowStepService(store=store, resolver=resolver)


def test_step_recovers_state_and_prepares_one_need_without_mutation(
    monkeypatch,
    tmp_path,
) -> None:
    distribution, resolver = _distribution_and_resolver(monkeypatch)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, workflow, pin = _start_pinned(store, distribution)
    before = store.events(work.work_id)

    result = _stepper(store, resolver).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )

    assert isinstance(result.proposal, CapabilityNeed)
    assert result.work_id == work.work_id
    assert result.workflow_run_id == workflow.run.workflow_run_id
    assert result.work_revision == len(before)
    assert result.work_event_digest == before[-1].event_digest
    assert result.pack_binding_digest == pin.pack.binding_digest
    assert (
        result.workflow_binding_digest
        == workflow.run.binding.binding_digest
    )
    assert result.provider_ref == PROVIDER_REF
    assert store.events(work.work_id) == before


def test_repeated_step_without_admission_never_creates_durable_truth(
    monkeypatch,
    tmp_path,
) -> None:
    distribution, resolver = _distribution_and_resolver(monkeypatch)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, workflow, _ = _start_pinned(store, distribution)
    stepper = _stepper(store, resolver)
    before = store.events(work.work_id)

    first = stepper.step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )
    second = stepper.step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )

    assert isinstance(first.proposal, CapabilityNeed)
    assert isinstance(second.proposal, CapabilityNeed)
    assert store.events(work.work_id) == before


def test_pending_need_yields_no_new_proposal(monkeypatch, tmp_path) -> None:
    distribution, resolver = _distribution_and_resolver(monkeypatch)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, workflow, _ = _start_pinned(store, distribution)
    stepper = _stepper(store, resolver)
    first = stepper.step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )
    assert isinstance(first.proposal, CapabilityNeed)
    pending = CapabilityAdmission(store).admit_need(first.proposal)
    before = store.events(work.work_id)

    second = stepper.step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )

    assert pending.state is CapabilityNeedState.PENDING
    assert second.proposal is None
    assert store.events(work.work_id) == before


def test_succeeded_need_prepares_completion_but_does_not_admit_it(
    monkeypatch,
    tmp_path,
) -> None:
    distribution, resolver = _distribution_and_resolver(monkeypatch)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, workflow, _ = _start_pinned(store, distribution)
    stepper = _stepper(store, resolver)
    first = stepper.step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )
    assert isinstance(first.proposal, CapabilityNeed)
    pending = CapabilityAdmission(store).admit_need(first.proposal)
    CapabilityAdmission(store).admit_outcome(
        CapabilityOutcome.succeeded(
            pending,
            attempt_id="g2.11-attempt",
            attempt_digest=_sha("g2.11-attempt"),
        )
    )
    before = store.events(work.work_id)

    result = stepper.step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )

    assert isinstance(result.proposal, WorkflowCandidate)
    assert result.proposal.event.kind == WORKFLOW_COMPLETED_EVENT
    assert store.events(work.work_id) == before
    assert (
        project_workflow_run(
            before,
            workflow.run.workflow_run_id,
        ).state
        is WorkflowRunState.ACTIVE
    )

    completed = WorkflowAdmission(store).admit_candidate(result.proposal)
    assert completed.state is WorkflowRunState.COMPLETED


def test_step_requires_durable_pack_pin(monkeypatch, tmp_path) -> None:
    distribution, resolver = _distribution_and_resolver(monkeypatch)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work = Work.create(
        objective="Unpacked workflow",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="unpacked",
            payload_digest=_sha("unpacked"),
        ),
    )
    initial = store.create(work)
    run = WorkflowRun.create(
        snapshot=initial,
        binding=distribution.workflows[0],
    )
    workflow = WorkflowAdmission(store).admit_start(run)

    with pytest.raises(WorkflowStepPackRequiredError):
        _stepper(store, resolver).step(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
        )


def test_inconsistent_recovery_read_fails_closed(monkeypatch, tmp_path) -> None:
    distribution, resolver = _distribution_and_resolver(monkeypatch)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, workflow, _ = _start_pinned(store, distribution)

    class ConflictingReadStore:
        def events(self, work_id: str):
            return store.events(work_id)

        def snapshot(self, work_id: str):
            snapshot = store.snapshot(work_id)
            return replace(
                snapshot,
                revision=snapshot.revision + 1,
            )

    with pytest.raises(WorkflowStepReadConflictError):
        WorkflowStepService(
            store=ConflictingReadStore(),
            resolver=resolver,
        ).step(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
        )


def test_step_accepts_store_surface_without_append(monkeypatch, tmp_path) -> None:
    distribution, resolver = _distribution_and_resolver(monkeypatch)
    backing = SqliteWorkStore(tmp_path / "work.sqlite")
    work, workflow, _ = _start_pinned(backing, distribution)

    class ReadOnlyStore:
        def events(self, work_id: str):
            return backing.events(work_id)

        def snapshot(self, work_id: str):
            return backing.snapshot(work_id)

    result = WorkflowStepService(
        store=ReadOnlyStore(),
        resolver=resolver,
    ).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )

    assert isinstance(result.proposal, CapabilityNeed)


def test_post_step_concurrency_is_rejected_by_existing_admission_cas(
    monkeypatch,
    tmp_path,
) -> None:
    distribution, resolver = _distribution_and_resolver(monkeypatch)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, workflow, _ = _start_pinned(store, distribution)
    result = _stepper(store, resolver).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )
    assert isinstance(result.proposal, CapabilityNeed)

    current = store.snapshot(work.work_id)
    unrelated = current.next_event(
        kind="work.external-observation",
        payload={"source": "concurrent"},
    )
    store.append(
        work.work_id,
        expected_revision=current.revision,
        event=unrelated,
    )

    with pytest.raises(WorkConcurrencyError):
        CapabilityAdmission(store).admit_need(result.proposal)


def test_step_filters_capability_state_to_requested_workflow(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    capability = standalone_process_capability_binding()

    from codexia_manual_agent.pack_core import (
        PackBinding,
        PackMemberBinding,
        PackMemberKind,
    )
    from codexia_manual_agent.workflow_runtime import (
        standalone_process_workflow_binding,
    )

    workflow_binding = standalone_process_workflow_binding()
    pack = PackBinding.create(
        pack_id="codexia:standalone-process-pack",
        version="1.0.0",
        definition_digest=_sha("standalone-process-pack-v1"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow_binding.workflow_id,
                version=workflow_binding.version,
                binding_digest=workflow_binding.binding_digest,
            ),
            PackMemberBinding.create(
                kind=PackMemberKind.CAPABILITY,
                semantic_id=capability.capability_id,
                version=capability.version,
                binding_digest=capability.binding_digest,
            ),
        ),
    )
    work = Work.create(
        objective="Two workflows share one Work",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="two-workflows",
            payload_digest=_sha("two-workflows"),
        ),
    )
    initial = store.create(work)

    first_run = WorkflowRun.create(
        snapshot=initial,
        binding=workflow_binding,
    )
    first = WorkflowAdmission(store).admit_start(first_run)
    first_pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=first,
            snapshot=store.snapshot(work.work_id),
            pack=pack,
        )
    )

    second_run = WorkflowRun.create(
        snapshot=store.snapshot(work.work_id),
        binding=workflow_binding,
    )
    second = WorkflowAdmission(store).admit_start(second_run)
    second_pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=second,
            snapshot=store.snapshot(work.work_id),
            pack=pack,
        )
    )

    first_need = CapabilityNeed.create(
        workflow=first,
        snapshot=store.snapshot(work.work_id),
        binding=capability,
        operation="run",
        parameters={
            "argv": ["python", "-V"],
            "cwd_ref": "workspace",
            "cwd": ".",
            "limits": {
                "timeout_seconds": 30.0,
                "max_stdout_bytes": 65_536,
                "max_stderr_bytes": 65_536,
            },
        },
    )
    CapabilityAdmission(store).admit_need(first_need)

    second_need = CapabilityNeed.create(
        workflow=second,
        snapshot=store.snapshot(work.work_id),
        binding=capability,
        operation="run",
        parameters=first_need.to_dict()["parameters"],
    )
    CapabilityAdmission(store).admit_need(second_need)

    class CapturingImplementation:
        binding = workflow_binding

        def __init__(self) -> None:
            self.seen_need_ids: tuple[str, ...] = ()

        def propose(self, context):
            self.seen_need_ids = tuple(
                item.need.need_id for item in context.capabilities
            )

    implementation = CapturingImplementation()

    class Resolver:
        def resolve(self, *, provider_ref, workflow, pack_binding):
            return ResolvedWorkflowImplementation(
                provider_ref=provider_ref,
                workflow_binding=workflow.run.binding,
                pack_binding_digest=pack_binding.pack.binding_digest,
                implementation=implementation,
            )

    result = WorkflowStepService(
        store=store,
        resolver=Resolver(),
    ).step(
        work_id=work.work_id,
        workflow_run_id=first.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )

    assert result.proposal is None
    assert implementation.seen_need_ids == (first_need.need_id,)
    assert second_need.need_id not in implementation.seen_need_ids
    assert first_pin.workflow_run_id != second_pin.workflow_run_id


def test_terminal_workflow_cannot_be_stepped(monkeypatch, tmp_path) -> None:
    distribution, resolver = _distribution_and_resolver(monkeypatch)
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, workflow, _ = _start_pinned(store, distribution)
    completion = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=store.snapshot(work.work_id),
        event_kind=WORKFLOW_COMPLETED_EVENT,
    )
    WorkflowAdmission(store).admit_candidate(completion)

    with pytest.raises(WorkflowImplementationStateError):
        _stepper(store, resolver).step(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
        )


def test_step_source_has_no_admission_append_host_executor_or_loop_surface() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "workflow_orchestration" / "step.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

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
        "WorkflowAdmission",
        "RoleAdmission",
        "CapabilityAdmission",
        "CapabilityHostBridge",
        "ProcessExecutor",
        "CognitionPort",
        "Scheduler",
    }
    assert forbidden.isdisjoint(imported_names)
    assert ".append(" not in source
    assert "while " not in source
    assert not any(
        module.endswith((".admission", ".authority", ".execution"))
        for module in imported_modules
    )
