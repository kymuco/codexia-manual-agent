from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from uuid import uuid4

from codexia_manual_agent.authority import (
    ActionProposal,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import AuthorizationConsumedError


def _proposal(label: str) -> ActionProposal:
    return ActionProposal.create(
        capability=Capability.EXECUTE_PROCESS,
        action=f"test:{label}",
        workspace_root="W:/dev/example",
        parameters={"label": label},
        proposal_id=str(uuid4()),
        created_at="2026-08-07T08:20:00+00:00",
    )


class SharedConsumptionRegistryTests(unittest.TestCase):
    def test_consumption_is_shared_across_authority_instances(self) -> None:
        item = _proposal("cross-instance")
        first = LocalApprovalAuthority()
        second = LocalApprovalAuthority()
        receipt = first.decide(
            item,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="user",
        )

        first.consume(item, receipt, mode=ApprovalMode.RISKY)
        self.assertTrue(second.is_consumed(receipt))
        with self.assertRaises(AuthorizationConsumedError):
            second.consume(item, receipt, mode=ApprovalMode.RISKY)

    def test_concurrent_consumers_allow_exactly_one_success(self) -> None:
        item = _proposal("concurrent")
        issuer = LocalApprovalAuthority()
        receipt = issuer.decide(
            item,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="user",
        )
        authorities = (LocalApprovalAuthority(), LocalApprovalAuthority())
        barrier = Barrier(2)

        def attempt(authority: LocalApprovalAuthority) -> str:
            barrier.wait(timeout=5)
            try:
                authority.consume(item, receipt, mode=ApprovalMode.RISKY)
            except AuthorizationConsumedError:
                return "consumed"
            return "success"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, authorities))

        self.assertCountEqual(results, ["success", "consumed"])


if __name__ == "__main__":
    unittest.main()
