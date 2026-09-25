from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from codexia_manual_agent.artifact_core import (
    ARTIFACT_REF_RECORDED_EVENT,
    ArtifactAdmission,
    ArtifactIdentityConflictError,
    ArtifactProjectionError,
    ArtifactRef,
    ArtifactStateError,
    project_artifact_ref,
    project_artifact_refs,
)
from codexia_manual_agent.work_core import (
    WORK_CANCELLED_EVENT,
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkIngressBinding,
    WorkSnapshot,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
)
from codexia_manual_agent.workflow_runtime import (
    WorkflowImplementationOwnershipError,
    validate_generic_workflow_candidate_ownership,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work(store: SqliteWorkStore, *, source_id: str):
    return store.create(
        Work.create(
            objective=f"Record durable artifact relation for {source_id}",
            ingress=WorkIngressBinding.create(
                source_namespace="standalone.api",
                source_id=source_id,
                payload_digest=_sha(f"payload:{source_id}"),
            ),
        )
    )


def _artifact(
    *,
    artifact_id: str | None = None,
    content: str = "artifact bytes",
    locator: str = "provider+opaque://artifact/example",
) -> ArtifactRef:
    encoded = content.encode("utf-8")
    return ArtifactRef.create(
        artifact_id=artifact_id,
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded),
        locator=locator,
        media_type="application/octet-stream",
    )


def test_artifact_ref_is_recorded_as_durable_work_relation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "artifact.sqlite")
    initial = _work(store, source_id="record")
    artifact = _artifact()

    recorded = ArtifactAdmission(store).record(initial, artifact)

    assert recorded == artifact
    events = store.events(initial.work.work_id)
    assert len(events) == 1
    assert events[0].kind == ARTIFACT_REF_RECORDED_EVENT
    assert project_artifact_refs(events) == (artifact,)
    assert project_artifact_ref(events, artifact.artifact_id) == artifact


def test_exact_artifact_record_retry_after_lost_ack_is_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    initial = _work(store, source_id="retry")
    artifact = _artifact()
    admission = ArtifactAdmission(store)

    first = admission.record(initial, artifact)
    retried = admission.record(initial, artifact)

    assert first == artifact
    assert retried == artifact
    assert len(store.events(initial.work.work_id)) == 1
    assert store.snapshot(initial.work.work_id).revision == 1


def test_artifact_identity_drift_fails_closed_without_new_event(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "drift.sqlite")
    initial = _work(store, source_id="drift")
    artifact_id = str(uuid4())
    original = _artifact(artifact_id=artifact_id, content="first")
    ArtifactAdmission(store).record(initial, original)

    drifted = _artifact(
        artifact_id=artifact_id,
        content="second",
        locator=original.locator,
    )

    with pytest.raises(
        ArtifactIdentityConflictError,
        match="different exact semantics",
    ):
        ArtifactAdmission(store).record(initial, drifted)

    assert project_artifact_refs(store.events(initial.work.work_id)) == (original,)
    assert store.snapshot(initial.work.work_id).revision == 1


def test_idempotent_retry_still_requires_exact_work_binding(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "binding.sqlite")
    initial = _work(store, source_id="binding")
    artifact = _artifact()
    ArtifactAdmission(store).record(initial, artifact)

    foreign = Work.create(
        objective="Different immutable Work semantics",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="foreign-binding",
            payload_digest=_sha("foreign-binding"),
        ),
        work_id=initial.work.work_id,
    )
    forged_snapshot = WorkSnapshot(
        work=foreign,
        state=WorkState.ACTIVE,
        revision=initial.revision,
        last_event_digest=initial.last_event_digest,
        terminal_event_id=None,
    )

    from codexia_manual_agent.artifact_core import ArtifactBindingError

    with pytest.raises(ArtifactBindingError, match="changed Work binding"):
        ArtifactAdmission(store).record(forged_snapshot, artifact)


def test_same_artifact_ref_may_be_related_to_independent_works(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "shared.sqlite")
    first_work = _work(store, source_id="first")
    second_work = _work(store, source_id="second")
    artifact = _artifact()
    admission = ArtifactAdmission(store)

    assert admission.record(first_work, artifact) == artifact
    assert admission.record(second_work, artifact) == artifact

    assert project_artifact_refs(
        store.events(first_work.work.work_id)
    ) == (artifact,)
    assert project_artifact_refs(
        store.events(second_work.work.work_id)
    ) == (artifact,)


