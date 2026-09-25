from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from codexia_manual_agent.evidence_core import EvidenceRef, InvalidEvidenceRef


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_payload(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_evidence_ref_requires_existing_evidence_identity() -> None:
    with pytest.raises(TypeError):
        EvidenceRef.create(
            evidence_digest=_sha("payload"),
            evidence_kind="attention.response.v1",
            locator="work-event://response/example",
        )


def test_evidence_ref_binds_identity_to_exact_evidence_kind_and_locator() -> None:
    evidence_id = str(uuid4())
    first = EvidenceRef.create(
        evidence_id=evidence_id,
        evidence_digest=_sha("exact evidence payload"),
        evidence_kind="attention.response.v1",
        locator="work-event://response/first",
    )
    changed = EvidenceRef.create(
        evidence_id=evidence_id,
        evidence_digest=_sha("different evidence payload"),
        evidence_kind="attention.response.v1",
        locator="work-event://response/first",
    )

    assert first.evidence_id == changed.evidence_id
    assert first.ref_digest != changed.ref_digest


@pytest.mark.parametrize(
    ("kind", "locator"),
    [
        ("attention.response.v1", "work-event://response/abc"),
        ("capability.outcome.v1", "work-event://capability/outcome/abc"),
        ("lab.run-execution.v1", "lab-sqlite://run-evidence/abc"),
        ("mutation.patch-observation.v1", "host-receipt://mutation/abc"),
    ],
)
def test_evidence_ref_is_domain_neutral(
    kind: str,
    locator: str,
) -> None:
    ref = EvidenceRef.create(
        evidence_id=str(uuid4()),
        evidence_digest=_sha(kind),
        evidence_kind=kind,
        locator=locator,
    )

    assert ref.evidence_kind == kind
    assert ref.locator == locator


def test_evidence_ref_locator_is_opaque_and_does_not_require_reachability() -> None:
    ref = EvidenceRef.create(
        evidence_id=str(uuid4()),
        evidence_digest=_sha("detached evidence"),
        evidence_kind="external.observation.v1",
        locator="provider+opaque://not-present/evidence",
    )

    assert ref.locator == "provider+opaque://not-present/evidence"


def test_evidence_ref_round_trip_is_exact() -> None:
    ref = EvidenceRef.create(
        evidence_id=str(uuid4()),
        evidence_digest=_sha("payload"),
        evidence_kind="capability.outcome.v1",
        locator="work-event://outcome/example",
    )

    assert EvidenceRef.from_dict(ref.to_dict()) == ref


def test_evidence_ref_rejects_tampered_payload_with_stale_digest() -> None:
    ref = EvidenceRef.create(
        evidence_id=str(uuid4()),
        evidence_digest=_sha("payload"),
        evidence_kind="attention.response.v1",
        locator="work-event://response/example",
    )
    payload = ref.to_dict()
    payload["locator"] = "work-event://response/other"

    with pytest.raises(InvalidEvidenceRef, match="digest mismatch"):
        EvidenceRef.from_dict(payload)


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_evidence_ref_strict_decoder_rejects_schema_type_drift(
    schema_version: object,
) -> None:
    ref = EvidenceRef.create(
        evidence_id=str(uuid4()),
        evidence_digest=_sha("payload"),
        evidence_kind="attention.response.v1",
        locator="work-event://response/example",
    )
    payload = ref.to_dict()
    payload["schema_version"] = schema_version
    payload["ref_digest"] = _digest_payload(
        {
            key: value
            for key, value in payload.items()
            if key != "ref_digest"
        }
    )

    with pytest.raises(InvalidEvidenceRef, match="Unsupported EvidenceRef schema"):
        EvidenceRef.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_digest", "A" * 64),
        ("evidence_kind", "Attention.Response.v1"),
        ("evidence_kind", ""),
        ("evidence_kind", " leading"),
        ("locator", ""),
        ("locator", " trailing "),
    ],
)
def test_evidence_ref_rejects_noncanonical_fields(
    field: str,
    value: object,
) -> None:
    kwargs: dict[str, object] = {
        "evidence_id": str(uuid4()),
        "evidence_digest": _sha("payload"),
        "evidence_kind": "attention.response.v1",
        "locator": "work-event://response/example",
    }
    kwargs[field] = value

    with pytest.raises(InvalidEvidenceRef):
        EvidenceRef.create(**kwargs)


def test_evidence_ref_strict_decoder_rejects_shape_drift() -> None:
    ref = EvidenceRef.create(
        evidence_id=str(uuid4()),
        evidence_digest=_sha("payload"),
        evidence_kind="attention.response.v1",
        locator="work-event://response/example",
    )
    payload = ref.to_dict()
    payload["status"] = "verified"

    with pytest.raises(InvalidEvidenceRef, match="keys are not exact"):
        EvidenceRef.from_dict(payload)


def test_g2_32_evidence_ref_does_not_define_evidence_truth_or_completion() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "evidence_core" / "models.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    forbidden_names = {
        "CapabilityOutcomeStatus",
        "ConclusionVerdict",
        "CompletionClaim",
        "WorkCompletion",
        "AuthorizationReceipt",
        "ProcessExecutionObservation",
        "AttentionResponse",
        "ArtifactRef",
    }
    forbidden_modules = {
        "attention_core",
        "artifact_core",
        "capability_core",
        "lab",
        "authority",
        "execution",
        "mutation",
        "simple_work",
        "work_core",
        "workflow_core",
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
