from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.domain.errors import InvalidWorkspaceMutationError
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchChangeSet,
    PatchFileChange,
    PreimageSnapshot,
)
from codexia_manual_agent.mutation import patch_latest_review_repairs
from codexia_manual_agent.mutation import patch_namespace_stability_repairs
from codexia_manual_agent.mutation import patch_review_repairs


class PatchProposalNamespaceRepairTests(unittest.TestCase):
    def _namespace(self, root: Path, *, case_sensitive: bool):
        parent_info = root.stat()
        return patch_latest_review_repairs._DirectParentNamespace(
            identity=(int(parent_info.st_dev), int(parent_info.st_ino)),
            case_sensitive=case_sensitive,
        )

    def test_case_sensitive_namespace_rejects_unicode_normalization_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with mock.patch.object(
                patch_namespace_stability_repairs,
                "_inspect_direct_parent_namespace",
                return_value=self._namespace(root, case_sensitive=True),
            ) as inspector:
                with self.assertRaisesRegex(
                    InvalidWorkspaceMutationError,
                    "duplicate target",
                ):
                    patch_review_repairs._assert_unique_namespace_targets(
                        root,
                        ("\u00e9.txt", "e\u0301.txt"),
                        label="Patch proposal contains duplicate target",
                    )
                self.assertEqual(inspector.call_count, 2)

    def test_non_windows_empty_target_namespace_does_not_probe_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            mounted = root / "mounted"
            mounted.mkdir()
            with (
                mock.patch.object(
                    patch_review_repairs,
                    "_is_windows_host",
                    return_value=False,
                ),
                mock.patch.object(
                    patch_review_repairs,
                    "_probe_name_case_sensitivity",
                    side_effect=AssertionError("ancestor namespace must not be probed"),
                ),
            ):
                self.assertIsNone(
                    patch_review_repairs._filesystem_case_sensitive(mounted)
                )

    def test_direct_change_set_paths_enforce_namespace_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            changes = (
                PatchFileChange.create(
                    operation=MutationOperation.CREATE,
                    target="File.txt",
                    expected_preimage=PreimageSnapshot.absent(),
                    preimage=None,
                    postimage=b"upper\n",
                ),
                PatchFileChange.create(
                    operation=MutationOperation.CREATE,
                    target="file.txt",
                    expected_preimage=PreimageSnapshot.absent(),
                    preimage=None,
                    postimage=b"lower\n",
                ),
            )

            with mock.patch.object(
                patch_namespace_stability_repairs,
                "_inspect_direct_parent_namespace",
                return_value=self._namespace(root, case_sensitive=True),
            ) as inspector:
                baseline = PatchChangeSet.create(
                    workspace_root=str(root),
                    changes=changes,
                )
                self.assertEqual(inspector.call_count, 2)

            with mock.patch.object(
                patch_namespace_stability_repairs,
                "_inspect_direct_parent_namespace",
                return_value=self._namespace(root, case_sensitive=False),
            ) as inspector:
                with self.subTest(path="factory"):
                    with self.assertRaisesRegex(
                        InvalidWorkspaceMutationError,
                        "unique namespace targets",
                    ):
                        PatchChangeSet.create(
                            workspace_root=str(root),
                            changes=changes,
                        )

                with self.subTest(path="constructor"):
                    with self.assertRaisesRegex(
                        InvalidWorkspaceMutationError,
                        "unique namespace targets",
                    ):
                        PatchChangeSet(
                            workspace_root=str(root),
                            changes=changes,
                            change_set_digest=baseline.change_set_digest,
                        )
                self.assertEqual(inspector.call_count, 4)


if __name__ == "__main__":
    unittest.main()
