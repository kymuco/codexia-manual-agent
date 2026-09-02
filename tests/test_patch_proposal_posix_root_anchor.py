from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.domain.errors import WorkspaceMutationPreimageChangedError
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchFileRequest,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation import patch_final_review_repairs
from codexia_manual_agent.mutation import patch_hardening
from codexia_manual_agent.mutation import patch_portability_repairs
from codexia_manual_agent.mutation import patch_posix_root_anchor
from codexia_manual_agent.mutation import patch_review_repairs
from codexia_manual_agent.mutation import patches


class PatchProposalPosixRootAnchorTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX root-fd anchor regression")
    def test_workspace_swap_before_parent_open_reads_only_pinned_root_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            root = base / "workspace"
            root.mkdir()
            parent = root / "nested"
            parent.mkdir()
            (parent / "old.txt").write_bytes(b"inside\n")

            moved = base / "original-workspace"
            captured_payloads: list[bytes | None] = []
            swapped = False

            original_open_parent = patch_posix_root_anchor._open_parent_from_root_fd
            original_capture = patch_posix_root_anchor._capture_preimage_from_parent_fd

            def open_parent_with_workspace_swap(root_fd: int, parent_parts: tuple[str, ...]) -> int:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    root.rename(moved)
                    root.mkdir()
                    replacement_parent = root / "nested"
                    replacement_parent.mkdir()
                    (replacement_parent / "old.txt").write_bytes(b"secret\n")
                return original_open_parent(root_fd, parent_parts)

            def capture_and_record(parent_fd: int, *, target_name: str, max_bytes: int):
                result = original_capture(
                    parent_fd,
                    target_name=target_name,
                    max_bytes=max_bytes,
                )
                captured_payloads.append(result[1])
                return result

            with (
                mock.patch.object(
                    patch_posix_root_anchor,
                    "_open_parent_from_root_fd",
                    side_effect=open_parent_with_workspace_swap,
                ),
                mock.patch.object(
                    patch_posix_root_anchor,
                    "_capture_preimage_from_parent_fd",
                    side_effect=capture_and_record,
                ),
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationPreimageChangedError,
                    "workspace identity changed",
                ):
                    prepare_patch_proposal(
                        workspace=root,
                        changes=(
                            PatchFileRequest(
                                MutationOperation.REPLACE,
                                "nested/old.txt",
                                b"new\n",
                            ),
                        ),
                    )

            self.assertTrue(swapped)
            self.assertEqual(captured_payloads, [b"inside\n"])
            self.assertEqual((moved / "nested" / "old.txt").read_bytes(), b"inside\n")
            self.assertEqual((root / "nested" / "old.txt").read_bytes(), b"secret\n")

    def test_prepare_entrypoints_are_sealed_to_root_anchor_wrapper(self) -> None:
        self.assertIs(prepare_patch_proposal, patch_posix_root_anchor.prepare_patch_proposal)
        self.assertIs(
            patch_portability_repairs.prepare_patch_proposal,
            patch_posix_root_anchor.prepare_patch_proposal,
        )
        self.assertIs(
            patch_final_review_repairs.prepare_patch_proposal,
            patch_posix_root_anchor.prepare_patch_proposal,
        )
        self.assertIs(
            patch_review_repairs.prepare_patch_proposal,
            patch_posix_root_anchor.prepare_patch_proposal,
        )
        self.assertIs(
            patch_hardening.prepare_patch_proposal,
            patch_posix_root_anchor.prepare_patch_proposal,
        )
        self.assertIs(
            patches.prepare_patch_proposal,
            patch_posix_root_anchor.prepare_patch_proposal,
        )


if __name__ == "__main__":
    unittest.main()
