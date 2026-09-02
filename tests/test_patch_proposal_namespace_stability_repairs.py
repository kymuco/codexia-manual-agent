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
from codexia_manual_agent.mutation import patch_latest_review_repairs
from codexia_manual_agent.mutation import patch_namespace_stability_repairs


class PatchProposalNamespaceStabilityRepairTests(unittest.TestCase):
    @staticmethod
    def _namespace(
        identity: tuple[int, int],
        case_sensitive: bool | None,
    ) -> patch_latest_review_repairs._DirectParentNamespace:
        return patch_latest_review_repairs._DirectParentNamespace(
            identity=identity,
            case_sensitive=case_sensitive,
        )

    def test_direct_namespace_reinspects_cached_parent_semantics(self) -> None:
        with mock.patch.object(
            patch_namespace_stability_repairs,
            "_inspect_direct_parent_namespace",
            side_effect=(
                self._namespace((17, 23), True),
                self._namespace((17, 23), False),
            ),
        ) as inspect_parent:
            cache: dict[str, object] = {}
            patch_namespace_stability_repairs._direct_target_namespace_key(
                Path("/workspace/File.txt"),
                sensitivity_cache=cache,
            )
            with self.assertRaisesRegex(
                WorkspaceMutationPreimageChangedError,
                "namespace changed",
            ):
                patch_namespace_stability_repairs._direct_target_namespace_key(
                    Path("/workspace/other.txt"),
                    sensitivity_cache=cache,
                )

        self.assertEqual(inspect_parent.call_count, 2)

    def test_direct_namespace_rejects_parent_identity_change_for_same_path(self) -> None:
        with mock.patch.object(
            patch_namespace_stability_repairs,
            "_inspect_direct_parent_namespace",
            side_effect=(
                self._namespace((31, 41), True),
                self._namespace((31, 43), True),
            ),
        ):
            cache: dict[str, object] = {}
            patch_namespace_stability_repairs._direct_target_namespace_key(
                Path("/workspace/first.txt"),
                sensitivity_cache=cache,
            )
            with self.assertRaises(WorkspaceMutationPreimageChangedError):
                patch_namespace_stability_repairs._direct_target_namespace_key(
                    Path("/workspace/second.txt"),
                    sensitivity_cache=cache,
                )

    @unittest.skipIf(os.name == "nt", "POSIX held-parent inspection regression")
    def test_posix_direct_namespace_identity_comes_from_held_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw).resolve()
            (parent / "Probe.txt").write_bytes(b"x")
            namespace = patch_namespace_stability_repairs._inspect_direct_parent_namespace(
                parent
            )
            info = os.stat(parent, follow_symlinks=False)

        self.assertEqual(namespace.identity, (int(info.st_dev), int(info.st_ino)))

    @unittest.skipUnless(os.name == "nt", "requires Windows pinned directory handles")
    def test_windows_prepare_rejects_case_semantics_change_between_requests(self) -> None:
        stable = self._namespace((7, 11), True)
        changed = self._namespace((7, 11), False)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with mock.patch.object(
                patch_namespace_stability_repairs,
                "_stable_windows_parent_namespace",
                side_effect=(stable, stable, changed),
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationPreimageChangedError,
                    "namespace changed",
                ):
                    prepare_patch_proposal(
                        workspace=root,
                        changes=(
                            PatchFileRequest(
                                MutationOperation.CREATE,
                                "File.txt",
                                b"one\n",
                            ),
                            PatchFileRequest(
                                MutationOperation.CREATE,
                                "file.txt",
                                b"two\n",
                            ),
                        ),
                    )

    @unittest.skipUnless(os.name == "nt", "requires Windows held case query")
    def test_windows_stable_namespace_rejects_intra_request_case_flip(self) -> None:
        pinned = mock.Mock()
        pinned._windows_handles = [101, 202]
        pinned.parent = Path("C:/workspace")
        with (
            mock.patch.object(
                patch_namespace_stability_repairs._latest,
                "_windows_directory_identity",
                return_value=(7, 11),
            ),
            mock.patch.object(
                patch_namespace_stability_repairs._latest._review,
                "_filesystem_case_sensitive",
                side_effect=(True, False),
            ),
        ):
            with self.assertRaisesRegex(
                WorkspaceMutationPreimageChangedError,
                "case semantics changed",
            ):
                patch_namespace_stability_repairs._stable_windows_parent_namespace(
                    pinned
                )


if __name__ == "__main__":
    unittest.main()
