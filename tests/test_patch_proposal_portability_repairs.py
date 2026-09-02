from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchFileChange,
    PreimageSnapshot,
    parse_patch_proposal,
)
from codexia_manual_agent.mutation import (
    patch_final_review_repairs,
    patch_portability_repairs,
    patches,
)


class PatchProposalPortabilityRepairTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX component-open boundary regression")
    def test_posix_component_open_failure_is_translated_to_boundary_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            real = base / "real"
            real.mkdir()
            link = base / "link"
            os.symlink(real, link, target_is_directory=True)

            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "cannot be pinned",
            ):
                patch_final_review_repairs._open_posix_directory_chain(link)

    def test_parser_rejects_digest_valid_nul_target_lexically(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            change = PatchFileChange.create(
                operation=MutationOperation.CREATE,
                target="bad\x00name",
                expected_preimage=PreimageSnapshot.absent(),
                preimage=None,
                postimage=b"new\n",
            )
            encoded = change.to_parameter_dict()
            change_set_digest = patches._digest(
                {
                    "schema_version": patches.PATCH_SCHEMA_VERSION,
                    "workspace_root": str(root),
                    "changes": [encoded],
                }
            )
            proposal = ActionProposal.create(
                capability=Capability.WRITE_WORKSPACE,
                action=patches.PATCH_ACTION,
                workspace_root=str(root),
                parameters={
                    "schema_version": patches.PATCH_SCHEMA_VERSION,
                    "change_set_digest": change_set_digest,
                    "changes": [encoded],
                },
                summary="NUL target parser regression",
            )

            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "NUL",
            ):
                parse_patch_proposal(proposal)

    def test_portability_repairs_seal_final_runtime_globals(self) -> None:
        self.assertIs(
            patch_final_review_repairs._open_posix_directory_chain,
            patch_portability_repairs._open_posix_directory_chain,
        )
        self.assertIs(
            patch_final_review_repairs._lexical_target,
            patch_portability_repairs._lexical_target,
        )
        self.assertIs(
            patch_final_review_repairs._lexical_workspace_root,
            patch_portability_repairs._lexical_workspace_root,
        )


if __name__ == "__main__":
    unittest.main()
