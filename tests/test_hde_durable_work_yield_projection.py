from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from codexia_manual_agent.attention_core import AttentionAdmission, AttentionNeed
from codexia_manual_agent.completion_core import (
    CompletionAdmissionService,
    CompletionClaim,
    CompletionCriterionContext,
    CompletionCriterionResult,
    WorkCompletion,
    WorkCompletionAdmissionService,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import (
    WORK_CANCELLED_EVENT,
    WORK_COMPLETED_EVENT,
    SqliteWorkStore,
    Work,
    WorkEvent,
    WorkIngressBinding,
    WorkSnapshot,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    DurableWorkYieldKind,
    DurableWorkYieldProjectionError,
    project_durable_work_yield,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work(store: SqliteWorkStore, *, label: str) -> WorkSnapshot:
    return store.create(
        Work.create(
            objective=f"Reach one exact durable return frontier for {label}.",
            ingress=WorkIngressBinding.create(
                source_namespace="hde.worker.test",
                source_id=label,
                payload_digest=_sha(f"payload:{label}"),
            ),
        )
    )


def _started(store: SqliteWorkStore, *, label: str):
    work = _work(store, label=label)
    workflow_binding = WorkflowBinding.create(
        workflow_id=f"codexia:yield-{label}",
        version="1.0.0",
        definition_digest=_sha(f"workflow:{label}"),
    )
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=work,
            binding=workflow_binding,
        )
    )
    pack = PackBinding.create(
        pack_id=f"codexia:yield-pack-{label}",
        version="1.0.0",
        definition_digest=_sha(f"pack:{label}"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow_binding.workflow_id,
                version=workflow_binding.version,
                binding_digest=workflow_binding.binding_digest,
            ),
        ),
    )
    pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work.work_id),
            pack=pack,
        )
    )
    return work, workflow, pin


class _Criterion:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding

    def evaluate(
        self,
        _context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        return CompletionCriterionResult(
            accepted=True,
            reason="exact durable yield criterion accepted",
        )


@dataclass(frozen=True)
class _ResolvedCriterion:
    workflow_binding: WorkflowBinding
    pack_binding_digest: str
    criterion: _Criterion


class _Resolver:
    def __init__(
        self,
        *,
        workflow_binding: WorkflowBinding,
        pack_binding_digest: str,
    ) -> None:
        self._workflow_binding = workflow_binding
        self._pack_binding_digest = pack_binding_digest
        self._criterion = _Criterion(workflow_binding)

    def resolve(self, *, provider_ref: str, workflow, pack_binding):
        assert provider_ref == "codexia:yield-provider@1.0.0"
        assert workflow.run.binding == self._workflow_binding
        assert pack_binding.pack.binding_digest == self._pack_binding_digest
        return _ResolvedCriterion(
            workflow_binding=self._workflow_binding,
            pack_binding_digest=self._pack_binding_digest,
            criterion=self._criterion,
        )


def _complete(
    store: SqliteWorkStore,
    *,
    label: str,
) -> tuple[str, WorkCompletion]:
    work, workflow, pin = _started(store, label=label)
    claim = CompletionClaim.create(
        snapshot=store.snapshot(work.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary=f"Exact delegated Work {label} is semantically complete.",
    )
    resolver = _Resolver(
        workflow_binding=workflow.run.binding,
        pack_binding_digest=pin.pack.binding_digest,
    )
    CompletionAdmissionService(
        store=store,
        resolver=resolver,
    ).admit(
        claim,
        provider_ref="codexia:yield-provider@1.0.0",
    )
    admission_event = store.events(work.work.work_id)[-1]
    completion = WorkCompletion.create(
        snapshot=store.snapshot(work.work.work_id),
        claim=claim,
        claim_admission_event=admission_event,
    )
    WorkCompletionAdmissionService(store).admit(completion)
    return work.work.work_id, completion


def test_active_work_without_current_return_frontier_projects_none(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "none.sqlite")
    work = _work(store, label="none")

    result = project_durable_work_yield(
        work.work.work_id,
        store=store,
    )

    assert result.kind is DurableWorkYieldKind.NONE
    assert result.snapshot == store.snapshot(work.work.work_id)
    assert result.completion is None
    assert result.attention is None


def test_current_head_attention_need_projects_exact_attention(
    tmp_path,
) -> None:
    path = tmp_path / "attention.sqlite"
    store = SqliteWorkStore(path)
    work, workflow, _ = _started(store, label="attention")
    need = AttentionAdmission(store).admit_need(
        AttentionNeed.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work.work_id),
            question="Which exact semantic branch should continue?",
            reason="Only human judgment can choose the remaining branch.",
        )
    )

    result = project_durable_work_yield(
        work.work.work_id,
        store=store,
    )

    assert result.kind is DurableWorkYieldKind.ATTENTION
    assert result.attention == need
    assert result.completion is None
    assert result.snapshot.revision == store.snapshot(work.work.work_id).revision

    restarted = SqliteWorkStore(path)
    recovered = project_durable_work_yield(
        work.work.work_id,
        store=restarted,
    )
    assert recovered.kind is DurableWorkYieldKind.ATTENTION
    assert recovered.attention == need


