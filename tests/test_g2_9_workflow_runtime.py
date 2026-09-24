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
    CapabilityBinding,
    CapabilityNeed,
    CapabilityNeedSnapshot,
    CapabilityNeedState,
    CapabilityOutcome,
    project_capability_needs,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.role_core import (
    ContextProjection,
    RoleBinding,
    RoleRun,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkEvent,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WORKFLOW_COMPLETED_EVENT,
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
    WorkflowRunState,
    project_workflow_run,
)
from codexia_manual_agent.workflow_runtime import (
    StandaloneProcessWorkflowImplementation,
    WorkflowImplementationBindingError,
    WorkflowImplementationBoundary,
    WorkflowImplementationError,
    WorkflowImplementationStateError,
    WorkflowStepContext,
    standalone_process_capability_binding,
    standalone_process_workflow_binding,
)

EXAMPLE_PROVIDER_REF = "codexia:process-pack-provider@7.3.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_member(binding: WorkflowBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.WORKFLOW,
        semantic_id=binding.workflow_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _capability_member(binding: CapabilityBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.CAPABILITY,
        semantic_id=binding.capability_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _role_member(binding: RoleBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.ROLE,
        semantic_id=binding.role_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _pack(
    workflow_binding: WorkflowBinding,
    capability_binding: CapabilityBinding,
    *,
    role_binding: RoleBinding | None = None,
) -> PackBinding:
    members = [
        _workflow_member(workflow_binding),
        _capability_member(capability_binding),
    ]
    if role_binding is not None:
        members.append(_role_member(role_binding))
    return PackBinding.create(
        pack_id="codexia:standalone-process-pack",
        version="1.0.0",
        definition_digest=_sha("standalone-process-pack-v1"),
        members=members,
    )


def _started(
    store: SqliteWorkStore,
    *,
    workflow_binding: WorkflowBinding | None = None,
    capability_binding: CapabilityBinding | None = None,
    role_binding: RoleBinding | None = None,
    source_id: str = "g2.9",
):
    workflow_binding = (
        workflow_binding or standalone_process_workflow_binding()
    )
    capability_binding = (
        capability_binding or standalone_process_capability_binding()
    )
    work = Work.create(
        objective="Run the exact standalone process workflow",
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
            pack=_pack(
                workflow_binding,
                capability_binding,
                role_binding=role_binding,
            ),
        )
    )
    return work, workflow, pin


def _context(
    store: SqliteWorkStore,
    workflow,
    pin,
) -> WorkflowStepContext:
    events = store.events(workflow.run.work_id)
    capabilities = tuple(
        snapshot
        for snapshot in project_capability_needs(events)
        if snapshot.need.workflow_run_id == workflow.run.workflow_run_id
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


def test_context_surface_has_no_store_authority_executor_or_scheduler(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)

    context = _context(store, workflow, pin)

    assert set(context.__dataclass_fields__) == {
        "work",
        "workflow",
        "pack_binding",
        "roles",
        "capabilities",
        "attentions",
    }


def test_exact_implementation_prepares_need_without_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    context = _context(store, workflow, pin)
    before = store.events(workflow.run.work_id)

    proposal = WorkflowImplementationBoundary().prepare(
        StandaloneProcessWorkflowImplementation(),
        context,
    )

    assert isinstance(proposal, CapabilityNeed)
    assert proposal.binding == standalone_process_capability_binding()
    assert proposal.workflow_run_id == workflow.run.workflow_run_id
    assert proposal.start_revision == context.work.revision
    assert store.events(workflow.run.work_id) == before


def test_process_need_parameters_are_fixed_implementation_semantics(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)

    proposal = WorkflowImplementationBoundary().prepare(
        StandaloneProcessWorkflowImplementation(),
        _context(store, workflow, pin),
    )

    assert isinstance(proposal, CapabilityNeed)
    assert proposal.to_dict()["parameters"] == {
        "argv": ["python", "-V"],
        "cwd_ref": "workspace",
        "cwd": ".",
        "limits": {
            "timeout_seconds": 30.0,
            "max_stdout_bytes": 65_536,
            "max_stderr_bytes": 65_536,
        },
    }


def test_need_requires_explicit_capability_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    boundary = WorkflowImplementationBoundary()
    proposal = boundary.prepare(
        StandaloneProcessWorkflowImplementation(),
        _context(store, workflow, pin),
    )
    assert isinstance(proposal, CapabilityNeed)

    pending = CapabilityAdmission(store).admit_need(proposal)

    assert pending.state is CapabilityNeedState.PENDING
    assert len(project_capability_needs(store.events(workflow.run.work_id))) == 1


def test_pending_need_yields_no_second_proposal(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    boundary = WorkflowImplementationBoundary()
    implementation = StandaloneProcessWorkflowImplementation()
    proposal = boundary.prepare(
        implementation,
        _context(store, workflow, pin),
    )
    assert isinstance(proposal, CapabilityNeed)
    CapabilityAdmission(store).admit_need(proposal)

    next_proposal = boundary.prepare(
        implementation,
        _context(store, workflow, pin),
    )

    assert next_proposal is None
    assert len(project_capability_needs(store.events(workflow.run.work_id))) == 1


def test_existing_need_with_changed_parameters_is_rejected(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    changed = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow.run.work_id),
        binding=standalone_process_capability_binding(),
        operation="run",
        parameters={
            "argv": ["python", "-c", "print('different')"],
            "cwd_ref": "workspace",
            "cwd": ".",
            "limits": {
                "timeout_seconds": 30.0,
                "max_stdout_bytes": 65_536,
                "max_stderr_bytes": 65_536,
            },
        },
    )
    CapabilityAdmission(store).admit_need(changed)

    with pytest.raises(WorkflowImplementationBindingError):
        WorkflowImplementationBoundary().prepare(
            StandaloneProcessWorkflowImplementation(),
            _context(store, workflow, pin),
        )


def test_multiple_process_needs_are_not_silently_interpreted(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    implementation = StandaloneProcessWorkflowImplementation()
    boundary = WorkflowImplementationBoundary()
    first = boundary.prepare(
        implementation,
        _context(store, workflow, pin),
    )
    assert isinstance(first, CapabilityNeed)
    CapabilityAdmission(store).admit_need(first)

    second = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow.run.work_id),
        binding=standalone_process_capability_binding(),
        operation="run",
        parameters=first.to_dict()["parameters"],
    )
    CapabilityAdmission(store).admit_need(second)

    with pytest.raises(WorkflowImplementationStateError):
        boundary.prepare(
            implementation,
            _context(store, workflow, pin),
        )


def test_succeeded_need_prepares_workflow_completion_without_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    boundary = WorkflowImplementationBoundary()
    implementation = StandaloneProcessWorkflowImplementation()
    need = boundary.prepare(
        implementation,
        _context(store, workflow, pin),
    )
    assert isinstance(need, CapabilityNeed)
    pending = CapabilityAdmission(store).admit_need(need)
    outcome = CapabilityOutcome.succeeded(
        pending,
        attempt_id="g2.9-attempt",
        attempt_digest=_sha("g2.9-attempt"),
        observation={"proof": "success"},
    )
    CapabilityAdmission(store).admit_outcome(outcome)
    before = store.events(workflow.run.work_id)

    completion = boundary.prepare(
        implementation,
        _context(store, workflow, pin),
    )

    assert isinstance(completion, WorkflowCandidate)
    assert completion.event.kind == WORKFLOW_COMPLETED_EVENT
    assert store.events(workflow.run.work_id) == before
    assert (
        project_workflow_run(
            before,
            workflow.run.workflow_run_id,
        ).state
        is WorkflowRunState.ACTIVE
    )


def test_workflow_completion_requires_explicit_workflow_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    boundary = WorkflowImplementationBoundary()
    implementation = StandaloneProcessWorkflowImplementation()
    need = boundary.prepare(
        implementation,
        _context(store, workflow, pin),
    )
    assert isinstance(need, CapabilityNeed)
    pending = CapabilityAdmission(store).admit_need(need)
    CapabilityAdmission(store).admit_outcome(
        CapabilityOutcome.succeeded(
            pending,
            attempt_id="g2.9-attempt",
            attempt_digest=_sha("g2.9-attempt"),
        )
    )
    completion = boundary.prepare(
        implementation,
        _context(store, workflow, pin),
    )
    assert isinstance(completion, WorkflowCandidate)

    admitted = WorkflowAdmission(store).admit_candidate(completion)

    assert admitted.state is WorkflowRunState.COMPLETED


@pytest.mark.parametrize(
    "status",
    ["failed", "unknown"],
)
def test_failure_policy_is_not_inferred_by_g2_9(tmp_path, status: str) -> None:
    store = SqliteWorkStore(tmp_path / f"{status}.sqlite")
    _, workflow, pin = _started(store, source_id=status)
    boundary = WorkflowImplementationBoundary()
    implementation = StandaloneProcessWorkflowImplementation()
    need = boundary.prepare(
        implementation,
        _context(store, workflow, pin),
    )
    assert isinstance(need, CapabilityNeed)
    pending = CapabilityAdmission(store).admit_need(need)
    if status == "failed":
        outcome = CapabilityOutcome.failed(
            pending,
            attempt_id=f"{status}-attempt",
            attempt_digest=_sha(f"{status}-attempt"),
            error="synthetic failure",
        )
    else:
        outcome = CapabilityOutcome.unknown(
            pending,
            attempt_id=f"{status}-attempt",
            attempt_digest=_sha(f"{status}-attempt"),
            detail="synthetic ambiguity",
        )
    CapabilityAdmission(store).admit_outcome(outcome)

    proposal = boundary.prepare(
        implementation,
        _context(store, workflow, pin),
    )

    assert proposal is None
    assert (
        project_workflow_run(
            store.events(workflow.run.work_id),
            workflow.run.workflow_run_id,
        ).state
        is WorkflowRunState.ACTIVE
    )


def test_same_workflow_id_version_with_changed_digest_is_rejected(tmp_path) -> None:
    changed = WorkflowBinding.create(
        workflow_id="codexia:standalone-process",
        version="1.0.0",
        definition_digest=_sha("changed-definition"),
    )
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(
        store,
        workflow_binding=changed,
        capability_binding=standalone_process_capability_binding(),
    )

    with pytest.raises(WorkflowImplementationBindingError):
        WorkflowImplementationBoundary().prepare(
            StandaloneProcessWorkflowImplementation(),
            _context(store, workflow, pin),
        )


def test_context_rejects_pack_pin_for_another_workflow(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, first_pin = _started(store, source_id="first")

    second_work = Work.create(
        objective="Second",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="second",
            payload_digest=_sha("second"),
        ),
    )
    second_initial = store.create(second_work)
    second_run = WorkflowRun.create(
        snapshot=second_initial,
        binding=standalone_process_workflow_binding(),
    )
    second = WorkflowAdmission(store).admit_start(second_run)

    with pytest.raises(WorkflowImplementationBindingError):
        WorkflowStepContext(
            work=store.snapshot(second.run.work_id),
            workflow=second,
            pack_binding=first_pin,
        )


def test_context_rejects_capability_state_from_another_workflow(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, first, first_pin = _started(store, source_id="first")
    boundary = WorkflowImplementationBoundary()
    first_need = boundary.prepare(
        StandaloneProcessWorkflowImplementation(),
        _context(store, first, first_pin),
    )
    assert isinstance(first_need, CapabilityNeed)
    first_pending = CapabilityAdmission(store).admit_need(first_need)

    second_work = Work.create(
        objective="Second",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="second",
            payload_digest=_sha("second"),
        ),
    )
    second_initial = store.create(second_work)
    second_run = WorkflowRun.create(
        snapshot=second_initial,
        binding=standalone_process_workflow_binding(),
    )
    second = WorkflowAdmission(store).admit_start(second_run)
    second_pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=second,
            snapshot=store.snapshot(second.run.work_id),
            pack=_pack(
                standalone_process_workflow_binding(),
                standalone_process_capability_binding(),
            ),
        )
    )

    with pytest.raises(WorkflowImplementationBindingError):
        WorkflowStepContext(
            work=store.snapshot(second.run.work_id),
            workflow=second,
            pack_binding=second_pin,
            capabilities=(first_pending,),
        )


