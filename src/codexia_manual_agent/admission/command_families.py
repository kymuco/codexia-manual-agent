from __future__ import annotations

import sys
from pathlib import Path

from codexia_manual_agent.admission.models import (
    CapabilityEnvelope,
    CommandFamily,
    CommandRisk,
    ModelProcessRequest,
    NormalizedCommand,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import CommandAdmissionError


def _require_no_arguments(request: ModelProcessRequest) -> None:
    if request.arguments:
        raise CommandAdmissionError(
            f"{request.family.value} does not accept model-supplied arguments"
        )


def _python_executable() -> str:
    executable = Path(sys.executable).resolve(strict=True)
    if not executable.is_file():  # pragma: no cover - sys.executable contract
        raise CommandAdmissionError("Current Python executable is unavailable")
    return str(executable)


def normalize_command(request: ModelProcessRequest) -> NormalizedCommand:
    """Construct the exact local argv for one known command family.

    The model selects a family only. It never supplies an executable, raw argv,
    capability labels, risk labels, approval state, or authority metadata.

    Bare host commands such as ``git`` intentionally remain unresolved here.
    The M2.1 proposal builder owns workspace-aware executable resolution and must
    bind the canonical executable through its filtered host PATH. Resolving a
    bare command in this layer would let an absolute path bypass that boundary.
    """

    _require_no_arguments(request)

    if request.family is CommandFamily.PYTHON_VERSION:
        return NormalizedCommand(
            family=request.family,
            argv=(_python_executable(), "--version"),
            cwd=".",
            risk=CommandRisk.DIAGNOSTIC,
            envelope=CapabilityEnvelope(
                required_capabilities=(Capability.EXECUTE_PROCESS,),
                bounded=True,
                rationale=(
                    "Fixed current-interpreter version query; no model-controlled argv."
                ),
            ),
        )

    if request.family is CommandFamily.GIT_VERSION:
        return NormalizedCommand(
            family=request.family,
            argv=("git", "--version"),
            cwd=".",
            risk=CommandRisk.DIAGNOSTIC,
            envelope=CapabilityEnvelope(
                required_capabilities=(Capability.EXECUTE_PROCESS,),
                bounded=True,
                rationale=(
                    "Fixed Git version query; M2.1 binds the canonical executable "
                    "through its workspace-filtered host PATH."
                ),
            ),
        )

    if request.family is CommandFamily.PYTHON_COMPILEALL:
        return NormalizedCommand(
            family=request.family,
            argv=(_python_executable(), "-m", "compileall", "-q", "."),
            cwd=".",
            risk=CommandRisk.WORKSPACE_MUTATION,
            envelope=CapabilityEnvelope(
                required_capabilities=(
                    Capability.EXECUTE_PROCESS,
                    Capability.WRITE_WORKSPACE,
                ),
                bounded=True,
                rationale=(
                    "compileall may create or replace __pycache__ bytecode inside the workspace."
                ),
            ),
        )

    if request.family is CommandFamily.PYTHON_UNITTEST_DISCOVER:
        return NormalizedCommand(
            family=request.family,
            argv=(
                _python_executable(),
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ),
            cwd=".",
            risk=CommandRisk.UNBOUNDED_CHILD_CODE,
            envelope=CapabilityEnvelope(
                required_capabilities=(Capability.EXECUTE_PROCESS,),
                bounded=False,
                rationale=(
                    "Test discovery imports and executes repository-controlled Python; without "
                    "filesystem/network capability containment its downstream authority cannot "
                    "be proven from the command family."
                ),
            ),
        )

    raise CommandAdmissionError(f"Unsupported command family: {request.family!r}")
