from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchChangeSet,
    PatchFileChange,
    PreimageSnapshot,
)
from codexia_manual_agent.mutation import patches


def _change_set_digest(
    workspace_root: str,
    changes: tuple[PatchFileChange, ...],
) -> str:
    return patches._digest(
        {
            "schema_version": patches.PATCH_SCHEMA_VERSION,
            "workspace_root": workspace_root,
            "changes": [change.to_parameter_dict() for change in changes],
        }
    )


class PatchProposalCanonicalRepairTests(unittest.TestCase):
    def test_direct_change_set_rejects_noncanonical_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            noncanonical_root = str(root) + os.sep + "."
            changes = (
                PatchFileChange.create(
                    operation=MutationOperation.CREATE,
                    target="file.txt",
                    expected_preimage=PreimageSnapshot.absent(),
                    preimage=None,
                    postimage=b"new\n",
                ),
            )

            with self.subTest(path="factory"):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "workspace root is not canonical",
                ):
                    PatchChangeSet.create(
                        workspace_root=noncanonical_root,
                        changes=changes,
                    )

            with self.subTest(path="constructor"):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "workspace root is not canonical",
                ):
                    PatchChangeSet(
                        workspace_root=noncanonical_root,
                        changes=changes,
                        change_set_digest=_change_set_digest(
                            noncanonical_root,
                            changes,
                        ),
                    )

    def test_direct_change_set_rejects_noncanonical_target_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root / "sub").mkdir()
            changes = (
                PatchFileChange.create(
                    operation=MutationOperation.CREATE,
                    target="sub//file.txt",
                    expected_preimage=PreimageSnapshot.absent(),
                    preimage=None,
                    postimage=b"new\n",
                ),
            )

            with self.subTest(path="factory"):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "target is not canonical",
                ):
                    PatchChangeSet.create(
                        workspace_root=str(root),
                        changes=changes,
                    )

            with self.subTest(path="constructor"):
                with self.assertRaisesRegex(
                    WorkspaceMutationBoundaryError,
                    "target is not canonical",
                ):
                    PatchChangeSet(
                        workspace_root=str(root),
                        changes=changes,
                        change_set_digest=_change_set_digest(
                            str(root),
                            changes,
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
