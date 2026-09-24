from __future__ import annotations

import hashlib

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
    WorkflowProposalAdmissionService,
    WorkflowStepPreconditionError,
    WorkflowStepReadPrecondition,
    WorkflowStepService,
)

PROVIDER_REF = "codexia:g2.18-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:g2.18-workflow",
        version="1.0.0",
        definition_digest=_sha("g2.18-workflow"),
    )


def _started(store: SqliteWorkStore, *, source_id: str):
    binding = _binding()
    pack = PackBinding.create(
        pack_id="codexia:g2.18-pack",
        version="1.0.0",
        definition_digest=_sha("g2.18-pack"),
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
        objective="Bind one Workflow step to an exact caller read view",
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
            payload={"call": self.calls},
        )


class CountingResolver:
    def __init__(self, implementation: NoteImplementation) -> None:
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


def test_exact_caller_read_precondition_allows_one_step(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "exact.sqlite")
    work, workflow = _started(store, source_id="exact")
    snapshot = store.snapshot(work.work_id)
    precondition = WorkflowStepReadPrecondition.from_snapshot(snapshot)
    implementation = NoteImplementation(workflow.run.binding)
    resolver = CountingResolver(implementation)

    result = WorkflowStepService(store=store, resolver=resolver).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=precondition,
    )

    assert result.work_revision == snapshot.revision
    assert result.work_event_digest == snapshot.last_event_digest
    assert resolver.calls == 1
    assert implementation.calls == 1


def test_stale_revision_fails_before_resolver_or_implementation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    work, workflow = _started(store, source_id="stale")
    precondition = WorkflowStepReadPrecondition.from_snapshot(
        store.snapshot(work.work_id)
    )
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

    with pytest.raises(WorkflowStepPreconditionError):
        WorkflowStepService(store=store, resolver=resolver).step(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
            precondition=precondition,
        )

    assert resolver.calls == 0
    assert implementation.calls == 0


def test_same_revision_wrong_digest_fails_before_resolver(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "digest.sqlite")
    work, workflow = _started(store, source_id="digest")
    snapshot = store.snapshot(work.work_id)
    precondition = WorkflowStepReadPrecondition(
        revision=snapshot.revision,
        event_digest="0" * 64,
    )
    implementation = NoteImplementation(workflow.run.binding)
    resolver = CountingResolver(implementation)

    with pytest.raises(WorkflowStepPreconditionError):
        WorkflowStepService(store=store, resolver=resolver).step(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
            precondition=precondition,
        )

    assert resolver.calls == 0
    assert implementation.calls == 0


def test_retry_after_successful_admission_cannot_compute_next_step(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    work, workflow = _started(store, source_id="retry")
    precondition = WorkflowStepReadPrecondition.from_snapshot(
        store.snapshot(work.work_id)
    )
    implementation = NoteImplementation(workflow.run.binding)
    resolver = CountingResolver(implementation)
    stepper = WorkflowStepService(store=store, resolver=resolver)

    result = stepper.step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=precondition,
    )
    admitted = WorkflowProposalAdmissionService(store).admit(result)
    assert admitted is not None
    assert resolver.calls == 1
    assert implementation.calls == 1
    before_retry = store.events(work.work_id)

    with pytest.raises(WorkflowStepPreconditionError):
        stepper.step(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
            precondition=precondition,
        )

    assert resolver.calls == 1
    assert implementation.calls == 1
    assert store.events(work.work_id) == before_retry


def test_unbound_step_remains_available_for_read_only_callers(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "legacy.sqlite")
    work, workflow = _started(store, source_id="legacy")
    implementation = NoteImplementation(workflow.run.binding)
    resolver = CountingResolver(implementation)

    result = WorkflowStepService(store=store, resolver=resolver).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )

    assert result.proposal is not None
    assert resolver.calls == 1
    assert implementation.calls == 1
