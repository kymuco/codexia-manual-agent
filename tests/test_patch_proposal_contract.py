from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationTargetExistsError,
    WorkspaceMutationTargetMissingError,
)
from codexia_manual_agent.mutation import (
    MutationOperation,
    PatchFileRequest,
    WorkspaceMutationExecutor,
    build_patch_approval_preview,
    parse_patch_proposal,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation.patches import MAX_PATCH_FILES, PATCH_ACTION


class PatchProposalContractTests(unittest.TestCase):
    def test_patch_proposal_binds_sorted_exact_before_after_set(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "old.py").write_bytes(b"print('old')\n")

            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(
                        MutationOperation.REPLACE,
                        "old.py",
                        b"print('changed')\n",
                    ),
                    PatchFileRequest(
                        MutationOperation.CREATE,
                        "new.py",
                        b"print('new')\n",
                    ),
                ),
            )

            self.assertEqual(proposal.capability, Capability.WRITE_WORKSPACE)
            self.assertEqual(proposal.action, PATCH_ACTION)
            self.assertEqual(proposal.summary, "Apply 2-file workspace patch.")

            payload = proposal.to_dict()
            self.assertEqual(payload["parameters"]["schema_version"], 1)
            changes = payload["parameters"]["changes"]
            self.assertEqual([item["target"] for item in changes], ["new.py", "old.py"])

            create = changes[0]
            self.assertEqual(create["operation"], "create")
            self.assertEqual(create["expected_preimage"]["state"], "absent")
            self.assertIsNone(create["preimage_data_base64"])
            self.assertEqual(create["postimage"]["size_bytes"], len(b"print('new')\n"))

            replace = changes[1]
            self.assertEqual(replace["operation"], "replace")
            self.assertEqual(replace["expected_preimage"]["state"], "present")
            self.assertEqual(
                replace["expected_preimage"]["size_bytes"],
                len(b"print('old')\n"),
            )
            self.assertIsInstance(replace["preimage_data_base64"], str)
            self.assertEqual(
                replace["postimage"]["size_bytes"],
                len(b"print('changed')\n"),
            )
            self.assertEqual(len(replace["change_digest"]), 64)
            self.assertEqual(len(payload["parameters"]["change_set_digest"]), 64)

    def test_change_set_digest_is_stable_across_proposal_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "old.py").write_bytes(b"old\n")
            changes = (
                PatchFileRequest(MutationOperation.REPLACE, "old.py", b"new\n"),
                PatchFileRequest(MutationOperation.CREATE, "new.py", b"created\n"),
            )

            first = prepare_patch_proposal(workspace=root, changes=changes)
            second = prepare_patch_proposal(workspace=root, changes=tuple(reversed(changes)))

            self.assertNotEqual(first.proposal_id, second.proposal_id)
            self.assertNotEqual(first.proposal_digest, second.proposal_digest)
            self.assertEqual(
                first.parameters["change_set_digest"],
                second.parameters["change_set_digest"],
            )

    def test_preview_is_self_contained_and_human_readable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = root / "old.py"
            old.write_bytes(b"print('old')\n")
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(
                        MutationOperation.REPLACE,
                        "old.py",
                        b"print('changed')\n",
                    ),
                    PatchFileRequest(
                        MutationOperation.CREATE,
                        "new.py",
                        b"print('new')\n",
                    ),
                ),
            )

            old.write_bytes(b"raced\n")

            preview = build_patch_approval_preview(proposal).to_dict()
            self.assertTrue(preview["requires_human"])
            self.assertEqual(preview["action"], PATCH_ACTION)
            self.assertEqual(preview["file_count"], 2)
            self.assertEqual(
                preview["change_set_digest"],
                proposal.parameters["change_set_digest"],
            )
            diffs = "\n".join(item["unified_diff"] for item in preview["changes"])
            self.assertIn("--- /dev/null", diffs)
            self.assertIn("+++ b/new.py", diffs)
            self.assertIn("+print('new')", diffs)
            self.assertIn("--- a/old.py", diffs)
            self.assertIn("-print('old')", diffs)
            self.assertIn("+print('changed')", diffs)
            self.assertNotIn("raced", diffs)

            for forbidden in (
                "approved",
                "approval_mode",
                "proposal_digest",
                "receipt_id",
                "receipt_digest",
            ):
                self.assertNotIn(forbidden, preview)

    def test_patch_parameters_are_deeply_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(
                        MutationOperation.CREATE,
                        "new.py",
                        b"print('new')\n",
                    ),
                ),
            )

            with self.assertRaises(TypeError):
                proposal.parameters["changes"][0]["target"] = "other.py"

    def test_duplicate_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(InvalidWorkspaceMutationError, "duplicate target"):
                prepare_patch_proposal(
                    workspace=root,
                    changes=(
                        PatchFileRequest(MutationOperation.CREATE, "same.py", b"a\n"),
                        PatchFileRequest(MutationOperation.CREATE, "same.py", b"b\n"),
                    ),
                )

    def test_create_and_replace_semantics_are_not_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "exists.py").write_bytes(b"old\n")

            with self.assertRaises(WorkspaceMutationTargetExistsError):
                prepare_patch_proposal(
                    workspace=root,
                    changes=(
                        PatchFileRequest(MutationOperation.CREATE, "exists.py", b"new\n"),
                    ),
                )

            with self.assertRaises(WorkspaceMutationTargetMissingError):
                prepare_patch_proposal(
                    workspace=root,
                    changes=(
                        PatchFileRequest(MutationOperation.REPLACE, "missing.py", b"new\n"),
                    ),
                )

    def test_replace_noop_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "same.py").write_bytes(b"same\n")
            with self.assertRaisesRegex(InvalidWorkspaceMutationError, "no-op"):
                prepare_patch_proposal(
                    workspace=root,
                    changes=(
                        PatchFileRequest(MutationOperation.REPLACE, "same.py", b"same\n"),
                    ),
                )

    def test_binary_postimage_is_rejected_in_patch_surface(self) -> None:
        with self.assertRaisesRegex(InvalidWorkspaceMutationError, "not UTF-8 text"):
            PatchFileRequest(
                MutationOperation.CREATE,
                "binary.bin",
                b"\xff\xfe\x00",
            )

    def test_binary_preimage_is_rejected_in_patch_surface(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "binary.bin").write_bytes(b"\xff\xfe\x00")
            with self.assertRaisesRegex(InvalidWorkspaceMutationError, "not UTF-8 text"):
                prepare_patch_proposal(
                    workspace=root,
                    changes=(
                        PatchFileRequest(MutationOperation.REPLACE, "binary.bin", b"text\n"),
                    ),
                )

    def test_patch_file_count_is_bounded_before_workspace_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            changes = tuple(
                PatchFileRequest(MutationOperation.CREATE, f"file_{index}.py", b"")
                for index in range(MAX_PATCH_FILES + 1)
            )
            with self.assertRaisesRegex(InvalidWorkspaceMutationError, "1.."):
                prepare_patch_proposal(workspace=root, changes=changes)

    def test_parser_detects_change_set_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.py", b"new\n"),
                ),
            )
            parameters = proposal.to_dict()["parameters"]
            parameters["change_set_digest"] = "0" * 64
            tampered = ActionProposal.create(
                capability=Capability.WRITE_WORKSPACE,
                action=PATCH_ACTION,
                workspace_root=str(root.resolve()),
                parameters=parameters,
                summary=proposal.summary,
            )
            with self.assertRaisesRegex(InvalidWorkspaceMutationError, "change-set digest"):
                parse_patch_proposal(tampered)

    def test_preview_rejects_non_patch_action_even_with_patch_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.py", b"new\n"),
                ),
            )
            wrong = ActionProposal.create(
                capability=Capability.WRITE_WORKSPACE,
                action="workspace.replace_file.v1",
                workspace_root=proposal.workspace_root,
                parameters=proposal.to_dict()["parameters"],
                summary="wrong action",
            )
            with self.assertRaisesRegex(
                InvalidWorkspaceMutationError,
                "not an M2.4 patch proposal",
            ):
                build_patch_approval_preview(wrong)

    def test_m23_executor_does_not_execute_patch_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proposal = prepare_patch_proposal(
                workspace=root,
                changes=(
                    PatchFileRequest(MutationOperation.CREATE, "new.py", b"new\n"),
                ),
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(
                proposal,
                mode=ApprovalMode.RISKY,
                approved=True,
            )
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)

            with self.assertRaises(InvalidWorkspaceMutationError):
                WorkspaceMutationExecutor().execute(lifecycle, authority=authority)

            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertFalse((root / "new.py").exists())


if __name__ == "__main__":
    unittest.main()
