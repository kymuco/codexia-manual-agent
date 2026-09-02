"""M2.2 command-family admission and capability-envelope contracts."""

from .command_families import normalize_command
from .models import (
    CapabilityEnvelope,
    CommandAdmission,
    CommandAdmissionVerdict,
    CommandFamily,
    CommandRisk,
    ModelProcessRequest,
    NormalizedCommand,
    ProcessApprovalPreview,
)
from .policy import CommandAdmissionPolicy
from .preview import build_approval_preview
from .proposal_bridge import build_process_proposal

__all__ = [
    "CapabilityEnvelope",
    "CommandAdmission",
    "CommandAdmissionPolicy",
    "CommandAdmissionVerdict",
    "CommandFamily",
    "CommandRisk",
    "ModelProcessRequest",
    "NormalizedCommand",
    "ProcessApprovalPreview",
    "build_approval_preview",
    "build_process_proposal",
    "normalize_command",
]
