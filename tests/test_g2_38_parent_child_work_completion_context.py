from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from codexia_manual_agent.completion_core import (
    CompletionAdmissionService,
    CompletionClaim,
    CompletionCriterionContext,
    CompletionCriterionResult,
    WorkCompletion,
    WorkCompletionAdmissionService,
    WorkCompletionProjectionError,
)
from codexia_manual_agent.delegation_core import (
    Delegation,
    DelegationAdmission,
)
from codexia_manual_agent.delegation_core.completion import (
    _DelegationCompletionGuard,
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
    WORK_COMPLETED_EVENT,
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkEvent,
    WorkIngressBinding,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    WorkflowProposalAdmissionService,
    WorkflowStepReadPrecondition,
    WorkflowStepResult,
    WorkflowStepService,
)
from codexia_manual_agent.workflow_runtime import (
    OwnedChildWorkSnapshot,
    WorkflowImplementationBindingError,
)

PROVIDER_REF = "codexia:g2.38-parent-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work(store: SqliteWorkStore, *, source_id: str) -> Work:
    work = Work.create(
        objective=f"Observe child WorkCompletion for {source_id}",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    store.create(work)
    return work


def _binding(label: str) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=f"codexia:g2.38-{label}",
        version="1.0.0",
        definition_digest=_sha(f"workflow:{label}"),
    )


def _start_workflow(
    store: SqliteWorkStore,
    work_id: str,
    *,
    label: str,
):
    binding = _binding(label)
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=store.snapshot(work_id),
            binding=binding,
        )
    )
    pack = PackBinding.create(
        pack_id=f"codexia:g2.38-pack-{label}",
        version="1.0.0",
        definition_digest=_sha(f"pack:{label}"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=binding.workflow_id,
                version=binding.version,
                binding_digest=binding.binding_digest,
            ),
        ),
    )
    pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work_id),
            pack=pack,
        )
    )
    return workflow, pin


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


