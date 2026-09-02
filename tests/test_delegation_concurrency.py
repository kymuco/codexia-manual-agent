from __future__ import annotations

import tempfile
import threading
import unittest

from codexia_manual_agent.delegation import (
    DelegationBudget,
    DelegationBudgetError,
    DelegationCoordinator,
)
from codexia_manual_agent.domain.capabilities import Capability


class DelegationConcurrencyTests(unittest.TestCase):
    def test_concurrent_children_cannot_reserve_the_same_parent_budget_twice(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=5, tool_calls=2, model_chars=10_000),
            )
            barrier = threading.Barrier(3)
            outcomes: list[tuple[str, str]] = []
            outcomes_lock = threading.Lock()

            def worker(name: str) -> None:
                barrier.wait()
                try:
                    child = coordinator.create_child(
                        root.delegation_id,
                        task=f"Child {name}",
                        capabilities=(Capability.READ_WORKSPACE,),
                        budget=DelegationBudget(
                            turns=4,
                            tool_calls=1,
                            model_chars=7_000,
                        ),
                    )
                except DelegationBudgetError:
                    outcome = ("rejected", name)
                else:
                    outcome = ("created", child.delegation_id)
                with outcomes_lock:
                    outcomes.append(outcome)

            threads = [
                threading.Thread(target=worker, args=("a",)),
                threading.Thread(target=worker, args=("b",)),
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

            self.assertEqual(sum(kind == "created" for kind, _ in outcomes), 1)
            self.assertEqual(sum(kind == "rejected" for kind, _ in outcomes), 1)
            snapshot = coordinator.snapshot(root.delegation_id)
            self.assertEqual(len(snapshot.child_ids), 1)
            self.assertEqual(
                snapshot.remaining_budget,
                DelegationBudget(turns=1, tool_calls=1, model_chars=3_000),
            )
            self.assertEqual(coordinator.root_delegation_count(root.delegation_id), 2)


if __name__ == "__main__":
    unittest.main()
