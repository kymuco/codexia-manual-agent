from __future__ import annotations

import unittest
from uuid import uuid4

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionProposal,
    ApprovalMode,
    AuthorizationDecision,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidActionTransitionError,
    InvalidApprovalDecisionError,
)


def _proposal(label: str) -> ActionProposal:
    return ActionProposal.create(
        capability=Capability.EXECUTE_PROCESS,
        action=f"test:{label}",
        workspace_root="W:/dev/example",
        parameters={"label": label},
        proposal_id=str(uuid4()),
        created_at="2026-08-07T08:20:00+00:00",
    )


class LifecycleIdentityRepairTests(unittest.TestCase):
    def _consumed(self) -> tuple[ActionLifecycle, LocalApprovalAuthority]:
        item = _proposal("lifecycle")
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            item,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="user",
        )
        lifecycle = ActionLifecycle(item, ApprovalMode.RISKY)
        lifecycle.apply_receipt(receipt, authority=authority)
        lifecycle.consume_authorization(authority=authority)
        return lifecycle, authority

    def test_proposal_is_write_once(self) -> None:
        lifecycle, _authority = self._consumed()
        with self.assertRaises(AttributeError):
            lifecycle.proposal = _proposal("replacement")

    def test_mode_is_write_once(self) -> None:
        lifecycle, _authority = self._consumed()
        with self.assertRaises(AttributeError):
            lifecycle.mode = ApprovalMode.ALWAYS

    def test_receipt_swap_after_consume_blocks_execution(self) -> None:
        lifecycle, authority = self._consumed()
        lifecycle.authorization = authority.decide(
            lifecycle.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="user",
        )
        with self.assertRaisesRegex(
            InvalidActionTransitionError,
            "changed after consumption",
        ):
            lifecycle.record_executed("exec-replaced")


class StrictApprovalTypeTests(unittest.TestCase):
    def test_non_boolean_human_decisions_are_rejected(self) -> None:
        item = _proposal("invalid")
        authority = LocalApprovalAuthority()
        for value in ("false", "true", 0, 1, [], {}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(InvalidApprovalDecisionError):
                    authority.decide(
                        item,
                        mode=ApprovalMode.RISKY,
                        approved=value,
                        actor="user",
                    )

    def test_true_is_allow(self) -> None:
        receipt = LocalApprovalAuthority().decide(
            _proposal("true"),
            mode=ApprovalMode.RISKY,
            approved=True,
        )
        self.assertEqual(receipt.decision, AuthorizationDecision.ALLOW)

    def test_false_is_deny(self) -> None:
        receipt = LocalApprovalAuthority().decide(
            _proposal("false"),
            mode=ApprovalMode.RISKY,
            approved=False,
        )
        self.assertEqual(receipt.decision, AuthorizationDecision.DENY)


if __name__ == "__main__":
    unittest.main()
