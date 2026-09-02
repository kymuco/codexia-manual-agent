from __future__ import annotations

from codexia_manual_agent.delegation.coordinator import DelegationCoordinator
from codexia_manual_agent.delegation.models import DelegationEnvelope, EscalationRequest
from codexia_manual_agent.delegation.protocol import (
    DelegateWorkRequest,
    DelegationControlRequest,
    EscalateWorkRequest,
)


DelegationControlResult = DelegationEnvelope | EscalationRequest


def apply_delegation_control_request(
    coordinator: DelegationCoordinator,
    *,
    current_delegation_id: str,
    request: DelegationControlRequest,
) -> DelegationControlResult:
    """Bind exact model orchestration intent to coordinator-owned local lineage.

    The request cannot choose workspace, root/parent lineage, delegation ids,
    proposal ids, receipts, approval fields, or authority. Those are deliberately
    absent from the model protocol and are supplied/derived locally here. The
    exact request id/digest is claimed before applying it, so replay or same-id
    payload substitution fails closed.
    """

    if not isinstance(coordinator, DelegationCoordinator):
        raise TypeError("coordinator must be a DelegationCoordinator")
    if not isinstance(current_delegation_id, str) or not current_delegation_id.strip():
        raise ValueError("current_delegation_id must be a non-empty string")
    if not isinstance(request, (DelegateWorkRequest, EscalateWorkRequest)):
        raise TypeError("request must be a bounded delegation control request")

    coordinator.claim_control_request(
        current_delegation_id,
        request_id=request.request_id,
        request_digest=request.request_digest,
    )

    if isinstance(request, DelegateWorkRequest):
        return coordinator.create_child(
            current_delegation_id,
            task=request.task,
            capabilities=request.capabilities,
            budget=request.budget,
        )
    return coordinator.request_escalation(
        current_delegation_id,
        reason=request.reason,
        summary=request.summary,
        requested_capability=request.requested_capability,
        requested_action=request.requested_action,
    )
