from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from codexia_manual_agent.attention_core import (
    AttentionNeed,
    project_attention_needs,
)
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
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    WorkflowProgressionService,
    WorkflowProposalAdmissionBindingError,
    WorkflowProposalAdmissionService,
    WorkflowStepPreconditionError,
    WorkflowStepReadPrecondition,
    WorkflowStepService,
)

PROVIDER_REF = "codexia:g2.22-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding(suffix: str = "main") -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=f"codexia:g2.22-{suffix}",
        version="1.0.0",
        definition_digest=_sha(f"g2.22-{suffix}"),
    )


def _start_work(store: SqliteWorkStore, *, source_id: str) -> Work:
    work = Work.create(
        objective="Integrate durable human-attention semantics with Workflow",
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
    run = WorkflowRun.create(
        snapshot=store.snapshot(work.work_id),
        binding=binding,
    )
    workflow = WorkflowAdmission(store).admit_start(run)
    pack = PackBinding.create(
        pack_id=f"codexia:g2.22-pack-{suffix}",
        version="1.0.0",
        definition_digest=_sha(f"g2.22-pack-{suffix}"),
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


class AttentionAwareImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.calls = 0
        self.seen_attentions: list[tuple[AttentionNeed, ...]] = []

    def propose(self, context):
        self.calls += 1
        self.seen_attentions.append(context.attentions)
        if context.attentions:
            return None
        return AttentionNeed.create(
            workflow=context.workflow,
            snapshot=context.work,
            question="Which semantic direction should continue?",
            reason="The alternatives require human judgment before choosing.",
        )


class CaptureImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.attentions: tuple[AttentionNeed, ...] | None = None

    def propose(self, context):
        self.attentions = context.attentions
        return None


class Resolver:
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


def test_workflow_progression_can_propose_and_admit_attention_need(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "typed.sqlite")
    work = _start_work(store, source_id="typed")
    workflow = _start_workflow(store, work, suffix="typed")
    implementation = AttentionAwareImplementation(workflow.run.binding)
    resolver = Resolver(implementation)

    result = WorkflowProgressionService(
        store=store,
        resolver=resolver,
    ).progress_once(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(store, work.work_id),
    )

    assert isinstance(result.step.proposal, AttentionNeed)
    assert result.admitted == result.step.proposal
    assert project_attention_needs(store.events(work.work_id)) == (
        result.step.proposal,
    )
    assert implementation.seen_attentions == [()]


def test_reentry_sees_existing_attention_and_can_avoid_duplicate(tmp_path) -> None:
    path = tmp_path / "reentry.sqlite"
    store = SqliteWorkStore(path)
    work = _start_work(store, source_id="reentry")
    workflow = _start_workflow(store, work, suffix="reentry")
    first_impl = AttentionAwareImplementation(workflow.run.binding)

    first = WorkflowProgressionService(
        store=store,
        resolver=Resolver(first_impl),
    ).progress_once(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(store, work.work_id),
    )
    assert isinstance(first.admitted, AttentionNeed)

    restarted = SqliteWorkStore(path)
    second_impl = AttentionAwareImplementation(workflow.run.binding)
    before = restarted.events(work.work_id)
    second = WorkflowProgressionService(
        store=restarted,
        resolver=Resolver(second_impl),
    ).progress_once(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(restarted, work.work_id),
    )

    assert second.step.proposal is None
    assert second.admitted is None
    assert second_impl.seen_attentions == [(first.admitted,)]
    assert restarted.events(work.work_id) == before


def test_attention_context_is_scoped_to_exact_workflow_run(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "scope.sqlite")
    work = _start_work(store, source_id="scope")
    first_workflow = _start_workflow(store, work, suffix="first")
    first_impl = AttentionAwareImplementation(first_workflow.run.binding)
    first = WorkflowProgressionService(
        store=store,
        resolver=Resolver(first_impl),
    ).progress_once(
        work_id=work.work_id,
        workflow_run_id=first_workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(store, work.work_id),
    )
    assert isinstance(first.admitted, AttentionNeed)

    second_workflow = _start_workflow(store, work, suffix="second")
    capture = CaptureImplementation(second_workflow.run.binding)
    result = WorkflowStepService(
        store=store,
        resolver=Resolver(capture),
    ).step(
        work_id=work.work_id,
        workflow_run_id=second_workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(store, work.work_id),
    )

    assert result.proposal is None
    assert capture.attentions == ()


def test_retry_same_progression_after_attention_admission_cannot_duplicate(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    work = _start_work(store, source_id="retry")
    workflow = _start_workflow(store, work, suffix="retry")
    implementation = AttentionAwareImplementation(workflow.run.binding)
    resolver = Resolver(implementation)
    service = WorkflowProgressionService(store=store, resolver=resolver)
    precondition = _precondition(store, work.work_id)

    first = service.progress_once(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=precondition,
    )
    assert isinstance(first.admitted, AttentionNeed)
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


def test_forged_attention_step_binding_fails_before_mutation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "forged.sqlite")
    work = _start_work(store, source_id="forged")
    workflow = _start_workflow(store, work, suffix="forged")
    implementation = AttentionAwareImplementation(workflow.run.binding)
    result = WorkflowStepService(
        store=store,
        resolver=Resolver(implementation),
    ).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )
    assert isinstance(result.proposal, AttentionNeed)
    forged = replace(result, work_revision=result.work_revision + 1)
    before = store.events(work.work_id)

    with pytest.raises(WorkflowProposalAdmissionBindingError):
        WorkflowProposalAdmissionService(store).admit(forged)

    assert store.events(work.work_id) == before


def test_attention_integration_has_no_scheduler_or_notification_surface() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "workflow_runtime" / "boundary.py",
        root / "workflow_orchestration" / "step.py",
        root / "workflow_orchestration" / "proposal_admission.py",
    ]

    forbidden_names = {
        "Scheduler",
        "NotificationPort",
        "AttentionAnswer",
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
