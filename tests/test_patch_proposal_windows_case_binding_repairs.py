from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.domain.errors import WorkspaceMutationPreimageChangedError
from codexia_manual_agent.mutation import patch_final_review_repairs
from codexia_manual_agent.mutation import patch_latest_review_repairs
from codexia_manual_agent.mutation import patch_namespace_stability_repairs
from codexia_manual_agent.mutation import patch_windows_case_binding_repairs
from codexia_manual_agent.mutation import patch_windows_namespace_guard


class PatchProposalWindowsCaseBindingRepairTests(unittest.TestCase):
    def test_live_case_query_is_not_masked_outside_nt_lookup(self) -> None:
        token = patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.set(
            patch_windows_case_binding_repairs._AdmittedWindowsLookup(
                parent_handle=31337,
                case_sensitive=True,
            )
        )
        try:
            with mock.patch.object(
                patch_windows_case_binding_repairs,
                "_base_windows_case_sensitive_by_handle",
                return_value=False,
            ) as live_query:
                observed = patch_windows_namespace_guard._windows_case_sensitive_by_handle(
                    31337
                )
        finally:
            patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.reset(token)

        self.assertIs(observed, False)
        live_query.assert_called_once_with(31337)

    def test_nt_lookup_uses_admitted_case_without_live_requery(self) -> None:
        observed_case: list[bool | None] = []
        token = patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.set(
            patch_windows_case_binding_repairs._AdmittedWindowsLookup(
                parent_handle=31337,
                case_sensitive=True,
            )
        )

        def fake_nt_open(parent_handle: int, target_name: str) -> int:
            observed_case.append(
                patch_windows_namespace_guard._windows_case_sensitive_by_handle(
                    parent_handle
                )
            )
            self.assertEqual(target_name, "file.txt")
            return 909

        try:
            with (
                mock.patch.object(
                    patch_windows_case_binding_repairs,
                    "_base_windows_case_sensitive_by_handle",
                    side_effect=AssertionError(
                        "authority lookup must not independently re-query case semantics"
                    ),
                ),
                mock.patch.object(
                    patch_windows_case_binding_repairs,
                    "_base_nt_open_relative_target",
                    side_effect=fake_nt_open,
                ),
                mock.patch.object(
                    patch_windows_case_binding_repairs,
                    "_opened_leaf_name",
                    return_value="file.txt",
                ),
            ):
                opened = patch_windows_namespace_guard._nt_open_relative_target(
                    31337,
                    "file.txt",
                )
        finally:
            patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.reset(token)

        self.assertEqual(opened, 909)
        self.assertEqual(observed_case, [True])
        self.assertFalse(
            patch_windows_case_binding_repairs._WINDOWS_NT_LOOKUP_ACTIVE.get()
        )

    def test_case_sensitive_lookup_rejects_wrong_opened_leaf_and_closes_handle(self) -> None:
        token = patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.set(
            patch_windows_case_binding_repairs._AdmittedWindowsLookup(
                parent_handle=31337,
                case_sensitive=True,
            )
        )
        try:
            with (
                mock.patch.object(
                    patch_windows_case_binding_repairs,
                    "_base_nt_open_relative_target",
                    return_value=909,
                ),
                mock.patch.object(
                    patch_windows_case_binding_repairs,
                    "_opened_leaf_name",
                    return_value="File.txt",
                ),
                mock.patch.object(
                    patch_latest_review_repairs._parent_anchor,
                    "_win_close_handle",
                ) as close_handle,
            ):
                with self.assertRaisesRegex(
                    WorkspaceMutationPreimageChangedError,
                    "leaf changed case identity",
                ):
                    patch_windows_namespace_guard._nt_open_relative_target(
                        31337,
                        "file.txt",
                    )
        finally:
            patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.reset(token)

        close_handle.assert_called_once_with(909)

    def test_namespace_before_records_admitted_case_for_held_parent(self) -> None:
        pinned = mock.Mock()
        pinned._windows_handles = [31337]
        namespace = patch_latest_review_repairs._DirectParentNamespace(
            identity=(17, 23),
            case_sensitive=True,
        )
        token = patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.set(None)
        try:
            with mock.patch.object(
                patch_windows_case_binding_repairs,
                "_base_stable_windows_parent_namespace",
                return_value=namespace,
            ):
                observed = (
                    patch_namespace_stability_repairs._stable_windows_parent_namespace(
                        pinned
                    )
                )
                admitted = (
                    patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.get()
                )
        finally:
            patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.reset(token)

        self.assertEqual(observed, namespace)
        self.assertEqual(
            admitted,
            patch_windows_case_binding_repairs._AdmittedWindowsLookup(
                parent_handle=31337,
                case_sensitive=True,
            ),
        )

    def test_windows_prepare_scope_restores_admitted_lookup(self) -> None:
        outer = patch_windows_case_binding_repairs._AdmittedWindowsLookup(
            parent_handle=7,
            case_sensitive=False,
        )
        token = patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.set(outer)
        seen_inside: list[object] = []
        sentinel = object()

        def fake_prepare(*, workspace, changes, summary):
            seen_inside.append(
                patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.get()
            )
            return sentinel

        try:
            with mock.patch.object(
                patch_windows_case_binding_repairs,
                "_base_prepare_windows_patch_proposal",
                side_effect=fake_prepare,
            ):
                observed = patch_windows_case_binding_repairs._prepare_windows_patch_proposal(
                    workspace="W:/workspace",
                    changes=(),
                    summary=None,
                )
            restored = patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.get()
        finally:
            patch_windows_case_binding_repairs._WINDOWS_ADMITTED_LOOKUP.reset(token)

        self.assertIs(observed, sentinel)
        self.assertEqual(seen_inside, [None])
        self.assertEqual(restored, outer)

    @unittest.skipUnless(os.name == "nt", "requires Windows final-path handle query")
    def test_opened_leaf_name_reads_actual_leaf_from_real_handle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            parent = root / "nested"
            parent.mkdir()
            (parent / "old.txt").write_bytes(b"old\n")

            with patch_final_review_repairs.PinnedMutationTarget(
                root=root,
                parent=parent,
                target_name="old.txt",
            ) as pinned:
                handle = patch_windows_case_binding_repairs._base_nt_open_relative_target(
                    pinned._windows_handles[-1],
                    "old.txt",
                )
                self.assertIsNotNone(handle)
                try:
                    leaf = patch_windows_case_binding_repairs._opened_leaf_name(int(handle))
                finally:
                    patch_latest_review_repairs._parent_anchor._win_close_handle(
                        int(handle)
                    )

        self.assertEqual(leaf, "old.txt")

    def test_case_binding_helpers_are_sealed_on_active_windows_path(self) -> None:
        self.assertIs(
            patch_namespace_stability_repairs._stable_windows_parent_namespace,
            patch_windows_case_binding_repairs._stable_windows_parent_namespace,
        )
        self.assertIs(
            patch_windows_namespace_guard._windows_case_sensitive_by_handle,
            patch_windows_case_binding_repairs._windows_case_sensitive_by_handle,
        )
        self.assertIs(
            patch_windows_namespace_guard._nt_open_relative_target,
            patch_windows_case_binding_repairs._nt_open_relative_target,
        )
        self.assertIs(
            patch_latest_review_repairs._prepare_windows_patch_proposal,
            patch_windows_case_binding_repairs._prepare_windows_patch_proposal,
        )


if __name__ == "__main__":
    unittest.main()
