from __future__ import annotations

import unittest

from codexia_manual_agent.agent.patch_protocol import ModelPatchChangeRequest
from codexia_manual_agent.domain.errors import ProtocolError


class ModelPatchProtocolUnicodeTests(unittest.TestCase):
    def test_lone_surrogate_is_rejected_as_protocol_error(self) -> None:
        for field, kwargs in (
            (
                "target",
                {"operation": "create", "target": "bad\ud800.txt", "content": "x"},
            ),
            (
                "content",
                {"operation": "create", "target": "a.txt", "content": "bad\ud800"},
            ),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ProtocolError, "valid UTF-8"):
                    ModelPatchChangeRequest(**kwargs)
