from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

from codexia_manual_agent.artifact_core import ArtifactRef
from codexia_manual_agent.completion_core import (
    COMPLETION_CLAIM_ADMITTED_EVENT,
    CompletionAdmissionService,
    CompletionClaim,
    CompletionCriteriaRejected,
    CompletionCriterionContext,
    CompletionCriterionResult,
    WorkCompletion,
    WorkCompletionAdmissionService,
    WorkCompletionRef,
)
from codexia_manual_agent.delegation_core import (
    Delegation,
    DelegationAdmission,
)
from codexia_manual_agent.evidence_core import EvidenceRef
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
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    WorkflowProgressionService,
    WorkflowProposalAdmissionCompletionProviderRequiredError,
    WorkflowProposalAdmissionCompletionResolverRequiredError,
    WorkflowProposalAdmissionService,
    WorkflowStepReadPrecondition,
    WorkflowStepService,
)
from codexia_manual_agent.workflow_runtime import (
    WorkflowImplementationBindingError,
)

PROVIDER_REF = "codexia:g2.40-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work(store: SqliteWorkStore, *, label: str) -> Work:
    work = Work.create(
        objective=f"G2.40 Workflow completion claim for {label}",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=f"g2.40-{label}",
            payload_digest=_sha(f"payload:{label}"),
        ),
    )
    store.create(work)
    return work


