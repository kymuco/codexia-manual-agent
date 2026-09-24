from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from codexia_manual_agent.attention_core import (
    AttentionAdmission,
    AttentionNeed,
    AttentionResponse,
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
    WorkflowStepReadPrecondition,
    WorkflowStepService,
)
from codexia_manual_agent.workflow_runtime import (
    WorkflowImplementationBindingError,
    WorkflowStepContext,
)

PROVIDER_REF = "codexia:g2.24-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding(suffix: str) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=f"codexia:g2.24-{suffix}",
        version="1.0.0",
        definition_digest=_sha(f"g2.24-{suffix}"),
    )


def _start_work(store: SqliteWorkStore, *, source_id: str) -> Work:
    work = Work.create(
        objective="Let Workflow observe durable human response evidence",
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
        pack_id=f"codexia:g2.24-pack-{suffix}",
        version="1.0.0",
        definition_digest=_sha(f"g2.24-pack-{suffix}"),
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
            snapshot=store.snapshot(work.work_id),
            pack=pack,
        )
    )
    return workflow, pin


def _need(store: SqliteWorkStore, workflow) -> AttentionNeed:
    return AttentionAdmission(store).admit_need(
        AttentionNeed.create(
            workflow=workflow,
            snapshot=store.snapshot(workflow.run.work_id),
            question="Which direction should continue?",
            reason="The semantic branch requires human judgment.",
        )
    )


def _response(
    store: SqliteWorkStore,
    need: AttentionNeed,
    *,
    text: str,
    source_id: str,
) -> AttentionResponse:
    return AttentionAdmission(store).admit_response(
        AttentionResponse.create(
            need=need,
            snapshot=store.snapshot(need.work_id),
            response_text=text,
            source_namespace="standalone.ui",
            source_id=source_id,
        )
    )


class CaptureImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.attentions: tuple[AttentionNeed, ...] | None = None
        self.responses: tuple[AttentionResponse, ...] | None = None

    def propose(self, context):
        self.attentions = context.attentions
        self.responses = context.attention_responses
        return None


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


def _precondition(store: SqliteWorkStore, work_id: str):
    return WorkflowStepReadPrecondition.from_snapshot(store.snapshot(work_id))


def test_restart_step_observes_durable_attention_response(tmp_path) -> None:
    path = tmp_path / "restart.sqlite"
    store = SqliteWorkStore(path)
    work = _start_work(store, source_id="restart")
    workflow, _ = _start_workflow(store, work, suffix="restart")
    need = _need(store, workflow)
    response = _response(
        store,
        need,
        text="Continue with direction A.",
        source_id="turn-1",
    )

    restarted = SqliteWorkStore(path)
    capture = CaptureImplementation(workflow.run.binding)
    before = restarted.events(work.work_id)
    result = WorkflowStepService(
        store=restarted,
        resolver=Resolver(capture),
    ).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(restarted, work.work_id),
    )

    assert result.proposal is None
    assert capture.attentions == (need,)
    assert capture.responses == (response,)
    assert restarted.events(work.work_id) == before


def test_workflow_context_preserves_full_response_correction_order(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "corrections.sqlite")
    work = _start_work(store, source_id="corrections")
    workflow, _ = _start_workflow(store, work, suffix="corrections")
    need = _need(store, workflow)
    first = _response(
        store,
        need,
        text="Choose A.",
        source_id="turn-1",
    )
    second = _response(
        store,
        need,
        text="Correction: choose B.",
        source_id="turn-2",
    )
    capture = CaptureImplementation(workflow.run.binding)

    WorkflowStepService(
        store=store,
        resolver=Resolver(capture),
    ).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(store, work.work_id),
    )

    assert capture.attentions == (need,)
    assert capture.responses == (first, second)


def test_attention_responses_are_scoped_to_exact_workflow_run(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "scope.sqlite")
    work = _start_work(store, source_id="scope")
    first_workflow, _ = _start_workflow(store, work, suffix="first")
    need = _need(store, first_workflow)
    _response(
        store,
        need,
        text="First workflow answer.",
        source_id="turn-first",
    )

    second_workflow, _ = _start_workflow(store, work, suffix="second")
    capture = CaptureImplementation(second_workflow.run.binding)
    WorkflowStepService(
        store=store,
        resolver=Resolver(capture),
    ).step(
        work_id=work.work_id,
        workflow_run_id=second_workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(store, work.work_id),
    )

    assert capture.attentions == ()
    assert capture.responses == ()


def test_context_rejects_response_without_visible_attention_need(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "binding.sqlite")
    work = _start_work(store, source_id="binding")
    workflow, pin = _start_workflow(store, work, suffix="binding")
    need = _need(store, workflow)
    response = _response(
        store,
        need,
        text="Choose A.",
        source_id="turn-1",
    )

    with pytest.raises(
        WorkflowImplementationBindingError,
        match="outside context",
    ):
        WorkflowStepContext(
            work=store.snapshot(work.work_id),
            workflow=workflow,
            pack_binding=pin,
            attentions=(),
            attention_responses=(response,),
        )


def test_context_exposes_ordered_history_not_latest_response_policy() -> None:
    fields = set(WorkflowStepContext.__dataclass_fields__)
    assert "attention_responses" in fields
    assert "latest_attention_response" not in fields
    assert "resolved_attentions" not in fields
    assert "resumable_attentions" not in fields


def test_response_context_adds_no_resume_scheduler_or_authority_surface() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "workflow_runtime" / "boundary.py",
        root / "workflow_orchestration" / "step.py",
    ]
    forbidden_names = {
        "Scheduler",
        "NotificationPort",
        "AttentionAnswer",
        "AuthorizationReceipt",
        "AttentionResume",
        "ResumePolicy",
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
