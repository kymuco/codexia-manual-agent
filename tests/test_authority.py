from __future__ import annotations

import unittest
from dataclasses import replace

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    ActionRisk,
    ApprovalMode,
    ApprovalPolicy,
    ApprovalRequirement,
    AuthorizationDecision,
    AuthorizationSource,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    ActionIntegrityError,
    ApprovalRequiredError,
    AuthorizationConsumedError,
    AuthorizationMismatchError,
    InvalidActionTransitionError,
)


_FIXED_ID = "00000000-0000-0000-0000-000000000001"
_FIXED_TIME = "2026-08-07T07:00:00+00:00"


def proposal(
    capability: Capability,
    *,
    parameters=None,
    proposal_id: str = _FIXED_ID,
) -> ActionProposal:
    return ActionProposal.create(
        capability=capability,
        action=f"test:{capability.value}",
        workspace_root="W:/dev/example",
        parameters=parameters or {},
        summary="test action",
        proposal_id=proposal_id,
        created_at=_FIXED_TIME,
    )


class ActionProposalTests(unittest.TestCase):
    def test_digest_is_stable_across_mapping_order(self) -> None:
        first = proposal(
            Capability.EXECUTE_PROCESS,
            parameters={"argv": ["python", "-V"], "timeout": 10},
        )
        second = proposal(
            Capability.EXECUTE_PROCESS,
            parameters={"timeout": 10, "argv": ["python", "-V"]},
        )
        self.assertEqual(first.proposal_digest, second.proposal_digest)

    def test_input_parameters_are_snapshotted(self) -> None:
        original = {"argv": ["python", "-V"]}
        item = proposal(Capability.EXECUTE_PROCESS, parameters=original)
        original["argv"].append("--changed")
        self.assertEqual(item.to_dict()["parameters"]["argv"], ["python", "-V"])

    def test_nested_parameters_are_not_mutable(self) -> None:
        item = proposal(
            Capability.EXECUTE_PROCESS,
            parameters={"argv": ["python", "-V"]},
        )
        with self.assertRaises(TypeError):
            item.parameters["argv"] = ("other",)

    def test_non_finite_numbers_are_rejected(self) -> None:
        with self.assertRaises(ActionIntegrityError):
            proposal(
                Capability.EXECUTE_PROCESS,
                parameters={"timeout": float("nan")},
            )

    def test_tampered_proposal_digest_is_rejected(self) -> None:
        item = proposal(Capability.WRITE_WORKSPACE)
        with self.assertRaises(ActionIntegrityError):
            replace(item, proposal_digest="0" * 64)


class ApprovalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ApprovalPolicy()

    def test_capability_classification_is_local_and_fixed(self) -> None:
        expected = {
            Capability.READ_WORKSPACE: ActionRisk.READ_ONLY,
            Capability.WRITE_WORKSPACE: ActionRisk.WORKSPACE_MUTATION,
            Capability.EXECUTE_PROCESS: ActionRisk.PROCESS_EXECUTION,
            Capability.NETWORK_ACCESS: ActionRisk.NETWORK_ACCESS,
            Capability.GIT_COMMIT: ActionRisk.EXTERNAL_GIT,
            Capability.GIT_PUSH: ActionRisk.EXTERNAL_GIT,
            Capability.DELETE_FILES: ActionRisk.DESTRUCTIVE,
            Capability.OUTSIDE_WORKSPACE: ActionRisk.OUTSIDE_WORKSPACE,
        }
        for capability, risk in expected.items():
            with self.subTest(capability=capability):
                self.assertEqual(self.policy.classify(proposal(capability)), risk)

    def test_reads_auto_authorize_in_all_modes(self) -> None:
        item = proposal(Capability.READ_WORKSPACE)
        for mode in ApprovalMode:
            with self.subTest(mode=mode):
                self.assertEqual(
                    self.policy.evaluate(item, mode),
                    ApprovalRequirement.AUTO_AUTHORIZE,
                )

    def test_side_effects_require_human_in_always_and_risky(self) -> None:
        item = proposal(Capability.WRITE_WORKSPACE)
        for mode in (ApprovalMode.ALWAYS, ApprovalMode.RISKY):
            with self.subTest(mode=mode):
                self.assertEqual(
                    self.policy.evaluate(item, mode),
                    ApprovalRequirement.REQUIRE_HUMAN,
                )

    def test_never_mode_denies_side_effects(self) -> None:
        item = proposal(Capability.EXECUTE_PROCESS)
        self.assertEqual(
            self.policy.evaluate(item, ApprovalMode.NEVER),
            ApprovalRequirement.DENY,
        )


class LocalApprovalAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = LocalApprovalAuthority()

    def test_auto_authorization_is_policy_sourced_and_single_use(self) -> None:
        item = proposal(Capability.READ_WORKSPACE)
        receipt = self.authority.decide(item, mode=ApprovalMode.NEVER)
        self.assertEqual(receipt.decision, AuthorizationDecision.ALLOW)
        self.assertEqual(receipt.source, AuthorizationSource.POLICY)
        self.assertTrue(receipt.single_use)

    def test_side_effect_requires_explicit_human_decision(self) -> None:
        item = proposal(Capability.WRITE_WORKSPACE)
        with self.assertRaises(ApprovalRequiredError):
            self.authority.decide(item, mode=ApprovalMode.RISKY)

    def test_human_denial_is_bound_to_proposal(self) -> None:
        item = proposal(Capability.WRITE_WORKSPACE)
        receipt = self.authority.decide(
            item,
            mode=ApprovalMode.RISKY,
            approved=False,
            actor="user",
            reason="not now",
        )
        self.assertEqual(receipt.decision, AuthorizationDecision.DENY)
        self.assertEqual(receipt.source, AuthorizationSource.HUMAN)
        self.authority.verify_binding(item, receipt, mode=ApprovalMode.RISKY)

    def test_never_mode_cannot_be_overridden_by_approved_true(self) -> None:
        item = proposal(Capability.WRITE_WORKSPACE)
        receipt = self.authority.decide(
            item,
            mode=ApprovalMode.NEVER,
            approved=True,
            actor="user",
        )
        self.assertEqual(receipt.decision, AuthorizationDecision.DENY)
        self.assertEqual(receipt.source, AuthorizationSource.POLICY)

    def test_policy_sourced_allow_cannot_forge_side_effect_approval(self) -> None:
        item = proposal(Capability.WRITE_WORKSPACE)
        from codexia_manual_agent.authority import AuthorizationReceipt

        forged = AuthorizationReceipt.issue(
            proposal=item,
            decision=AuthorizationDecision.ALLOW,
            mode=ApprovalMode.RISKY,
            source=AuthorizationSource.POLICY,
            actor="local-policy",
        )
        with self.assertRaises(AuthorizationMismatchError):
            self.authority.verify_authorization(
                item,
                forged,
                mode=ApprovalMode.RISKY,
            )

    def test_receipt_cannot_cross_proposals(self) -> None:
        first = proposal(Capability.WRITE_WORKSPACE)
        second = proposal(
            Capability.WRITE_WORKSPACE,
            proposal_id="00000000-0000-0000-0000-000000000002",
        )
        receipt = self.authority.decide(
            first,
            mode=ApprovalMode.ALWAYS,
            approved=True,
        )
        with self.assertRaises(AuthorizationMismatchError):
            self.authority.verify_authorization(
                second,
                receipt,
                mode=ApprovalMode.ALWAYS,
            )

    def test_receipt_cannot_cross_approval_modes(self) -> None:
        item = proposal(Capability.WRITE_WORKSPACE)
        receipt = self.authority.decide(
            item,
            mode=ApprovalMode.RISKY,
            approved=True,
        )
        with self.assertRaises(AuthorizationMismatchError):
            self.authority.verify_authorization(
                item,
                receipt,
                mode=ApprovalMode.ALWAYS,
            )

    def test_consumption_is_one_shot(self) -> None:
        item = proposal(Capability.WRITE_WORKSPACE)
        receipt = self.authority.decide(
            item,
            mode=ApprovalMode.RISKY,
            approved=True,
        )
        self.authority.consume(item, receipt, mode=ApprovalMode.RISKY)
        self.assertTrue(self.authority.is_consumed(receipt))
        with self.assertRaises(AuthorizationConsumedError):
            self.authority.consume(item, receipt, mode=ApprovalMode.RISKY)

    def test_tampered_receipt_digest_is_rejected(self) -> None:
        item = proposal(Capability.WRITE_WORKSPACE)
        receipt = self.authority.decide(
            item,
            mode=ApprovalMode.RISKY,
            approved=True,
        )
        with self.assertRaises(ActionIntegrityError):
            replace(receipt, receipt_digest="0" * 64)


class ActionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = LocalApprovalAuthority()
        self.item = proposal(Capability.WRITE_WORKSPACE)

    def test_happy_path_is_strictly_ordered(self) -> None:
        receipt = self.authority.decide(
            self.item,
            mode=ApprovalMode.RISKY,
            approved=True,
        )
        lifecycle = ActionLifecycle(self.item, ApprovalMode.RISKY)
        self.assertEqual(lifecycle.phase, ActionPhase.PROPOSED)

        self.assertEqual(
            lifecycle.apply_receipt(receipt, authority=self.authority),
            ActionPhase.AUTHORIZED,
        )
        lifecycle.consume_authorization(authority=self.authority)
        execution_id = lifecycle.record_executed("exec-1")
        self.assertEqual(execution_id, "exec-1")
        self.assertEqual(lifecycle.phase, ActionPhase.EXECUTED)

        observation_id = lifecycle.record_observed("obs-1")
        self.assertEqual(observation_id, "obs-1")
        self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)

    def test_record_executed_requires_prior_consumption(self) -> None:
        receipt = self.authority.decide(
            self.item,
            mode=ApprovalMode.RISKY,
            approved=True,
        )
        lifecycle = ActionLifecycle(self.item, ApprovalMode.RISKY)
        lifecycle.apply_receipt(receipt, authority=self.authority)
        with self.assertRaises(InvalidActionTransitionError):
            lifecycle.record_executed("exec-too-early")

    def test_execution_before_authorization_is_rejected(self) -> None:
        lifecycle = ActionLifecycle(self.item, ApprovalMode.RISKY)
        with self.assertRaises(InvalidActionTransitionError):
            lifecycle.consume_authorization(authority=self.authority)

    def test_denial_is_terminal(self) -> None:
        receipt = self.authority.decide(
            self.item,
            mode=ApprovalMode.RISKY,
            approved=False,
        )
        lifecycle = ActionLifecycle(self.item, ApprovalMode.RISKY)
        self.assertEqual(
            lifecycle.apply_receipt(receipt, authority=self.authority),
            ActionPhase.DENIED,
        )
        with self.assertRaises(InvalidActionTransitionError):
            lifecycle.consume_authorization(authority=self.authority)

    def test_observation_before_execution_is_rejected(self) -> None:
        receipt = self.authority.decide(
            self.item,
            mode=ApprovalMode.RISKY,
            approved=True,
        )
        lifecycle = ActionLifecycle(self.item, ApprovalMode.RISKY)
        lifecycle.apply_receipt(receipt, authority=self.authority)
        with self.assertRaises(InvalidActionTransitionError):
            lifecycle.record_observed("obs-early")

    def test_same_receipt_cannot_execute_twice_across_lifecycles(self) -> None:
        receipt = self.authority.decide(
            self.item,
            mode=ApprovalMode.RISKY,
            approved=True,
        )
        first = ActionLifecycle(self.item, ApprovalMode.RISKY)
        first.apply_receipt(receipt, authority=self.authority)
        first.consume_authorization(authority=self.authority)
        first.record_executed()

        second = ActionLifecycle(self.item, ApprovalMode.RISKY)
        second.apply_receipt(receipt, authority=self.authority)
        with self.assertRaises(AuthorizationConsumedError):
            second.consume_authorization(authority=self.authority)


if __name__ == "__main__":
    unittest.main()