def _binding(label: str) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=f"codexia:g2.40-{label}",
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
        pack_id=f"codexia:g2.40-pack-{label}",
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
    def __init__(
        self,
        binding: WorkflowBinding,
        *,
        accepted: bool = True,
        reason: str = "completion criterion accepted",
    ) -> None:
        self.binding = binding
        self.accepted = accepted
        self.reason = reason
        self.calls = 0
        self.contexts: list[CompletionCriterionContext] = []

    def evaluate(
        self,
        context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        self.calls += 1
        self.contexts.append(context)
        return CompletionCriterionResult(
            accepted=self.accepted,
            reason=self.reason,
        )


@dataclass(frozen=True)
class _ResolvedCriterion:
    workflow_binding: WorkflowBinding
    pack_binding_digest: str
    criterion: _Criterion


class _CompletionResolver:
    def __init__(
        self,
        binding: WorkflowBinding,
        pack_digest: str,
        *,
        accepted: bool = True,
        reason: str = "completion criterion accepted",
    ) -> None:
        self.binding = binding
        self.pack_digest = pack_digest
        self.criterion = _Criterion(
            binding,
            accepted=accepted,
            reason=reason,
        )
        self.calls = 0
        self.provider_refs: list[str] = []

    def resolve(self, *, provider_ref, workflow, pack_binding):
        self.calls += 1
        self.provider_refs.append(provider_ref)
        return _ResolvedCriterion(
            workflow_binding=self.binding,
            pack_binding_digest=self.pack_digest,
            criterion=self.criterion,
        )


def _complete_child(
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
        summary="The child objective is satisfied.",
    )
    resolver = _CompletionResolver(
        workflow.run.binding,
        pin.pack.binding_digest,
    )
    CompletionAdmissionService(
        store=store,
        resolver=resolver,
    ).admit(
        claim,
        provider_ref="codexia:g2.40-child-provider@1.0.0",
    )
    admission_event = store.events(child_id)[-1]
    completion = WorkCompletion.create(
        snapshot=store.snapshot(child_id),
        claim=claim,
        claim_admission_event=admission_event,
    )
    WorkCompletionAdmissionService(store).admit(completion)
    return completion


class _WorkflowResolver:
    def __init__(self, implementation) -> None:
        self.implementation = implementation

    def resolve(self, *, provider_ref, workflow, pack_binding):
        return ResolvedWorkflowImplementation(
            provider_ref=provider_ref,
            workflow_binding=workflow.run.binding,
            pack_binding_digest=pack_binding.pack.binding_digest,
            implementation=self.implementation,
        )


class _CompletionClaimImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.contexts = []

    def propose(self, context):
        self.contexts.append(context)
        child_refs = tuple(
            owned.completion.to_ref()
            for owned in context.owned_children
            if owned.completion is not None
        )
        return CompletionClaim.create(
            snapshot=context.work,
            workflow=context.workflow,
            pack_binding=context.pack_binding,
            summary="Observed state satisfies the parent objective.",
            artifact_refs=context.artifacts,
            evidence_refs=context.evidence_refs,
            child_completion_refs=child_refs,
        )


class _InventedChildBasisImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding

    def propose(self, context):
        invented = WorkCompletionRef.create(
            work_id=str(uuid4()),
            completion_event_id=str(uuid4()),
            completion_digest=_sha("invented-child-completion"),
        )
        return CompletionClaim.create(
            snapshot=context.work,
            workflow=context.workflow,
            pack_binding=context.pack_binding,
            summary="Invented child basis must fail before admission.",
            child_completion_refs=(invented,),
        )


class _InventedMaterialBasisImplementation:
    def __init__(
        self,
        binding: WorkflowBinding,
        *,
        kind: str,
    ) -> None:
        self.binding = binding
        self.kind = kind

    def propose(self, context):
        kwargs = {}
        if self.kind == "artifact":
            raw = b"detached artifact"
            kwargs["artifact_refs"] = (
                ArtifactRef.create(
                    content_sha256=hashlib.sha256(raw).hexdigest(),
                    size_bytes=len(raw),
                    locator="provider+opaque://detached/artifact",
                    media_type="application/octet-stream",
                ),
            )
        elif self.kind == "evidence":
            kwargs["evidence_refs"] = (
                EvidenceRef.create(
                    evidence_id=str(uuid4()),
                    evidence_digest=_sha("detached-evidence"),
                    evidence_kind="external.observation.v1",
                    locator="provider+opaque://detached/evidence",
                ),
            )
        else:
            raise AssertionError(self.kind)
        return CompletionClaim.create(
            snapshot=context.work,
            workflow=context.workflow,
            pack_binding=context.pack_binding,
            summary="Detached material basis must fail before admission.",
            **kwargs,
        )


def _parent_setup(
    store: SqliteWorkStore,
    *,
    label: str,
    complete_child: bool = True,
    second_child: bool = False,
):
    parent = _work(store, label=f"parent-{label}")
    workflow, pin = _start_workflow(
        store,
        parent.work_id,
        label=f"parent-{label}",
    )
    first = _delegate(
        store,
        parent,
        objective=f"First child for {label}",
    )
    completion = (
        _complete_child(store, first, label=label)
        if complete_child
        else None
    )
    second = None
    if second_child:
        second = _delegate(
            store,
            parent,
            objective=f"Second child for {label}",
        )
    return parent, workflow, pin, first, completion, second


def _step(
    store: SqliteWorkStore,
    parent: Work,
    workflow,
    implementation,
):
    return WorkflowStepService(
        store=store,
        resolver=_WorkflowResolver(implementation),
    ).step(
        work_id=parent.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=WorkflowStepReadPrecondition.from_snapshot(
            store.snapshot(parent.work_id)
        ),
    )


def test_workflow_can_propose_completion_claim_from_observed_child_completion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "prepare.sqlite")
    parent, workflow, _, _, completion, _ = _parent_setup(
        store,
        label="prepare",
    )
    implementation = _CompletionClaimImplementation(workflow.run.binding)

    result = _step(
        store,
        parent,
        workflow,
        implementation,
    )

    assert isinstance(result.proposal, CompletionClaim)
    assert completion is not None
    assert result.proposal.child_completion_refs == (
        completion.to_ref(),
    )
    assert implementation.contexts[0].owned_children[0].completion == completion


@pytest.mark.parametrize(
    ("kind", "match"),
    [
        ("artifact", "ArtifactRef outside Workflow context"),
        ("evidence", "EvidenceRef outside Workflow context"),
    ],
)
def test_workflow_cannot_invent_material_completion_basis(
    tmp_path,
    kind: str,
    match: str,
) -> None:
    store = SqliteWorkStore(tmp_path / f"invent-{kind}.sqlite")
    parent, workflow, _, _, _, _ = _parent_setup(
        store,
        label=f"invent-{kind}",
        complete_child=False,
    )

    with pytest.raises(
        WorkflowImplementationBindingError,
        match=match,
    ):
        _step(
            store,
            parent,
            workflow,
            _InventedMaterialBasisImplementation(
                workflow.run.binding,
                kind=kind,
            ),
        )


def test_workflow_cannot_invent_child_completion_basis(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "invent-child.sqlite")
    parent, workflow, _, _, _, _ = _parent_setup(
        store,
        label="invent-child",
        complete_child=False,
    )

    with pytest.raises(
        WorkflowImplementationBindingError,
        match="child outside Workflow context",
    ):
        _step(
            store,
            parent,
            workflow,
            _InventedChildBasisImplementation(workflow.run.binding),
        )


