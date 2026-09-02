from __future__ import annotations

import hmac
from pathlib import Path
from typing import Iterable

from codexia_manual_agent.delegation.coordinator import (
    MAX_DELEGATION_RESULT_CHARS,
    DelegationCoordinator,
    DelegationSnapshot,
)
from codexia_manual_agent.delegation.errors import (
    DelegationAuthorityError,
    DelegationBudgetError,
    DelegationStateError,
    EscalationBindingError,
    InvalidDelegationError,
)
from codexia_manual_agent.delegation.managed_persistence import (
    SqliteDelegationEventStore,
)
from codexia_manual_agent.delegation.models import (
    ContinuationDecision,
    DelegationBudget,
    DelegationEnvelope,
    DelegationLimits,
    DelegationState,
    EscalationReason,
    EscalationRequest,
    OperatorContinuation,
)
from codexia_manual_agent.delegation.persistence import (
    DelegationEventKind,
    DelegationRecovery,
    _ReplayState,
)
from codexia_manual_agent.domain.capabilities import Capability


class SqliteDelegationCoordinator(DelegationCoordinator):
    """Durable M3.2 implementation of the bounded M2.6 orchestration API.

    Subclassing preserves compatibility with the strict M2.6 model-control bridge.
    The inherited process-local dictionaries are never authoritative: every public
    stateful operation below is overridden and derives state from the SQLite event
    chronology.
    """

    def __init__(self, database_path: str | Path) -> None:
        super().__init__()
        self._event_store = SqliteDelegationEventStore(database_path)

    @property
    def database_path(self) -> Path:
        return self._event_store.database_path

    def create_root(
        self,
        *,
        workspace_root: str | Path,
        task: str,
        capabilities: Iterable[Capability | str] = (Capability.READ_WORKSPACE,),
        budget: DelegationBudget,
        limits: DelegationLimits | None = None,
    ) -> DelegationEnvelope:
        requested = self._capabilities_for_new_node(capabilities)
        envelope = DelegationEnvelope.create_root(
            workspace_root=workspace_root,
            task=task,
            capabilities=requested,
            budget=budget,
            limits=limits,
        )
        self._event_store.create_root(envelope)
        return envelope

    def create_child(
        self,
        parent_delegation_id: str,
        *,
        task: str,
        capabilities: Iterable[Capability | str],
        budget: DelegationBudget,
    ) -> DelegationEnvelope:
        requested = self._capabilities_for_new_node(capabilities)
        budget.require_allocation()
        created: DelegationEnvelope | None = None

        def prepare(state: _ReplayState):
            nonlocal created
            try:
                parent = state.nodes[parent_delegation_id]
            except KeyError as exc:
                raise InvalidDelegationError("Unknown delegation id") from exc
            if parent.state is not DelegationState.ACTIVE:
                raise DelegationStateError(
                    f"Delegation is {parent.state.value}; expected {DelegationState.ACTIVE.value}"
                )
            parent_caps = set(parent.envelope.capabilities)
            if not set(requested).issubset(parent_caps):
                raise DelegationAuthorityError(
                    "Child delegation capabilities must be a subset of the parent envelope"
                )
            if parent.envelope.depth + 1 > parent.envelope.limits.max_depth:
                raise DelegationBudgetError("Delegation depth limit exhausted")
            if len(state.nodes) >= parent.envelope.limits.max_total_delegations:
                raise DelegationBudgetError("Total delegation-count limit exhausted")
            if not parent.remaining_budget.contains(budget):
                raise DelegationBudgetError(
                    "Child budget allocation exceeds the parent's remaining budget"
                )
            created = DelegationEnvelope.create_child(
                parent=parent.envelope,
                task=task,
                capabilities=requested,
                budget=budget,
            )
            return DelegationEventKind.CHILD_CREATED, {"envelope": created.to_dict()}

        self._event_store.mutate_delegation(parent_delegation_id, prepare)
        if created is None:  # pragma: no cover - prepare must run before commit
            raise RuntimeError("Durable child creation did not prepare an envelope")
        return created

    def claim_control_request(
        self,
        delegation_id: str,
        *,
        request_id: str,
        request_digest: str,
    ) -> None:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 100:
            raise InvalidDelegationError("Control request id is invalid")
        if not isinstance(request_digest, str) or len(request_digest) != 64:
            raise InvalidDelegationError("Control request digest must be SHA-256 hex")
        try:
            int(request_digest, 16)
        except ValueError as exc:
            raise InvalidDelegationError("Control request digest must be SHA-256 hex") from exc

        self._event_store.mutate_delegation(
            delegation_id,
            lambda state: (
                DelegationEventKind.CONTROL_REQUEST_CLAIMED,
                {
                    "delegation_id": delegation_id,
                    "request_id": request_id,
                    "request_digest": request_digest,
                },
            ),
        )

    def consume_budget(
        self,
        delegation_id: str,
        *,
        turns: int = 0,
        tool_calls: int = 0,
        model_chars: int = 0,
    ) -> DelegationBudget:
        amount = DelegationBudget(
            turns=turns,
            tool_calls=tool_calls,
            model_chars=model_chars,
        )
        if turns == 0 and tool_calls == 0 and model_chars == 0:
            raise DelegationBudgetError("Budget consumption must consume at least one unit")
        recovery = self._event_store.mutate_delegation(
            delegation_id,
            lambda state: (
                DelegationEventKind.BUDGET_CONSUMED,
                {"delegation_id": delegation_id, "amount": amount.to_dict()},
            ),
        )
        return recovery.snapshot(delegation_id).remaining_budget

    def assert_capability(
        self,
        delegation_id: str,
        capability: Capability | str,
    ) -> None:
        try:
            normalized = Capability(capability)
        except (TypeError, ValueError) as exc:
            raise InvalidDelegationError("Unknown capability") from exc
        snapshot = self.snapshot(delegation_id)
        if snapshot.state is not DelegationState.ACTIVE:
            raise DelegationStateError(
                f"Delegation is {snapshot.state.value}; expected {DelegationState.ACTIVE.value}"
            )
        if normalized not in snapshot.envelope.capabilities:
            raise DelegationAuthorityError(
                f"Delegated work has no {normalized.value} authority; human escalation is required"
            )

    def request_escalation(
        self,
        delegation_id: str,
        *,
        reason: EscalationReason,
        summary: str,
        requested_capability: Capability | str | None = None,
        requested_action: str | None = None,
    ) -> EscalationRequest:
        created: EscalationRequest | None = None

        def prepare(state: _ReplayState):
            nonlocal created
            try:
                node = state.nodes[delegation_id]
            except KeyError as exc:
                raise InvalidDelegationError("Unknown delegation id") from exc
            if node.state is not DelegationState.ACTIVE:
                raise DelegationStateError(
                    f"Delegation is {node.state.value}; expected {DelegationState.ACTIVE.value}"
                )
            created = EscalationRequest.create(
                delegation=node.envelope,
                reason=reason,
                summary=summary,
                requested_capability=requested_capability,
                requested_action=requested_action,
            )
            return DelegationEventKind.ESCALATION_REQUESTED, {
                "escalation": created.to_dict()
            }

        self._event_store.mutate_delegation(delegation_id, prepare)
        if created is None:  # pragma: no cover
            raise RuntimeError("Durable escalation creation did not prepare a record")
        return created

    def resolve_escalation(
        self,
        escalation: EscalationRequest,
        *,
        decision: ContinuationDecision,
        actor: str,
        note: str | None = None,
    ) -> OperatorContinuation:
        if not isinstance(escalation, EscalationRequest):
            raise TypeError("escalation must be an EscalationRequest")
        created: OperatorContinuation | None = None

        def prepare(state: _ReplayState):
            nonlocal created
            stored = state.escalations.get(escalation.escalation_id)
            if stored is None:
                raise EscalationBindingError("Unknown escalation")
            if (
                not hmac.compare_digest(stored.escalation_digest, escalation.escalation_digest)
                or stored.to_dict() != escalation.to_dict()
            ):
                raise EscalationBindingError(
                    "Escalation payload does not match the pending record"
                )
            node = state.nodes[stored.delegation_id]
            if node.state is not DelegationState.WAITING_HUMAN:
                raise DelegationStateError(
                    "Delegation is not waiting for an operator decision"
                )
            pending = node.pending_escalation
            if (
                pending is None
                or pending.escalation_id != stored.escalation_id
                or not hmac.compare_digest(
                    pending.escalation_digest,
                    stored.escalation_digest,
                )
            ):
                raise EscalationBindingError(
                    "Escalation is not the exact pending delegation escalation"
                )
            created = OperatorContinuation.create(
                escalation=stored,
                decision=decision,
                actor=actor,
                note=note,
            )
            return DelegationEventKind.ESCALATION_RESOLVED, {
                "continuation": created.to_dict()
            }

        self._event_store.mutate_escalation(escalation.escalation_id, prepare)
        if created is None:  # pragma: no cover
            raise RuntimeError("Durable escalation resolution did not prepare a continuation")
        return created

    def complete(self, delegation_id: str, *, result_summary: str) -> DelegationSnapshot:
        if not isinstance(result_summary, str):
            raise InvalidDelegationError("result_summary must be text")
        normalized = result_summary.strip()
        if (
            not normalized
            or len(normalized) > MAX_DELEGATION_RESULT_CHARS
            or "\x00" in normalized
        ):
            raise InvalidDelegationError("result_summary is empty or exceeds the M2.6 budget")
        recovery = self._event_store.mutate_delegation(
            delegation_id,
            lambda state: (
                DelegationEventKind.DELEGATION_COMPLETED,
                {"delegation_id": delegation_id, "result_summary": normalized},
            ),
        )
        return recovery.snapshot(delegation_id)

    def snapshot(self, delegation_id: str) -> DelegationSnapshot:
        return self._event_store.recover_for_delegation(delegation_id).snapshot(delegation_id)

    def continuation(self, continuation_id: str) -> OperatorContinuation:
        recovery = self._event_store.recover_for_continuation(continuation_id)
        return recovery.continuation(continuation_id)

    def root_delegation_count(self, root_delegation_id: str) -> int:
        if not isinstance(root_delegation_id, str):
            return 0
        try:
            return self._event_store.recover(root_delegation_id).root_delegation_count
        except InvalidDelegationError:
            return 0

    def recover(self, root_delegation_id: str) -> DelegationRecovery:
        """Deterministically reconstruct one root without launching delegated work."""

        return self._event_store.recover(root_delegation_id)
