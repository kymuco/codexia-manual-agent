from __future__ import annotations

import tempfile
import unittest

from codexia_manual_agent.delegation import (
    ContinuationDecision,
    DelegationAuthorityError,
    DelegationBudget,
    DelegationCoordinator,
    DelegationState,
    DelegationStateError,
    EscalationBindingError,
    EscalationReason,
)
from codexia_manual_agent.domain.capabilities import Capability


class DelegationEscalationTests(unittest.TestCase):
    def _root(self, raw: str):
        coordinator = DelegationCoordinator()
        root = coordinator.create_root(
            workspace_root=raw,
            task="Inspect and report.",
            budget=DelegationBudget(turns=6, tool_calls=4, model_chars=30_000),
        )
        return coordinator, root

    def test_external_git_push_escalation_does_not_mint_git_push_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator, root = self._root(raw)
            with self.assertRaises(DelegationAuthorityError):
                coordinator.assert_capability(root.delegation_id, Capability.GIT_PUSH)

            before = coordinator.snapshot(root.delegation_id)
            escalation = coordinator.request_escalation(
                root.delegation_id,
                reason=EscalationReason.EXTERNAL,
                summary="A remote push would be needed to continue.",
                requested_capability=Capability.GIT_PUSH,
                requested_action="git.push.v1",
            )
            waiting = coordinator.snapshot(root.delegation_id)
            self.assertIs(waiting.state, DelegationState.WAITING_HUMAN)
            self.assertEqual(waiting.pending_escalation, escalation)
            self.assertEqual(waiting.envelope, before.envelope)
            self.assertEqual(waiting.remaining_budget, before.remaining_budget)

            continuation = coordinator.resolve_escalation(
                escalation,
                decision=ContinuationDecision.CONTINUE,
                actor="operator",
                note="The separately governed action was considered.",
            )
            resumed = coordinator.snapshot(root.delegation_id)
            self.assertIs(resumed.state, DelegationState.ACTIVE)
            self.assertEqual(resumed.envelope, before.envelope)
            self.assertEqual(resumed.remaining_budget, before.remaining_budget)
            self.assertIn(continuation.continuation_id, resumed.continuation_ids)

            # Human continuation is workflow control only. It never inserts the
            # requested mutation capability into the delegation envelope.
            with self.assertRaises(DelegationAuthorityError):
                coordinator.assert_capability(root.delegation_id, Capability.GIT_PUSH)

            continuation_payload = continuation.to_dict()
            for forbidden in (
                "proposal_id",
                "proposal_digest",
                "receipt_id",
                "receipt_digest",
                "authorization",
                "approved",
            ):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, continuation_payload)

    def test_waiting_human_blocks_child_budget_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator, root = self._root(raw)
            escalation = coordinator.request_escalation(
                root.delegation_id,
                reason=EscalationReason.AMBIGUOUS,
                summary="The requested next step is ambiguous.",
            )
            with self.assertRaises(DelegationStateError):
                coordinator.create_child(
                    root.delegation_id,
                    task="Blocked child",
                    capabilities=(Capability.READ_WORKSPACE,),
                    budget=DelegationBudget(turns=1, tool_calls=1, model_chars=1_000),
                )
            with self.assertRaises(DelegationStateError):
                coordinator.consume_budget(root.delegation_id, turns=1)
            with self.assertRaises(DelegationStateError):
                coordinator.complete(root.delegation_id, result_summary="Blocked")

            coordinator.resolve_escalation(
                escalation,
                decision=ContinuationDecision.CONTINUE,
                actor="operator",
            )
            coordinator.consume_budget(root.delegation_id, turns=1)

    def test_wrong_or_replayed_escalation_cannot_resume_work(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator, root = self._root(raw)
            escalation = coordinator.request_escalation(
                root.delegation_id,
                reason=EscalationReason.NOVEL,
                summary="Novel decision.",
            )
            coordinator.resolve_escalation(
                escalation,
                decision=ContinuationDecision.CONTINUE,
                actor="operator",
            )
            with self.assertRaises(DelegationStateError):
                coordinator.resolve_escalation(
                    escalation,
                    decision=ContinuationDecision.CONTINUE,
                    actor="operator",
                )

            second = coordinator.request_escalation(
                root.delegation_id,
                reason=EscalationReason.POLICY_SENSITIVE,
                summary="Policy decision.",
            )
            with self.assertRaises((DelegationStateError, EscalationBindingError)):
                coordinator.resolve_escalation(
                    escalation,
                    decision=ContinuationDecision.CONTINUE,
                    actor="operator",
                )
            coordinator.resolve_escalation(
                second,
                decision=ContinuationDecision.CONTINUE,
                actor="operator",
            )

    def test_cancel_cascades_to_live_descendants_but_preserves_completed_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator, root = self._root(raw)
            completed_child = coordinator.create_child(
                root.delegation_id,
                task="Completed child",
                capabilities=(),
                budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
            )
            live_child = coordinator.create_child(
                root.delegation_id,
                task="Live child",
                capabilities=(),
                budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
            )
            coordinator.complete(
                completed_child.delegation_id,
                result_summary="Completed before cancellation.",
            )
            escalation = coordinator.request_escalation(
                root.delegation_id,
                reason=EscalationReason.DESTRUCTIVE,
                summary="Operator cancellation required.",
                requested_capability=Capability.DELETE_FILES,
            )
            coordinator.resolve_escalation(
                escalation,
                decision=ContinuationDecision.CANCEL,
                actor="operator",
            )
            self.assertIs(
                coordinator.snapshot(root.delegation_id).state,
                DelegationState.CANCELLED,
            )
            self.assertIs(
                coordinator.snapshot(live_child.delegation_id).state,
                DelegationState.CANCELLED,
            )
            self.assertIs(
                coordinator.snapshot(completed_child.delegation_id).state,
                DelegationState.COMPLETED,
            )


if __name__ == "__main__":
    unittest.main()
