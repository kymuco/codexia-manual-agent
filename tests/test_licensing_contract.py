from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LICENSE_REF = "LicenseRef-PolyForm-Perimeter-1.0.1"


def test_current_distribution_declares_polyform_perimeter_1_0_1() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["license"] == LICENSE_REF
    assert metadata["project"]["license-files"] == ["LICENSE", "LICENSING.md"]

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith(
        "# PolyForm Perimeter License 1.0.1\n\n"
        "<https://polyformproject.org/licenses/perimeter/1.0.1>"
    )
    assert "## Noncompete" in license_text
    assert (
        "Any purpose is a permitted purpose, except for providing to others any product "
        "that competes with the software."
    ) in license_text
    assert license_text.rstrip().endswith("Required Notice: Copyright 2026 kymuco")


def test_public_licensing_surfaces_are_consistent() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    policy = (ROOT / "LICENSING.md").read_text(encoding="utf-8")

    for text in (readme, contributing, policy):
        assert "PolyForm Perimeter License 1.0.1" in text

    assert "source-available" in readme.lower()
    assert "not OSI open-source software" in readme
    assert "commercial licenses" in readme.lower()
    assert "contributor-agreement" in contributing
    assert LICENSE_REF in policy
    assert "commercial licenses" in policy.lower()

    legacy_copyleft = "AGPL" + "-3.0-only"
    legacy_permissive_name = "Apache" + " License 2.0"
    for text in (readme, contributing, policy):
        assert legacy_copyleft not in text
        assert legacy_permissive_name not in text
