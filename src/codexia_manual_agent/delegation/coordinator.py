from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from codexia_manual_agent.delegation.errors import (
    DelegationAuthorityError,
    DelegationBudgetError,
    DelegationReplayError,
    DelegationStateError,
    EscalationBindingError,
    InvalidDelegationError,
)
from codexia_manual_agent.delegation.models import (
    DELEGABLE_CAPABILITIES,
    ContinuationDecision,
    DelegationBudget,
    DelegationEnvelope,
    DelegationLimits,
    DelegationState,
    EscalationReason,
    EscalationRequest,
    OperatorContinuation,
)
from codexia_manual_agent.domain.capabilities import Capability


MAX_DELEGATION_RESULT_CHARS = 16_384


@dataclass(frozen=True, slots=True)
class DelegationSnapshot:
    envelope: DelegationEnvelope
    state: DelegationState
    remaining_budget: DelegationBudget
    child_ids: tuple[str, ...]
    pending_escalation: EscalationRequest | None
    result_summary: str | None
    continuation_ids: tuple[str, ...]
    control_request_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope": self.envelope.to_dict(),
            "state": self.state.value,
            "remaining_budget": self.remaining_budget.to_dict(),
            "child_ids": list(self.child_ids),
            "pending_escalation": (
                self.pending_escalation.to_dict()
                if self.pending_escalation is not None
                else None
            ),
            "result_summary": self.result_summary,
            "continuation_ids": list(self.continuation_ids),
            "control_request_ids": list(self.control_request_ids),
        }


@dataclass(slots=True)
class _DelegationNode:
    envelope: DelegationEnvelope
    state: DelegationState
    remaining_budget: DelegationBudget
    child_ids: list[str] = field(default_factory=list)
    pending_escalation: EscalationRequest | None = None
    result_summary: str | None = None
    continuation_ids: list[str] = field(default_factory=list)
    control_requests: dict[str, str] = field(default_factory=dict)


