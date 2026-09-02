from __future__ import annotations

import json
import tempfile
import unittest

from codexia_manual_agent.delegation import (
    DelegationBudget,
    DelegationBudgetError,
    DelegationCoordinator,
    DelegationReplayError,
    parse_delegation_control_request,
)
from codexia_manual_agent.delegation.bridge import apply_delegation_control_request


class DelegationReplayTests(unittest.TestCase):
    def test_exact_control_request_cannot_create_two_children(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=6, tool_calls=3, model_chars=20_000),
            )
            request = parse_delegation_control_request(
                json.dumps(
                    {
                        "type": "delegate_request",
                        "request_id": "same-id",
                        "task": "Child",
                        "capabilities": [],
                        "budget": {"turns": 2, "tool_calls": 0, "model_chars": 3000},
                    }
                )
            )
            child = apply_delegation_control_request(
                coordinator,
                current_delegation_id=root.delegation_id,
                request=request,
            )
            with self.assertRaisesRegex(DelegationReplayError, "already claimed"):
                apply_delegation_control_request(
                    coordinator,
                    current_delegation_id=root.delegation_id,
                    request=request,
                )
            snapshot = coordinator.snapshot(root.delegation_id)
            self.assertEqual(snapshot.child_ids, (child.delegation_id,))
            self.assertEqual(snapshot.control_request_ids, ("same-id",))
            self.assertEqual(
                snapshot.remaining_budget,
                DelegationBudget(turns=4, tool_calls=3, model_chars=17_000),
            )

    def test_same_request_id_cannot_be_rebound_to_different_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=6, tool_calls=3, model_chars=20_000),
            )
            first = parse_delegation_control_request(
                json.dumps(
                    {
                        "type": "delegate_request",
                        "request_id": "collision",
                        "task": "Child A",
                        "capabilities": [],
                        "budget": {"turns": 1, "tool_calls": 0, "model_chars": 1000},
                    }
                )
            )
            apply_delegation_control_request(
                coordinator,
                current_delegation_id=root.delegation_id,
                request=first,
            )

            second = parse_delegation_control_request(
                json.dumps(
                    {
                        "type": "delegate_request",
                        "request_id": "collision",
                        "task": "Child B",
                        "capabilities": [],
                        "budget": {"turns": 1, "tool_calls": 0, "model_chars": 1000},
                    }
                )
            )
            self.assertNotEqual(first.request_digest, second.request_digest)
            with self.assertRaisesRegex(DelegationReplayError, "different payload"):
                apply_delegation_control_request(
                    coordinator,
                    current_delegation_id=root.delegation_id,
                    request=second,
                )

    def test_failed_application_keeps_claimed_request_conservatively_non_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=2, tool_calls=1, model_chars=2000),
            )
            oversized = parse_delegation_control_request(
                json.dumps(
                    {
                        "type": "delegate_request",
                        "request_id": "oversized",
                        "task": "Too large",
                        "capabilities": [],
                        "budget": {"turns": 2, "tool_calls": 1, "model_chars": 3000},
                    }
                )
            )
            with self.assertRaises(DelegationBudgetError):
                apply_delegation_control_request(
                    coordinator,
                    current_delegation_id=root.delegation_id,
                    request=oversized,
                )
            with self.assertRaises(DelegationReplayError):
                apply_delegation_control_request(
                    coordinator,
                    current_delegation_id=root.delegation_id,
                    request=oversized,
                )
            self.assertIn(
                "oversized",
                coordinator.snapshot(root.delegation_id).control_request_ids,
            )


if __name__ == "__main__":
    unittest.main()
