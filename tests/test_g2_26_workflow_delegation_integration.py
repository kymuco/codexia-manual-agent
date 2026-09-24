from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from codexia_manual_agent.delegation_core import (
    Delegation,
    project_delegations,
)
from codexia_manual_agent.invariant_bridge import ResolvedWorkflowImplementation
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
    WorkConcurrencyError,
    WorkIngressBinding,
    WorkNotFoundError,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    WorkflowProgressionService,
    WorkflowProposalAdmissionBindingError,
    WorkflowProposalAdmissionService,
    WorkflowStepReadPrecondition,
    WorkflowStepService,
)
from codexia_manual_agent.workflow_runtime import (
    WorkflowDelegationProposal,
    WorkflowImplementationError,
    WorkflowStepContext,
)

PROVIDER_REF = "codexia:g2.26-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding(suffix: str) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=f"codexia:g2.26-{suffix}",
        version="1.0.0",
        definition_digest=_sha(f"g2.26-{suffix}"),
    )


def _start_work(store: SqliteWorkStore, *, source_id: str) -> Work:
    work = Work.create(
        objective="Prove typed Workflow to durable Delegation integration",
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
        pack_id=f"codexia:g2.26-pack-{suffix}",
        version="1.0.0",
        definition_digest=_sha(f"g2.26-pack-{suffix}"),
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


class DelegatingImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding

    def propose(self, context):
        return WorkflowDelegationProposal.create(
            workflow=context.workflow,
            snapshot=context.work,
            child_objective="Independently survive restart and continue this sub-objective",
        )


class RawDelegationImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding

    def propose(self, context):
        return Delegation.create(
            parent=context.work,
            child_objective="Raw relation without Workflow provenance",
        )


def _precondition(
    store: SqliteWorkStore,
    work_id: str,
) -> WorkflowStepReadPrecondition:
    return WorkflowStepReadPrecondition.from_snapshot(store.snapshot(work_id))


def _step(
    store: SqliteWorkStore,
    work: Work,
    workflow,
):
    return WorkflowStepService(
        store=store,
        resolver=Resolver(DelegatingImplementation(workflow.run.binding)),
    ).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(store, work.work_id),
    )


def test_workflow_progression_can_propose_and_atomically_admit_child_work(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "typed.sqlite")
    work = _start_work(store, source_id="typed")
    workflow = _start_workflow(store, work, suffix="typed")

    result = WorkflowProgressionService(
        store=store,
        resolver=Resolver(DelegatingImplementation(workflow.run.binding)),
    ).progress_once(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(store, work.work_id),
    )

    assert isinstance(result.step.proposal, WorkflowDelegationProposal)
    assert isinstance(result.admitted, Delegation)
    assert result.admitted == result.step.proposal.delegation
    assert project_delegations(store.events(work.work_id)) == (result.admitted,)

    child = store.snapshot(result.admitted.child_work.work_id)
    assert child.work == result.admitted.child_work
    assert child.state is WorkState.ACTIVE
    assert child.revision == 0
    assert store.events(child.work.work_id) == ()
    assert store.snapshot(work.work_id).state is WorkState.ACTIVE

    durable = result.admitted.to_dict()
    assert "workflow_run_id" not in durable
    assert "workflow_run_digest" not in durable


def test_raw_delegation_without_workflow_provenance_is_not_a_workflow_proposal(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "raw.sqlite")
    work = _start_work(store, source_id="raw")
    workflow = _start_workflow(store, work, suffix="raw")
    before = store.events(work.work_id)

    with pytest.raises(
        WorkflowImplementationError,
        match="unsupported proposal type",
    ):
        WorkflowStepService(
            store=store,
            resolver=Resolver(RawDelegationImplementation(workflow.run.binding)),
        ).step(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
            precondition=_precondition(store, work.work_id),
        )

    assert store.events(work.work_id) == before


def test_forged_workflow_provenance_fails_before_child_creation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "forged.sqlite")
    work = _start_work(store, source_id="forged")
    first = _start_workflow(store, work, suffix="first")
    second = _start_workflow(store, work, suffix="second")
    result = _step(store, work, first)
    assert isinstance(result.proposal, WorkflowDelegationProposal)

    forged_proposal = replace(
        result.proposal,
        workflow_run_id=second.run.workflow_run_id,
        workflow_run_digest=second.run.run_digest,
    )
    forged = replace(result, proposal=forged_proposal)
    child_work_id = forged_proposal.delegation.child_work.work_id
    before = store.events(work.work_id)

    with pytest.raises(
        WorkflowProposalAdmissionBindingError,
        match="WorkflowRun identity",
    ):
        WorkflowProposalAdmissionService(store).admit(forged)

    assert store.events(work.work_id) == before
    with pytest.raises(WorkNotFoundError):
        store.snapshot(child_work_id)


def test_stale_parent_state_rejects_delegation_without_orphan_child(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    work = _start_work(store, source_id="stale")
    workflow = _start_workflow(store, work, suffix="stale")
    result = _step(store, work, workflow)
    assert isinstance(result.proposal, WorkflowDelegationProposal)
    child_work_id = result.proposal.delegation.child_work.work_id

    current = store.snapshot(work.work_id)
    store.append(
        work.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.concurrent-observation",
            payload={"source": "other-writer"},
        ),
    )
    before = store.events(work.work_id)

    with pytest.raises(WorkConcurrencyError):
        WorkflowProposalAdmissionService(store).admit(result)

    assert store.events(work.work_id) == before
    with pytest.raises(WorkNotFoundError):
        store.snapshot(child_work_id)


def test_exact_delegation_step_retry_after_later_parent_event_is_idempotent(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    work = _start_work(store, source_id="retry")
    workflow = _start_workflow(store, work, suffix="retry")
    result = _step(store, work, workflow)
    service = WorkflowProposalAdmissionService(store)

    first = service.admit(result)
    assert isinstance(first, Delegation)
    child_work_id = first.child_work.work_id

    current = store.snapshot(work.work_id)
    store.append(
        work.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.note",
            payload={"after": "delegation admission"},
        ),
    )
    before_retry = store.events(work.work_id)

    retried = service.admit(result)

    assert retried == first
    assert store.events(work.work_id) == before_retry
    assert store.snapshot(child_work_id).work == first.child_work
    assert project_delegations(store.events(work.work_id)) == (first,)


def test_child_read_surface_uses_explicit_owned_children_only() -> None:
    fields = set(WorkflowStepContext.__dataclass_fields__)
    assert "owned_children" in fields
    assert "delegations" not in fields
    assert "children" not in fields
    assert "child_works" not in fields


def test_workflow_delegation_integration_adds_no_scheduler_executor_or_child_result(
) -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "workflow_runtime" / "boundary.py",
        root / "workflow_orchestration" / "proposal_admission.py",
    ]
    forbidden_names = {
        "Scheduler",
        "ChildResult",
        "WorkGraph",
        "ProcessExecutor",
        "AuthorizationReceipt",
        "CapabilityHostBridge",
    }

    for path in paths:
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

        assert forbidden_names.isdisjoint(imported_names)
        assert not any(
            module.endswith(
                (".authority", ".execution", ".standalone_host", ".providers")
            )
            for module in imported_modules
        )
        assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
