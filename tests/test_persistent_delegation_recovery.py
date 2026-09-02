from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.delegation import (
    ContinuationDecision,
    DelegateWorkRequest,
    DelegationBudget,
    DelegationBudgetError,
    DelegationLimits,
    DelegationReplayError,
    DelegationState,
    EscalationReason,
    SqliteDelegationCoordinator,
    apply_delegation_control_request,
)
from codexia_manual_agent.domain.capabilities import Capability


class PersistentDelegationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.database = self.root / "delegation.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def coordinator(self) -> SqliteDelegationCoordinator:
        return SqliteDelegationCoordinator(self.database)

    def test_child_reservation_and_budget_consumption_survive_restart(self) -> None:
        first = self.coordinator()
        root = first.create_root(
            workspace_root=self.workspace,
            task="root",
            budget=DelegationBudget(turns=10, tool_calls=4, model_chars=1000),
        )
        child = first.create_child(
            root.delegation_id,
            task="child",
            capabilities=(Capability.READ_WORKSPACE,),
            budget=DelegationBudget(turns=3, tool_calls=1, model_chars=300),
        )
        first.consume_budget(child.delegation_id, turns=1, model_chars=50)

        restarted = self.coordinator()
        root_snapshot = restarted.snapshot(root.delegation_id)
        child_snapshot = restarted.snapshot(child.delegation_id)

        self.assertEqual(
            root_snapshot.remaining_budget,
            DelegationBudget(turns=7, tool_calls=3, model_chars=700),
        )
        self.assertEqual(
            child_snapshot.remaining_budget,
            DelegationBudget(turns=2, tool_calls=1, model_chars=250),
        )
        self.assertEqual(restarted.root_delegation_count(root.delegation_id), 2)

        with self.assertRaises(DelegationBudgetError):
            restarted.create_child(
                root.delegation_id,
                task="cannot refund reserved budget",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=8, tool_calls=1, model_chars=100),
            )

    def test_control_request_claim_remains_non_replayable_after_restart(self) -> None:
        first = self.coordinator()
        root = first.create_root(
            workspace_root=self.workspace,
            task="root",
            budget=DelegationBudget(turns=5, tool_calls=2, model_chars=500),
        )
        first.claim_control_request(
            root.delegation_id,
            request_id="request-1",
            request_digest="1" * 64,
        )

        restarted = self.coordinator()
        with self.assertRaises(DelegationReplayError):
            restarted.claim_control_request(
                root.delegation_id,
                request_id="request-1",
                request_digest="1" * 64,
            )
        with self.assertRaises(DelegationReplayError):
            restarted.claim_control_request(
                root.delegation_id,
                request_id="request-1",
                request_digest="2" * 64,
            )

    def test_failed_model_control_application_keeps_durable_claim(self) -> None:
        first = self.coordinator()
        root = first.create_root(
            workspace_root=self.workspace,
            task="root",
            budget=DelegationBudget(turns=2, tool_calls=1, model_chars=200),
        )
        request = DelegateWorkRequest.create(
            request_id="too-large-child",
            task="child",
            capabilities=(Capability.READ_WORKSPACE,),
            budget=DelegationBudget(turns=3, tool_calls=1, model_chars=100),
        )

        with self.assertRaises(DelegationBudgetError):
            apply_delegation_control_request(
                first,
                current_delegation_id=root.delegation_id,
                request=request,
            )

        restarted = self.coordinator()
        self.assertIn(
            request.request_id,
            restarted.snapshot(root.delegation_id).control_request_ids,
        )
        with self.assertRaises(DelegationReplayError):
            apply_delegation_control_request(
                restarted,
                current_delegation_id=root.delegation_id,
                request=request,
            )

    def test_pending_escalation_and_continue_survive_restart_without_resource_growth(self) -> None:
        first = self.coordinator()
        root = first.create_root(
            workspace_root=self.workspace,
            task="root",
            budget=DelegationBudget(turns=6, tool_calls=2, model_chars=600),
        )
        first.consume_budget(root.delegation_id, turns=2, model_chars=100)
        before = first.snapshot(root.delegation_id)
        escalation = first.request_escalation(
            root.delegation_id,
            reason=EscalationReason.EXTERNAL,
            summary="Need an independently authorized Git push",
            requested_capability=Capability.GIT_PUSH,
            requested_action="git.push.v1",
        )

        restarted = self.coordinator()
        waiting = restarted.snapshot(root.delegation_id)
        self.assertIs(waiting.state, DelegationState.WAITING_HUMAN)
        self.assertIsNotNone(waiting.pending_escalation)
        assert waiting.pending_escalation is not None
        self.assertEqual(waiting.pending_escalation.to_dict(), escalation.to_dict())
        self.assertEqual(waiting.remaining_budget, before.remaining_budget)
        self.assertEqual(waiting.envelope.capabilities, before.envelope.capabilities)

        continuation = restarted.resolve_escalation(
            escalation,
            decision=ContinuationDecision.CONTINUE,
            actor="operator",
            note="External action was handled under separate authority",
        )

        second_restart = self.coordinator()
        resumed = second_restart.snapshot(root.delegation_id)
        self.assertIs(resumed.state, DelegationState.ACTIVE)
        self.assertIsNone(resumed.pending_escalation)
        self.assertEqual(resumed.remaining_budget, before.remaining_budget)
        self.assertEqual(resumed.envelope.capabilities, before.envelope.capabilities)
        self.assertEqual(
            second_restart.continuation(continuation.continuation_id).to_dict(),
            continuation.to_dict(),
        )

    def test_cancel_reconstructs_subtree_without_reviving_completed_children(self) -> None:
        first = self.coordinator()
        root = first.create_root(
            workspace_root=self.workspace,
            task="root",
            budget=DelegationBudget(turns=12, tool_calls=6, model_chars=1200),
            limits=DelegationLimits(max_depth=2, max_total_delegations=4),
        )
        completed_child = first.create_child(
            root.delegation_id,
            task="done child",
            capabilities=(Capability.READ_WORKSPACE,),
            budget=DelegationBudget(turns=2, tool_calls=1, model_chars=200),
        )
        first.complete(completed_child.delegation_id, result_summary="done")

        live_child = first.create_child(
            root.delegation_id,
            task="live child",
            capabilities=(Capability.READ_WORKSPACE,),
            budget=DelegationBudget(turns=4, tool_calls=2, model_chars=400),
        )
        grandchild = first.create_child(
            live_child.delegation_id,
            task="grandchild",
            capabilities=(Capability.READ_WORKSPACE,),
            budget=DelegationBudget(turns=1, tool_calls=1, model_chars=100),
        )
        escalation = first.request_escalation(
            live_child.delegation_id,
            reason=EscalationReason.AMBIGUOUS,
            summary="Cancel this subtree",
        )
        first.resolve_escalation(
            escalation,
            decision=ContinuationDecision.CANCEL,
            actor="operator",
        )

        restarted = self.coordinator()
        self.assertIs(
            restarted.snapshot(completed_child.delegation_id).state,
            DelegationState.COMPLETED,
        )
        self.assertIs(
            restarted.snapshot(live_child.delegation_id).state,
            DelegationState.CANCELLED,
        )
        self.assertIs(
            restarted.snapshot(grandchild.delegation_id).state,
            DelegationState.CANCELLED,
        )
        self.assertIs(restarted.snapshot(root.delegation_id).state, DelegationState.ACTIVE)
        self.assertEqual(restarted.root_delegation_count(root.delegation_id), 4)

    def test_cancelled_child_does_not_refund_root_lifetime_node_slot(self) -> None:
        first = self.coordinator()
        root = first.create_root(
            workspace_root=self.workspace,
            task="root",
            budget=DelegationBudget(turns=8, tool_calls=4, model_chars=800),
            limits=DelegationLimits(max_depth=1, max_total_delegations=2),
        )
        child = first.create_child(
            root.delegation_id,
            task="child",
            capabilities=(Capability.READ_WORKSPACE,),
            budget=DelegationBudget(turns=2, tool_calls=1, model_chars=200),
        )
        escalation = first.request_escalation(
            child.delegation_id,
            reason=EscalationReason.NOVEL,
            summary="stop child",
        )
        first.resolve_escalation(
            escalation,
            decision=ContinuationDecision.CANCEL,
            actor="operator",
        )

        restarted = self.coordinator()
        self.assertEqual(restarted.root_delegation_count(root.delegation_id), 2)
        with self.assertRaises(DelegationBudgetError):
            restarted.create_child(
                root.delegation_id,
                task="replacement child must not reuse lifetime slot",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=1, tool_calls=1, model_chars=100),
            )


if __name__ == "__main__":
    unittest.main()
