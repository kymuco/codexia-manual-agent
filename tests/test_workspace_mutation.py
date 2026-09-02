from __future__ import annotations

import io
import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

from codexia_manual_agent.application.mutate_workspace import MutateWorkspaceService
from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.cli import _read_bounded_content_file
from codexia_manual_agent.domain.errors import (
    ApprovalRequiredError,
    AuthorizationDeniedError,
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
    WorkspaceMutationTargetExistsError,
    WorkspaceMutationTargetMissingError,
)
from codexia_manual_agent.domain.models import ToolName
from codexia_manual_agent.mutation import (
    MAX_POSTIMAGE_BYTES,
    MutationOperation,
    MutationTerminationReason,
    PreimageSnapshot,
    PreimageState,
    WorkspaceMutationExecutor,
    prepare_create_proposal,
    prepare_replace_proposal,
)
from codexia_manual_agent.mutation import metadata_executor as metadata_executor_module
from codexia_manual_agent.mutation.bounded_io import hash_bounded_stream
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget
from codexia_manual_agent.mutation.workspace import _MutationPlan, _staging_mode


class PortableWorkspaceMutationContractTests(unittest.TestCase):
    def test_create_requires_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ApprovalRequiredError):
                MutateWorkspaceService().run(
                    workspace=raw,
                    operation=MutationOperation.CREATE,
                    target="created.txt",
                    content=b"hello\n",
                )
            self.assertFalse((Path(raw) / "created.txt").exists())

    def test_never_mode_denies_even_with_approve(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(AuthorizationDeniedError):
                MutateWorkspaceService().run(
                    workspace=raw,
                    operation=MutationOperation.CREATE,
                    target="created.txt",
                    content=b"hello\n",
                    mode=ApprovalMode.NEVER,
                    approved=True,
                )
            self.assertFalse((Path(raw) / "created.txt").exists())

    def test_create_rejects_existing_and_replace_rejects_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, "exists.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(WorkspaceMutationTargetExistsError):
                prepare_create_proposal(
                    workspace=raw,
                    target="exists.txt",
                    content=b"new",
                )
            with self.assertRaises(WorkspaceMutationTargetMissingError):
                prepare_replace_proposal(
                    workspace=raw,
                    target="missing.txt",
                    content=b"new",
                )

    def test_zero_replace_mode_is_not_defaulted_to_0644(self) -> None:
        expected = PreimageSnapshot.present(
            size_bytes=3,
            digest=sha256(b"old").hexdigest(),
            mode=0,
        )
        plan = _MutationPlan(
            root=Path("."),
            target="file.txt",
            target_path=Path("file.txt"),
            parent=Path("."),
            operation=MutationOperation.REPLACE,
            expected_preimage=expected,
            postimage=b"new",
            postimage_sha256=sha256(b"new").hexdigest(),
        )
        self.assertEqual(_staging_mode(plan), 0)

    def test_streaming_hash_budget_stops_growing_source(self) -> None:
        class GrowingReader:
            def __init__(self) -> None:
                self.read_calls = 0

            def read(self, size: int) -> bytes:
                self.read_calls += 1
                return b"x" * size

        source = GrowingReader()
        with self.assertRaises(InvalidWorkspaceMutationError):
            hash_bounded_stream(source, max_bytes=16, label="test preimage")
        self.assertEqual(source.read_calls, 1)

    def test_bounded_hash_accepts_exact_budget(self) -> None:
        size, digest = hash_bounded_stream(io.BytesIO(b"abcd"), max_bytes=4)
        self.assertEqual(size, 4)
        self.assertEqual(digest, sha256(b"abcd").hexdigest())

    def test_cli_content_file_rejects_oversize_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "large.bin"
            with source.open("wb") as handle:
                handle.truncate(MAX_POSTIMAGE_BYTES + 1)
            with mock.patch.object(Path, "open", side_effect=AssertionError("must not read")):
                with self.assertRaises(InvalidWorkspaceMutationError):
                    _read_bounded_content_file(source)

    def test_boundary_protected_and_sensitive_targets_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, ".git").mkdir()
            Path(raw, ".codexia").mkdir()
            for target in ("../escape.txt", ".git/config", ".codexia/state.json", ".env"):
                with self.subTest(target=target):
                    with self.assertRaises(WorkspaceMutationBoundaryError):
                        prepare_create_proposal(
                            workspace=raw,
                            target=target,
                            content=b"x",
                        )

    def test_symlink_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("Directory symlinks are unavailable on this platform")
            with self.assertRaises(WorkspaceMutationBoundaryError):
                prepare_create_proposal(
                    workspace=root,
                    target="alias/file.txt",
                    content=b"x",
                )

    def test_remote_tool_surface_remains_read_only(self) -> None:
        self.assertEqual(
            {tool.value for tool in ToolName},
            {"read_file", "list_files", "search_text", "git_status"},
        )


