from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from codexia_manual_agent.authority.authority import LocalApprovalAuthority
from codexia_manual_agent.authority.models import (
    ActionPhase,
    ActionProposal,
    ApprovalMode,
    AuthorizationDecision,
    AuthorizationReceipt,
)
from codexia_manual_agent.domain.errors import InvalidActionTransitionError


@dataclass(slots=True)
class ActionLifecycle:
    """State machine with immutable authorization identity fields."""

    proposal: ActionProposal
    mode: ApprovalMode
    phase: ActionPhase = ActionPhase.PROPOSED
    authorization: AuthorizationReceipt | None = None
    execution_id: str | None = None
    observation_id: str | None = None
    _consumed_receipt_id: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, ActionProposal):
            raise TypeError("proposal must be an ActionProposal")
        object.__setattr__(self, "mode", ApprovalMode(self.mode))

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"proposal", "mode"} and hasattr(self, name):
            raise AttributeError(
                f"ActionLifecycle.{name} is immutable after construction"
            )
        object.__setattr__(self, name, value)

    def apply_receipt(
        self,
        receipt: AuthorizationReceipt,
        *,
        authority: LocalApprovalAuthority,
    ) -> ActionPhase:
        self._require_phase(ActionPhase.PROPOSED)
        authority.verify_binding(self.proposal, receipt, mode=self.mode)
        self.authorization = receipt

        if receipt.decision is AuthorizationDecision.DENY:
            self.phase = ActionPhase.DENIED
            return self.phase

        self.phase = ActionPhase.AUTHORIZED
        return self.phase

    def consume_authorization(
        self,
        *,
        authority: LocalApprovalAuthority,
    ) -> None:
        self._require_phase(ActionPhase.AUTHORIZED)
        if self.authorization is None:
            raise InvalidActionTransitionError(
                "Authorized phase requires an authorization receipt"
            )
        if self._consumed_receipt_id is not None:
            raise InvalidActionTransitionError(
                "Lifecycle authorization was already consumed"
            )
        authority.consume(
            self.proposal,
            self.authorization,
            mode=self.mode,
        )
        self._consumed_receipt_id = self.authorization.receipt_id

    def record_executed(self, execution_id: str | None = None) -> str:
        self._require_phase(ActionPhase.AUTHORIZED)
        if self._consumed_receipt_id is None:
            raise InvalidActionTransitionError(
                "Authorization must be consumed immediately before execution"
            )
        if self.authorization is None:
            raise InvalidActionTransitionError(
                "Consumed authorization receipt is missing"
            )
        if self.authorization.receipt_id != self._consumed_receipt_id:
            raise InvalidActionTransitionError(
                "Authorization receipt changed after consumption"
            )
        if (
            self.authorization.proposal_id != self.proposal.proposal_id
            or self.authorization.proposal_digest != self.proposal.proposal_digest
            or self.authorization.mode is not self.mode
        ):
            raise InvalidActionTransitionError(
                "Authorization binding changed before execution"
            )

        execution_id = execution_id or str(uuid4())
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("execution_id must be a non-empty string")
        self.execution_id = execution_id
        self.phase = ActionPhase.EXECUTED
        return execution_id

    def record_observed(self, observation_id: str | None = None) -> str:
        self._require_phase(ActionPhase.EXECUTED)
        observation_id = observation_id or str(uuid4())
        if not isinstance(observation_id, str) or not observation_id.strip():
            raise ValueError("observation_id must be a non-empty string")
        self.observation_id = observation_id
        self.phase = ActionPhase.OBSERVED
        return observation_id

    def _require_phase(self, expected: ActionPhase) -> None:
        if self.phase is not expected:
            raise InvalidActionTransitionError(
                f"Action lifecycle is {self.phase.value}; expected {expected.value}"
            )
