from __future__ import annotations

import json
import unittest

from codexia_manual_agent.admission import CommandFamily
from codexia_manual_agent.agent.action_protocol import parse_model_process_request
from codexia_manual_agent.agent.protocol import parse_model_reply
from codexia_manual_agent.domain.errors import ProtocolError
from codexia_manual_agent.domain.models import ToolName


class ProcessRequestProtocolTests(unittest.TestCase):
    def test_parses_known_family_without_raw_command_material(self) -> None:
        request = parse_model_process_request(
            json.dumps(
                {
                    "type": "process_request",
                    "request_id": "proc-1",
                    "family": "python_version",
                    "arguments": {},
                }
            )
        )
        self.assertEqual(request.request_id, "proc-1")
        self.assertIs(request.family, CommandFamily.PYTHON_VERSION)
        self.assertEqual(dict(request.arguments), {})

    def test_rejects_unknown_family(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_model_process_request(
                json.dumps(
                    {
                        "type": "process_request",
                        "request_id": "proc-2",
                        "family": "shell",
                        "arguments": {},
                    }
                )
            )

    def test_rejects_model_authority_and_raw_argv_fields(self) -> None:
        forbidden_fields = {
            "argv": ["python", "-c", "print('x')"],
            "executable": "python",
            "risk": "diagnostic",
            "capabilities": ["execute_process"],
            "approved": True,
            "approval_mode": "risky",
            "proposal_digest": "0" * 64,
            "receipt_id": "00000000-0000-0000-0000-000000000001",
        }
        for field, value in forbidden_fields.items():
            with self.subTest(field=field):
                payload = {
                    "type": "process_request",
                    "request_id": "proc-extra",
                    "family": "python_version",
                    "arguments": {},
                    field: value,
                }
                with self.assertRaises(ProtocolError):
                    parse_model_process_request(json.dumps(payload))

    def test_arguments_must_be_object(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_model_process_request(
                json.dumps(
                    {
                        "type": "process_request",
                        "request_id": "proc-3",
                        "family": "python_version",
                        "arguments": [],
                    }
                )
            )

    def test_existing_agent_protocol_still_rejects_process_request(self) -> None:
        payload = json.dumps(
            {
                "type": "process_request",
                "request_id": "proc-disabled",
                "family": "python_version",
                "arguments": {},
            }
        )
        with self.assertRaises(ProtocolError):
            parse_model_reply(payload, max_chars=32_768)

    def test_existing_tool_enum_remains_read_only(self) -> None:
        self.assertEqual(
            {item.value for item in ToolName},
            {"read_file", "list_files", "search_text", "git_status"},
        )


if __name__ == "__main__":
    unittest.main()
