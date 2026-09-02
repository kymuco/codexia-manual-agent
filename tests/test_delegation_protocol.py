from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace

from codexia_manual_agent.delegation import (
    DelegationBudget,
    DelegationCoordinator,
    DelegationState,
    DelegateWorkRequest,
    EscalateWorkRequest,
    EscalationReason,
    parse_delegation_control_request,
)
from codexia_manual_agent.delegation.bridge import apply_delegation_control_request
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import ProtocolError


class DelegationProtocolTests(unittest.TestCase):
    def test_delegate_request_parses_only_read_only_intent_and_is_digest_bound(self) -> None:
        request = parse_delegation_control_request(
            json.dumps(
                {
                    "type": "delegate_request",
                    "request_id": "delegate-1",
                    "task": "Inspect tests for the failing boundary.",
                    "capabilities": ["read_workspace"],
                    "budget": {"turns": 3, "tool_calls": 2, "model_chars": 12000},
                }
            )
        )
        self.assertIsInstance(request, DelegateWorkRequest)
        assert isinstance(request, DelegateWorkRequest)
        self.assertEqual(request.capabilities, (Capability.READ_WORKSPACE,))
        self.assertEqual(
            request.budget,
            DelegationBudget(turns=3, tool_calls=2, model_chars=12_000),
        )
        self.assertEqual(len(request.request_digest), 64)
        with self.assertRaisesRegex(ProtocolError, "digest does not match"):
            replace(request, task="Different task")

    def test_direct_request_factories_share_parser_normalization_and_fail_closed(self) -> None:
        delegated = DelegateWorkRequest.create(
            request_id="direct-delegate",
            task="  Inspect code.  ",
            capabilities=("read_workspace",),
            budget=DelegationBudget(turns=1, tool_calls=1, model_chars=1_000),
        )
        self.assertEqual(delegated.task, "Inspect code.")
        self.assertEqual(delegated.capabilities, (Capability.READ_WORKSPACE,))
        self.assertEqual(len(delegated.request_digest), 64)

        with self.assertRaisesRegex(ProtocolError, "use escalation_request"):
            DelegateWorkRequest.create(
                request_id="direct-mutation",
                task="Push.",
                capabilities=("git_push",),
                budget=DelegationBudget(turns=1, tool_calls=0, model_chars=1_000),
            )

        escalated = EscalateWorkRequest.create(
            request_id="direct-escalation",
            reason="external",
            requested_capability="git_push",
            requested_action="  git.push.v1  ",
            summary="  Push is required.  ",
        )
        self.assertIs(escalated.reason, EscalationReason.EXTERNAL)
        self.assertIs(escalated.requested_capability, Capability.GIT_PUSH)
        self.assertEqual(escalated.requested_action, "git.push.v1")
        self.assertEqual(escalated.summary, "Push is required.")

        with self.assertRaisesRegex(ProtocolError, "reason is unsupported"):
            EscalateWorkRequest.create(
                request_id="direct-bad-reason",
                reason="magic",
                requested_capability=None,
                requested_action=None,
                summary="Unknown decision class.",
            )

    def test_mutation_capability_must_be_escalated_not_delegated(self) -> None:
        payload = {
            "type": "delegate_request",
            "request_id": "delegate-2",
            "task": "Push changes.",
            "capabilities": ["git_push"],
            "budget": {"turns": 1, "tool_calls": 0, "model_chars": 1000},
        }
        with self.assertRaisesRegex(ProtocolError, "use escalation_request"):
            parse_delegation_control_request(json.dumps(payload))

    def test_model_cannot_supply_local_lineage_or_authority_fields(self) -> None:
        base = {
            "type": "delegate_request",
            "request_id": "delegate-3",
            "task": "Inspect code.",
            "capabilities": ["read_workspace"],
            "budget": {"turns": 1, "tool_calls": 1, "model_chars": 1000},
        }
        for forbidden, value in (
            ("workspace_root", "C:/attacker"),
            ("root_delegation_id", "00000000-0000-0000-0000-000000000000"),
            ("parent_delegation_id", "00000000-0000-0000-0000-000000000000"),
            ("proposal_id", "00000000-0000-0000-0000-000000000000"),
            ("receipt_id", "00000000-0000-0000-0000-000000000000"),
            ("approved", True),
        ):
            with self.subTest(forbidden=forbidden):
                payload = {**base, forbidden: value}
                with self.assertRaisesRegex(ProtocolError, "keys mismatch"):
                    parse_delegation_control_request(json.dumps(payload))

    def test_escalation_request_may_name_git_push_as_digest_bound_intent_only(self) -> None:
        request = parse_delegation_control_request(
            json.dumps(
                {
                    "type": "escalation_request",
                    "request_id": "escalate-1",
                    "reason": "external",
                    "requested_capability": "git_push",
                    "requested_action": "git.push.v1",
                    "summary": "A remote push is required to continue.",
                }
            )
        )
        self.assertIsInstance(request, EscalateWorkRequest)
        assert isinstance(request, EscalateWorkRequest)
        self.assertIs(request.reason, EscalationReason.EXTERNAL)
        self.assertIs(request.requested_capability, Capability.GIT_PUSH)
        self.assertEqual(len(request.request_digest), 64)
        with self.assertRaisesRegex(ProtocolError, "digest does not match"):
            replace(request, summary="Different escalation")

    def test_bridge_derives_parent_and_workspace_locally(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            coordinator = DelegationCoordinator()
            root = coordinator.create_root(
                workspace_root=raw,
                task="Root",
                budget=DelegationBudget(turns=5, tool_calls=3, model_chars=20_000),
            )
            request = parse_delegation_control_request(
                json.dumps(
                    {
                        "type": "delegate_request",
                        "request_id": "delegate-4",
                        "task": "Inspect a narrower question.",
                        "capabilities": ["read_workspace"],
                        "budget": {"turns": 2, "tool_calls": 1, "model_chars": 5000},
                    }
                )
            )
            child = apply_delegation_control_request(
                coordinator,
                current_delegation_id=root.delegation_id,
                request=request,
            )
            self.assertEqual(child.parent_delegation_id, root.delegation_id)
            self.assertEqual(child.parent_delegation_digest, root.delegation_digest)
            self.assertEqual(child.workspace_root, root.workspace_root)

            escalation_request = parse_delegation_control_request(
                json.dumps(
                    {
                        "type": "escalation_request",
                        "request_id": "escalate-2",
                        "reason": "policy_sensitive",
                        "requested_capability": "write_workspace",
                        "requested_action": "workspace.replace_file.v1",
                        "summary": "A write would be needed.",
                    }
                )
            )
            escalation = apply_delegation_control_request(
                coordinator,
                current_delegation_id=child.delegation_id,
                request=escalation_request,
            )
            snapshot = coordinator.snapshot(child.delegation_id)
            self.assertIs(snapshot.state, DelegationState.WAITING_HUMAN)
            self.assertEqual(escalation.delegation_id, child.delegation_id)
            self.assertEqual(escalation.delegation_digest, child.delegation_digest)
            self.assertIs(escalation.requested_capability, Capability.WRITE_WORKSPACE)
            self.assertEqual(child.capabilities, (Capability.READ_WORKSPACE,))

    def test_invalid_budget_reason_capability_and_boolean_integer_fail_closed(self) -> None:
        bad_payloads = (
            {
                "type": "delegate_request",
                "request_id": "bad-1",
                "task": "Task",
                "capabilities": ["read_workspace"],
                "budget": {"turns": True, "tool_calls": 0, "model_chars": 1000},
            },
            {
                "type": "delegate_request",
                "request_id": "bad-2",
                "task": "Task",
                "capabilities": ["read_workspace"],
                "budget": {"turns": 0, "tool_calls": 0, "model_chars": 1000},
            },
            {
                "type": "escalation_request",
                "request_id": "bad-3",
                "reason": "magic",
                "requested_capability": None,
                "requested_action": None,
                "summary": "Task",
            },
            {
                "type": "escalation_request",
                "request_id": "bad-4",
                "reason": "novel",
                "requested_capability": "root_shell",
                "requested_action": None,
                "summary": "Task",
            },
        )
        for payload in bad_payloads:
            with self.subTest(request_id=payload["request_id"]):
                with self.assertRaises(ProtocolError):
                    parse_delegation_control_request(json.dumps(payload))

    def test_duplicate_json_keys_and_non_finite_constants_fail_closed(self) -> None:
        duplicate = (
            '{"type":"delegate_request","request_id":"a","request_id":"b",'
            '"task":"Read","capabilities":[],"budget":'
            '{"turns":1,"tool_calls":0,"model_chars":1000}}'
        )
        with self.assertRaisesRegex(ProtocolError, "duplicate JSON key"):
            parse_delegation_control_request(duplicate)

        non_finite = (
            '{"type":"delegate_request","request_id":"nan","task":"Read",'
            '"capabilities":[],"budget":{"turns":1,"tool_calls":0,'
            '"model_chars":NaN}}'
        )
        with self.assertRaisesRegex(ProtocolError, "invalid JSON constant"):
            parse_delegation_control_request(non_finite)

    def test_json_fence_and_parser_budget_are_strict(self) -> None:
        payload = {
            "type": "delegate_request",
            "request_id": "fenced",
            "task": "Read.",
            "capabilities": [],
            "budget": {"turns": 1, "tool_calls": 0, "model_chars": 1000},
        }
        request = parse_delegation_control_request(
            "```json\n" + json.dumps(payload) + "\n```"
        )
        self.assertIsInstance(request, DelegateWorkRequest)
        with self.assertRaises(ProtocolError):
            parse_delegation_control_request("```python\n{}\n```")
        with self.assertRaises(ProtocolError):
            parse_delegation_control_request("x" * 100, max_chars=10)
        with self.assertRaises(ValueError):
            parse_delegation_control_request("{}", max_chars=0)


if __name__ == "__main__":
    unittest.main()
