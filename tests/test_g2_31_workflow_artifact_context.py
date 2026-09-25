from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from codexia_manual_agent.artifact_core import (
    ARTIFACT_REF_RECORDED_EVENT,
    ArtifactAdmission,
    ArtifactProjectionError,
    ArtifactRef,
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
    WorkflowImplementationStateError,
    WorkflowStepContext,
)

PROVIDER_REF = "codexia:g2.31-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _start_work(store: SqliteWorkStore, *, source_id: str) -> Work:
    work = Work.create(
        objective="Let Workflow observe durable Work ArtifactRef relations",
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
    binding = WorkflowBinding.create(
        workflow_id=f"codexia:g2.31-{suffix}",
        version="1.0.0",
        definition_digest=_sha(f"g2.31-{suffix}"),
    )
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=store.snapshot(work.work_id),
            binding=binding,
        )
    )
    pack = PackBinding.create(
        pack_id=f"codexia:g2.31-pack-{suffix}",
        version="1.0.0",
        definition_digest=_sha(f"g2.31-pack-{suffix}"),
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


def _artifact(label: str) -> ArtifactRef:
    data = f"bytes:{label}".encode("utf-8")
    return ArtifactRef.create(
        content_sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        locator=f"provider+opaque://artifact/{label}",
        media_type="application/octet-stream",
    )


def _record(
    store: SqliteWorkStore,
    work: Work,
    artifact: ArtifactRef,
) -> ArtifactRef:
    return ArtifactAdmission(store).record(
        store.snapshot(work.work_id),
        artifact,
    )


def _precondition(
    store: SqliteWorkStore,
    work_id: str,
) -> WorkflowStepReadPrecondition:
    return WorkflowStepReadPrecondition.from_snapshot(store.snapshot(work_id))


class CaptureImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.artifacts: tuple[ArtifactRef, ...] | None = None

    def propose(self, context):
        self.artifacts = context.artifacts
        return None


class NoteImplementation:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding

    def propose(self, context):
        return WorkflowCandidate.create(
            run_snapshot=context.workflow,
            work_snapshot=context.work,
            event_kind="workflow.note",
            payload={"artifact_count": len(context.artifacts)},
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


def test_restart_step_observes_durable_work_artifacts_in_order(tmp_path) -> None:
    path = tmp_path / "restart.sqlite"
    store = SqliteWorkStore(path)
    work = _start_work(store, source_id="restart")
    workflow, _ = _start_workflow(store, work, suffix="restart")
    first = _record(store, work, _artifact("first"))
    second = _record(store, work, _artifact("second"))

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
    assert capture.artifacts == (first, second)
    assert restarted.events(work.work_id) == before


def test_artifacts_are_work_level_context_not_workflow_scoped(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work-level.sqlite")
    work = _start_work(store, source_id="work-level")
    first_workflow, _ = _start_workflow(store, work, suffix="first")
    artifact = _record(store, work, _artifact("shared"))
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

    assert first_workflow.run.workflow_run_id != second_workflow.run.workflow_run_id
    assert capture.artifacts == (artifact,)


def test_context_rejects_duplicate_artifact_identity(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "duplicate.sqlite")
    work = _start_work(store, source_id="duplicate")
    workflow, pin = _start_workflow(store, work, suffix="duplicate")
    artifact = _artifact("duplicate")

    with pytest.raises(
        WorkflowImplementationStateError,
        match="duplicate ArtifactRef identity",
    ):
        WorkflowStepContext(
            work=store.snapshot(work.work_id),
            workflow=workflow,
            pack_binding=pin,
            artifacts=(artifact, artifact),
        )


def test_corrupt_artifact_relation_fails_step_before_implementation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "corrupt.sqlite")
    work = _start_work(store, source_id="corrupt")
    workflow, _ = _start_workflow(store, work, suffix="corrupt")
    current = store.snapshot(work.work_id)
    forged = current.next_event(
        kind=ARTIFACT_REF_RECORDED_EVENT,
        payload={"artifact_ref": {"not": "an ArtifactRef"}},
    )
    store.append(
        work.work_id,
        expected_revision=current.revision,
        event=forged,
    )
    capture = CaptureImplementation(workflow.run.binding)

    with pytest.raises(ArtifactProjectionError):
        WorkflowStepService(
            store=store,
            resolver=Resolver(capture),
        ).step(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=PROVIDER_REF,
            precondition=_precondition(store, work.work_id),
        )

    assert capture.artifacts is None


def test_artifact_added_after_step_makes_proposal_stale_via_parent_cas(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "cas.sqlite")
    work = _start_work(store, source_id="cas")
    workflow, _ = _start_workflow(store, work, suffix="cas")
    result = WorkflowStepService(
        store=store,
        resolver=Resolver(NoteImplementation(workflow.run.binding)),
    ).step(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
        precondition=_precondition(store, work.work_id),
    )

    assert isinstance(result.proposal, WorkflowCandidate)
    assert result.proposal.event.payload["payload"]["artifact_count"] == 0

    _record(store, work, _artifact("late"))

    with pytest.raises(WorkConcurrencyError):
        WorkflowProposalAdmissionService(store).admit(result)

    assert all(
        event.kind != "workflow.note"
        for event in store.events(work.work_id)
    )


def test_g2_31_needs_no_separate_artifact_read_set_or_completion_claim() -> None:
    assert "artifact_reads" not in WorkflowStepResult.__dataclass_fields__

    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "workflow_runtime" / "boundary.py",
        root / "workflow_orchestration" / "step.py",
        root / "workflow_orchestration" / "proposal_admission.py",
    ]
    forbidden_names = {
        "ArtifactReadBinding",
        "CompletionClaim",
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
