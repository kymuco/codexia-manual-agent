from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codexia_manual_agent.delegation import (
    DelegationBudget,
    DelegationEnvelope,
    DelegationLimits,
    EscalationRequest,
    InvalidDelegationError,
    OperatorContinuation,
)
from codexia_manual_agent.domain.capabilities import Capability


class DelegationModelHardeningTests(unittest.TestCase):
    def test_direct_child_factory_cannot_bypass_parent_capability_or_depth_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent_without_tools = DelegationEnvelope.create_root(
                workspace_root=raw,
                task="Parent",
                capabilities=(),
                budget=DelegationBudget(turns=4, tool_calls=0, model_chars=10_000),
            )
            with self.assertRaisesRegex(InvalidDelegationError, "subset"):
                DelegationEnvelope.create_child(
                    parent=parent_without_tools,
                    task="Attempt read privilege growth",
                    capabilities=(Capability.READ_WORKSPACE,),
                    budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
                )

            depth_zero = DelegationEnvelope.create_root(
                workspace_root=raw,
                task="No children",
                budget=DelegationBudget(turns=4, tool_calls=2, model_chars=10_000),
                limits=DelegationLimits(max_depth=0, max_total_delegations=4),
            )
            with self.assertRaisesRegex(InvalidDelegationError, "depth"):
                DelegationEnvelope.create_child(
                    parent=depth_zero,
                    task="Too deep",
                    capabilities=(),
                    budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
                )

    def test_direct_child_factory_respects_static_total_node_lower_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root_only = DelegationEnvelope.create_root(
                workspace_root=raw,
                task="Root only",
                budget=DelegationBudget(turns=4, tool_calls=2, model_chars=10_000),
                limits=DelegationLimits(max_depth=2, max_total_delegations=1),
            )
            with self.assertRaisesRegex(InvalidDelegationError, "total-node limit"):
                DelegationEnvelope.create_child(
                    parent=root_only,
                    task="Impossible child",
                    capabilities=(),
                    budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
                )

    def test_direct_child_factory_cannot_allocate_more_than_parent_total_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = DelegationEnvelope.create_root(
                workspace_root=raw,
                task="Parent",
                budget=DelegationBudget(turns=2, tool_calls=1, model_chars=2_000),
            )
            with self.assertRaisesRegex(InvalidDelegationError, "total envelope budget"):
                DelegationEnvelope.create_child(
                    parent=parent,
                    task="Oversized child",
                    capabilities=(Capability.READ_WORKSPACE,),
                    budget=DelegationBudget(turns=3, tool_calls=1, model_chars=2_000),
                )

    def test_direct_envelope_construction_cannot_leave_noncanonical_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            nested = Path(raw, "nested")
            nested.mkdir()
            root = DelegationEnvelope.create_root(
                workspace_root=raw,
                task="Canonical task",
                budget=DelegationBudget(turns=4, tool_calls=2, model_chars=8_000),
                limits=DelegationLimits(max_depth=2, max_total_delegations=4),
            )

            equivalent = replace(
                root,
                workspace_root=str(nested / ".."),
                task="  Canonical task  ",
            )
            self.assertEqual(equivalent.workspace_root, root.workspace_root)
            self.assertEqual(equivalent.task, root.task)
            self.assertEqual(equivalent.delegation_digest, root.delegation_digest)

            with self.assertRaisesRegex(InvalidDelegationError, "depth"):
                replace(root, depth=root.limits.max_depth + 1)

    def test_direct_child_constructor_rejects_impossible_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = DelegationEnvelope.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=6, tool_calls=3, model_chars=12_000),
                limits=DelegationLimits(max_depth=3, max_total_delegations=4),
            )
            child = DelegationEnvelope.create_child(
                parent=root,
                task="Child",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=3, tool_calls=2, model_chars=6_000),
            )
            with self.assertRaisesRegex(InvalidDelegationError, "own parent"):
                replace(child, parent_delegation_id=child.delegation_id)
            with self.assertRaisesRegex(InvalidDelegationError, "root id"):
                replace(child, root_delegation_id=child.delegation_id)
            with self.assertRaisesRegex(InvalidDelegationError, "skip directly to the root"):
                replace(child, depth=2)

            grandchild = DelegationEnvelope.create_child(
                parent=child,
                task="Grandchild",
                capabilities=(),
                budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
            )
            with self.assertRaisesRegex(InvalidDelegationError, "Depth-one child parent"):
                replace(grandchild, depth=1)

    def test_direct_envelope_depth_cannot_imply_more_nodes_than_root_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = DelegationEnvelope.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=6, tool_calls=3, model_chars=12_000),
                limits=DelegationLimits(max_depth=3, max_total_delegations=2),
            )
            child = DelegationEnvelope.create_child(
                parent=root,
                task="Child",
                capabilities=(),
                budget=DelegationBudget(turns=2, tool_calls=0, model_chars=2_000),
            )
            with self.assertRaisesRegex(InvalidDelegationError, "impossible"):
                replace(child, depth=2)

    def test_public_model_factories_normalize_unknown_enum_and_capability_failures(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = DelegationEnvelope.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=2, tool_calls=1, model_chars=2_000),
            )
            with self.assertRaises(InvalidDelegationError):
                EscalationRequest.create(
                    delegation=root,
                    reason="magic",
                    summary="Unsupported reason.",
                )
            with self.assertRaises(InvalidDelegationError):
                EscalationRequest.create(
                    delegation=root,
                    reason="novel",
                    summary="Unsupported capability.",
                    requested_capability="root_shell",
                )

            escalation = EscalationRequest.create(
                delegation=root,
                reason="novel",
                summary="Valid escalation.",
            )
            with self.assertRaises(InvalidDelegationError):
                OperatorContinuation.create(
                    escalation=escalation,
                    decision="maybe",
                    actor="operator",
                )

    def test_escalation_and_continuation_direct_construction_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = DelegationEnvelope.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=2, tool_calls=1, model_chars=2_000),
            )
            escalation = EscalationRequest.create(
                delegation=root,
                reason="external",
                summary="Canonical summary",
                requested_capability="git_push",
                requested_action="git.push.v1",
            )
            equivalent_escalation = replace(
                escalation,
                summary="  Canonical summary  ",
                requested_action="  git.push.v1  ",
            )
            self.assertEqual(equivalent_escalation.summary, escalation.summary)
            self.assertEqual(
                equivalent_escalation.requested_action,
                escalation.requested_action,
            )
            self.assertEqual(
                equivalent_escalation.escalation_digest,
                escalation.escalation_digest,
            )

            continuation = OperatorContinuation.create(
                escalation=escalation,
                decision="continue",
                actor="operator",
                note="bounded note",
            )
            equivalent_continuation = replace(
                continuation,
                actor="  operator  ",
                note="  bounded note  ",
            )
            self.assertEqual(equivalent_continuation.actor, "operator")
            self.assertEqual(equivalent_continuation.note, "bounded note")
            self.assertEqual(
                equivalent_continuation.continuation_digest,
                continuation.continuation_digest,
            )


if __name__ == "__main__":
    unittest.main()
