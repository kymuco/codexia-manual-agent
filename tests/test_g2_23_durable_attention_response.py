from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from codexia_manual_agent.attention_core import (
    ATTENTION_RESPONSE_RECORDED_EVENT,
    AttentionAdmission,
    AttentionNeed,
    AttentionResponse,
    AttentionResponseIngressConflictError,
    InvalidAttentionRecord,
    project_attention_need,
    project_attention_responses,
)
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
from codexia_manual_agent.workflow_runtime import (
    WorkflowImplementationBoundary,
    WorkflowImplementationError,
    WorkflowStepContext,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _started(store: SqliteWorkStore, *, source_id: str):
    binding = WorkflowBinding.create(
        workflow_id="codexia:g2.23-workflow",
        version="1.0.0",
        definition_digest=_sha("g2.23-workflow"),
    )
    work = Work.create(
        objective="Record exact human response evidence without hidden resume",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(snapshot=initial, binding=binding)
    )
    pack = PackBinding.create(
        pack_id="codexia:g2.23-pack",
        version="1.0.0",
        definition_digest=_sha("g2.23-pack"),
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
    need = AttentionNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(work.work_id),
        question="Which direction should continue?",
        reason="A human semantic choice is required.",
    )
    admitted_need = AttentionAdmission(store).admit_need(need)
    return work, workflow, pin, admitted_need


def _response(
    store: SqliteWorkStore,
    need: AttentionNeed,
    *,
    text: str = "Continue with direction A.",
    source_id: str = "human-turn-1",
) -> AttentionResponse:
    return AttentionResponse.create(
        need=need,
        snapshot=store.snapshot(need.work_id),
        response_text=text,
        source_namespace="standalone.ui",
        source_id=source_id,
    )


def test_attention_response_is_durable_restart_safe_need_evidence(tmp_path) -> None:
    path = tmp_path / "response.sqlite"
    store = SqliteWorkStore(path)
    work, _, _, need = _started(store, source_id="durable")
    response = _response(store, need)

    admitted = AttentionAdmission(store).admit_response(response)

    assert admitted == response
    assert store.events(work.work_id)[-1].kind == ATTENTION_RESPONSE_RECORDED_EVENT
    restarted = SqliteWorkStore(path)
    assert project_attention_responses(restarted.events(work.work_id)) == (
        response,
    )
    assert project_attention_need(
        restarted.events(work.work_id),
        need.attention_id,
    ) == need


def test_retry_same_capture_source_after_restart_is_ingress_idempotent(
    tmp_path,
) -> None:
    path = tmp_path / "retry.sqlite"
    store = SqliteWorkStore(path)
    work, _, _, need = _started(store, source_id="retry")
    first = AttentionAdmission(store).admit_response(_response(store, need))

    restarted = SqliteWorkStore(path)
    retry = _response(restarted, need)
    assert retry.response_id != first.response_id
    before = restarted.events(work.work_id)

    recovered = AttentionAdmission(restarted).admit_response(retry)

    assert recovered == first
    assert restarted.events(work.work_id) == before


def test_capture_source_reuse_with_different_payload_conflicts(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "conflict.sqlite")
    work, _, _, need = _started(store, source_id="conflict")
    service = AttentionAdmission(store)
    service.admit_response(_response(store, need))
    before = store.events(work.work_id)

    conflicting = _response(
        store,
        need,
        text="No, continue with direction B.",
    )
    with pytest.raises(AttentionResponseIngressConflictError):
        service.admit_response(conflicting)

    assert store.events(work.work_id) == before


def test_distinct_capture_sources_preserve_correction_chronology(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "correction.sqlite")
    work, _, _, need = _started(store, source_id="correction")
    service = AttentionAdmission(store)
    first = service.admit_response(
        _response(store, need, text="Choose A.", source_id="turn-1")
    )
    second = service.admit_response(
        _response(store, need, text="Correction: choose B.", source_id="turn-2")
    )

    assert first.response_id != second.response_id
    assert project_attention_responses(store.events(work.work_id)) == (
        first,
        second,
    )


def test_stale_response_capture_uses_existing_work_cas(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    work, _, _, need = _started(store, source_id="stale")
    response = _response(store, need)
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
        AttentionAdmission(store).admit_response(response)


def test_response_does_not_resolve_need_or_change_work_lifecycle(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "no-resume.sqlite")
    work, _, _, need = _started(store, source_id="no-resume")
    before_need = project_attention_need(
        store.events(work.work_id),
        need.attention_id,
    )

    AttentionAdmission(store).admit_response(_response(store, need))

    after_need = project_attention_need(
        store.events(work.work_id),
        need.attention_id,
    )
    assert after_need == before_need == need
    assert store.snapshot(work.work_id).state is WorkState.ACTIVE


def test_workflow_implementation_cannot_manufacture_human_response(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "ownership.sqlite")
    _, workflow, pin, need = _started(store, source_id="ownership")
    response = _response(store, need)
    context = WorkflowStepContext(
        work=store.snapshot(need.work_id),
        workflow=workflow,
        pack_binding=pin,
        attentions=(need,),
    )

    class Implementation:
        binding = workflow.run.binding

        def propose(self, _context):
            return response

    with pytest.raises(WorkflowImplementationError):
        WorkflowImplementationBoundary().prepare(Implementation(), context)


def test_response_source_namespace_is_capture_provenance_not_free_text(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "namespace.sqlite")
    _, _, _, need = _started(store, source_id="namespace")

    with pytest.raises(InvalidAttentionRecord):
        AttentionResponse.create(
            need=need,
            snapshot=store.snapshot(need.work_id),
            response_text="A",
            source_namespace="Standalone.UI",
            source_id="turn-1",
        )


def test_response_schema_has_no_resume_authority_or_notification_state() -> None:
    forbidden = {
        "approved",
        "authority",
        "resume",
        "resumable",
        "resolved",
        "notification_channel",
        "delivered",
        "execute",
        "permission",
    }
    assert forbidden.isdisjoint(AttentionResponse.__dataclass_fields__)


def test_attention_response_core_has_no_scheduler_authority_or_execution_imports() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "attention_core" / "models.py",
        root / "attention_core" / "projection.py",
        root / "attention_core" / "admission.py",
    ]
    forbidden_names = {
        "Scheduler",
        "AuthorizationReceipt",
        "CognitionPort",
        "CapabilityHostPort",
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
