from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
)
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchChangeSet,
    PatchFileRequest,
    build_patch_approval_preview,
    parse_patch_proposal,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation.patches import (
    MAX_PATCH_FILE_BYTES,
    MAX_PATCH_TOTAL_CONTENT_BYTES,
    PATCH_ACTION,
)


class PatchProposalHardeningTests(unittest.TestCase):
    def test_preview_escapes_terminal_and_bidi_controls(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "old.txt"
            target.write_bytes("safe\x1b[2J\u202eold\n".encode("utf-8"))
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(
                        MutationOperation.REPLACE,
                        "old.txt",
                        b"safe new\n",
                    ),
                ),
            )

            diff = build_patch_approval_preview(proposal).changes[0].unified_diff
            self.assertNotIn("\x1b", diff)
            self.assertNotIn("\u202e", diff)
            self.assertIn(r"\u001b", diff)
            self.assertIn(r"\u202e", diff)

    def test_preview_escapes_bidi_in_target_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = "bi\u202edi.txt"
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, target, b"new\n"),
                ),
            )

            preview = build_patch_approval_preview(proposal)
            self.assertNotIn("\u202e", preview.changes[0].target)
            self.assertIn(r"\u202e", preview.changes[0].target)
            self.assertNotIn("\u202e", preview.changes[0].unified_diff)
            self.assertIn(r"\u202e", preview.changes[0].unified_diff)

    def test_preview_marks_missing_final_newline_without_line_concatenation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "old.txt").write_bytes(b"old")
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.REPLACE, "old.txt", b"new"),
                ),
            )

            diff = build_patch_approval_preview(proposal).changes[0].unified_diff
            self.assertIn("-old\n+new\n", diff)
            self.assertIn("Codexia: preimage has no final LF", diff)
            self.assertIn("Codexia: postimage has no final LF", diff)
            self.assertNotIn("-old+new", diff)

    def test_preview_explicitly_represents_empty_create(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "empty.txt", b""),
                ),
            )

            diff = build_patch_approval_preview(proposal).changes[0].unified_diff
            self.assertIn("--- /dev/null", diff)
            self.assertIn("+++ b/empty.txt", diff)
            self.assertIn("Codexia: empty postimage (0 bytes)", diff)

    def test_exact_preimage_capture_does_not_use_unbounded_path_read_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "old.txt").write_bytes(b"old\n")
            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("Path.read_bytes must not be used"),
            ):
                proposal = prepare_patch_proposal(
                    workspace=root,
                    changes=(
                        PatchFileRequest(
                            MutationOperation.REPLACE,
                            "old.txt",
                            b"new\n",
                        ),
                    ),
                )
            self.assertEqual(proposal.action, PATCH_ACTION)

    def test_total_postimage_budget_fails_before_workspace_preimage_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = b"x" * MAX_PATCH_FILE_BYTES
            changes = tuple(
                PatchFileRequest(MutationOperation.CREATE, f"file_{index}.txt", payload)
                for index in range(
                    MAX_PATCH_TOTAL_CONTENT_BYTES // MAX_PATCH_FILE_BYTES + 1
                )
            )
            with mock.patch(
                "codexia_manual_agent.mutation.patch_hardening._capture_exact_preimage",
                side_effect=AssertionError("preimage inspection must not start"),
            ):
                with self.assertRaisesRegex(
                    InvalidWorkspaceMutationError,
                    "total proposal budget",
                ):
                    prepare_patch_proposal(workspace=root, changes=changes)

    def test_parser_rejects_oversized_base64_before_decode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.txt", b""),
                ),
            )
            parameters = proposal.to_dict()["parameters"]
            parameters["changes"][0]["postimage"]["data_base64"] = "A" * 4
            tampered = ActionProposal.create(
                capability=Capability.WRITE_WORKSPACE,
                action=PATCH_ACTION,
                workspace_root=proposal.workspace_root,
                parameters=parameters,
                summary=proposal.summary,
            )

            with mock.patch(
                "codexia_manual_agent.mutation.patch_hardening.base64.b64decode",
                side_effect=AssertionError("decode must not run"),
            ):
                with self.assertRaisesRegex(
                    InvalidWorkspaceMutationError,
                    "base64 length",
                ):
                    parse_patch_proposal(tampered)

    def test_patch_uses_same_windows_target_preflight_as_m23(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch(
                "codexia_manual_agent.mutation.hardened_workspace._is_windows_host",
                return_value=True,
            ):
                for target in ("CON.txt", "safe.txt:stream", "trailing."):
                    with self.subTest(target=target):
                        with self.assertRaises(WorkspaceMutationBoundaryError):
                            prepare_patch_proposal(
                                workspace=root,
                                changes=(
                                    PatchFileRequest(
                                        MutationOperation.CREATE,
                                        target,
                                        b"new\n",
                                    ),
                                ),
                            )

    def test_parser_rechecks_windows_target_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "ok.txt", b"new\n"),
                ),
            )
            parameters = proposal.to_dict()["parameters"]
            parameters["changes"][0]["target"] = "CON.txt"
            tampered = ActionProposal.create(
                capability=Capability.WRITE_WORKSPACE,
                action=PATCH_ACTION,
                workspace_root=proposal.workspace_root,
                parameters=parameters,
                summary=proposal.summary,
            )
            with mock.patch(
                "codexia_manual_agent.mutation.hardened_workspace._is_windows_host",
                return_value=True,
            ):
                with self.assertRaises(WorkspaceMutationBoundaryError):
                    parse_patch_proposal(tampered)

    def test_direct_change_set_constructor_normalizes_mutable_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.txt", b"new\n"),
                ),
            )
            parsed = parse_patch_proposal(proposal)
            mutable = list(parsed.changes)
            rebuilt = PatchChangeSet(
                workspace_root=parsed.workspace_root,
                changes=mutable,  # type: ignore[arg-type]
                change_set_digest=parsed.change_set_digest,
            )
            mutable.clear()

            self.assertIsInstance(rebuilt.changes, tuple)
            self.assertEqual(len(rebuilt.changes), 1)

    def test_display_expansion_is_still_subject_to_preview_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = b"\x1b" * 100_000
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "preview exceeds review budget",
            ):
                prepare_patch_proposal(
                    workspace=root,
                    changes=(
                        PatchFileRequest(
                            MutationOperation.CREATE,
                            "controls.txt",
                            payload,
                        ),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
