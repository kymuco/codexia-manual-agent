from __future__ import annotations

import json
import unittest

from codexia_manual_agent.agent.protocol import parse_model_reply
from codexia_manual_agent.domain.errors import ProtocolError


class DelegationAgentBoundaryTests(unittest.TestCase):
    def test_m1_1_agent_protocol_does_not_implicitly_gain_delegation_requests(self) -> None:
        payload = {
            "type": "delegate_request",
            "request_id": "delegate-1",
            "task": "Inspect another module.",
            "capabilities": ["read_workspace"],
            "budget": {"turns": 2, "tool_calls": 1, "model_chars": 5000},
        }
        with self.assertRaises(ProtocolError):
            parse_model_reply(json.dumps(payload), max_chars=32_768)

    def test_m1_1_agent_protocol_does_not_implicitly_gain_human_escalation_requests(self) -> None:
        payload = {
            "type": "escalation_request",
            "request_id": "escalate-1",
            "reason": "external",
            "requested_capability": "git_push",
            "requested_action": "git.push.v1",
            "summary": "Push required.",
        }
        with self.assertRaises(ProtocolError):
            parse_model_reply(json.dumps(payload), max_chars=32_768)


if __name__ == "__main__":
    unittest.main()
