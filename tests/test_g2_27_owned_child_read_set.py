from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from codexia_manual_agent.delegation_core import Delegation, DelegationAdmission
from codexia_manual_agent.invariant_bridge import ResolvedWorkflowImplementation
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import (
    WORK_COMPLETED_EVENT,
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkIngressBinding,
    WorkNotFoundError,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    WorkflowChildReadBinding,
    WorkflowProposalAdmissionBindingError,
    WorkflowProposalAdmissionService,
    WorkflowStepReadPrecondition,
    WorkflowStepService,
)
from codexia_manual_agent.workflow_runtime import (
    OwnedChildWorkSnapshot,
    WorkflowDelegationProposal,
)

PROVIDER_REF = "codexia:g2.27-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding(suffix: str) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=f"codexia:g2.27-{suffix}",
        version="1.0.0",
        definition_digest=_sha(f"g2.27-{suffix}"),
    )


def _start_work(store: SqliteWorkStore, *, source_id: str) -> Work:
    work = Work.create(
        objective="Observe exact owned child Work state without stale admission",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    store.create(work)
    return work


def _start_workflow(
    store: SqliteWorkStore,
    work: Work,
    *,
    suffix: str,
):
    binding = _binding(suffix)
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=store.snapshot(work.work_id),
            binding=binding,
        )
    )
    pack = PackBinding.create(
        pack_id=f"codexia:g2.27-pack-{suffix}",
        version="1.0.0",
        definition_digest=_sha(f"g2.27-pack-{suffix}"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=binding.workflow_id,
                version=binding.version,
                binding_digest=binding.binding_digest,
            ),
        ),
    )
    PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work_id),
            pack=pack,
        )
    )
    return workflow


def _delegate(
    store: SqliteWorkStore,
    parent: Work,
    *,
    objective: str,
) -> Delegation:
    return DelegationAdmission(store).admit(
        Delegation.create(
            parent=store.snapshot(parent.work_id),
            child_objective=objective,
        )
    )


def _append_child_observation(
    store: SqliteWorkStore,
    delegation: Delegation,
    *,
    label: str,
) -> None:
    child = store.snapshot(delegation.child_work.work_id)
    store.append(
        child.work.work_id,
        expected_revision=child.revision,
        event=child.next_event(
            kind="work.child-observation",
            payload={"label": label},
        ),
    )


def _complete_child(
    store: SqliteWorkStore,
    delegation: Delegation,
) -> None:
    child = store.snapshot(delegation.child_work.work_id)
    store.append(
        child.work.work_id,
        expected_revision=child.revision,
        event=child.next_event(
            kind=WORK_COMPLETED_EVENT,
            payload={"summary": "child complete"},
        ),
    )


class Resolver:
    def __init__(self, implementation) -> None:
        self.implementation = implementation

    def resolve(self, *, provider_ref, workflow, pack_binding):
        return ResolvedWorkflowImplementation(
            provider_ref=provider_ref,
            workflow_binding=workflow.run.binding,
            pack_binding_digest=pack_binding.pack.binding_digest,
            implementation=self.implementation,
        )


class CaptureImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.owned_children: tuple[OwnedChildWorkSnapshot, ...] | None = None

    def propose(self, context):
        self.owned_children = context.owned_children
        return None


class NoteImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.owned_children: tuple[OwnedChildWorkSnapshot, ...] | None = None

    def propose(self, context):
        self.owned_children = context.owned_children
        return WorkflowCandidate.create(
            run_snapshot=context.workflow,
            work_snapshot=context.work,
            event_kind="workflow.note",
            payload={
                "owned_children": [
                    {
                        "work_id": owned.child.work.work_id,
                        "state": owned.child.state.value,
                        "revision": owned.child.revision,
                    }
                    for owned in context.owned_children
                ]
            },
        )


class DelegateAgainImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding

    def propose(self, context):
        return WorkflowDelegationProposal.create(
            workflow=context.workflow,
            snapshot=context.work,
            child_objective="Second child created only from a stable first-child read",
        )


def _precondition(
    store: SqliteWorkStore,
    work_id: str,
) -> WorkflowStepReadPrecondition:
    return WorkflowStepReadPrecondition.from_snapshot(store.snapshot(work_id))


def _step(store, work, workflow, implementation):
    return WorkflowStepService(
        store=store,
        resolver=Resolver(implementation),
    ).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(store, work.work_id),
    )


def test_restart_step_observes_completed_owned_child_and_exact_read_binding(
    tmp_path,
) -> None:
    path = tmp_path / "restart.sqlite"
    store = SqliteWorkStore(path)
    work = _start_work(store, source_id="restart")
    workflow = _start_workflow(store, work, suffix="restart")
    delegation = _delegate(
        store,
        work,
        objective="Complete independently before parent continues",
    )
    _complete_child(store, delegation)

    restarted = SqliteWorkStore(path)
    capture = CaptureImplementation(workflow.run.binding)
    before_parent = restarted.events(work.work_id)
    result = _step(restarted, work, workflow, capture)

    assert result.proposal is None
    assert restarted.events(work.work_id) == before_parent
    assert capture.owned_children is not None
    assert len(capture.owned_children) == 1
    owned = capture.owned_children[0]
    assert owned.delegation == delegation
    assert owned.child.state is WorkState.COMPLETED
    assert owned.child.revision == 1
    assert len(result.child_reads) == 1
    read = result.child_reads[0]
    assert isinstance(read, WorkflowChildReadBinding)
    assert read.delegation_id == delegation.delegation_id
    assert read.delegation_digest == delegation.delegation_digest
    assert read.child == owned.child


