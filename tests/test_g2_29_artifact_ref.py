from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from codexia_manual_agent.artifact_core import ArtifactRef, InvalidArtifactRef


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_artifact_ref_binds_identity_to_exact_content_locator_and_type() -> None:
    artifact_id = str(uuid4())
    first = ArtifactRef.create(
        artifact_id=artifact_id,
        content_sha256=_sha("first bytes"),
        size_bytes=11,
        locator="sandbox:/mnt/data/report.txt",
        media_type="text/plain",
    )
    changed_content = ArtifactRef.create(
        artifact_id=artifact_id,
        content_sha256=_sha("other bytes"),
        size_bytes=11,
        locator="sandbox:/mnt/data/report.txt",
        media_type="text/plain",
    )

    assert first.artifact_id == changed_content.artifact_id
    assert first.locator == changed_content.locator
    assert first.ref_digest != changed_content.ref_digest


def test_artifact_ref_locator_is_opaque_and_does_not_require_existence() -> None:
    ref = ArtifactRef.create(
        content_sha256=_sha("detached bytes"),
        size_bytes=14,
        locator="provider+opaque://not-present/example",
        media_type=None,
    )

    assert ref.locator == "provider+opaque://not-present/example"


def test_artifact_ref_round_trip_is_exact() -> None:
    ref = ArtifactRef.create(
        content_sha256=_sha("payload"),
        size_bytes=7,
        locator="github:owner/repo@deadbeef:path/to/output.bin",
        media_type="application/octet-stream",
    )

    assert ArtifactRef.from_dict(ref.to_dict()) == ref


def test_artifact_ref_rejects_tampered_payload_with_stale_digest() -> None:
    ref = ArtifactRef.create(
        content_sha256=_sha("payload"),
        size_bytes=7,
        locator="sandbox:/mnt/data/output.bin",
    )
    payload = ref.to_dict()
    payload["locator"] = "sandbox:/mnt/data/other.bin"

    with pytest.raises(InvalidArtifactRef, match="digest mismatch"):
        ArtifactRef.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content_sha256", "A" * 64),
        ("size_bytes", -1),
        ("size_bytes", True),
        ("locator", ""),
        ("locator", " leading"),
        ("locator", "trailing "),
        ("media_type", ""),
    ],
)
def test_artifact_ref_rejects_noncanonical_fields(field: str, value: object) -> None:
    kwargs: dict[str, object] = {
        "content_sha256": _sha("payload"),
        "size_bytes": 7,
        "locator": "sandbox:/mnt/data/output.bin",
        "media_type": "application/octet-stream",
    }
    kwargs[field] = value

    with pytest.raises(InvalidArtifactRef):
        ArtifactRef.create(**kwargs)


def test_artifact_ref_strict_decoder_rejects_shape_drift() -> None:
    ref = ArtifactRef.create(
        content_sha256=_sha("payload"),
        size_bytes=7,
        locator="sandbox:/mnt/data/output.bin",
    )
    payload = ref.to_dict()
    payload["unexpected"] = True

    with pytest.raises(InvalidArtifactRef, match="keys are not exact"):
        ArtifactRef.from_dict(payload)


def test_g2_29_artifact_ref_is_semantic_only() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "artifact_core" / "models.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    forbidden_modules = {
        "simple_work",
        "lab",
        "authority",
        "execution",
        "mutation",
        "providers",
        "work_core",
        "workflow_core",
    }
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    assert not any(
        any(part in module for part in forbidden_modules)
        for module in imported_modules
    )