def test_completion_claim_requires_injected_completion_resolver(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "missing-resolver.sqlite")
    parent, workflow, _, _, _, _ = _parent_setup(
        store,
        label="missing-resolver",
    )
    result = _step(
        store,
        parent,
        workflow,
        _CompletionClaimImplementation(workflow.run.binding),
    )
    before = store.events(parent.work_id)

    with pytest.raises(
        WorkflowProposalAdmissionCompletionResolverRequiredError,
        match="requires completion resolver",
    ):
        WorkflowProposalAdmissionService(store).admit(result)

    assert store.events(parent.work_id) == before


def test_completion_claim_routes_to_existing_completion_admission_owner(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "admit.sqlite")
    parent, workflow, pin, _, completion, _ = _parent_setup(
        store,
        label="admit",
    )
    result = _step(
        store,
        parent,
        workflow,
        _CompletionClaimImplementation(workflow.run.binding),
    )
    resolver = _CompletionResolver(
        workflow.run.binding,
        pin.pack.binding_digest,
    )

    admitted = WorkflowProposalAdmissionService(
        store,
        completion_resolver=resolver,
    ).admit(
        result,
        completion_provider_ref=PROVIDER_REF,
    )

    assert admitted == result.proposal
    assert isinstance(admitted, CompletionClaim)
    assert completion is not None
    assert admitted.child_completion_refs == (completion.to_ref(),)
    assert store.events(parent.work_id)[-1].kind == COMPLETION_CLAIM_ADMITTED_EVENT
    assert store.snapshot(parent.work_id).state is WorkState.ACTIVE
    assert resolver.calls == 1
    assert resolver.provider_refs == [PROVIDER_REF]
    assert resolver.criterion.calls == 1


def test_completion_claim_requires_explicit_completion_provider(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "missing-provider.sqlite")
    parent, workflow, pin, _, _, _ = _parent_setup(
        store,
        label="missing-provider",
    )
    result = _step(
        store,
        parent,
        workflow,
        _CompletionClaimImplementation(workflow.run.binding),
    )
    resolver = _CompletionResolver(
        workflow.run.binding,
        pin.pack.binding_digest,
    )
    before = store.events(parent.work_id)

    with pytest.raises(
        WorkflowProposalAdmissionCompletionProviderRequiredError,
        match="requires explicit completion provider",
    ):
        WorkflowProposalAdmissionService(
            store,
            completion_resolver=resolver,
        ).admit(result)

    assert store.events(parent.work_id) == before
    assert resolver.calls == 0


def test_step_provider_provenance_does_not_select_completion_criterion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "provider-separation.sqlite")
    parent, workflow, pin, _, _, _ = _parent_setup(
        store,
        label="provider-separation",
    )
    result = _step(
        store,
        parent,
        workflow,
        _CompletionClaimImplementation(workflow.run.binding),
    )
    rebound = replace(
        result,
        provider_ref="codexia:tampered-step-provenance@9.9.9",
    )
    resolver = _CompletionResolver(
        workflow.run.binding,
        pin.pack.binding_digest,
    )

    admitted = WorkflowProposalAdmissionService(
        store,
        completion_resolver=resolver,
    ).admit(
        rebound,
        completion_provider_ref="codexia:explicit-completion-provider@1.0.0",
    )

    assert admitted == result.proposal
    assert resolver.provider_refs == [
        "codexia:explicit-completion-provider@1.0.0"
    ]


def test_completion_criterion_rejection_does_not_mutate_parent(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "reject.sqlite")
    parent, workflow, pin, _, _, _ = _parent_setup(
        store,
        label="reject",
    )
    result = _step(
        store,
        parent,
        workflow,
        _CompletionClaimImplementation(workflow.run.binding),
    )
    resolver = _CompletionResolver(
        workflow.run.binding,
        pin.pack.binding_digest,
        accepted=False,
        reason="parent completion criteria not satisfied",
    )
    before = store.events(parent.work_id)

    with pytest.raises(
        CompletionCriteriaRejected,
        match="parent completion criteria not satisfied",
    ):
        WorkflowProposalAdmissionService(
            store,
            completion_resolver=resolver,
        ).admit(
            result,
            completion_provider_ref=PROVIDER_REF,
        )

    assert store.events(parent.work_id) == before
    assert resolver.criterion.calls == 1


