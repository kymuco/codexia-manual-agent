from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import InvalidWorkspaceMutationError
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchFileRequest,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation import (
    hardened_workspace,
    patch_hardening,
    patch_latest_review_repairs,
    patch_namespace_stability_repairs,
    patch_posix_namespace_repairs,
    patch_posix_root_anchor,
    patch_review_repairs,
    patches,
)
from codexia_manual_agent.mutation.patches import MAX_PATCH_FILES, PATCH_ACTION


class PatchProposalReviewRepairTests(unittest.TestCase):
    @staticmethod
    def _namespace(case_sensitive: bool | None):
        return patch_latest_review_repairs._DirectParentNamespace(
            identity=(17, 23),
            case_sensitive=case_sensitive,
        )

    def test_case_insensitive_namespace_rejects_case_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with mock.patch.object(
                patch_namespace_stability_repairs,
                "_inspect_direct_parent_namespace",
                return_value=self._namespace(False),
            ):
                with self.assertRaisesRegex(
                    InvalidWorkspaceMutationError,
                    "duplicate target",
                ):
                    patch_review_repairs._assert_unique_namespace_targets(
                        root,
                        ("File.txt", "file.txt"),
                        label="Patch proposal contains duplicate target",
                    )

    def test_case_sensitive_namespace_keeps_case_distinct_targets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with mock.patch.object(
                patch_namespace_stability_repairs,
                "_inspect_direct_parent_namespace",
                return_value=self._namespace(True),
            ):
                patch_review_repairs._assert_unique_namespace_targets(
                    root,
                    ("File.txt", "file.txt"),
                    label="Patch proposal contains duplicate target",
                )

    def test_windows_case_detection_queries_target_directory_not_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw).resolve()
            target = parent / "case_sensitive_child"
            target.mkdir()
            with (
                mock.patch.object(
                    patch_review_repairs,
                    "_is_windows_host",
                    return_value=True,
                ),
                mock.patch.object(
                    patch_review_repairs,
                    "_query_windows_directory_case_sensitive",
                    return_value=True,
                ) as query,
                mock.patch.object(
                    patch_review_repairs,
                    "_probe_name_case_sensitivity",
                    side_effect=AssertionError(
                        "Windows per-directory detection must not probe parent names"
                    ),
                ),
            ):
                self.assertIs(
                    patch_review_repairs._filesystem_case_sensitive(target),
                    True,
                )
            query.assert_called_once_with(target)

    @unittest.skipUnless(os.name == "nt", "requires real Windows directory metadata")
    def test_windows_directory_case_sensitive_flag_query_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            result = patch_review_repairs._query_windows_directory_case_sensitive(
                Path(raw).resolve()
            )
            self.assertIsInstance(result, bool)

    @unittest.skipUnless(os.name == "nt", "requires active Windows proposal dispatcher")
    def test_windows_per_directory_case_sensitive_targets_survive_base_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with (
                mock.patch.object(
                    hardened_workspace,
                    "_is_windows_host",
                    return_value=True,
                ),
                mock.patch.object(
                    patch_review_repairs,
                    "_is_windows_host",
                    return_value=True,
                ),
                mock.patch.object(
                    patch_review_repairs,
                    "_query_windows_directory_case_sensitive",
                    return_value=True,
                ) as query,
            ):
                proposal = prepare_patch_proposal(
                    workspace=root,
                    changes=(
                        PatchFileRequest(
                            MutationOperation.CREATE,
                            "File.txt",
                            b"upper\n",
                        ),
                        PatchFileRequest(
                            MutationOperation.CREATE,
                            "file.txt",
                            b"lower\n",
                        ),
                    ),
                )
                change_set = patch_review_repairs.parse_patch_proposal(proposal)

            self.assertEqual(
                tuple(change.target for change in change_set.changes),
                ("File.txt", "file.txt"),
            )
            self.assertGreaterEqual(query.call_count, 2)
            self.assertTrue(all(call.args == (root,) for call in query.call_args_list))

    def test_unknown_case_sensitivity_fails_closed_for_case_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with mock.patch.object(
                patch_namespace_stability_repairs,
                "_inspect_direct_parent_namespace",
                return_value=self._namespace(None),
            ):
                with self.assertRaisesRegex(
                    InvalidWorkspaceMutationError,
                    "duplicate target",
                ):
                    patch_review_repairs._assert_unique_namespace_targets(
                        root,
                        ("File.txt", "file.txt"),
                        label="Patch proposal contains duplicate target",
                    )

    def test_parser_does_not_recheck_live_case_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            if os.name == "nt":
                prepare_case_evidence = mock.patch.object(
                    patch_review_repairs,
                    "_filesystem_case_sensitive",
                    return_value=True,
                )
            else:
                parent_info = os.stat(root)
                prepare_case_evidence = mock.patch.object(
                    patch_posix_root_anchor,
                    "_probe_parent_case_sensitivity",
                    return_value=patch_posix_namespace_repairs._PinnedParentNamespace(
                        identity=(int(parent_info.st_dev), int(parent_info.st_ino)),
                        case_sensitive=True,
                    ),
                )

            with prepare_case_evidence:
                proposal = prepare_patch_proposal(
                    workspace=root,
                    changes=(
                        PatchFileRequest(MutationOperation.CREATE, "File.txt", b"upper\n"),
                        PatchFileRequest(MutationOperation.CREATE, "file.txt", b"lower\n"),
                    ),
                )

            with (
                mock.patch.object(
                    patch_review_repairs,
                    "_filesystem_case_sensitive",
                    side_effect=AssertionError(
                        "bound proposal parsing must not consult live namespace"
                    ),
                ),
                mock.patch.object(
                    patch_posix_root_anchor,
                    "_probe_parent_case_sensitivity",
                    side_effect=AssertionError(
                        "bound proposal parsing must not probe live POSIX namespace"
                    ),
                ),
            ):
                parsed = patch_review_repairs.parse_patch_proposal(proposal)

            self.assertEqual(
                tuple(change.target for change in parsed.changes),
                ("File.txt", "file.txt"),
            )

    def test_prepare_bounds_lazy_iterable_before_materializing_beyond_limit(self) -> None:
        class GuardedChanges:
            def __init__(self) -> None:
                self.yielded = 0

            def __iter__(self):
                for index in range(MAX_PATCH_FILES + 1):
                    self.yielded += 1
                    yield PatchFileRequest(
                        MutationOperation.CREATE,
                        f"file_{index}.txt",
                        b"",
                    )
                raise AssertionError("proposal consumed beyond MAX_PATCH_FILES + 1")

        with tempfile.TemporaryDirectory() as raw:
            guarded = GuardedChanges()
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "1..",
            ):
                prepare_patch_proposal(
                    workspace=Path(raw),
                    changes=guarded,
                )
            self.assertEqual(guarded.yielded, MAX_PATCH_FILES + 1)

    def test_parser_rejects_duplicate_targets_without_live_filesystem_access(self) -> None:
        proposal = ActionProposal.create(
            capability=Capability.WRITE_WORKSPACE,
            action=PATCH_ACTION,
            workspace_root=str(Path("/workspace").resolve()),
            parameters={
                "schema_version": patches.PATCH_SCHEMA_VERSION,
                "workspace_root": str(Path("/workspace").resolve()),
                "changes": [],
                "change_set_digest": "0" * 64,
            },
            summary="test",
        )
        with self.assertRaises(InvalidWorkspaceMutationError):
            patch_review_repairs.parse_patch_proposal(proposal)

    def test_review_repair_seals_package_and_direct_submodule_entrypoints(self) -> None:
        self.assertIs(
            patch_hardening.prepare_patch_proposal,
            patch_review_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            patch_hardening.parse_patch_proposal,
            patch_review_repairs.parse_patch_proposal,
        )
        self.assertIs(
            patches.prepare_patch_proposal,
            patch_review_repairs.prepare_patch_proposal,
        )
        self.assertIs(
            patches.parse_patch_proposal,
            patch_review_repairs.parse_patch_proposal,
        )
        self.assertIs(
            patch_hardening._target_identity_key,
            patch_review_repairs._filesystem_target_identity_key,
        )


if __name__ == "__main__":
    unittest.main()
