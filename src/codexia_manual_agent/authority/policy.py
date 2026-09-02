from __future__ import annotations

from codexia_manual_agent.authority.models import (
    ActionProposal,
    ActionRisk,
    ApprovalMode,
    ApprovalRequirement,
)
from codexia_manual_agent.domain.capabilities import Capability


_RISK_BY_CAPABILITY: dict[Capability, ActionRisk] = {
    Capability.READ_WORKSPACE: ActionRisk.READ_ONLY,
    Capability.WRITE_WORKSPACE: ActionRisk.WORKSPACE_MUTATION,
    Capability.EXECUTE_PROCESS: ActionRisk.PROCESS_EXECUTION,
    Capability.NETWORK_ACCESS: ActionRisk.NETWORK_ACCESS,
    Capability.GIT_COMMIT: ActionRisk.EXTERNAL_GIT,
    Capability.GIT_PUSH: ActionRisk.EXTERNAL_GIT,
    Capability.DELETE_FILES: ActionRisk.DESTRUCTIVE,
    Capability.OUTSIDE_WORKSPACE: ActionRisk.OUTSIDE_WORKSPACE,
}


class ApprovalPolicy:
    """Pure policy: classify locally, never trust model-supplied risk labels."""

    def classify(self, proposal: ActionProposal) -> ActionRisk:
        return _RISK_BY_CAPABILITY[proposal.capability]

    def evaluate(
        self,
        proposal: ActionProposal,
        mode: ApprovalMode,
    ) -> ApprovalRequirement:
        risk = self.classify(proposal)
        mode = ApprovalMode(mode)

        if risk is ActionRisk.READ_ONLY:
            return ApprovalRequirement.AUTO_AUTHORIZE

        if mode is ApprovalMode.NEVER:
            return ApprovalRequirement.DENY

        # Under the frozen CMA policy, writes, process execution, network, and
        # Git mutation all require a human decision in both ALWAYS and RISKY.
        # Later command-level classification may distinguish additional safe
        # subcases without changing this capability-level contract.
        return ApprovalRequirement.REQUIRE_HUMAN