def test_context_rejects_derived_capability_outside_pack(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    other = CapabilityBinding.create(
        capability_id="filesystem",
        version="1.0.0",
        contract_digest=_sha("filesystem"),
    )
    synthetic_need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow.run.work_id),
        binding=other,
        operation="read",
        parameters={"path": "README.md"},
    )
    synthetic_snapshot = CapabilityNeedSnapshot(
        need=synthetic_need,
        state=CapabilityNeedState.PENDING,
        outcome=None,
    )

    with pytest.raises(WorkflowImplementationBindingError):
        WorkflowStepContext(
            work=store.snapshot(workflow.run.work_id),
            workflow=workflow,
            pack_binding=pin,
            capabilities=(synthetic_snapshot,),
        )


def test_boundary_rejects_raw_work_event_from_implementation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    context = _context(store, workflow, pin)

    class BadImplementation:
        binding = standalone_process_workflow_binding()

        def propose(self, context):
            return WorkEvent.create(
                work_id=context.work.work.work_id,
                sequence=context.work.revision + 1,
                kind="bad.raw-event",
                payload={},
                previous_event_digest=context.work.last_event_digest,
            )

    with pytest.raises(WorkflowImplementationError):
        WorkflowImplementationBoundary().prepare(
            BadImplementation(),
            context,
        )


