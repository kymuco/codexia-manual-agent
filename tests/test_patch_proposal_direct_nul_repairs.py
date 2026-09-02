from __future__ import annotations

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
from codexia_manual_agent.mutation import (
    patch_case_seam_repairs,
    patch_latest_review_repairs,
    patch_review_repairs,
    patches,
)


class PatchProposalDirectNulRepairTests(unittest.TestCase):
    @staticmethod
    def _nul_change() -> PatchFileChange:
        return PatchFileChange.create(
            operation=MutationOperation.CREATE,
            target="bad\x00name",
            expected_preimage=PreimageSnapshot.absent(),
            preimage=None,
            postimage=b"new\n",
        )

    def test_direct_change_set_create_rejects_nul_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            # Windows may reject NUL earlier through the established mutation
            # target preflight as a control character; POSIX reaches the explicit
            # direct NUL guard. Both are the required fail-closed boundary.
            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "NUL|control character",
            ):
                PatchChangeSet.create(
                    workspace_root=str(root),
                    changes=(self._nul_change(),),
                )

    def test_direct_change_set_constructor_rejects_digest_valid_nul_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            change = self._nul_change()
            digest = patches._digest(
                {
                    "schema_version": patches.PATCH_SCHEMA_VERSION,
                    "workspace_root": str(root),
                    "changes": [change.to_parameter_dict()],
                }
            )

            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "NUL|control character",
            ):
                PatchChangeSet(
                    workspace_root=str(root),
                    changes=(change,),
                    change_set_digest=digest,
                )

    def test_direct_namespace_key_is_sealed_to_nul_guard(self) -> None:
        self.assertIs(
            patch_review_repairs._target_namespace_key,
            patch_case_seam_repairs._target_namespace_key,
        )
        self.assertIs(
            patch_case_seam_repairs._base_target_namespace_key,
            patch_latest_review_repairs._direct_target_namespace_key,
        )


if __name__ == "__main__":
    unittest.main()
