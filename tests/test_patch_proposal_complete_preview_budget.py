from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.domain.errors import InvalidWorkspaceMutationError
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchFileRequest,
    build_patch_approval_preview,
    parse_patch_proposal,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation import patch_case_seam_repairs
from codexia_manual_agent.mutation import patch_final_review_repairs
from codexia_manual_agent.mutation import patch_hardening
from codexia_manual_agent.mutation import patch_latest_review_repairs
from codexia_manual_agent.mutation import patch_namespace_stability_repairs
from codexia_manual_agent.mutation import patch_portability_repairs
from codexia_manual_agent.mutation import patch_posix_namespace_repairs
from codexia_manual_agent.mutation import patch_posix_proposal_stability
from codexia_manual_agent.mutation import patch_posix_root_anchor
from codexia_manual_agent.mutation import patch_preview_budget_repairs
from codexia_manual_agent.mutation import patch_review_repairs
from codexia_manual_agent.mutation import patches


class PatchProposalCompletePreviewBudgetTests(unittest.TestCase):
    @staticmethod
    def _request() -> PatchFileRequest:
        return PatchFileRequest(
            MutationOperation.CREATE,
            "a.txt",
            b"x\n",
        )

    def _proposal_and_budget_gap(self, root: Path):
        proposal = prepare_patch_proposal(
            workspace=root,
            changes=(self._request(),),
        )
        preview = build_patch_approval_preview(proposal)
        diff_only = sum(
            len(change.unified_diff.encode("utf-8"))
            for change in preview.changes
        )
        complete = len(
            patch_preview_budget_repairs._canonical_preview_bytes(preview)
        )
        self.assertGreater(complete, diff_only)
        limit = diff_only + max(1, (complete - diff_only) // 2)
        self.assertLess(diff_only, limit)
        self.assertLess(limit, complete)
        return proposal, limit

    def test_prepare_counts_complete_preview_metadata_not_only_diffs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            _, limit = self._proposal_and_budget_gap(root)

            with mock.patch.object(
                patches,
                "MAX_PATCH_PREVIEW_BYTES",
                limit,
            ):
                with self.assertRaisesRegex(
                    InvalidWorkspaceMutationError,
                    "complete human-readable preview exceeds review budget",
                ):
                    prepare_patch_proposal(
                        workspace=root,
                        changes=(self._request(),),
                    )

    def test_build_preview_rechecks_complete_serialized_surface(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            proposal, limit = self._proposal_and_budget_gap(root)

            with mock.patch.object(
                patches,
                "MAX_PATCH_PREVIEW_BYTES",
                limit,
            ):
                with self.assertRaisesRegex(
                    InvalidWorkspaceMutationError,
                    "complete human-readable preview exceeds review budget",
                ):
                    build_patch_approval_preview(proposal)

    def test_complete_preview_budget_entrypoints_are_sealed(self) -> None:
        self.assertIs(
            prepare_patch_proposal,
            patch_preview_budget_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            build_patch_approval_preview,
            patch_preview_budget_repairs.build_patch_approval_preview,
        )
        self.assertIs(
            parse_patch_proposal,
            patch_preview_budget_repairs._base_parse_patch_proposal,
        )

        for module in (
            patches,
            patch_hardening,
            patch_review_repairs,
            patch_final_review_repairs,
            patch_portability_repairs,
            patch_posix_root_anchor,
            patch_posix_namespace_repairs,
            patch_latest_review_repairs,
            patch_case_seam_repairs,
            patch_namespace_stability_repairs,
            patch_posix_proposal_stability,
        ):
            with self.subTest(module=module.__name__):
                self.assertIs(
                    module.prepare_patch_proposal,
                    patch_preview_budget_repairs.prepare_patch_proposal,
                )
                self.assertIs(
                    module.build_patch_approval_preview,
                    patch_preview_budget_repairs.build_patch_approval_preview,
                )


if __name__ == "__main__":
    unittest.main()
