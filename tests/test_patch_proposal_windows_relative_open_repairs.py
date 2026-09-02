from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.mutation import PreimageSnapshot, PreimageState
from codexia_manual_agent.mutation import patch_final_review_repairs
from codexia_manual_agent.mutation import patch_windows_namespace_guard
from codexia_manual_agent.mutation.patches import MAX_PATCH_FILE_BYTES


class PatchProposalWindowsRelativeOpenRepairTests(unittest.TestCase):
    def test_authority_capture_is_sealed_to_relative_open_wrapper(self) -> None:
        self.assertIs(
            patch_final_review_repairs._capture_windows_exact_path,
            patch_windows_namespace_guard._capture_windows_exact_path,
        )
        self.assertIs(
            patch_final_review_repairs.PinnedMutationTarget,
            patch_windows_namespace_guard._VerifiedPinnedMutationTarget,
        )

    def test_pinned_capture_routes_leaf_through_parent_handle_not_base_path_open(self) -> None:
        parent = Path("/workspace/nested")
        target = parent / "old.txt"
        verifier = mock.Mock()
        target_token = patch_windows_namespace_guard._WINDOWS_PINNED_TARGET.set(
            (31337, parent, "old.txt")
        )
        verify_token = patch_windows_namespace_guard._WINDOWS_PARENT_VERIFY.set(verifier)
        expected = (
            PreimageSnapshot.present(
                size_bytes=4,
                digest="0" * 64,
                mode=0o644,
            ),
            b"old\n",
        )
        try:
            with (
                mock.patch.object(
                    patch_windows_namespace_guard.os,
                    "name",
                    "nt",
                ),
                mock.patch.object(
                    patch_windows_namespace_guard,
                    "_nt_open_relative_target",
                    return_value=909,
                ) as relative_open,
                mock.patch.object(
                    patch_windows_namespace_guard,
                    "_capture_windows_exact_handle",
                    return_value=expected,
                ) as capture_handle,
                mock.patch.object(
                    patch_windows_namespace_guard,
                    "_base_capture_windows_exact_path",
                    side_effect=AssertionError(
                        "authority-bearing capture must not reopen the mutable path"
                    ),
                ),
            ):
                observed = patch_windows_namespace_guard._capture_windows_exact_path(
                    target,
                    max_bytes=MAX_PATCH_FILE_BYTES,
                )
        finally:
            patch_windows_namespace_guard._WINDOWS_PARENT_VERIFY.reset(verify_token)
            patch_windows_namespace_guard._WINDOWS_PINNED_TARGET.reset(target_token)

        self.assertEqual(observed, expected)
        relative_open.assert_called_once_with(31337, "old.txt")
        capture_handle.assert_called_once_with(909, max_bytes=MAX_PATCH_FILE_BYTES)
        verifier.assert_called_once_with()

    @unittest.skipUnless(os.name == "nt", "requires Windows NtOpenFile RootDirectory")
    def test_nt_relative_open_reads_existing_leaf_from_held_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            parent = root / "nested"
            parent.mkdir()
            target = parent / "old.txt"
            target.write_bytes(b"old\n")

            with patch_final_review_repairs.PinnedMutationTarget(
                root=root,
                parent=parent,
                target_name="old.txt",
            ) as pinned:
                handle = patch_windows_namespace_guard._nt_open_relative_target(
                    pinned._windows_handles[-1],
                    "old.txt",
                )
                self.assertIsNotNone(handle)
                snapshot, payload = patch_windows_namespace_guard._capture_windows_exact_handle(
                    int(handle),
                    max_bytes=MAX_PATCH_FILE_BYTES,
                )

            self.assertIs(snapshot.state, PreimageState.PRESENT)
            self.assertEqual(snapshot.size_bytes, 4)
            self.assertEqual(payload, b"old\n")

    @unittest.skipUnless(os.name == "nt", "requires Windows NtOpenFile RootDirectory")
    def test_nt_relative_open_reports_missing_leaf_without_path_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            parent = root / "nested"
            parent.mkdir()

            with patch_final_review_repairs.PinnedMutationTarget(
                root=root,
                parent=parent,
                target_name="missing.txt",
            ) as pinned:
                handle = patch_windows_namespace_guard._nt_open_relative_target(
                    pinned._windows_handles[-1],
                    "missing.txt",
                )

            self.assertIsNone(handle)


if __name__ == "__main__":
    unittest.main()