class DelegationCoordinator:
    """Process-local M2.6 orchestration ledger.

    The coordinator owns no `LocalApprovalAuthority` and intentionally has no API
    that accepts or consumes an `AuthorizationReceipt`. It can bound read-only
    work, request a human escalation, and resume/cancel orchestration only.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._nodes: dict[str, _DelegationNode] = {}
        self._escalations: dict[str, EscalationRequest] = {}
        self._continuations: dict[str, OperatorContinuation] = {}
        self._root_counts: dict[str, int] = {}

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
        with self._lock:
            if envelope.delegation_id in self._nodes:
                raise InvalidDelegationError("Delegation id collision")
            self._nodes[envelope.delegation_id] = _DelegationNode(
                envelope=envelope,
                state=DelegationState.ACTIVE,
                remaining_budget=envelope.budget,
            )
            self._root_counts[envelope.root_delegation_id] = 1
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
        with self._lock:
            parent = self._node(parent_delegation_id)
            self._require_state(parent, DelegationState.ACTIVE)
            parent_caps = set(parent.envelope.capabilities)
            if not set(requested).issubset(parent_caps):
                raise DelegationAuthorityError(
                    "Child delegation capabilities must be a subset of the parent envelope"
                )
            if parent.envelope.depth + 1 > parent.envelope.limits.max_depth:
                raise DelegationBudgetError("Delegation depth limit exhausted")
            root_id = parent.envelope.root_delegation_id
            if self._root_counts.get(root_id, 0) >= parent.envelope.limits.max_total_delegations:
                raise DelegationBudgetError("Total delegation-count limit exhausted")
            if not parent.remaining_budget.contains(budget):
                raise DelegationBudgetError(
                    "Child budget allocation exceeds the parent's remaining budget"
                )

            child = DelegationEnvelope.create_child(
                parent=parent.envelope,
                task=task,
                capabilities=requested,
                budget=budget,
            )
            if child.delegation_id in self._nodes:
                raise InvalidDelegationError("Delegation id collision")

            # Reserve the full slice before publishing the child node. Reserved
            # budget is intentionally not refunded on child completion/cancel.
            parent.remaining_budget = parent.remaining_budget.subtract(budget)
            parent.child_ids.append(child.delegation_id)
            self._nodes[child.delegation_id] = _DelegationNode(
                envelope=child,
                state=DelegationState.ACTIVE,
                remaining_budget=child.budget,
            )
            self._root_counts[root_id] = self._root_counts.get(root_id, 0) + 1
            return child

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
        with self._lock:
            node = self._node(delegation_id)
            self._require_state(node, DelegationState.ACTIVE)
            existing = node.control_requests.get(request_id)
            if existing is not None:
                if hmac.compare_digest(existing, request_digest):
                    raise DelegationReplayError("Control request was already claimed")
                raise DelegationReplayError(
                    "Control request id was rebound to a different payload"
                )
            node.control_requests[request_id] = request_digest

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
        with self._lock:
            node = self._node(delegation_id)
            self._require_state(node, DelegationState.ACTIVE)
            if not node.remaining_budget.contains(amount):
                raise DelegationBudgetError("Delegation budget exhausted")
            node.remaining_budget = node.remaining_budget.subtract(amount)
            return node.remaining_budget

    def assert_capability(
        self,
        delegation_id: str,
        capability: Capability | str,
    ) -> None:
        try:
            normalized = Capability(capability)
        except (TypeError, ValueError) as exc:
            raise InvalidDelegationError("Unknown capability") from exc
        with self._lock:
            node = self._node(delegation_id)
            self._require_state(node, DelegationState.ACTIVE)
            if normalized not in node.envelope.capabilities:
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
        with self._lock:
            node = self._node(delegation_id)
            self._require_state(node, DelegationState.ACTIVE)
            escalation = EscalationRequest.create(
                delegation=node.envelope,
                reason=reason,
                summary=summary,
                requested_capability=requested_capability,
                requested_action=requested_action,
            )
            if escalation.escalation_id in self._escalations:
                raise InvalidDelegationError("Escalation id collision")
            node.pending_escalation = escalation
            node.state = DelegationState.WAITING_HUMAN
            self._escalations[escalation.escalation_id] = escalation
            return escalation

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
        with self._lock:
            stored = self._escalations.get(escalation.escalation_id)
            if stored is None:
                raise EscalationBindingError("Unknown escalation")
            if (
                not hmac.compare_digest(stored.escalation_digest, escalation.escalation_digest)
                or stored.to_dict() != escalation.to_dict()
            ):
                raise EscalationBindingError("Escalation payload does not match the pending record")
            node = self._node(stored.delegation_id)
            if node.state is not DelegationState.WAITING_HUMAN:
                raise DelegationStateError("Delegation is not waiting for an operator decision")
            pending = node.pending_escalation
            if (
                pending is None
                or pending.escalation_id != stored.escalation_id
                or not hmac.compare_digest(
                    pending.escalation_digest,
                    stored.escalation_digest,
                )
            ):
                raise EscalationBindingError("Escalation is not the exact pending delegation escalation")

            continuation = OperatorContinuation.create(
                escalation=stored,
                decision=decision,
                actor=actor,
                note=note,
            )
            self._continuations[continuation.continuation_id] = continuation
            node.continuation_ids.append(continuation.continuation_id)
            node.pending_escalation = None
            if continuation.decision is ContinuationDecision.CONTINUE:
                # Deliberately change orchestration state only. Capabilities and
                # budget remain exactly unchanged.
                node.state = DelegationState.ACTIVE
            else:
                self._cancel_subtree(node.envelope.delegation_id)
            return continuation

    def complete(self, delegation_id: str, *, result_summary: str) -> DelegationSnapshot:
        if not isinstance(result_summary, str):
            raise InvalidDelegationError("result_summary must be text")
        result_summary = result_summary.strip()
        if (
            not result_summary
            or len(result_summary) > MAX_DELEGATION_RESULT_CHARS
            or "\x00" in result_summary
        ):
            raise InvalidDelegationError("result_summary is empty or exceeds the M2.6 budget")
        with self._lock:
            node = self._node(delegation_id)
            self._require_state(node, DelegationState.ACTIVE)
            live_children = [
                child_id
                for child_id in node.child_ids
                if self._nodes[child_id].state
                not in {DelegationState.COMPLETED, DelegationState.CANCELLED}
            ]
            if live_children:
                raise DelegationStateError(
                    "Parent delegation cannot complete while child work is still live"
                )
            node.result_summary = result_summary
            node.state = DelegationState.COMPLETED
            return self._snapshot(node)

    def snapshot(self, delegation_id: str) -> DelegationSnapshot:
        with self._lock:
            return self._snapshot(self._node(delegation_id))

    def continuation(self, continuation_id: str) -> OperatorContinuation:
        with self._lock:
            try:
                return self._continuations[continuation_id]
            except KeyError as exc:
                raise InvalidDelegationError("Unknown continuation id") from exc

    def root_delegation_count(self, root_delegation_id: str) -> int:
        with self._lock:
            return self._root_counts.get(root_delegation_id, 0)

    @staticmethod
    def _capabilities_for_new_node(
        values: Iterable[Capability | str],
    ) -> tuple[Capability, ...]:
        try:
            capabilities = tuple(Capability(value) for value in values)
        except (TypeError, ValueError) as exc:
            raise InvalidDelegationError("Delegation contains an unknown capability") from exc
        if len(set(capabilities)) != len(capabilities):
            raise InvalidDelegationError("Delegation capabilities must not contain duplicates")
        forbidden = [item for item in capabilities if item not in DELEGABLE_CAPABILITIES]
        if forbidden:
            rendered = ", ".join(sorted(item.value for item in forbidden))
            raise DelegationAuthorityError(
                f"M2.6 delegation cannot carry local authority: {rendered}"
            )
        return tuple(sorted(capabilities, key=lambda item: item.value))

    def _node(self, delegation_id: str) -> _DelegationNode:
        try:
            return self._nodes[delegation_id]
        except KeyError as exc:
            raise InvalidDelegationError("Unknown delegation id") from exc

    @staticmethod
    def _require_state(node: _DelegationNode, expected: DelegationState) -> None:
        if node.state is not expected:
            raise DelegationStateError(
                f"Delegation is {node.state.value}; expected {expected.value}"
            )

    def _cancel_subtree(self, delegation_id: str) -> None:
        node = self._node(delegation_id)
        for child_id in node.child_ids:
            child = self._node(child_id)
            if child.state not in {DelegationState.COMPLETED, DelegationState.CANCELLED}:
                self._cancel_subtree(child_id)
        if node.state is not DelegationState.COMPLETED:
            node.pending_escalation = None
            node.state = DelegationState.CANCELLED

    @staticmethod
    def _snapshot(node: _DelegationNode) -> DelegationSnapshot:
        return DelegationSnapshot(
            envelope=node.envelope,
            state=node.state,
            remaining_budget=node.remaining_budget,
            child_ids=tuple(node.child_ids),
            pending_escalation=node.pending_escalation,
            result_summary=node.result_summary,
            continuation_ids=tuple(node.continuation_ids),
            control_request_ids=tuple(sorted(node.control_requests)),
        )
