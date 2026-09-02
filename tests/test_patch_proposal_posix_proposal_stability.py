from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.domain.errors import WorkspaceMutationPreimageChangedError
from codexia_manual_agent.mutation import prepare_patch_proposal
from codexia_manual_agent.mutation import patch_posix_namespace_repairs
from codexia_manual_agent.mutation import patch_posix_proposal_stability
from codexia_manual_agent.mutation import patch_posix_root_anchor


class PatchProposalPosixProposalStabilityTests(unittest.TestCase):
    def _open_root(self, root: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        return os.open(root, flags)

    @unittest.skipIf(os.name == "nt", "POSIX proposal namespace stability")
    def test_same_lexical_parent_can_be_reopened_when_namespace_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root / "nested").mkdir()
            root_fd = self._open_root(root)
            token = patch_posix_proposal_stability._POSIX_PROPOSAL_NAMESPACE_STATE.set(
                patch_posix_proposal_stability._PosixProposalNamespaceState()
            )
            try:
                first = patch_posix_proposal_stability._open_parent_from_root_fd(
                    root_fd,
                    ("nested",),
                )
                os.close(first)
                second = patch_posix_proposal_stability._open_parent_from_root_fd(
                    root_fd,
                    ("nested",),
                )
                os.close(second)
            finally:
                patch_posix_proposal_stability._POSIX_PROPOSAL_NAMESPACE_STATE.reset(token)
                os.close(root_fd)

    @unittest.skipIf(os.name == "nt", "POSIX proposal namespace stability")
    def test_same_lexical_parent_replacement_is_rejected_across_requests(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            parent = root / "nested"
            parent.mkdir()
            moved = root / "moved"
            root_fd = self._open_root(root)
            token = patch_posix_proposal_stability._POSIX_PROPOSAL_NAMESPACE_STATE.set(
                patch_posix_proposal_stability._PosixProposalNamespaceState()
            )
            try:
                first = patch_posix_proposal_stability._open_parent_from_root_fd(
                    root_fd,
                    ("nested",),
                )
                os.close(first)

                parent.rename(moved)
                parent.mkdir()

                with self.assertRaisesRegex(
                    WorkspaceMutationPreimageChangedError,
                    "namespace changed across proposal requests",
                ):
                    patch_posix_proposal_stability._open_parent_from_root_fd(
                        root_fd,
                        ("nested",),
                    )
            finally:
                patch_posix_proposal_stability._POSIX_PROPOSAL_NAMESPACE_STATE.reset(token)
                os.close(root_fd)

    def test_duplicate_key_probe_reuses_admitted_namespace_evidence(self) -> None:
        identity = (17, 23)
        evidence = patch_posix_namespace_repairs._PinnedParentNamespace(
            identity=identity,
            case_sensitive=False,
        )
        state = patch_posix_proposal_stability._PosixProposalNamespaceState()
        state.by_identity[identity] = (
            patch_posix_proposal_stability._PosixParentNamespaceSnapshot(
                identity=identity,
                case_sensitive=False,
                probe_evidence=evidence,
            )
        )
        token = patch_posix_proposal_stability._POSIX_PROPOSAL_NAMESPACE_STATE.set(state)
        try:
            with (
                mock.patch.object(
                    patch_posix_proposal_stability.os,
                    "fstat",
                    return_value=object(),
                ),
                mock.patch.object(
                    patch_posix_root_anchor,
                    "_stat_identity",
                    return_value=identity,
                ),
                mock.patch.object(
                    patch_posix_proposal_stability,
                    "_base_probe_parent_case_sensitivity",
                    side_effect=AssertionError(
                        "duplicate-key path must not perform an independent namespace probe"
                    ),
                ),
            ):
                observed = patch_posix_proposal_stability._probe_parent_case_sensitivity(99)
        finally:
            patch_posix_proposal_stability._POSIX_PROPOSAL_NAMESPACE_STATE.reset(token)

        self.assertIs(observed, evidence)

    def test_first_observation_is_the_only_probe_used_for_duplicate_key(self) -> None:
        identity = (29, 37)
        admitted = patch_posix_namespace_repairs._PinnedParentNamespace(
            identity=identity,
            case_sensitive=False,
        )
        contradictory = patch_posix_namespace_repairs._PinnedParentNamespace(
            identity=identity,
            case_sensitive=True,
        )
        token = patch_posix_proposal_stability._POSIX_PROPOSAL_NAMESPACE_STATE.set(
            patch_posix_proposal_stability._PosixProposalNamespaceState()
        )
        try:
            with (
                mock.patch.object(
                    patch_posix_proposal_stability.os,
                    "fstat",
                    return_value=object(),
                ),
                mock.patch.object(
                    patch_posix_root_anchor,
                    "_stat_identity",
                    return_value=identity,
                ),
                mock.patch.object(
                    patch_posix_proposal_stability,
                    "_base_probe_parent_case_sensitivity",
                    side_effect=[admitted, contradictory],
                ) as base_probe,
            ):
                snapshot = patch_posix_proposal_stability._namespace_snapshot(99)
                patch_posix_proposal_stability._assert_lexical_parent_stable(
                    ("nested",),
                    snapshot,
                )
                reused = patch_posix_proposal_stability._probe_parent_case_sensitivity(99)
        finally:
            patch_posix_proposal_stability._POSIX_PROPOSAL_NAMESPACE_STATE.reset(token)

        self.assertIs(reused, admitted)
        self.assertEqual(base_probe.call_count, 1)

    def test_reused_probe_evidence_preserves_parent_identity_leaf_key(self) -> None:
        identity = (31, 47)
        evidence = patch_posix_namespace_repairs._PinnedParentNamespace(
            identity=identity,
            case_sensitive=False,
        )
        key = patch_posix_root_anchor._namespace_key(
            "AliasParent/File.txt",
            case_sensitive=evidence,
        )
        self.assertEqual(key, (identity, "file.txt"))

    def test_namespace_snapshot_uses_base_probe_for_real_revalidation(self) -> None:
        identity = (53, 59)
        evidence = patch_posix_namespace_repairs._PinnedParentNamespace(
            identity=identity,
            case_sensitive=True,
        )
        with (
            mock.patch.object(
                patch_posix_proposal_stability.os,
                "fstat",
                return_value=object(),
            ),
            mock.patch.object(
                patch_posix_root_anchor,
                "_stat_identity",
                return_value=identity,
            ),
            mock.patch.object(
                patch_posix_proposal_stability,
                "_base_probe_parent_case_sensitivity",
                return_value=evidence,
            ) as base_probe,
        ):
            observed = patch_posix_proposal_stability._namespace_snapshot(99)

        self.assertEqual(observed.identity, identity)
        self.assertIs(observed.case_sensitive, True)
        self.assertIs(observed.probe_evidence, evidence)
        base_probe.assert_called_once_with(99)

    def test_posix_stability_helpers_are_sealed_on_active_anchor(self) -> None:
        self.assertIs(
            patch_posix_root_anchor._open_parent_from_root_fd,
            patch_posix_proposal_stability._open_parent_from_root_fd,
        )
        self.assertIs(
            patch_posix_root_anchor._probe_parent_case_sensitivity,
            patch_posix_proposal_stability._probe_parent_case_sensitivity,
        )
        self.assertIs(
            patch_posix_root_anchor._verify_parent_still_names_anchor,
            patch_posix_proposal_stability._verify_parent_still_names_anchor,
        )
        self.assertIs(
            patch_posix_root_anchor._prepare_posix_patch_proposal,
            patch_posix_proposal_stability._prepare_posix_patch_proposal,
        )
        self.assertIs(
            prepare_patch_proposal,
            patch_posix_proposal_stability.prepare_patch_proposal,
        )


if __name__ == "__main__":
    unittest.main()