def test_exact_retry_does_not_re_evaluate_completion_criterion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    parent, workflow, pin, _, _, _ = _parent_setup(
        store,
        label="retry",
    )
    result = _step(
        store,
        parent,
        workflow,
        _CompletionClaimImplementation(workflow.run.binding),
    )
    resolver = _CompletionResolver(
        workflow.run.binding,
        pin.pack.binding_digest,
    )
    service = WorkflowProposalAdmissionService(
        store,
        completion_resolver=resolver,
    )

    first = service.admit(
        result,
        completion_provider_ref=PROVIDER_REF,
    )
    current = store.snapshot(parent.work_id)
    store.append(
        parent.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.after-claim",
            payload={"source": "post-admission"},
        ),
    )
    before_retry = store.events(parent.work_id)
    second = service.admit(
        result,
        completion_provider_ref=PROVIDER_REF,
    )

    assert second == first
    assert store.events(parent.work_id) == before_retry
    assert resolver.calls == 1
    assert resolver.criterion.calls == 1


def test_unreferenced_child_change_after_step_stales_claim_admission(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "child-race.sqlite")
    parent, workflow, pin, _, _, second = _parent_setup(
        store,
        label="child-race",
        second_child=True,
    )
    assert second is not None
    result = _step(
        store,
        parent,
        workflow,
        _CompletionClaimImplementation(workflow.run.binding),
    )
    assert isinstance(result.proposal, CompletionClaim)
    assert len(result.child_reads) == 2
    assert len(result.proposal.child_completion_refs) == 1

    second_snapshot = store.snapshot(second.child_work.work_id)
    store.append(
        second.child_work.work_id,
        expected_revision=second_snapshot.revision,
        event=second_snapshot.next_event(
            kind="work.child-observation",
            payload={"changed": True},
        ),
    )
    resolver = _CompletionResolver(
        workflow.run.binding,
        pin.pack.binding_digest,
    )
    before_parent = store.events(parent.work_id)

    with pytest.raises(
        WorkConcurrencyError,
        match="Stale read precondition",
    ):
        WorkflowProposalAdmissionService(
            store,
            completion_resolver=resolver,
        ).admit(
            result,
            completion_provider_ref=PROVIDER_REF,
        )

    assert store.events(parent.work_id) == before_parent
    assert resolver.criterion.calls == 1


def test_workflow_progression_can_compute_and_admit_completion_claim(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "progression.sqlite")
    parent, workflow, pin, _, _, _ = _parent_setup(
        store,
        label="progression",
    )
    implementation = _CompletionClaimImplementation(workflow.run.binding)
    completion_resolver = _CompletionResolver(
        workflow.run.binding,
        pin.pack.binding_digest,
    )
    precondition = WorkflowStepReadPrecondition.from_snapshot(
        store.snapshot(parent.work_id)
    )

    progressed = WorkflowProgressionService(
        store=store,
        resolver=_WorkflowResolver(implementation),
        completion_resolver=completion_resolver,
    ).progress_once(
        work_id=parent.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=precondition,
    )

    assert isinstance(progressed.step.proposal, CompletionClaim)
    assert progressed.admitted == progressed.step.proposal
    assert store.events(parent.work_id)[-1].kind == COMPLETION_CLAIM_ADMITTED_EVENT
    assert store.snapshot(parent.work_id).state is WorkState.ACTIVE


def test_g2_40_workflow_claim_integration_does_not_gain_terminal_authority(
) -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "workflow_runtime" / "boundary.py",
        root / "workflow_orchestration" / "proposal_admission.py",
        root / "workflow_orchestration" / "workflow_progression.py",
    ]
    forbidden_names = {
        "WorkCompletionAdmissionService",
        "WORK_COMPLETED_EVENT",
        "InvariantCompletionCriterionBridge",
        "ChildResult",
        "ParentCompletion",
        "Scheduler",
        "ProcessExecutor",
        "AuthorizationReceipt",
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


def test_g2_40_adds_no_new_completion_or_child_read_set() -> None:
    from codexia_manual_agent.workflow_orchestration import WorkflowStepResult

    assert "completion_reads" not in WorkflowStepResult.__dataclass_fields__
    assert "child_completion_reads" not in WorkflowStepResult.__dataclass_fields__
