from __future__ import annotations

from codexia_manual_agent.admission.models import (
    CommandAdmission,
    CommandAdmissionVerdict,
    ProcessApprovalPreview,
)


def build_approval_preview(admission: CommandAdmission) -> ProcessApprovalPreview:
    command = admission.command
    return ProcessApprovalPreview(
        request_id=admission.request.request_id,
        family=command.family,
        verdict=admission.verdict,
        risk=command.risk,
        argv=command.argv,
        cwd=command.cwd,
        required_capabilities=command.envelope.required_capabilities,
        bounded=command.envelope.bounded,
        requires_human=(
            admission.verdict is CommandAdmissionVerdict.ADMIT_REQUIRES_HUMAN
        ),
        reason=admission.reason,
    )
