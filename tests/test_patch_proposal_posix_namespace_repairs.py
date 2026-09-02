from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codexia_manual_agent.domain.errors import InvalidWorkspaceMutationError
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchFileRequest,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation import patch_posix_namespace_repairs
from codexia_manual_agent.mutation import patch_posix_proposal_stability
from codexia_manual_agent.mutation import patch_posix_root_anchor


class _CountingEntries:
    def __init__(self, total: int) -> None:
        self.total = total
        self.count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self.count >= self.total:
            raise StopIteration
        self.count += 1
        return SimpleNamespace(name=f"{self.count:06d}")


class PatchProposalPosixNamespaceRepairTests(unittest.TestCase):
    def test_namespace_key_uses_parent_identity_and_leaf_only(self) -> None:
        namespace = patch_posix_namespace_repairs._PinnedParentNamespace(
            identity=(17, 23),
            case_sensitive=True,
        )

        upper_ancestor = patch_posix_namespace_repairs._namespace_key(
            "Dir/File.txt",
            case_sensitive=namespace,
        )
        lower_ancestor = patch_posix_namespace_repairs._namespace_key(
            "dir/File.txt",
            case_sensitive=namespace,
        )
        different_leaf = patch_posix_namespace_repairs._namespace_key(
            "Dir/file.txt",
            case_sensitive=namespace,
        )
        other_parent = patch_posix_namespace_repairs._namespace_key(
            "Dir/File.txt",
            case_sensitive=patch_posix_namespace_repairs._PinnedParentNamespace(
                identity=(17, 24),
                case_sensitive=True,
            ),
        )

        self.assertEqual(upper_ancestor, lower_ancestor)
        self.assertEqual(upper_ancestor, ((17, 23), "File.txt"))
        self.assertNotEqual(upper_ancestor, different_leaf)
        self.assertNotEqual(upper_ancestor, other_parent)

        insensitive = patch_posix_namespace_repairs._PinnedParentNamespace(
            identity=(17, 23),
            case_sensitive=False,
        )
        self.assertEqual(
            patch_posix_namespace_repairs._namespace_key(
                "Dir/File.txt",
                case_sensitive=insensitive,
            ),
            patch_posix_namespace_repairs._namespace_key(
                "Dir/file.txt",
                case_sensitive=insensitive,
            ),
        )

    def test_case_probe_consumes_only_bounded_directory_entries(self) -> None:
        entries = _CountingEntries(total=10_000)
        fake_stat = SimpleNamespace(st_dev=31, st_ino=37)

        with (
            mock.patch.object(
                patch_posix_namespace_repairs.os,
                "fstat",
                return_value=fake_stat,
            ),
            mock.patch.object(
                patch_posix_namespace_repairs.os,
                "scandir",
                return_value=entries,
            ),
            mock.patch.object(
                patch_posix_namespace_repairs._review,
                "_case_variant",
                return_value=None,
            ),
        ):
            result = patch_posix_namespace_repairs._probe_parent_case_sensitivity(123)

        self.assertEqual(entries.count, patch_posix_root_anchor._CASE_PROBE_SCAN_LIMIT)
        self.assertEqual(result.identity, (31, 37))
        self.assertIsNone(result.case_sensitive)

    @unittest.skipIf(os.name == "nt", "POSIX parent-inode alias regression")
    def test_alias_parent_spellings_for_same_pinned_inode_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            real_parent = root / "Dir"
            real_parent.mkdir()

            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
            original_open_parent = (
                patch_posix_proposal_stability._base_open_parent_from_root_fd
            )

            def open_alias_parent(root_fd: int, parent_parts: tuple[str, ...]) -> int:
                if parent_parts in {("Dir",), ("dir",)}:
                    return os.open(real_parent, flags)
                return original_open_parent(root_fd, parent_parts)

            with mock.patch.object(
                patch_posix_proposal_stability,
                "_base_open_parent_from_root_fd",
                side_effect=open_alias_parent,
            ):
                with self.assertRaisesRegex(
                    InvalidWorkspaceMutationError,
                    "duplicate target",
                ):
                    prepare_patch_proposal(
                        workspace=root,
                        changes=(
                            PatchFileRequest(
                                MutationOperation.CREATE,
                                "Dir/new.txt",
                                b"one\n",
                            ),
                            PatchFileRequest(
                                MutationOperation.CREATE,
                                "dir/new.txt",
                                b"two\n",
                            ),
                        ),
                    )

    def test_namespace_helpers_are_sealed_on_root_anchor(self) -> None:
        self.assertIs(
            patch_posix_root_anchor._probe_parent_case_sensitivity,
            patch_posix_proposal_stability._probe_parent_case_sensitivity,
        )
        self.assertIs(
            patch_posix_proposal_stability._base_probe_parent_case_sensitivity,
            patch_posix_namespace_repairs._probe_parent_case_sensitivity,
        )
        self.assertIs(
            patch_posix_root_anchor._namespace_key,
            patch_posix_namespace_repairs._namespace_key,
        )
        self.assertIs(
            prepare_patch_proposal,
            patch_posix_namespace_repairs.prepare_patch_proposal,
        )


if __name__ == "__main__":
    unittest.main()