@unittest.skipUnless(os.name == "nt", "M2.3 secure mutation execution is Windows-only")
class WindowsWorkspaceMutationExecutionTests(unittest.TestCase):
    def test_create_binds_exact_postimage_and_observes_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            payload = b"hello\x00world\n"
            result = MutateWorkspaceService().run(
                workspace=raw,
                operation=MutationOperation.CREATE,
                target="created.bin",
                content=payload,
                approved=True,
            )
            self.assertEqual((Path(raw) / "created.bin").read_bytes(), payload)
            self.assertEqual(
                result.observation.termination_reason,
                MutationTerminationReason.APPLIED,
            )
            self.assertTrue(result.observation.applied)
            self.assertEqual(result.observation.expected_preimage.state, PreimageState.ABSENT)
            self.assertEqual(result.observation.postimage_size_bytes, len(payload))
            self.assertEqual(
                result.proposal.to_dict()["parameters"]["postimage"]["sha256"],
                result.observation.postimage_sha256,
            )

    def test_replace_requires_present_exact_preimage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "replace.txt"
            target.write_bytes(b"old")
            result = MutateWorkspaceService().run(
                workspace=raw,
                operation=MutationOperation.REPLACE,
                target="replace.txt",
                content=b"new bytes",
                approved=True,
            )
            self.assertEqual(target.read_bytes(), b"new bytes")
            self.assertEqual(
                result.observation.termination_reason,
                MutationTerminationReason.APPLIED,
            )
            self.assertTrue(result.observation.applied)

    def test_preimage_change_before_consume_does_not_burn_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "file.txt"
            target.write_bytes(b"one")
            proposal = prepare_replace_proposal(
                workspace=raw,
                target="file.txt",
                content=b"two",
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(
                proposal,
                mode=ApprovalMode.RISKY,
                approved=True,
            )
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)
            target.write_bytes(b"changed")

            with self.assertRaises(WorkspaceMutationPreimageChangedError):
                WorkspaceMutationExecutor().execute(lifecycle, authority=authority)
            self.assertFalse(authority.is_consumed(receipt))

    def test_change_after_consume_fails_closed_with_observation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "file.txt"
            target.write_bytes(b"one")
            proposal = prepare_replace_proposal(
                workspace=raw,
                target="file.txt",
                content=b"two",
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(proposal, mode=ApprovalMode.RISKY, approved=True)
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)

            original_capture = metadata_executor_module.capture_windows_replace_metadata_fd
            calls = 0

            def capture_metadata(fd: int, *, expected_path=None):
                nonlocal calls
                calls += 1
                result = original_capture(fd, expected_path=expected_path)
                if calls == 2:
                    result.binding = dict(result.binding)
                    result.binding["file_attributes"] = int(result.binding["file_attributes"]) ^ 0x20
                return result

            with mock.patch.object(
                metadata_executor_module,
                "capture_windows_replace_metadata_fd",
                new=capture_metadata,
            ):
                observation = WorkspaceMutationExecutor().execute(
                    lifecycle,
                    authority=authority,
                )
            self.assertTrue(authority.is_consumed(receipt))
            self.assertFalse(observation.applied)
            self.assertEqual(
                observation.termination_reason,
                MutationTerminationReason.PREIMAGE_CHANGED,
            )
            self.assertEqual(target.read_bytes(), b"one")
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)

    def test_create_target_appearing_after_consume_is_not_clobbered(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "file.txt"
            proposal = prepare_create_proposal(
                workspace=raw,
                target="file.txt",
                content=b"ours",
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(proposal, mode=ApprovalMode.RISKY, approved=True)
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)

            original_write_temp = PinnedMutationTarget.write_temp

            def write_temp(anchor: PinnedMutationTarget, content: bytes, *, mode: int):
                staged = original_write_temp(anchor, content, mode=mode)
                target.write_bytes(b"theirs")
                return staged

            with mock.patch.object(PinnedMutationTarget, "write_temp", new=write_temp):
                observation = WorkspaceMutationExecutor().execute(
                    lifecycle,
                    authority=authority,
                )
            self.assertFalse(observation.applied)
            self.assertEqual(
                observation.termination_reason,
                MutationTerminationReason.TARGET_APPEARED,
            )
            self.assertEqual(target.read_bytes(), b"theirs")

    def test_post_consumption_inspection_error_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "file.txt"
            target.write_bytes(b"one")
            proposal = prepare_replace_proposal(
                workspace=raw,
                target="file.txt",
                content=b"two",
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(proposal, mode=ApprovalMode.RISKY, approved=True)
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)

            original_capture = metadata_executor_module.capture_windows_replace_metadata_fd
            calls = 0

            def capture_metadata(fd: int, *, expected_path=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise InvalidWorkspaceMutationError("target became unreadable")
                return original_capture(fd, expected_path=expected_path)

            with mock.patch.object(
                metadata_executor_module,
                "capture_windows_replace_metadata_fd",
                new=capture_metadata,
            ):
                observation = WorkspaceMutationExecutor().execute(
                    lifecycle,
                    authority=authority,
                )

            self.assertTrue(authority.is_consumed(receipt))
            self.assertFalse(observation.applied)
            self.assertEqual(observation.termination_reason, MutationTerminationReason.WRITE_ERROR)
            self.assertIn("target became unreadable", observation.error or "")
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)

    def test_create_cleanup_failure_after_commit_still_reports_applied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "file.txt"
            proposal = prepare_create_proposal(
                workspace=raw,
                target="file.txt",
                content=b"ours",
            )
            authority = LocalApprovalAuthority()
            receipt = authority.decide(proposal, mode=ApprovalMode.RISKY, approved=True)
            lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
            lifecycle.apply_receipt(receipt, authority=authority)

            original_close = PinnedMutationTarget.close_staged

            def close_then_fail(anchor: PinnedMutationTarget, staged):
                original_close(anchor, staged)
                raise OSError("cleanup blocked")

            with mock.patch.object(
                PinnedMutationTarget,
                "close_staged",
                new=close_then_fail,
            ):
                observation = WorkspaceMutationExecutor().execute(
                    lifecycle,
                    authority=authority,
                )

            self.assertEqual(target.read_bytes(), b"ours")
            self.assertTrue(observation.applied)
            self.assertEqual(observation.termination_reason, MutationTerminationReason.APPLIED)
            self.assertIn("cleanup", observation.error or "")
            self.assertEqual(lifecycle.phase, ActionPhase.OBSERVED)


if __name__ == "__main__":
    unittest.main()
