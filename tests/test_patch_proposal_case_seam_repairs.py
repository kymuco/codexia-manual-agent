from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codexia_manual_agent.mutation import patch_case_seam_repairs
from codexia_manual_agent.mutation import patch_latest_review_repairs
from codexia_manual_agent.mutation import patch_review_repairs


class PatchProposalCaseSeamRepairTests(unittest.TestCase):
    def test_query_seam_uses_context_pinned_handle_without_path_reopen(self) -> None:
        token = patch_case_seam_repairs._WINDOWS_CASE_PARENT_HANDLE.set(202)
        try:
            with (
                mock.patch.object(
                    patch_latest_review_repairs,
                    "_windows_case_sensitive_by_handle",
                    return_value=True,
                ) as held_query,
                mock.patch.object(
                    patch_case_seam_repairs,
                    "_base_query_windows_directory_case_sensitive",
                    side_effect=AssertionError("live proposal query must not reopen the parent path"),
                ),
            ):
                result = patch_case_seam_repairs._query_windows_directory_case_sensitive(
                    Path("C:/workspace")
                )
        finally:
            patch_case_seam_repairs._WINDOWS_CASE_PARENT_HANDLE.reset(token)

        held_query.assert_called_once_with(202)
        self.assertIs(result, True)

    def test_parent_namespace_preserves_filesystem_case_sensitivity_seam(self) -> None:
        parent = Path("C:/workspace")
        pinned = SimpleNamespace(_windows_handles=[101, 202], parent=parent)
        with (
            mock.patch.object(
                patch_latest_review_repairs,
                "_windows_directory_identity",
                return_value=(7, 11),
            ),
            mock.patch.object(
                patch_review_repairs,
                "_filesystem_case_sensitive",
                return_value=True,
            ) as sensitivity,
        ):
            namespace = patch_case_seam_repairs._windows_parent_namespace(pinned)

        self.assertEqual(sensitivity.call_count, 2)
        self.assertTrue(all(call.args == (parent,) for call in sensitivity.call_args_list))
        self.assertEqual(namespace.identity, (7, 11))
        self.assertIs(namespace.case_sensitive, True)

    def test_parent_namespace_fails_closed_on_unstable_case_evidence(self) -> None:
        pinned = SimpleNamespace(
            _windows_handles=[101, 202],
            parent=Path("C:/workspace"),
        )
        with (
            mock.patch.object(
                patch_latest_review_repairs,
                "_windows_directory_identity",
                return_value=(7, 11),
            ),
            mock.patch.object(
                patch_review_repairs,
                "_filesystem_case_sensitive",
                side_effect=(True, False),
            ),
        ):
            namespace = patch_case_seam_repairs._windows_parent_namespace(pinned)

        self.assertIsNone(namespace.case_sensitive)


if __name__ == "__main__":
    unittest.main()
