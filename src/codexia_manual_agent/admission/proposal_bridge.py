from __future__ import annotations

from pathlib import Path

from codexia_manual_agent.admission.models import CommandAdmission
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import CommandAdmissionError
from codexia_manual_agent.execution import prepare_process_proposal


def build_process_proposal(
    admission: CommandAdmission,
    *,
    workspace: str | Path,
):
    """Convert only an admitted execute-process-only command into an M2.1 proposal."""

    if not admission.admitted:
        raise CommandAdmissionError(
            f"Rejected admission cannot become a process proposal: {admission.verdict.value}"
        )
    if admission.command.envelope.required_capabilities != (
        Capability.EXECUTE_PROCESS,
    ):
        raise CommandAdmissionError(
            "M2.2 only bridges the exact execute_process-only capability envelope"
        )
    return prepare_process_proposal(
        workspace=workspace,
        argv=admission.command.argv,
        cwd=admission.command.cwd,
        summary=(
            f"M2.2 admitted command family {admission.command.family.value}; "
            "explicit local human authorization remains required."
        ),
    )