def test_boundary_rejects_role_outside_pinned_pack(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    context = _context(store, workflow, pin)
    role_binding = RoleBinding.create(
        role_id="reviewer",
        version="1.0.0",
        instructions_digest=_sha("review"),
    )

    class RoleImplementation:
        binding = standalone_process_workflow_binding()

        def propose(self, context):
            return RoleRun.create(
                workflow=context.workflow,
                snapshot=context.work,
                binding=role_binding,
                context=ContextProjection.create(
                    content_digest=_sha("context"),
                ),
            )

    with pytest.raises(WorkflowImplementationBindingError):
        WorkflowImplementationBoundary().prepare(
            RoleImplementation(),
            context,
        )


def test_boundary_rejects_capability_outside_pinned_pack(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    context = _context(store, workflow, pin)
    other = CapabilityBinding.create(
        capability_id="filesystem",
        version="1.0.0",
        contract_digest=_sha("filesystem"),
    )

    class CapabilityImplementation:
        binding = standalone_process_workflow_binding()

        def propose(self, context):
            return CapabilityNeed.create(
                workflow=context.workflow,
                snapshot=context.work,
                binding=other,
                operation="read",
                parameters={"path": "README.md"},
            )

    with pytest.raises(WorkflowImplementationBindingError):
        WorkflowImplementationBoundary().prepare(
            CapabilityImplementation(),
            context,
        )


def test_boundary_rejects_stale_candidate(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow, pin = _started(store)
    old_context = _context(store, workflow, pin)
    stale = WorkflowCandidate.create(
        run_snapshot=old_context.workflow,
        work_snapshot=old_context.work,
        event_kind="workflow.note",
        payload={"note": "stale"},
    )
    WorkflowAdmission(store).admit_candidate(
        WorkflowCandidate.create(
            run_snapshot=old_context.workflow,
            work_snapshot=old_context.work,
            event_kind="workflow.note",
            payload={"note": "advance"},
        )
    )
    current = _context(store, workflow, pin)

    class StaleImplementation:
        binding = standalone_process_workflow_binding()

        def propose(self, context):
            return stale

    with pytest.raises(WorkflowImplementationStateError):
        WorkflowImplementationBoundary().prepare(
            StaleImplementation(),
            current,
        )


def test_concrete_binding_matches_g2_8_example_distribution(monkeypatch) -> None:
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
        "g2_9_example_process_pack",
        plugin_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    distribution = module.ProcessPackProvider().codexia_pack_distribution()

    distributed_workflow = WorkflowBinding.from_dict(
        distribution["workflows"][0]
    )
    distributed_capability = CapabilityBinding.from_dict(
        distribution["capabilities"][0]
    )
    implementation = StandaloneProcessWorkflowImplementation()

    assert implementation.binding == distributed_workflow
    assert implementation.capability_binding == distributed_capability


def test_workflow_runtime_has_no_store_admission_executor_or_model_imports() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    forbidden_names = {
        "WorkStore",
        "WorkflowAdmission",
        "RoleAdmission",
        "CapabilityAdmission",
        "ProcessExecutor",
        "CognitionPort",
        "Scheduler",
    }

    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for path in (
        root / "workflow_runtime" / "boundary.py",
        root / "workflow_runtime" / "standalone_process.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    imported_modules.add(node.module)
                imported_names.update(alias.name for alias in node.names)

    assert forbidden_names.isdisjoint(imported_names)
    assert not any(
        module.endswith((".admission", ".authority", ".execution"))
        for module in imported_modules
    )
