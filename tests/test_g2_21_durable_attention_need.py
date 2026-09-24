from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from codexia_manual_agent.attention_core import (
    ATTENTION_NEED_DECLARED_EVENT,
    AttentionAdmission,
    AttentionNeed,
    AttentionStateError,
    InvalidAttentionRecord,
    project_attention_need,
    project_attention_needs,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WORKFLOW_COMPLETED_EVENT,
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
    project_workflow_run,
)
from codexia_manual_agent.workflow_runtime import (
    WorkflowImplementationBoundary,
    WorkflowImplementationOwnershipError,
    WorkflowStepContext,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:g2.21-workflow",
        version="1.0.0",
        definition_digest=_sha("g2.21-workflow"),
    )


def _started(store: SqliteWorkStore, *, source_id: str):
    binding = _binding()
    work = Work.create(
        objective="Represent one exact human-attention boundary",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    workflow_run = WorkflowRun.create(snapshot=initial, binding=binding)
    workflow = WorkflowAdmission(store).admit_start(workflow_run)
    pack = PackBinding.create(
        pack_id="codexia:g2.21-pack",
        version="1.0.0",
        definition_digest=_sha("g2.21-pack"),
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
    return work, workflow, pin


def _need(store: SqliteWorkStore, workflow, *, question: str = "Choose A or B?"):
    return AttentionNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow.run.work_id),
        question=question,
        reason="The alternatives have materially different future costs.",
    )


def test_attention_need_is_durable_restart_safe_work_truth(tmp_path) -> None:
    path = tmp_path / "attention.sqlite"
    store = SqliteWorkStore(path)
    work, workflow, _ = _started(store, source_id="durable")
    need = _need(store, workflow)

    admitted = AttentionAdmission(store).admit_need(need)

    assert admitted == need
    event = store.events(work.work_id)[-1]
    assert event.kind == ATTENTION_NEED_DECLARED_EVENT
    restarted = SqliteWorkStore(path)
    assert project_attention_need(
        restarted.events(work.work_id),
        need.attention_id,
    ) == need


def test_attention_need_binds_exact_question_reason_and_workflow_state(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "binding.sqlite")
    _, workflow, _ = _started(store, source_id="binding")
    need = _need(store, workflow, question="Approve the semantic direction?")

    assert need.question == "Approve the semantic direction?"
    assert need.reason == "The alternatives have materially different future costs."
    assert need.work_id == workflow.run.work_id
    assert need.workflow_run_id == workflow.run.workflow_run_id
    assert need.workflow_run_digest == workflow.run.run_digest


def test_exact_retry_after_later_work_event_remains_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    work, workflow, _ = _started(store, source_id="retry")
    need = _need(store, workflow)
    service = AttentionAdmission(store)
    first = service.admit_need(need)

    current_workflow = project_workflow_run(
        store.events(work.work_id),
        workflow.run.workflow_run_id,
    )
    note = WorkflowCandidate.create(
        run_snapshot=current_workflow,
        work_snapshot=store.snapshot(work.work_id),
        event_kind="workflow.note",
        payload={"after": "attention"},
    )
    WorkflowAdmission(store).admit_candidate(note)
    before_retry = store.events(work.work_id)

    retried = service.admit_need(need)

    assert retried == first
    assert store.events(work.work_id) == before_retry


def test_new_attention_need_uses_existing_work_cas(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    work, workflow, _ = _started(store, source_id="stale")
    need = _need(store, workflow)
    current = store.snapshot(work.work_id)
    store.append(
        work.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.external-observation",
            payload={"source": "concurrent"},
        ),
    )

    with pytest.raises(WorkConcurrencyError):
        AttentionAdmission(store).admit_need(need)


