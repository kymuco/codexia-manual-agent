from __future__ import annotations

import unittest

from codexia_manual_agent.agent.protocol import (
    FinalReply,
    parse_model_reply,
    serialize_observation,
)
from codexia_manual_agent.domain.errors import ProtocolError
from codexia_manual_agent.domain.models import ToolName, ToolRequest


class AgentProtocolTests(unittest.TestCase):
    def test_parses_final(self) -> None:
        reply = parse_model_reply('{"type":"final","text":"done"}', max_chars=100)
        self.assertEqual(reply, FinalReply("done"))

    def test_parses_single_json_fence(self) -> None:
        reply = parse_model_reply(
            '```json\n{"type":"final","text":"done"}\n```',
            max_chars=100,
        )
        self.assertEqual(reply, FinalReply("done"))

    def test_parses_tool_request(self) -> None:
        reply = parse_model_reply(
            '{"type":"tool_request","request_id":"read-1","tool":"read_file",'
            '"arguments":{"path":"README.md"}}',
            max_chars=500,
        )
        self.assertEqual(
            reply,
            ToolRequest("read-1", ToolName.READ_FILE, {"path": "README.md"}),
        )

    def test_rejects_surrounding_prose(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_model_reply(
                'Here is JSON: {"type":"final","text":"done"}',
                max_chars=200,
            )

    def test_rejects_extra_keys(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_model_reply(
                '{"type":"final","text":"done","claim":"extra"}',
                max_chars=200,
            )

    def test_rejects_unsupported_tool(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_model_reply(
                '{"type":"tool_request","request_id":"x","tool":"shell",'
                '"arguments":{}}',
                max_chars=200,
            )

    def test_rejects_invalid_request_id(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_model_reply(
                '{"type":"tool_request","request_id":"bad id","tool":"git_status",'
                '"arguments":{}}',
                max_chars=200,
            )

    def test_rejects_response_limit(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_model_reply('{"type":"final","text":"done"}', max_chars=5)

    def test_observation_serialization_is_bounded(self) -> None:
        with self.assertRaises(ProtocolError):
            serialize_observation({"text": "x" * 100}, max_chars=20)


if __name__ == "__main__":
    unittest.main()