class _Criterion:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding

    def evaluate(
        self,
        context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        return CompletionCriterionResult(
            accepted=True,
            reason="child objective satisfied",
        )


class _ResolvedCriterion:
    def __init__(
        self,
        binding: WorkflowBinding,
        pack_digest: str,
        criterion: _Criterion,
    ) -> None:
        self.workflow_binding = binding
        self.pack_binding_digest = pack_digest
        self.criterion = criterion


class _CriterionResolver:
    def __init__(
        self,
        binding: WorkflowBinding,
        pack_digest: str,
    ) -> None:
        self.binding = binding
        self.pack_digest = pack_digest

    def resolve(self, *, provider_ref, workflow, pack_binding):
        return _ResolvedCriterion(
            self.binding,
            self.pack_digest,
            _Criterion(self.binding),
        )


def _complete_child_semantically(
    store: SqliteWorkStore,
    delegation: Delegation,
    *,
    label: str,
) -> WorkCompletion:
    child_id = delegation.child_work.work_id
    workflow, pin = _start_workflow(
        store,
        child_id,
        label=f"child-{label}",
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(child_id),
        workflow=workflow,
        pack_binding=pin,
        summary="The delegated child objective is satisfied.",
    )
    CompletionAdmissionService(
        store=store,
        resolver=_CriterionResolver(
            workflow.run.binding,
            pin.pack.binding_digest,
        ),
    ).admit(
        claim,
        provider_ref="codexia:g2.38-child-provider@1.0.0",
    )
    admission_event = store.events(child_id)[-1]
    completion = WorkCompletion.create(
        snapshot=store.snapshot(child_id),
        claim=claim,
        claim_admission_event=admission_event,
    )
    WorkCompletionAdmissionService(store).admit(completion)
    return completion


def _complete_child_legacy(
    store: SqliteWorkStore,
    delegation: Delegation,
) -> None:
    child = store.snapshot(delegation.child_work.work_id)
    _DelegationCompletionGuard(store).admit(
        child.next_event(
            kind=WORK_COMPLETED_EVENT,
            payload={"summary": "legacy child completion"},
        )
    )


class _Resolver:
    def __init__(self, implementation) -> None:
        self.implementation = implementation

    def resolve(self, *, provider_ref, workflow, pack_binding):
        return ResolvedWorkflowImplementation(
            provider_ref=provider_ref,
            workflow_binding=workflow.run.binding,
            pack_binding_digest=pack_binding.pack.binding_digest,
            implementation=self.implementation,
        )


class _Capture:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.owned_children: tuple[OwnedChildWorkSnapshot, ...] | None = None

    def propose(self, context):
        self.owned_children = context.owned_children
        return None


class _Note:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding

    def propose(self, context):
        owned = context.owned_children[0]
        return WorkflowCandidate.create(
            run_snapshot=context.workflow,
            work_snapshot=context.work,
            event_kind="workflow.child-completion-observed",
            payload={
                "child_work_id": owned.child.work.work_id,
                "child_state": owned.child.state.value,
                "completion_id": (
                    None
                    if owned.completion is None
                    else owned.completion.completion_id
                ),
            },
        )


def _step(
    store: SqliteWorkStore,
    parent: Work,
    workflow,
    implementation,
):
    return WorkflowStepService(
        store=store,
        resolver=_Resolver(implementation),
    ).step(
        work_id=parent.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=WorkflowStepReadPrecondition.from_snapshot(
            store.snapshot(parent.work_id)
        ),
    )


def test_restart_parent_observes_exact_child_work_completion(tmp_path) -> None:
    path = tmp_path / "restart.sqlite"
    store = SqliteWorkStore(path)
    parent = _work(store, source_id="restart-parent")
    parent_workflow, _ = _start_workflow(
        store,
        parent.work_id,
        label="restart-parent",
    )
    delegation = _delegate(
        store,
        parent,
        objective="Finish semantically before parent resumes.",
    )
    completion = _complete_child_semantically(
        store,
        delegation,
        label="restart",
    )

    restarted = SqliteWorkStore(path)
    capture = _Capture(parent_workflow.run.binding)
    result = _step(
        restarted,
        parent,
        parent_workflow,
        capture,
    )

    assert result.proposal is None
    assert capture.owned_children is not None
    assert len(capture.owned_children) == 1
    owned = capture.owned_children[0]
    assert owned.delegation == delegation
    assert owned.child.state is WorkState.COMPLETED
    assert owned.completion == completion
    assert owned.child.terminal_event_id == completion.completion_id
    assert len(result.child_reads) == 1
    assert result.child_reads[0].child == owned.child


def test_child_work_completion_is_work_level_not_parent_workflow_scoped(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work-level.sqlite")
    parent = _work(store, source_id="work-level-parent")
    _start_workflow(store, parent.work_id, label="first-parent")
    delegation = _delegate(
        store,
        parent,
        objective="Expose completion to any parent WorkflowRun.",
    )
    completion = _complete_child_semantically(
        store,
        delegation,
        label="shared",
    )
    second, _ = _start_workflow(
        store,
        parent.work_id,
        label="second-parent",
    )
    capture = _Capture(second.run.binding)

    _step(store, parent, second, capture)

    assert capture.owned_children is not None
    assert capture.owned_children[0].completion == completion


def test_active_child_exposes_no_work_completion(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "active.sqlite")
    parent = _work(store, source_id="active-parent")
    workflow, _ = _start_workflow(
        store,
        parent.work_id,
        label="active-parent",
    )
    _delegate(
        store,
        parent,
        objective="Remain active.",
    )
    capture = _Capture(workflow.run.binding)

    _step(store, parent, workflow, capture)

    assert capture.owned_children is not None
    owned = capture.owned_children[0]
    assert owned.child.state is WorkState.ACTIVE
    assert owned.completion is None


def test_legacy_raw_completed_child_remains_visible_without_synthetic_result(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "legacy.sqlite")
    parent = _work(store, source_id="legacy-parent")
    workflow, _ = _start_workflow(
        store,
        parent.work_id,
        label="legacy-parent",
    )
    delegation = _delegate(
        store,
        parent,
        objective="Represent pre-G2.37 terminal data.",
    )
    _complete_child_legacy(store, delegation)
    capture = _Capture(workflow.run.binding)

    _step(store, parent, workflow, capture)

    assert capture.owned_children is not None
    owned = capture.owned_children[0]
    assert owned.child.state is WorkState.COMPLETED
    assert owned.completion is None


def test_owned_child_snapshot_rejects_mismatched_work_completion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "binding.sqlite")
    first_parent = _work(store, source_id="first-parent")
    first = _delegate(
        store,
        first_parent,
        objective="First child.",
    )
    first_completion = _complete_child_semantically(
        store,
        first,
        label="first",
    )

    second_parent = _work(store, source_id="second-parent")
    second = _delegate(
        store,
        second_parent,
        objective="Second child.",
    )
    second_completion = _complete_child_semantically(
        store,
        second,
        label="second",
    )

    with pytest.raises(
        WorkflowImplementationBindingError,
        match="crossed child Work identity",
    ):
        OwnedChildWorkSnapshot(
            delegation=first,
            child=store.snapshot(first.child_work.work_id),
            completion=second_completion,
        )

    assert first_completion.work_id != second_completion.work_id


def test_owned_child_work_completion_field_is_appended_for_compatibility(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "positional.sqlite")
    parent = _work(store, source_id="positional-parent")
    delegation = _delegate(
        store,
        parent,
        objective="Keep the original positional constructor valid.",
    )
    child = store.snapshot(delegation.child_work.work_id)

    legacy_shape = OwnedChildWorkSnapshot(delegation, child)

    assert tuple(OwnedChildWorkSnapshot.__dataclass_fields__) == (
        "delegation",
        "child",
        "completion",
    )
    assert legacy_shape.completion is None


def test_semantic_child_completion_after_parent_step_stales_existing_read_set(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    parent = _work(store, source_id="stale-parent")
    workflow, _ = _start_workflow(
        store,
        parent.work_id,
        label="stale-parent",
    )
    delegation = _delegate(
        store,
        parent,
        objective="Complete after parent reads active state.",
    )
    result = _step(
        store,
        parent,
        workflow,
        _Note(workflow.run.binding),
    )
    assert isinstance(result.proposal, WorkflowCandidate)
    assert result.proposal.event.to_dict()["payload"]["payload"][
        "completion_id"
    ] is None

    _complete_child_semantically(
        store,
        delegation,
        label="stale-after-step",
    )
    before_parent = store.events(parent.work_id)

    with pytest.raises(WorkConcurrencyError, match="Stale read precondition"):
        WorkflowProposalAdmissionService(store).admit(result)

    assert store.events(parent.work_id) == before_parent


def test_structured_corrupt_child_completion_fails_before_implementation(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "corrupt.sqlite")
    parent = _work(store, source_id="corrupt-parent")
    workflow, _ = _start_workflow(
        store,
        parent.work_id,
        label="corrupt-parent",
    )
    delegation = _delegate(
        store,
        parent,
        objective="Contain malformed structured completion.",
    )
    child = store.snapshot(delegation.child_work.work_id)
    forged = child.next_event(
        kind=WORK_COMPLETED_EVENT,
        payload={"work_completion": {"not": "a WorkCompletion"}},
    )
    store._append_completion(
        child.work.work_id,
        expected_revision=child.revision,
        event=forged,
    )
    capture = _Capture(workflow.run.binding)

    with pytest.raises(WorkCompletionProjectionError):
        _step(store, parent, workflow, capture)

    assert capture.owned_children is None


def test_existing_child_read_binding_already_protects_completion_observation(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "read-binding.sqlite")
    parent = _work(store, source_id="read-binding-parent")
    workflow, _ = _start_workflow(
        store,
        parent.work_id,
        label="read-binding-parent",
    )
    delegation = _delegate(
        store,
        parent,
        objective="Complete before exact parent read.",
    )
    completion = _complete_child_semantically(
        store,
        delegation,
        label="read-binding",
    )
    result = _step(
        store,
        parent,
        workflow,
        _Note(workflow.run.binding),
    )

    assert len(result.child_reads) == 1
    assert result.child_reads[0].child.terminal_event_id == completion.completion_id
    assert "child_completion_reads" not in WorkflowStepResult.__dataclass_fields__
    assert "completion_reads" not in WorkflowStepResult.__dataclass_fields__


def test_g2_38_does_not_introduce_child_result_or_parent_auto_completion() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "workflow_runtime" / "boundary.py",
        root / "workflow_orchestration" / "step.py",
        root / "workflow_orchestration" / "proposal_admission.py",
    ]
    forbidden_names = {
        "ChildResult",
        "ParentCompletion",
        "WorkGraph",
        "DelegationCompletionGuard",
        "WorkCompletionAdmissionService",
        "WORK_COMPLETED_EVENT",
        "Scheduler",
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
