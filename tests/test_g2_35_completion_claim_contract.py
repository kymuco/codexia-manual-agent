from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from codexia_manual_agent.artifact_core import ArtifactRef
from codexia_manual_agent.completion_core import (
    CompletionClaim,
    InvalidCompletionClaim,
)
from codexia_manual_agent.evidence_core import EvidenceRef
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import (
    WORK_CANCELLED_EVENT,
    SqliteWorkStore,
    Work,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work(store: SqliteWorkStore, *, source_id: str):
    return store.create(
        Work.create(
            objective=f"Prove CompletionClaim semantics for {source_id}",
            ingress=WorkIngressBinding.create(
                source_namespace="standalone.api",
                source_id=source_id,
                payload_digest=_sha(f"payload:{source_id}"),
            ),
        )
    )


def _started(store: SqliteWorkStore, *, source_id: str):
    work = _work(store, source_id=source_id)
    workflow_binding = WorkflowBinding.create(
        workflow_id=f"codexia:g2.35-{source_id}",
        version="1.0.0",
        definition_digest=_sha(f"workflow:{source_id}"),
    )
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=work,
            binding=workflow_binding,
        )
    )
    pack = PackBinding.create(
        pack_id=f"codexia:g2.35-pack-{source_id}",
        version="1.0.0",
        definition_digest=_sha(f"pack:{source_id}"),
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


def _artifact(label: str) -> ArtifactRef:
    raw = f"artifact:{label}".encode("utf-8")
    return ArtifactRef.create(
        content_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        locator=f"provider+opaque://artifact/{label}",
        media_type="application/octet-stream",
    )


def _evidence(label: str) -> EvidenceRef:
    return EvidenceRef.create(
        evidence_id=str(uuid4()),
        evidence_digest=_sha(f"evidence:{label}"),
        evidence_kind="external.observation.v1",
        locator=f"provider+opaque://evidence/{label}",
    )


def _claim(
    store: SqliteWorkStore,
    work_id: str,
    workflow,
    pin,
    *,
    artifacts=(),
    evidence=(),
) -> CompletionClaim:
    return CompletionClaim.create(
        snapshot=store.snapshot(work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="The exact Work objective is satisfied.",
        artifact_refs=artifacts,
        evidence_refs=evidence,
    )


def _digest_payload(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_completion_claim_binds_exact_work_workflow_and_pack_checkpoint(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "claim.sqlite")
    work, workflow, pin = _started(store, source_id="binding")

    claim = _claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    snapshot = store.snapshot(work.work.work_id)

    assert claim.work_id == snapshot.work.work_id
    assert claim.work_digest == snapshot.work.work_digest
    assert claim.work_revision == snapshot.revision
    assert claim.work_event_digest == snapshot.last_event_digest
    assert claim.workflow_run_id == workflow.run.workflow_run_id
    assert claim.workflow_run_digest == workflow.run.run_digest
    assert claim.pack_binding_digest == pin.pack.binding_digest
    assert claim.pack_pin_id == pin.binding_id
    assert claim.pack_pin_digest == pin.pin_digest


def test_completion_claim_basis_is_explicit_canonical_subset(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "basis.sqlite")
    work, workflow, pin = _started(store, source_id="basis")
    first_artifact = _artifact("a")
    second_artifact = _artifact("b")
    first_evidence = _evidence("a")
    second_evidence = _evidence("b")

    claim = _claim(
        store,
        work.work.work_id,
        workflow,
        pin,
        artifacts=(second_artifact, first_artifact),
        evidence=(second_evidence, first_evidence),
    )

    assert claim.artifact_refs == tuple(
        sorted(
            (first_artifact, second_artifact),
            key=lambda item: item.artifact_id,
        )
    )
    assert claim.evidence_refs == tuple(
        sorted(
            (first_evidence, second_evidence),
            key=lambda item: item.evidence_id,
        )
    )


def test_completion_claim_allows_empty_basis_for_pack_policy_to_decide(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "empty.sqlite")
    work, workflow, pin = _started(store, source_id="empty")

    claim = _claim(store, work.work.work_id, workflow, pin)

    assert claim.artifact_refs == ()
    assert claim.evidence_refs == ()


def test_completion_claim_contract_does_not_assert_basis_work_membership(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "detached.sqlite")
    work, workflow, pin = _started(store, source_id="detached")
    detached = _evidence("not-recorded-in-work")

    claim = _claim(
        store,
        work.work.work_id,
        workflow,
        pin,
        evidence=(detached,),
    )

    assert claim.evidence_refs == (detached,)


def test_completion_claim_rejects_cross_work_workflow_binding(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "cross.sqlite")
    first, first_workflow, first_pin = _started(store, source_id="first")
    second, second_workflow, _ = _started(store, source_id="second")

    with pytest.raises(
        InvalidCompletionClaim,
        match="WorkflowRun belongs to another Work",
    ):
        CompletionClaim.create(
            snapshot=store.snapshot(first.work.work_id),
            workflow=second_workflow,
            pack_binding=first_pin,
            summary="Should not cross Work identity.",
        )

    assert first_workflow.run.work_id != second.work.work_id


def test_completion_claim_rejects_cross_pack_binding(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "pack.sqlite")
    first, first_workflow, _ = _started(store, source_id="first-pack")
    _, _, second_pin = _started(store, source_id="second-pack")

    with pytest.raises(
        InvalidCompletionClaim,
        match="Pack binding belongs to another Work",
    ):
        CompletionClaim.create(
            snapshot=store.snapshot(first.work.work_id),
            workflow=first_workflow,
            pack_binding=second_pin,
            summary="Should not cross Pack provenance.",
        )


def test_completion_claim_rejects_terminal_work(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "terminal.sqlite")
    work, workflow, pin = _started(store, source_id="terminal")
    current = store.snapshot(work.work.work_id)
    cancelled = current.next_event(
        kind=WORK_CANCELLED_EVENT,
        payload={"reason": "test"},
    )
    terminal = store.append(
        work.work.work_id,
        expected_revision=current.revision,
        event=cancelled,
    )

    with pytest.raises(
        InvalidCompletionClaim,
        match="requires active Work",
    ):
        CompletionClaim.create(
            snapshot=terminal,
            workflow=workflow,
            pack_binding=pin,
            summary="Terminal Work cannot produce another claim.",
        )


def test_completion_claim_rejects_duplicate_basis_identity(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "duplicates.sqlite")
    work, workflow, pin = _started(store, source_id="duplicates")
    artifact = _artifact("same")
    evidence = _evidence("same")

    with pytest.raises(
        InvalidCompletionClaim,
        match="repeat ArtifactRef identity",
    ):
        _claim(
            store,
            work.work.work_id,
            workflow,
            pin,
            artifacts=(artifact, artifact),
        )

    with pytest.raises(
        InvalidCompletionClaim,
        match="repeat EvidenceRef identity",
    ):
        _claim(
            store,
            work.work.work_id,
            workflow,
            pin,
            evidence=(evidence, evidence),
        )


def test_completion_claim_round_trip_is_exact(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "roundtrip.sqlite")
    work, workflow, pin = _started(store, source_id="roundtrip")
    claim = _claim(
        store,
        work.work.work_id,
        workflow,
        pin,
        artifacts=(_artifact("result"),),
        evidence=(_evidence("verification"),),
    )

    assert CompletionClaim.from_dict(claim.to_dict()) == claim


def test_completion_claim_rejects_tampered_summary_with_stale_digest(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "tamper.sqlite")
    work, workflow, pin = _started(store, source_id="tamper")
    claim = _claim(store, work.work.work_id, workflow, pin)
    payload = claim.to_dict()
    payload["summary"] = "Different claim."

    with pytest.raises(
        InvalidCompletionClaim,
        match="digest mismatch",
    ):
        CompletionClaim.from_dict(payload)


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_completion_claim_rejects_schema_type_drift_even_with_valid_digest(
    tmp_path,
    schema_version,
) -> None:
    store = SqliteWorkStore(tmp_path / f"schema-{schema_version!r}.sqlite")
    work, workflow, pin = _started(
        store,
        source_id=f"schema-{schema_version!r}",
    )
    claim = _claim(store, work.work.work_id, workflow, pin)
    payload = claim.to_dict()
    payload["schema_version"] = schema_version
    payload["claim_digest"] = _digest_payload(
        {
            key: value
            for key, value in payload.items()
            if key != "claim_digest"
        }
    )

    with pytest.raises(
        InvalidCompletionClaim,
        match="Unsupported CompletionClaim schema",
    ):
        CompletionClaim.from_dict(payload)


def test_completion_claim_rejects_noncanonical_basis_order_on_decode(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "order.sqlite")
    work, workflow, pin = _started(store, source_id="order")
    first = _artifact("first")
    second = _artifact("second")
    ordered = tuple(sorted((first, second), key=lambda item: item.artifact_id))
    claim = _claim(
        store,
        work.work.work_id,
        workflow,
        pin,
        artifacts=ordered,
    )
    payload = claim.to_dict()
    payload["artifact_refs"] = list(reversed(payload["artifact_refs"]))
    payload["claim_digest"] = _digest_payload(
        {
            key: value
            for key, value in payload.items()
            if key != "claim_digest"
        }
    )

    with pytest.raises(
        InvalidCompletionClaim,
        match="canonical sorted",
    ):
        CompletionClaim.from_dict(payload)


def test_completion_claim_is_not_work_completion_or_admission_surface() -> None:
    assert not hasattr(CompletionClaim, "to_event")
    assert not hasattr(CompletionClaim, "admit")
    assert not hasattr(CompletionClaim, "complete")


def test_g2_35_claim_contract_does_not_invent_terminal_or_domain_policy() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "completion_core" / "models.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    forbidden_names = {
        "WORK_COMPLETED_EVENT",
        "WorkCompletion",
        "DelegationCompletionGuard",
        "WorkStore",
        "EvidenceVerdict",
        "ConclusionVerdict",
        "AuthorizationReceipt",
        "ProcessExecutor",
        "CleanupProof",
        "WorkActorKind",
    }
    forbidden_modules = {
        "delegation_core",
        "simple_work",
        "lab",
        "authority",
        "execution",
        "mutation",
        "providers",
    }

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
