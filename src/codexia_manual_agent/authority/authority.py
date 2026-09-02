from __future__ import annotations

from codexia_manual_agent.authority.models import (
    ActionProposal,
    ApprovalMode,
    ApprovalRequirement,
    AuthorizationDecision,
    AuthorizationReceipt,
    AuthorizationSource,
)
from codexia_manual_agent.authority.policy import ApprovalPolicy
from codexia_manual_agent.authority.registry import (
    AuthorizationConsumptionRegistryProtocol,
    process_authorization_consumption_registry,
)
from codexia_manual_agent.domain.errors import (
    ApprovalRequiredError,
    AuthorizationDeniedError,
    AuthorizationMismatchError,
    InvalidApprovalDecisionError,
)


class LocalApprovalAuthority:
    """Local authorization authority with an explicit single-use replay registry.

    The default remains the shared process-local registry used since M2.0. M3 may
    explicitly inject a durable session-bound registry. Persistence never becomes
    an authorization source; policy/receipt verification still happens here before
    the registry is allowed to consume the exact receipt.
    """

    def __init__(
        self,
        policy: ApprovalPolicy | None = None,
        *,
        consumption_registry: AuthorizationConsumptionRegistryProtocol | None = None,
    ) -> None:
        self._policy = policy or ApprovalPolicy()
        self._consumption_registry = (
            consumption_registry
            if consumption_registry is not None
            else process_authorization_consumption_registry()
        )

    def requirement(
        self,
        proposal: ActionProposal,
        mode: ApprovalMode,
    ) -> ApprovalRequirement:
        return self._policy.evaluate(proposal, mode)

    def decide(
        self,
        proposal: ActionProposal,
        *,
        mode: ApprovalMode,
        approved: bool | None = None,
        actor: str = "local-human",
        reason: str | None = None,
    ) -> AuthorizationReceipt:
        mode = ApprovalMode(mode)
        requirement = self.requirement(proposal, mode)

        if requirement is ApprovalRequirement.AUTO_AUTHORIZE:
            return AuthorizationReceipt.issue(
                proposal=proposal,
                decision=AuthorizationDecision.ALLOW,
                mode=mode,
                source=AuthorizationSource.POLICY,
                actor="local-policy",
                reason=reason or "Read-only action auto-authorized by local policy.",
            )

        if requirement is ApprovalRequirement.DENY:
            return AuthorizationReceipt.issue(
                proposal=proposal,
                decision=AuthorizationDecision.DENY,
                mode=mode,
                source=AuthorizationSource.POLICY,
                actor="local-policy",
                reason=reason or "Session approval mode forbids side effects.",
            )

        if approved is None:
            raise ApprovalRequiredError(
                f"Action {proposal.action!r} requires explicit local approval"
            )
        if type(approved) is not bool:
            raise InvalidApprovalDecisionError(
                "approved must be exactly True, False, or None"
            )

        return AuthorizationReceipt.issue(
            proposal=proposal,
            decision=(
                AuthorizationDecision.ALLOW
                if approved
                else AuthorizationDecision.DENY
            ),
            mode=mode,
            source=AuthorizationSource.HUMAN,
            actor=actor,
            reason=reason,
        )

    def verify_binding(
        self,
        proposal: ActionProposal,
        receipt: AuthorizationReceipt,
        *,
        mode: ApprovalMode,
    ) -> None:
        mode = ApprovalMode(mode)
        if receipt.proposal_id != proposal.proposal_id:
            raise AuthorizationMismatchError(
                "Authorization receipt belongs to a different proposal id"
            )
        if receipt.proposal_digest != proposal.proposal_digest:
            raise AuthorizationMismatchError(
                "Authorization receipt is bound to a different proposal payload"
            )
        if receipt.mode is not mode:
            raise AuthorizationMismatchError(
                "Authorization receipt was issued under a different approval mode"
            )

        requirement = self.requirement(proposal, mode)
        if requirement is ApprovalRequirement.AUTO_AUTHORIZE:
            if (
                receipt.source is not AuthorizationSource.POLICY
                or receipt.decision is not AuthorizationDecision.ALLOW
            ):
                raise AuthorizationMismatchError(
                    "Read-only auto-authorization must be an allow receipt from local policy"
                )
        elif requirement is ApprovalRequirement.REQUIRE_HUMAN:
            if receipt.source is not AuthorizationSource.HUMAN:
                raise AuthorizationMismatchError(
                    "This side effect requires a human-sourced authorization receipt"
                )
        else:
            if (
                receipt.source is not AuthorizationSource.POLICY
                or receipt.decision is not AuthorizationDecision.DENY
            ):
                raise AuthorizationMismatchError(
                    "Never mode requires a policy denial receipt for side effects"
                )

    def verify_authorization(
        self,
        proposal: ActionProposal,
        receipt: AuthorizationReceipt,
        *,
        mode: ApprovalMode,
    ) -> None:
        self.verify_binding(proposal, receipt, mode=mode)
        if receipt.decision is not AuthorizationDecision.ALLOW:
            raise AuthorizationDeniedError(
                "Authorization receipt does not allow execution"
            )

    def consume(
        self,
        proposal: ActionProposal,
        receipt: AuthorizationReceipt,
        *,
        mode: ApprovalMode,
    ) -> None:
        self.verify_authorization(proposal, receipt, mode=mode)
        self._consumption_registry.consume(
            receipt.receipt_id,
            receipt_digest=receipt.receipt_digest,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
        )

    def is_consumed(self, receipt: AuthorizationReceipt) -> bool:
        return self._consumption_registry.is_consumed(receipt.receipt_id)
