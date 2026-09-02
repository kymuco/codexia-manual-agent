from __future__ import annotations

import unittest

from codexia_manual_agent.domain.errors import InvalidGitMutationError
from codexia_manual_agent.git_mutation.commit import _parse_index_pack_oid


class GitCommitIndexPackOutputTests(unittest.TestCase):
    def test_index_pack_oid_accepts_git_whitespace_forms(self) -> None:
        oid = "a" * 40
        for payload in (
            f"pack\t{oid}\n".encode("ascii"),
            f"pack {oid}\n".encode("ascii"),
            f"{oid}\n".encode("ascii"),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(_parse_index_pack_oid(payload, 40), oid)

    def test_index_pack_oid_rejects_ambiguous_output(self) -> None:
        oid = "b" * 40
        with self.assertRaises(InvalidGitMutationError):
            _parse_index_pack_oid(f"pack\t{oid}\textra\n".encode("ascii"), 40)


if __name__ == "__main__":
    unittest.main()
