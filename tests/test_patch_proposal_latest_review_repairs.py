from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
)
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchFileRequest,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation import patch_case_seam_repairs
from codexia_manual_agent.mutation import patch_final_review_repairs
from codexia_manual_agent.mutation import patch_hardening
from codexia_manual_agent.mutation import patch_latest_review_repairs
from codexia_manual_agent.mutation import patch_namespace_stability_repairs
from codexia_manual_agent.mutation import patch_portability_repairs
from codexia_manual_agent.mutation import patch_posix_namespace_repairs
from codexia_manual_agent.mutation import patch_posix_root_anchor
from codexia_manual_agent.mutation import patch_review_repairs
from codexia_manual_agent.mutation import patches


class PatchProposalLatestReviewRepairTests(unittest.TestCase):
    def test_direct_namespace_identity_uses_parent_inode_plus_leaf(self) -> None:
        namespace = patch_latest_review_repairs._DirectParentNamespace(
            identity=(17, 23),
            case_sensitive=True,
        )

        def normalized(root: Path, target: str):
            rendered = Path(target).as_posix()
            return rendered, Path("/fake") / Path(target), Path("/fake") / Path(target).parent

        with (
            mock.patch.object(
                patch_namespace_stability_repairs,
                "_inspect_direct_parent_namespace",
                return_value=namespace,
            ),
            mock.patch.object(
                patch_review_repairs._legacy,
                "_normalize_target",
                side_effect=normalized,
            ),
        ):
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "unique namespace targets",
            ):
                patch_review_repairs._assert_unique_namespace_targets(
                    Path("/fake"),
                    ("Dir/File.txt", "dir/File.txt"),
                    label="Patch changes must have unique namespace targets",
                )

    def test_direct_namespace_key_preserves_distinct_parent_inodes(self) -> None:
        with mock.patch.object(
            patch_namespace_stability_repairs,
            "_inspect_direct_parent_namespace",
            side_effect=(
                patch_latest_review_repairs._DirectParentNamespace(
                    identity=(31, 41),
                    case_sensitive=True,
                ),
                patch_latest_review_repairs._DirectParentNamespace(
                    identity=(31, 43),
                    case_sensitive=True,
                ),
            ),
        ):
            cache: dict[str, object] = {}
            first = patch_latest_review_repairs._direct_target_namespace_key(
                Path("/one/File.txt"),
                sensitivity_cache=cache,
            )
            second = patch_latest_review_repairs._direct_target_namespace_key(
                Path("/two/File.txt"),
                sensitivity_cache=cache,
            )

        self.assertNotEqual(first, second)

    @unittest.skipUnless(os.name == "nt", "requires Windows pinned directory handles")
    def test_windows_parent_swap_after_validation_fails_before_target_capture(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            parent = root / "nested"
            parent.mkdir()
            (parent / "old.txt").write_bytes(b"inside\n")
            moved = root / "moved"
            swapped = False

            original_normalize = patch_latest_review_repairs._legacy._normalize_target

            def normalize_with_swap(root_arg: Path, target: str):
                nonlocal swapped
                result = original_normalize(root_arg, target)
                if not swapped:
                    try:
                        parent.rename(moved)
                    except OSError as exc:
                        self.skipTest(
                            f"Windows host physically blocks the parent rename race: {exc}"
                        )
                    swapped = True
                    parent.mkdir()
                    (parent / "old.txt").write_bytes(b"secret\n")
                return result

            with (
                mock.patch.object(
                    patch_latest_review_repairs._legacy,
                    "_normalize_target",
                    side_effect=normalize_with_swap,
                ),
                mock.patch.object(
                    patch_final_review_repairs,
                    "_capture_windows_exact_path",
                    side_effect=AssertionError(
                        "replacement target must not be opened after parent swap"
                    ),
                ),
            ):
                with self.assertRaises(WorkspaceMutationBoundaryError):
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
            self.assertEqual((moved / "old.txt").read_bytes(), b"inside\n")
            self.assertEqual((parent / "old.txt").read_bytes(), b"secret\n")

    @unittest.skipUnless(os.name == "nt", "requires Windows directory handles")
    def test_windows_workspace_alias_is_bound_to_canonical_directory_object(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            real = base / "real-workspace"
            real.mkdir()
            (real / "old.txt").write_bytes(b"old\n")
            alias = base / "workspace-alias"

            try:
                os.symlink(real, alias, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Windows host cannot create directory symlink: {exc}")

            proposal = prepare_patch_proposal(
                workspace=alias,
                changes=(
                    PatchFileRequest(
                        MutationOperation.REPLACE,
                        "old.txt",
                        b"new\n",
                    ),
                ),
            )

            self.assertEqual(proposal.workspace_root, str(real.resolve()))
            self.assertEqual(
                proposal.to_dict()["parameters"]["changes"][0]["target"],
                "old.txt",
            )

    @unittest.skipUnless(os.name == "nt", "requires Windows FileCaseSensitiveInfo")
    def test_windows_parent_namespace_queries_held_handle(self) -> None:
        pinned = SimpleNamespace(_windows_handles=[101, 202])
        with mock.patch.object(
            patch_latest_review_repairs,
            "_windows_directory_identity",
            return_value=(7, 11),
        ) as identity, mock.patch.object(
            patch_latest_review_repairs,
            "_windows_case_sensitive_by_handle",
            return_value=True,
        ) as sensitivity:
            namespace = patch_latest_review_repairs._windows_parent_namespace(pinned)

        identity.assert_called_once_with(202)
        sensitivity.assert_called_once_with(202)
        self.assertEqual(namespace.identity, (7, 11))
        self.assertIs(namespace.case_sensitive, True)

    def test_latest_prepare_and_direct_helpers_are_sealed(self) -> None:
        self.assertIs(
            prepare_patch_proposal,
            patch_latest_review_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            patch_namespace_stability_repairs.prepare_patch_proposal,
            patch_latest_review_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            patch_posix_namespace_repairs.prepare_patch_proposal,
            patch_latest_review_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            patch_posix_root_anchor.prepare_patch_proposal,
            patch_latest_review_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            patch_portability_repairs.prepare_patch_proposal,
            patch_latest_review_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            patch_final_review_repairs.prepare_patch_proposal,
            patch_latest_review_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            patch_review_repairs.prepare_patch_proposal,
            patch_latest_review_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            patch_hardening.prepare_patch_proposal,
            patch_latest_review_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            patches.prepare_patch_proposal,
            patch_latest_review_repairs.prepare_patch_proposal,
        )
        # The newest direct target chain is intentionally two-stage: the review
        # surface reaches the case-seam NUL guard, which delegates valid targets
        # to the held-parent namespace stability helper.
        self.assertIs(
            patch_review_repairs._target_namespace_key,
            patch_case_seam_repairs._target_namespace_key,
        )
        self.assertIs(
            patch_case_seam_repairs._base_target_namespace_key,
            patch_namespace_stability_repairs._direct_target_namespace_key,
        )
        self.assertIs(
            patch_latest_review_repairs._direct_target_namespace_key,
            patch_namespace_stability_repairs._direct_target_namespace_key,
        )


if __name__ == "__main__":
    unittest.main()
