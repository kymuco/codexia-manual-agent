from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.delegation import (
    DelegationAuthorityError,
    DelegationBudget,
    DelegationBudgetError,
    DelegationCoordinator,
    DelegationLimits,
    DelegationState,
    DelegationStateError,
)
from codexia_manual_agent.domain.capabilities import Capability


class DelegationContractTests(unittest.TestCase):
    def test_root_is_digest_bound_read_only_and_contains_no_authority_receipt_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Inspect the repository architecture.",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=8, tool_calls=8, model_chars=100_000),
            )

            payload = root.to_dict()
            self.assertEqual(payload["workspace_root"], str(Path(raw).resolve()))
            self.assertEqual(payload["capabilities"], ["read_workspace"])
            self.assertEqual(len(payload["delegation_digest"]), 64)
            rendered_keys = set(payload)
            for forbidden in (
                "proposal_id",
                "proposal_digest",
                "receipt_id",
                "receipt_digest",
                "authorization",
                "approved",
            ):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, rendered_keys)

            snapshot = coordinator.snapshot(root.delegation_id)
            self.assertIs(snapshot.state, DelegationState.ACTIVE)
            self.assertEqual(snapshot.remaining_budget, root.budget)

    def test_root_cannot_carry_mutation_process_network_or_git_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            for capability in (
                Capability.WRITE_WORKSPACE,
                Capability.EXECUTE_PROCESS,
                Capability.NETWORK_ACCESS,
                Capability.GIT_COMMIT,
                Capability.GIT_PUSH,
                Capability.DELETE_FILES,
                Capability.OUTSIDE_WORKSPACE,
            ):
                with self.subTest(capability=capability):
                    coordinator = DelegationCoordinator()
                    with self.assertRaises(DelegationAuthorityError):
                        coordinator.create_root(
                            workspace_root=raw,
                            task="Do bounded work.",
                            capabilities=(capability,),
                            budget=DelegationBudget(
                                turns=2,
                                tool_calls=1,
                                model_chars=10_000,
                            ),
                        )

    def test_child_capabilities_can_only_stay_same_or_shrink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Parent",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=8, tool_calls=8, model_chars=80_000),
            )
            child = coordinator.create_child(
                root.delegation_id,
                task="Read-only child",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=3, tool_calls=2, model_chars=20_000),
            )
            self.assertEqual(child.capabilities, (Capability.READ_WORKSPACE,))
            self.assertEqual(child.parent_delegation_id, root.delegation_id)
            self.assertEqual(child.parent_delegation_digest, root.delegation_digest)
            self.assertEqual(child.root_delegation_id, root.delegation_id)

            empty_child = coordinator.create_child(
                root.delegation_id,
                task="Reason without tools",
                capabilities=(),
                budget=DelegationBudget(turns=1, tool_calls=0, model_chars=5_000),
            )
            self.assertEqual(empty_child.capabilities, ())

            with self.assertRaises(DelegationAuthorityError):
                coordinator.create_child(
                    root.delegation_id,
                    task="Privilege growth",
                    capabilities=(Capability.GIT_PUSH,),
                    budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
                )

    def test_child_budget_is_reserved_from_parent_and_cannot_multiply(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Parent",
                budget=DelegationBudget(turns=8, tool_calls=8, model_chars=100_000),
            )
            coordinator.create_child(
                root.delegation_id,
                task="Child A",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=5, tool_calls=3, model_chars=60_000),
            )
            remaining = coordinator.snapshot(root.delegation_id).remaining_budget
            self.assertEqual(
                remaining,
                DelegationBudget(turns=3, tool_calls=5, model_chars=40_000),
            )

            with self.assertRaises(DelegationBudgetError):
                coordinator.create_child(
                    root.delegation_id,
                    task="Would multiply turns",
                    capabilities=(Capability.READ_WORKSPACE,),
                    budget=DelegationBudget(turns=4, tool_calls=1, model_chars=10_000),
                )
            self.assertEqual(
                coordinator.snapshot(root.delegation_id).remaining_budget,
                remaining,
            )

    def test_nested_delegation_conserves_the_original_root_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=10, tool_calls=6, model_chars=10_000),
                limits=DelegationLimits(max_depth=2, max_total_delegations=4),
            )
            child = coordinator.create_child(
                root.delegation_id,
                task="Child",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=6, tool_calls=4, model_chars=6_000),
            )
            grandchild = coordinator.create_child(
                child.delegation_id,
                task="Grandchild",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=2, tool_calls=1, model_chars=2_000),
            )

            root_remaining = coordinator.snapshot(root.delegation_id).remaining_budget
            child_remaining = coordinator.snapshot(child.delegation_id).remaining_budget
            grandchild_remaining = coordinator.snapshot(
                grandchild.delegation_id
            ).remaining_budget
            self.assertEqual(
                root_remaining,
                DelegationBudget(turns=4, tool_calls=2, model_chars=4_000),
            )
            self.assertEqual(
                child_remaining,
                DelegationBudget(turns=4, tool_calls=3, model_chars=4_000),
            )
            self.assertEqual(
                grandchild_remaining,
                DelegationBudget(turns=2, tool_calls=1, model_chars=2_000),
            )
            self.assertEqual(
                root_remaining.turns
                + child_remaining.turns
                + grandchild_remaining.turns,
                root.budget.turns,
            )
            self.assertEqual(
                root_remaining.tool_calls
                + child_remaining.tool_calls
                + grandchild_remaining.tool_calls,
                root.budget.tool_calls,
            )
            self.assertEqual(
                root_remaining.model_chars
                + child_remaining.model_chars
                + grandchild_remaining.model_chars,
                root.budget.model_chars,
            )

    def test_terminal_child_does_not_refund_budget_or_total_node_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=4, tool_calls=2, model_chars=4_000),
                limits=DelegationLimits(max_depth=1, max_total_delegations=2),
            )
            child = coordinator.create_child(
                root.delegation_id,
                task="Only lifetime child",
                capabilities=(),
                budget=DelegationBudget(turns=2, tool_calls=0, model_chars=2_000),
            )
            reserved_remaining = coordinator.snapshot(root.delegation_id).remaining_budget
            coordinator.complete(child.delegation_id, result_summary="Done")

            self.assertEqual(
                coordinator.snapshot(root.delegation_id).remaining_budget,
                reserved_remaining,
            )
            self.assertEqual(coordinator.root_delegation_count(root.delegation_id), 2)
            with self.assertRaises(DelegationBudgetError):
                coordinator.create_child(
                    root.delegation_id,
                    task="No churn refund",
                    capabilities=(),
                    budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
                )

    def test_depth_and_total_node_limits_are_root_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=10, tool_calls=4, model_chars=40_000),
                limits=DelegationLimits(max_depth=1, max_total_delegations=2),
            )
            child = coordinator.create_child(
                root.delegation_id,
                task="Only child",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=3, tool_calls=1, model_chars=10_000),
            )
            self.assertEqual(coordinator.root_delegation_count(root.delegation_id), 2)

            with self.assertRaises(DelegationBudgetError):
                coordinator.create_child(
                    child.delegation_id,
                    task="Too deep",
                    capabilities=(),
                    budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
                )
            with self.assertRaises(DelegationBudgetError):
                coordinator.create_child(
                    root.delegation_id,
                    task="Too many nodes",
                    capabilities=(),
                    budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
                )

    def test_budget_consumption_is_exact_and_waiting_or_terminal_state_blocks_work(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=3, tool_calls=2, model_chars=5_000),
            )
            remaining = coordinator.consume_budget(
                root.delegation_id,
                turns=1,
                tool_calls=1,
                model_chars=2_000,
            )
            self.assertEqual(
                remaining,
                DelegationBudget(turns=2, tool_calls=1, model_chars=3_000),
            )
            with self.assertRaises(DelegationBudgetError):
                coordinator.consume_budget(root.delegation_id, turns=3)

            completed = coordinator.complete(
                root.delegation_id,
                result_summary="Inspection complete.",
            )
            self.assertIs(completed.state, DelegationState.COMPLETED)
            with self.assertRaises(DelegationStateError):
                coordinator.consume_budget(root.delegation_id, turns=1)

    def test_parent_cannot_complete_while_child_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=5, tool_calls=2, model_chars=10_000),
            )
            child = coordinator.create_child(
                root.delegation_id,
                task="Child",
                capabilities=(),
                budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
            )
            with self.assertRaises(DelegationStateError):
                coordinator.complete(root.delegation_id, result_summary="Too early")
            coordinator.complete(child.delegation_id, result_summary="Child done")
            result = coordinator.complete(root.delegation_id, result_summary="Root done")
            self.assertIs(result.state, DelegationState.COMPLETED)


if __name__ == "__main__":
    unittest.main()