def test_terminal_workflow_cannot_declare_new_attention_need(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "terminal.sqlite")
    work, workflow, _ = _started(store, source_id="terminal")
    need = _need(store, workflow)
    current = project_workflow_run(
        store.events(work.work_id),
        workflow.run.workflow_run_id,
    )
    completion = WorkflowCandidate.create(
        run_snapshot=current,
        work_snapshot=store.snapshot(work.work_id),
        event_kind=WORKFLOW_COMPLETED_EVENT,
        payload={"reason": "closed before attention declaration"},
    )
    WorkflowAdmission(store).admit_candidate(completion)

    with pytest.raises(AttentionStateError):
        AttentionAdmission(store).admit_need(need)


def test_multiple_attention_needs_are_distinct_durable_boundaries(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "multiple.sqlite")
    _, workflow, _ = _started(store, source_id="multiple")
    first = AttentionAdmission(store).admit_need(_need(store, workflow))
    current = project_workflow_run(
        store.events(workflow.run.work_id),
        workflow.run.workflow_run_id,
    )
    second = AttentionNeed.create(
        workflow=current,
        snapshot=store.snapshot(workflow.run.work_id),
        question="Choose the revised scope?",
        reason="The scope change affects the remaining trajectory.",
    )
    AttentionAdmission(store).admit_need(second)

    assert project_attention_needs(store.events(workflow.run.work_id)) == (
        first,
        second,
    )


def test_attention_record_is_not_notification_authority_or_answer_state() -> None:
    assert set(AttentionNeed.__dataclass_fields__) == {
        "schema_version",
        "attention_id",
        "created_at",
        "work_id",
        "work_digest",
        "workflow_run_id",
        "workflow_run_digest",
        "question",
        "reason",
        "start_revision",
        "start_event_digest",
        "need_digest",
    }
    forbidden = {
        "approved",
        "authority",
        "notify_at",
        "notification_channel",
        "delivered",
        "answered",
        "answer",
        "resume",
        "scheduler",
    }
    assert forbidden.isdisjoint(AttentionNeed.__dataclass_fields__)


@pytest.mark.parametrize(
    "field,value",
    [
        ("question", ""),
        ("question", " padded "),
        ("reason", ""),
        ("reason", " padded "),
    ],
)
def test_attention_text_is_bounded_canonical(
    tmp_path,
    field: str,
    value: str,
) -> None:
    store = SqliteWorkStore(tmp_path / f"{field}.sqlite")
    _, workflow, _ = _started(store, source_id=f"text-{field}")
    kwargs = {
        "workflow": workflow,
        "snapshot": store.snapshot(workflow.run.work_id),
        "question": "Choose A or B?",
        "reason": "Human judgment changes the semantic trajectory.",
    }
    kwargs[field] = value

    with pytest.raises(InvalidAttentionRecord):
        AttentionNeed.create(**kwargs)


def test_generic_workflow_candidate_cannot_manufacture_attention_namespace(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "ownership.sqlite")
    work, workflow, pin = _started(store, source_id="ownership")
    candidate = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=store.snapshot(work.work_id),
        event_kind="attention.future-core-event",
        payload={"probe": True},
    )

    class Implementation:
        binding = _binding()

        def propose(self, _context):
            return candidate

    context = WorkflowStepContext(
        work=store.snapshot(work.work_id),
        workflow=workflow,
        pack_binding=pin,
    )

    with pytest.raises(WorkflowImplementationOwnershipError):
        WorkflowImplementationBoundary().prepare(Implementation(), context)


def test_attention_core_has_no_notification_authority_scheduler_or_execution_imports() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    attention = root / "attention_core"

    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for path in attention.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    imported_modules.add(node.module)
                imported_names.update(alias.name for alias in node.names)

    forbidden_names = {
        "Scheduler",
        "AuthorizationReceipt",
        "ApprovalMode",
        "CognitionPort",
        "CapabilityHostPort",
    }
    assert forbidden_names.isdisjoint(imported_names)
    assert not any(
        module.endswith(
            (".authority", ".execution", ".providers", ".standalone_host")
        )
        for module in imported_modules
    )
