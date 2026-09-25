from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from codexia_manual_agent.evidence_core import (
    EVIDENCE_REF_RECORDED_EVENT,
    EvidenceAdmission,
    EvidenceBindingError,
    EvidenceIdentityConflictError,
    EvidenceProjectionError,
    EvidenceRef,
    EvidenceStateError,
    project_evidence_ref,
    project_evidence_refs,
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
            objective=f"Record durable evidence relation for {source_id}",
            ingress=WorkIngressBinding.create(
                source_namespace="standalone.api",
                source_id=source_id,
                payload_digest=_sha(f"payload:{source_id}"),
            ),
        )
    )


def _evidence(
    *,
    evidence_id: str | None = None,
    payload: str = "exact evidence payload",
    kind: str = "external.observation.v1",
    locator: str = "provider+opaque://evidence/example",
) -> EvidenceRef:
    return EvidenceRef.create(
        evidence_id=evidence_id or str(uuid4()),
        evidence_digest=_sha(payload),
        evidence_kind=kind,
        locator=locator,
    )


def test_evidence_ref_is_recorded_as_durable_work_relation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "evidence.sqlite")
    initial = _work(store, source_id="record")
    evidence = _evidence()

    recorded = EvidenceAdmission(store).record(initial, evidence)

    assert recorded == evidence
    events = store.events(initial.work.work_id)
    assert len(events) == 1
    assert events[0].kind == EVIDENCE_REF_RECORDED_EVENT
    assert project_evidence_refs(events) == (evidence,)
    assert project_evidence_ref(events, evidence.evidence_id) == evidence


def test_evidence_relation_does_not_verify_locator_reachability(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "opaque.sqlite")
    initial = _work(store, source_id="opaque")
    evidence = _evidence(
        locator="provider+opaque://definitely-not-resolved/here",
    )

    assert EvidenceAdmission(store).record(initial, evidence) == evidence


def test_exact_evidence_record_retry_after_lost_ack_is_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    initial = _work(store, source_id="retry")
    evidence = _evidence()
    admission = EvidenceAdmission(store)

    first = admission.record(initial, evidence)
    retried = admission.record(initial, evidence)

    assert first == evidence
    assert retried == evidence
    assert len(store.events(initial.work.work_id)) == 1
    assert store.snapshot(initial.work.work_id).revision == 1


def test_evidence_identity_drift_fails_closed_without_new_event(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "drift.sqlite")
    initial = _work(store, source_id="drift")
    evidence_id = str(uuid4())
    original = _evidence(evidence_id=evidence_id, payload="first")
    EvidenceAdmission(store).record(initial, original)

    drifted = _evidence(
        evidence_id=evidence_id,
        payload="second",
        kind=original.evidence_kind,
        locator=original.locator,
    )

    with pytest.raises(
        EvidenceIdentityConflictError,
        match="different exact semantics",
    ):
        EvidenceAdmission(store).record(initial, drifted)

    assert project_evidence_refs(store.events(initial.work.work_id)) == (original,)
    assert store.snapshot(initial.work.work_id).revision == 1


def test_idempotent_retry_still_requires_exact_work_binding(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "binding.sqlite")
    initial = _work(store, source_id="binding")
    evidence = _evidence()
    EvidenceAdmission(store).record(initial, evidence)

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

    with pytest.raises(EvidenceBindingError, match="changed Work binding"):
        EvidenceAdmission(store).record(forged_snapshot, evidence)


def test_same_evidence_ref_may_be_related_to_independent_works(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "shared.sqlite")
    first_work = _work(store, source_id="first")
    second_work = _work(store, source_id="second")
    evidence = _evidence()
    admission = EvidenceAdmission(store)

    assert admission.record(first_work, evidence) == evidence
    assert admission.record(second_work, evidence) == evidence

    assert project_evidence_refs(
        store.events(first_work.work.work_id)
    ) == (evidence,)
    assert project_evidence_refs(
        store.events(second_work.work.work_id)
    ) == (evidence,)


def test_stale_work_snapshot_cannot_publish_evidence_relation(tmp_path) -> None:
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
        EvidenceAdmission(store).record(initial, _evidence())

    assert project_evidence_refs(store.events(initial.work.work_id)) == ()


def test_terminal_work_cannot_record_new_evidence_relation(tmp_path) -> None:
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

    with pytest.raises(EvidenceStateError, match="terminal Work"):
        EvidenceAdmission(store).record(terminal, _evidence())


def test_projection_rejects_duplicate_evidence_identity_in_one_work(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "duplicate.sqlite")
    initial = _work(store, source_id="duplicate")
    evidence = _evidence()

    first = initial.next_event(
        kind=EVIDENCE_REF_RECORDED_EVENT,
        payload={"evidence_ref": evidence.to_dict()},
    )
    current = store.append(
        initial.work.work_id,
        expected_revision=initial.revision,
        event=first,
    )
    duplicate = current.next_event(
        kind=EVIDENCE_REF_RECORDED_EVENT,
        payload={"evidence_ref": evidence.to_dict()},
    )
    store.append(
        initial.work.work_id,
        expected_revision=current.revision,
        event=duplicate,
    )

    with pytest.raises(
        EvidenceProjectionError,
        match="durably recorded twice",
    ):
        project_evidence_refs(store.events(initial.work.work_id))


def test_projection_rejects_evidence_payload_shape_drift(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "shape.sqlite")
    initial = _work(store, source_id="shape")
    evidence = _evidence()
    forged = initial.next_event(
        kind=EVIDENCE_REF_RECORDED_EVENT,
        payload={
            "evidence_ref": evidence.to_dict(),
            "unexpected": True,
        },
    )
    store.append(
        initial.work.work_id,
        expected_revision=initial.revision,
        event=forged,
    )

    with pytest.raises(EvidenceProjectionError, match="payload is not exact"):
        project_evidence_refs(store.events(initial.work.work_id))


def test_generic_workflow_candidate_cannot_manufacture_evidence_relation(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "workflow.sqlite")
    initial = _work(store, source_id="workflow")
    binding = WorkflowBinding.create(
        workflow_id="codexia:g2.33-workflow",
        version="1.0.0",
        definition_digest=_sha("g2.33-workflow"),
    )
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(snapshot=initial, binding=binding)
    )
    current = store.snapshot(initial.work.work_id)
    evidence = _evidence()
    forged = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=current,
        event_kind=EVIDENCE_REF_RECORDED_EVENT,
        payload={"evidence_ref": evidence.to_dict()},
    )

    with pytest.raises(
        WorkflowImplementationOwnershipError,
        match="cannot manufacture",
    ):
        validate_generic_workflow_candidate_ownership(forged)


def test_g2_33_evidence_relation_does_not_invent_verification_or_completion() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "evidence_core" / "models.py",
        root / "evidence_core" / "projection.py",
        root / "evidence_core" / "admission.py",
    ]
    forbidden_names = {
        "CompletionClaim",
        "WorkCompletion",
        "ConclusionVerdict",
        "AuthorizationReceipt",
        "ProcessExecutor",
        "ArtifactRef",
    }
    forbidden_modules = {
        "simple_work",
        "lab",
        "authority",
        "execution",
        "mutation",
        "providers",
        "artifact_core",
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
