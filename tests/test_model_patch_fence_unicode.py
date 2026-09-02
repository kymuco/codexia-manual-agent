from __future__ import annotations

import json
import unittest

from codexia_manual_agent.agent.patch_protocol import parse_model_patch_request


class ModelPatchFenceUnicodeTests(unittest.TestCase):
    def test_fenced_request_preserves_unicode_line_and_paragraph_separators(self) -> None:
        content = "alpha\u2028beta\u2029gamma"
        payload = {
            "type": "patch_request",
            "request_id": "unicode-fence",
            "changes": [
                {
                    "operation": "create",
                    "target": "unicode.txt",
                    "content": content,
                }
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        request = parse_model_patch_request(
            "```json\r\n" + encoded + "\r\n```"
        )

        self.assertEqual(request.changes[0].content, content)
        self.assertEqual(request.changes[0].content_bytes, content.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