def test_owned_child_is_work_level_input_to_another_parent_workflow(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "cross-workflow.sqlite")
    work = _start_work(store, source_id="cross-workflow")
    _start_workflow(store, work, suffix="first")
    delegation = _delegate(
        store,
        work,
        objective="Remain owned by Work rather than one WorkflowRun",
    )
    second = _start_workflow(store, work, suffix="second")
    capture = CaptureImplementation(second.run.binding)

    _step(store, work, second, capture)

    assert capture.owned_children is not None
    assert tuple(
        owned.delegation for owned in capture.owned_children
    ) == (delegation,)


def test_child_drift_after_step_rejects_parent_admission_without_mutation(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "stale-child.sqlite")
    work = _start_work(store, source_id="stale-child")
    workflow = _start_workflow(store, work, suffix="stale-child")
    delegation = _delegate(
        store,
        work,
        objective="Change after parent Workflow reads me",
    )
    implementation = NoteImplementation(workflow.run.binding)
    result = _step(store, work, workflow, implementation)
    assert isinstance(result.proposal, WorkflowCandidate)
    assert implementation.owned_children is not None
    assert implementation.owned_children[0].child.state is WorkState.ACTIVE

    _append_child_observation(store, delegation, label="concurrent child change")
    before_parent = store.events(work.work_id)

    with pytest.raises(WorkConcurrencyError, match="Stale read precondition"):
        WorkflowProposalAdmissionService(store).admit(result)

    assert store.events(work.work_id) == before_parent
    assert not any(
        event.event_id == result.proposal.event.event_id
        for event in before_parent
    )


def test_exact_retry_after_commit_survives_later_child_drift(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    work = _start_work(store, source_id="retry")
    workflow = _start_workflow(store, work, suffix="retry")
    delegation = _delegate(
        store,
        work,
        objective="Advance only after parent proposal is already canonical",
    )
    result = _step(
        store,
        work,
        workflow,
        NoteImplementation(workflow.run.binding),
    )
    service = WorkflowProposalAdmissionService(store)

    first = service.admit(result)
    _complete_child(store, delegation)
    later_child = _delegate(
        store,
        work,
        objective="A later owned child absent from the original read prefix",
    )
    before_retry = store.events(work.work_id)

    retried = service.admit(result)

    assert retried == first
    assert store.events(work.work_id) == before_retry
    assert store.snapshot(delegation.child_work.work_id).state is WorkState.COMPLETED
    assert store.snapshot(later_child.child_work.work_id).state is WorkState.ACTIVE


def test_incomplete_child_read_set_fails_before_parent_mutation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "incomplete.sqlite")
    work = _start_work(store, source_id="incomplete")
    workflow = _start_workflow(store, work, suffix="incomplete")
    _delegate(
        store,
        work,
        objective="Must be present in the exact parent read set",
    )
    result = _step(
        store,
        work,
        workflow,
        NoteImplementation(workflow.run.binding),
    )
    forged = replace(result, child_reads=())
    before_parent = store.events(work.work_id)

    with pytest.raises(
        WorkflowProposalAdmissionBindingError,
        match="child read set is incomplete",
    ):
        WorkflowProposalAdmissionService(store).admit(forged)

    assert store.events(work.work_id) == before_parent


def test_forged_child_snapshot_binding_fails_before_parent_mutation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "forged.sqlite")
    work = _start_work(store, source_id="forged")
    workflow = _start_workflow(store, work, suffix="forged")
    _delegate(
        store,
        work,
        objective="Exact child identity must survive the Workflow step",
    )
    result = _step(
        store,
        work,
        workflow,
        NoteImplementation(workflow.run.binding),
    )
    assert len(result.child_reads) == 1
    forged_read = replace(
        result.child_reads[0],
        child=store.snapshot(work.work_id),
    )
    forged = replace(result, child_reads=(forged_read,))
    before_parent = store.events(work.work_id)

    with pytest.raises(
        WorkflowProposalAdmissionBindingError,
        match="changed child Work identity",
    ):
        WorkflowProposalAdmissionService(store).admit(forged)

    assert store.events(work.work_id) == before_parent


def test_second_child_creation_is_guarded_by_existing_child_read_set(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "delegation-guard.sqlite")
    work = _start_work(store, source_id="delegation-guard")
    workflow = _start_workflow(store, work, suffix="delegation-guard")
    first = _delegate(
        store,
        work,
        objective="First child whose state guards second delegation",
    )
    result = _step(
        store,
        work,
        workflow,
        DelegateAgainImplementation(workflow.run.binding),
    )
    assert isinstance(result.proposal, WorkflowDelegationProposal)
    proposed_second_id = result.proposal.delegation.child_work.work_id

    _append_child_observation(store, first, label="invalidate second-child proposal")
    before_parent = store.events(work.work_id)

    with pytest.raises(WorkConcurrencyError, match="Stale read precondition"):
        WorkflowProposalAdmissionService(store).admit(result)

    assert store.events(work.work_id) == before_parent
    with pytest.raises(WorkNotFoundError):
        store.snapshot(proposed_second_id)


def test_g2_27_adds_no_scheduler_child_result_or_completion_policy() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "work_core" / "store.py",
        root / "workflow_runtime" / "boundary.py",
        root / "workflow_orchestration" / "step.py",
        root / "workflow_orchestration" / "proposal_admission.py",
    ]
    forbidden_names = {
        "Scheduler",
        "ChildResult",
        "WorkGraph",
        "CompletionClaim",
        "WorkCompletion",
        "AuthorizationReceipt",
        "ProcessExecutor",
    }

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_names.update(alias.name for alias in node.names)
        assert forbidden_names.isdisjoint(imported_names)
        assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
