from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from codexia_manual_agent.delegation import (
    DelegationBudget,
    DelegationBudgetError,
    DelegationReplayError,
    SqliteDelegationCoordinator,
)
from codexia_manual_agent.domain.capabilities import Capability


class PersistentDelegationConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.database = self.root / "delegation.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _race(self, left, right):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        results: list[tuple[str, object]] = []

        def run(label, operation):
            barrier.wait(timeout=10)
            try:
                value = operation()
            except Exception as exc:  # test captures exact one-winner failure type
                value = exc
            with lock:
                results.append((label, value))

        threads = [
            threading.Thread(target=run, args=("left", left)),
            threading.Thread(target=run, args=("right", right)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        return results

    def test_concurrent_child_reservation_has_one_budget_winner(self) -> None:
        creator = SqliteDelegationCoordinator(self.database)
        root = creator.create_root(
            workspace_root=self.workspace,
            task="root",
            budget=DelegationBudget(turns=10, tool_calls=2, model_chars=1000),
        )
        left = SqliteDelegationCoordinator(self.database)
        right = SqliteDelegationCoordinator(self.database)

        results = self._race(
            lambda: left.create_child(
                root.delegation_id,
                task="left",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=7, tool_calls=1, model_chars=700),
            ),
            lambda: right.create_child(
                root.delegation_id,
                task="right",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=7, tool_calls=1, model_chars=700),
            ),
        )

        successes = [value for _, value in results if not isinstance(value, Exception)]
        failures = [value for _, value in results if isinstance(value, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], DelegationBudgetError)

        recovered = SqliteDelegationCoordinator(self.database)
        snapshot = recovered.snapshot(root.delegation_id)
        self.assertEqual(
            snapshot.remaining_budget,
            DelegationBudget(turns=3, tool_calls=1, model_chars=300),
        )
        self.assertEqual(recovered.root_delegation_count(root.delegation_id), 2)

    def test_concurrent_same_control_claim_has_one_winner(self) -> None:
        creator = SqliteDelegationCoordinator(self.database)
        root = creator.create_root(
            workspace_root=self.workspace,
            task="root",
            budget=DelegationBudget(turns=5, tool_calls=2, model_chars=500),
        )
        left = SqliteDelegationCoordinator(self.database)
        right = SqliteDelegationCoordinator(self.database)

        def claim(coordinator):
            coordinator.claim_control_request(
                root.delegation_id,
                request_id="same-request",
                request_digest="a" * 64,
            )
            return "claimed"

        results = self._race(lambda: claim(left), lambda: claim(right))
        successes = [value for _, value in results if value == "claimed"]
        failures = [value for _, value in results if isinstance(value, Exception)]
        self.assertEqual(successes, ["claimed"])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], DelegationReplayError)

        recovered = SqliteDelegationCoordinator(self.database)
        self.assertEqual(
            recovered.snapshot(root.delegation_id).control_request_ids,
            ("same-request",),
        )

    def test_concurrent_budget_consumption_has_one_winner(self) -> None:
        creator = SqliteDelegationCoordinator(self.database)
        root = creator.create_root(
            workspace_root=self.workspace,
            task="root",
            budget=DelegationBudget(turns=10, tool_calls=0, model_chars=1000),
        )
        left = SqliteDelegationCoordinator(self.database)
        right = SqliteDelegationCoordinator(self.database)

        results = self._race(
            lambda: left.consume_budget(root.delegation_id, turns=7, model_chars=700),
            lambda: right.consume_budget(root.delegation_id, turns=7, model_chars=700),
        )
        successes = [value for _, value in results if not isinstance(value, Exception)]
        failures = [value for _, value in results if isinstance(value, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], DelegationBudgetError)
        self.assertEqual(
            SqliteDelegationCoordinator(self.database)
            .snapshot(root.delegation_id)
            .remaining_budget,
            DelegationBudget(turns=3, tool_calls=0, model_chars=300),
        )


if __name__ == "__main__":
    unittest.main()