def test_stale_work_snapshot_cannot_publish_artifact_relation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    initial = _work(store, source_id="stale")
    unrelated = initial.next_event(
        kind="work.progress",
        payload={"step": 1},
    )
    store.append(
        initial.work.work_id,
        expected_revision=initial.revision,
        event=unrelated,
    )

    with pytest.raises(WorkConcurrencyError):
        ArtifactAdmission(store).record(initial, _artifact())

    assert project_artifact_refs(store.events(initial.work.work_id)) == ()


def test_terminal_work_cannot_record_new_artifact_relation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "terminal.sqlite")
    initial = _work(store, source_id="terminal")
    cancelled = initial.next_event(
        kind=WORK_CANCELLED_EVENT,
        payload={"reason": "test"},
    )
    terminal = store.append(
        initial.work.work_id,
        expected_revision=initial.revision,
        event=cancelled,
    )

    with pytest.raises(ArtifactStateError, match="terminal Work"):
        ArtifactAdmission(store).record(terminal, _artifact())


def test_projection_rejects_duplicate_artifact_identity_in_one_work(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "duplicate.sqlite")
    initial = _work(store, source_id="duplicate")
    artifact = _artifact()

    first = initial.next_event(
        kind=ARTIFACT_REF_RECORDED_EVENT,
        payload={"artifact_ref": artifact.to_dict()},
    )
    current = store.append(
        initial.work.work_id,
        expected_revision=initial.revision,
        event=first,
    )
    duplicate = current.next_event(
        kind=ARTIFACT_REF_RECORDED_EVENT,
        payload={"artifact_ref": artifact.to_dict()},
    )
    store.append(
        initial.work.work_id,
        expected_revision=current.revision,
        event=duplicate,
    )

    with pytest.raises(
        ArtifactProjectionError,
        match="durably recorded twice",
    ):
        project_artifact_refs(store.events(initial.work.work_id))


def test_projection_rejects_artifact_payload_shape_drift(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "shape.sqlite")
    initial = _work(store, source_id="shape")
    artifact = _artifact()
    forged = initial.next_event(
        kind=ARTIFACT_REF_RECORDED_EVENT,
        payload={
            "artifact_ref": artifact.to_dict(),
            "unexpected": True,
        },
    )
    store.append(
        initial.work.work_id,
        expected_revision=initial.revision,
        event=forged,
    )

    with pytest.raises(ArtifactProjectionError, match="payload is not exact"):
        project_artifact_refs(store.events(initial.work.work_id))


def test_generic_workflow_candidate_cannot_manufacture_artifact_relation(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "workflow.sqlite")
    initial = _work(store, source_id="workflow")
    binding = WorkflowBinding.create(
        workflow_id="codexia:g2.30-workflow",
        version="1.0.0",
        definition_digest=_sha("g2.30-workflow"),
    )
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(snapshot=initial, binding=binding)
    )
    current = store.snapshot(initial.work.work_id)
    artifact = _artifact()
    forged = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=current,
        event_kind=ARTIFACT_REF_RECORDED_EVENT,
        payload={"artifact_ref": artifact.to_dict()},
    )

    with pytest.raises(
        WorkflowImplementationOwnershipError,
        match="cannot manufacture",
    ):
        validate_generic_workflow_candidate_ownership(forged)


def test_g2_30_artifact_relation_does_not_invent_evidence_or_completion_ontology() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "artifact_core" / "models.py",
        root / "artifact_core" / "projection.py",
        root / "artifact_core" / "admission.py",
    ]
    forbidden_names = {
        "EvidenceRef",
        "CompletionClaim",
        "WorkCompletion",
        "WorkArtifact",
        "Scheduler",
        "AuthorizationReceipt",
        "ProcessExecutor",
    }
    forbidden_modules = {
        "simple_work",
        "lab",
        "authority",
        "execution",
        "mutation",
        "providers",
    }

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_names: set[str] = set()
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_names.update(alias.name for alias in node.names)
                if node.module is not None:
                    imported_modules.add(node.module)

        assert forbidden_names.isdisjoint(imported_names)
        assert not any(
            any(part in module for part in forbidden_modules)
            for module in imported_modules
        )
        assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
