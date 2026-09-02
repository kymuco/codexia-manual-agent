from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchChangeSet,
    PatchFileChange,
    PatchFileRequest,
    PreimageSnapshot,
    build_patch_approval_preview,
    parse_patch_proposal,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation import (
    patch_final_review_repairs,
    patch_windows_namespace_guard,
)
from codexia_manual_agent.mutation.patches import MAX_PATCH_FILES, MAX_PATCH_FILE_BYTES


class PatchProposalFinalReviewRepairTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires Windows pinned directory handles")
    def test_windows_parent_replacement_fails_before_exact_preimage_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            parent = root / "nested"
            parent.mkdir()
            target = parent / "old.txt"
            target.write_bytes(b"old\n")
            moved = root / "moved"
            original_capture = patch_final_review_repairs._capture_windows_exact_path
            attempted = False

            def capture_with_race(path: Path, *, max_bytes: int):
                nonlocal attempted
                attempted = True
                try:
                    parent.rename(moved)
                except OSError:
                    self.skipTest(
                        "host blocks directory rename while the pinned handle is held"
                    )
                parent.mkdir()
                (parent / "old.txt").write_bytes(b"replacement\n")
                return original_capture(path, max_bytes=max_bytes)

            with (
                mock.patch.object(
                    patch_final_review_repairs,
                    "_capture_windows_exact_path",
                    side_effect=capture_with_race,
                ),
                mock.patch.object(
                    patch_windows_namespace_guard,
                    "_base_read_fd_payload",
                    side_effect=AssertionError(
                        "replacement target bytes must not be read before parent revalidation"
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "identity changed",
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

            self.assertTrue(attempted)
            self.assertTrue(moved.is_dir())
            self.assertEqual((moved / "old.txt").read_bytes(), b"old\n")
            self.assertEqual((parent / "old.txt").read_bytes(), b"replacement\n")

    @unittest.skipUnless(os.name == "nt", "requires Windows reparse metadata")
    def test_windows_exact_preimage_rejects_reparse_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw).resolve() / "target.txt"
            target.write_bytes(b"old\n")
            info = patch_final_review_repairs._WinFileInfo()
            info.dwFileAttributes = 0x00000400  # FILE_ATTRIBUTE_REPARSE_POINT
            with mock.patch.object(
                patch_final_review_repairs,
                "_win_file_info",
                return_value=info,
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "reparse point",
                ):
                    patch_final_review_repairs._capture_windows_exact_path(
                        target,
                        max_bytes=MAX_PATCH_FILE_BYTES,
                    )

    @unittest.skipIf(os.name == "nt", "POSIX openat/O_NOFOLLOW regression")
    def test_posix_parent_rename_to_symlink_fails_closed_during_capture(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            root = base / "workspace"
            root.mkdir()
            parent = root / "nested"
            parent.mkdir()
            (parent / "old.txt").write_bytes(b"inside\n")
            outside = base / "outside"
            outside.mkdir()
            (outside / "old.txt").write_bytes(b"secret\n")
            moved = root / "detached"
            original_read = patch_final_review_repairs._read_fd_payload
            raced = False

            def read_with_race(fd: int, *, max_bytes: int):
                nonlocal raced
                if not raced:
                    raced = True
                    parent.rename(moved)
                    os.symlink(outside, parent, target_is_directory=True)
                return original_read(fd, max_bytes=max_bytes)

            with mock.patch.object(
                patch_final_review_repairs,
                "_read_fd_payload",
                side_effect=read_with_race,
            ):
                with self.assertRaises(WorkspaceMutationPreimageChangedError):
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

            self.assertTrue(raced)
            self.assertEqual((outside / "old.txt").read_bytes(), b"secret\n")

    def test_bound_preview_survives_target_parent_rename_without_live_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            parent = root / "nested"
            parent.mkdir()
            (parent / "old.txt").write_bytes(b"old\n")
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(
                        MutationOperation.REPLACE,
                        "nested/old.txt",
                        b"new\n",
                    ),
                ),
            )
            parent.rename(root / "moved")

            with (
                mock.patch.object(
                    patch_final_review_repairs._legacy,
                    "_workspace_root",
                    side_effect=AssertionError("parse must not resolve the live workspace"),
                ),
                mock.patch.object(
                    patch_final_review_repairs._legacy,
                    "_normalize_target",
                    side_effect=AssertionError("parse must not inspect live target parents"),
                ),
            ):
                parsed = parse_patch_proposal(proposal)
                preview = build_patch_approval_preview(proposal)

            self.assertEqual(parsed.changes[0].target, "nested/old.txt")
            self.assertEqual(parsed.changes[0].preimage, b"old\n")
            self.assertIn("-old", preview.changes[0].unified_diff)
            self.assertIn("+new", preview.changes[0].unified_diff)

    def test_direct_change_set_constructor_bounds_iterable_before_tuple_materialization(self) -> None:
        change = PatchFileChange.create(
            operation=MutationOperation.CREATE,
            target="file.txt",
            expected_preimage=PreimageSnapshot.absent(),
            preimage=None,
            postimage=b"",
        )

        class GuardedChanges:
            def __init__(self) -> None:
                self.yielded = 0

            def __iter__(self):
                for _ in range(MAX_PATCH_FILES + 1):
                    self.yielded += 1
                    yield change
                raise AssertionError("constructor consumed beyond MAX_PATCH_FILES + 1")

        with tempfile.TemporaryDirectory() as raw:
            guarded = GuardedChanges()
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "1..",
            ):
                PatchChangeSet(
                    workspace_root=str(Path(raw).resolve()),
                    changes=guarded,  # type: ignore[arg-type]
                    change_set_digest="0" * 64,
                )
            self.assertEqual(guarded.yielded, MAX_PATCH_FILES + 1)

    def test_direct_change_set_factory_bounds_iterable_before_tuple_materialization(self) -> None:
        change = PatchFileChange.create(
            operation=MutationOperation.CREATE,
            target="file.txt",
            expected_preimage=PreimageSnapshot.absent(),
            preimage=None,
            postimage=b"",
        )

        class GuardedChanges:
            def __init__(self) -> None:
                self.yielded = 0

            def __iter__(self):
                for _ in range(MAX_PATCH_FILES + 1):
                    self.yielded += 1
                    yield change
                raise AssertionError("factory consumed beyond MAX_PATCH_FILES + 1")

        with tempfile.TemporaryDirectory() as raw:
            guarded = GuardedChanges()
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "1..",
            ):
                PatchChangeSet.create(
                    workspace_root=str(Path(raw).resolve()),
                    changes=guarded,
                )
            self.assertEqual(guarded.yielded, MAX_PATCH_FILES + 1)


if __name__ == "__main__":
    unittest.main()
