from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from codexia_manual_agent.invariant_bridge import ResolvedWorkflowImplementation
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import SqliteWorkStore, Work, WorkIngressBinding
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    WorkflowProgressionService,
    WorkflowStepPreconditionError,
    WorkflowStepReadPrecondition,
    WorkflowStepService,
)

PROVIDER_REF = "codexia:g2.19-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:g2.19-workflow",
        version="1.0.0",
        definition_digest=_sha("g2.19-workflow"),
    )


def _started(store: SqliteWorkStore, *, source_id: str):
    binding = _binding()
    pack = PackBinding.create(
        pack_id="codexia:g2.19-pack",
        version="1.0.0",
        definition_digest=_sha("g2.19-pack"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=binding.workflow_id,
                version=binding.version,
                binding_digest=binding.binding_digest,
            ),
        ),
    )
    work = Work.create(
        objective="Advance exactly one bounded Workflow step",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    run = WorkflowRun.create(snapshot=initial, binding=binding)
    workflow = WorkflowAdmission(store).admit_start(run)
    PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work_id),
            pack=pack,
        )
    )
    return work, workflow


class NoteImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.calls = 0

    def propose(self, context):
        self.calls += 1
        return WorkflowCandidate.create(
            run_snapshot=context.workflow,
            work_snapshot=context.work,
            event_kind="workflow.note",
            payload={"bounded": True},
        )


class NoneImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.calls = 0

    def propose(self, _context):
        self.calls += 1
        return None


class CountingResolver:
    def __init__(self, implementation) -> None:
        self.implementation = implementation
        self.calls = 0

    def resolve(self, *, provider_ref, workflow, pack_binding):
        self.calls += 1
        return ResolvedWorkflowImplementation(
            provider_ref=provider_ref,
            workflow_binding=workflow.run.binding,
            pack_binding_digest=pack_binding.pack.binding_digest,
            implementation=self.implementation,
        )


def _precondition(store: SqliteWorkStore, work_id: str):
    return WorkflowStepReadPrecondition.from_snapshot(store.snapshot(work_id))


def test_progress_once_computes_and_admits_exactly_one_proposal(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "one.sqlite")
    work, workflow = _started(store, source_id="one")
    precondition = _precondition(store, work.work_id)
    implementation = NoteImplementation(workflow.run.binding)
    resolver = CountingResolver(implementation)
    before = store.snapshot(work.work_id).revision

    result = WorkflowProgressionService(
        store=store,
        resolver=resolver,
    ).progress_once(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=precondition,
    )

    assert result.step.proposal is not None
    assert result.admitted is not None
    assert store.snapshot(work.work_id).revision == before + 1
    assert resolver.calls == 1
    assert implementation.calls == 1


def test_none_proposal_is_bounded_noop(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "none.sqlite")
    work, workflow = _started(store, source_id="none")
    precondition = _precondition(store, work.work_id)
    implementation = NoneImplementation(workflow.run.binding)
    resolver = CountingResolver(implementation)
    before = store.events(work.work_id)

    result = WorkflowProgressionService(
        store=store,
        resolver=resolver,
    ).progress_once(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=precondition,
    )

    assert result.step.proposal is None
    assert result.admitted is None
    assert store.events(work.work_id) == before
    assert resolver.calls == 1
    assert implementation.calls == 1


def test_stale_precondition_fails_before_computation_or_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    work, workflow = _started(store, source_id="stale")
    precondition = _precondition(store, work.work_id)
    current = store.snapshot(work.work_id)
    store.append(
        work.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.external-observation",
            payload={"source": "concurrent"},
        ),
    )
    implementation = NoteImplementation(workflow.run.binding)
    resolver = CountingResolver(implementation)
    before = store.events(work.work_id)

    with pytest.raises(WorkflowStepPreconditionError):
        WorkflowProgressionService(
            store=store,
            resolver=resolver,
        ).progress_once(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
            precondition=precondition,
        )

    assert resolver.calls == 0
    assert implementation.calls == 0
    assert store.events(work.work_id) == before


def test_crash_before_admission_can_recompute_same_view_then_admit_once(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "before-admission.sqlite")
    work, workflow = _started(store, source_id="before-admission")
    precondition = _precondition(store, work.work_id)
    implementation = NoteImplementation(workflow.run.binding)
    resolver = CountingResolver(implementation)

    lost = WorkflowStepService(store=store, resolver=resolver).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=precondition,
    )
    assert lost.proposal is not None
    before = store.snapshot(work.work_id).revision

    recovered = WorkflowProgressionService(
        store=store,
        resolver=resolver,
    ).progress_once(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=precondition,
    )

    assert recovered.step.proposal is not None
    assert recovered.admitted is not None
    assert store.snapshot(work.work_id).revision == before + 1
    assert resolver.calls == 2
    assert implementation.calls == 2
    assert lost.proposal.event.event_id != recovered.step.proposal.event.event_id


def test_retry_after_successful_progression_cannot_compute_next_step(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "after-admission.sqlite")
    work, workflow = _started(store, source_id="after-admission")
    precondition = _precondition(store, work.work_id)
    implementation = NoteImplementation(workflow.run.binding)
    resolver = CountingResolver(implementation)
    service = WorkflowProgressionService(store=store, resolver=resolver)

    first = service.progress_once(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=precondition,
    )
    assert first.admitted is not None
    before_retry = store.events(work.work_id)

    with pytest.raises(WorkflowStepPreconditionError):
        service.progress_once(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
            precondition=precondition,
        )

    assert resolver.calls == 1
    assert implementation.calls == 1
    assert store.events(work.work_id) == before_retry


def test_progression_requires_explicit_read_precondition(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "required.sqlite")
    _, workflow = _started(store, source_id="required")
    implementation = NoteImplementation(workflow.run.binding)
    resolver = CountingResolver(implementation)

    with pytest.raises(TypeError):
        WorkflowProgressionService(
            store=store,
            resolver=resolver,
        ).progress_once(
            work_id=workflow.run.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
            precondition=None,  # type: ignore[arg-type]
        )

    assert resolver.calls == 0
    assert implementation.calls == 0


def test_progression_source_has_no_loop_host_provider_or_execution() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "workflow_orchestration" / "workflow_progression.py"
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
        "CapabilityHostBridge",
        "CognitionPort",
        "ModelProvider",
        "ProcessExecutor",
        "Scheduler",
    }
    assert forbidden.isdisjoint(imported_names)
    assert not any(
        module.endswith(
            (".providers", ".authority", ".execution", ".standalone_host")
        )
        for module in imported_modules
    )
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