def test_stale_attention_need_is_not_a_current_return_frontier(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "stale-attention.sqlite")
    work, workflow, _ = _started(store, label="stale-attention")
    AttentionAdmission(store).admit_need(
        AttentionNeed.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work.work_id),
            question="Choose one branch?",
            reason="Human judgment changes the future trajectory.",
        )
    )
    current = store.snapshot(work.work.work_id)
    store.append(
        work.work.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.observation",
            payload={"after_attention": True},
        ),
    )

    result = project_durable_work_yield(
        work.work.work_id,
        store=store,
    )

    assert result.kind is DurableWorkYieldKind.NONE
    assert result.attention is None


def test_guarded_work_completion_projects_exact_terminal_completion(
    tmp_path,
) -> None:
    path = tmp_path / "completion.sqlite"
    store = SqliteWorkStore(path)
    work_id, completion = _complete(store, label="completion")

    result = project_durable_work_yield(work_id, store=store)

    assert result.kind is DurableWorkYieldKind.COMPLETION
    assert result.completion == completion
    assert result.attention is None
    assert result.snapshot.state is WorkState.COMPLETED
    assert result.snapshot.terminal_event_id == completion.completion_id

    restarted = SqliteWorkStore(path)
    recovered = project_durable_work_yield(work_id, store=restarted)
    assert recovered.kind is DurableWorkYieldKind.COMPLETION
    assert recovered.completion == completion


def test_cancelled_work_does_not_fabricate_hde_return_yield(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "cancelled.sqlite")
    work = _work(store, label="cancelled")
    current = store.snapshot(work.work.work_id)
    store.append(
        work.work.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind=WORK_CANCELLED_EVENT,
            payload={"reason": "cancelled outside HDE return v1"},
        ),
    )

    result = project_durable_work_yield(
        work.work.work_id,
        store=store,
    )

    assert result.snapshot.state is WorkState.CANCELLED
    assert result.kind is DurableWorkYieldKind.NONE
    assert result.completion is None
    assert result.attention is None


def test_completed_work_without_structured_completion_fails_closed(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "corrupt-completion.sqlite")
    work = _work(store, label="corrupt-completion")
    current = store.snapshot(work.work.work_id)
    forged = WorkEvent.create(
        work_id=work.work.work_id,
        sequence=current.revision + 1,
        kind=WORK_COMPLETED_EVENT,
        payload={"legacy": "not a WorkCompletion"},
        previous_event_digest=current.last_event_digest,
    )
    store._append_completion(
        work.work.work_id,
        expected_revision=current.revision,
        event=forged,
    )

    with pytest.raises(RuntimeError):
        project_durable_work_yield(
            work.work.work_id,
            store=store,
        )


class _InconsistentStore:
    def __init__(
        self,
        snapshot: WorkSnapshot,
        events: tuple[WorkEvent, ...],
    ) -> None:
        self._snapshot = snapshot
        self._events = events

    def snapshot(self, _work_id: str) -> WorkSnapshot:
        return self._snapshot

    def events(self, _work_id: str) -> tuple[WorkEvent, ...]:
        return self._events


def test_projection_rejects_store_adapter_crossing_requested_work(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "cross-work.sqlite")
    requested = _work(store, label="requested")
    returned = _work(store, label="returned")

    inconsistent = _InconsistentStore(
        snapshot=returned,
        events=(),
    )

    with pytest.raises(
        DurableWorkYieldProjectionError,
        match="requested Work identity",
    ):
        project_durable_work_yield(
            requested.work.work_id,
            store=inconsistent,
        )


def test_projection_fails_closed_if_snapshot_and_chronology_disagree(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "mismatch.sqlite")
    work = _work(store, label="mismatch")
    current = store.snapshot(work.work.work_id)
    event = current.next_event(
        kind="work.observation",
        payload={"x": 1},
    )

    inconsistent = _InconsistentStore(
        snapshot=current,
        events=(event,),
    )

    with pytest.raises(
        DurableWorkYieldProjectionError,
        match="revision",
    ):
        project_durable_work_yield(
            work.work.work_id,
            store=inconsistent,
        )


def test_yield_projection_has_no_hde_irr_authority_or_scheduler_ownership() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "codexia_manual_agent"
        / "workflow_orchestration"
        / "work_yield.py"
    )
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

    forbidden_names = {
        "AuthorizationReceipt",
        "CapabilityHostPort",
        "GovernanceDecision",
        "ProcessExecutor",
        "Scheduler",
        "WorkerNeed",
        "WorkerResult",
    }
    assert forbidden_names.isdisjoint(imported_names)
    assert not any(
        module.startswith(("hde_", "intent_resolution_runtime"))
        for module in imported_modules
    )
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
    assert ".append(" not in source
    assert "progress_once" not in source
