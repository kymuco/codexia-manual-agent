from __future__ import annotations

import hashlib
import unittest

from codexia_manual_agent.prompts.loader import available_prompt_versions, load_prompt


_EXPECTED_SHA256 = "9206fffdb7d03afe1ccca4e4b9163df95b9d1dfb8b8214194ddc184a7c9bdf16"


class PromptAssetTests(unittest.TestCase):
    def test_v03_is_packaged_and_hash_stable(self) -> None:
        prompt = load_prompt("v0.3")
        self.assertEqual(
            hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            _EXPECTED_SHA256,
        )
        self.assertTrue(prompt.startswith("# Codexia Manual Agent v0.3"))

    def test_registry_lists_v03(self) -> None:
        self.assertEqual(available_prompt_versions(), ("v0.3",))


if __name__ == "__main__":
    unittest.main()
