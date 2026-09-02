from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError
from codexia_manual_agent.mutation import (
    MutationOperation,
    WorkspaceMutationExecutor,
    prepare_replace_proposal,
)
from codexia_manual_agent.mutation import workspace as workspace_module
from codexia_manual_agent.mutation.metadata_executor import WindowsMetadataReplaceExecutor
from codexia_manual_agent.mutation.windows_metadata import (
    _binding,
    capture_windows_replace_binding,
    validate_windows_relative_target,
)


class PortableSelfReviewHardeningTests(unittest.TestCase):
    def test_win32_namespace_alias_spellings_are_rejected(self) -> None:
        rejected = (
            "credentials.json::$DATA",
            "credentials.json.",
            "credentials.json ",
            "NUL",
            "NUL.txt",
            "COM1.log",
            "COM¹.txt",
            "LPT9.dat",
            "folder/name:stream",
        )
        for target in rejected:
            with self.subTest(target=target):
                with self.assertRaises(WorkspaceMutationBoundaryError):
                    validate_windows_relative_target(target)

    def test_default_stream_only_policy_rejects_named_streams(self) -> None:
        self.assertEqual(
            _binding("O:SYG:SYD:", 0x20, ("::$DATA",))["stream_policy"],
            "default_only",
        )
        with self.assertRaisesRegex(
            WorkspaceMutationBoundaryError,
            "named data streams",
        ):
            _binding("O:SYG:SYD:", 0x20, ("::$DATA", ":secret:$DATA"))

    def test_direct_workspace_prepare_surface_is_hardened(self) -> None:
        self.assertIs(workspace_module.prepare_replace_proposal, prepare_replace_proposal)


@unittest.skipUnless(os.name == "nt", "Windows M2.3 strict-replace regressions")
class WindowsSelfReviewHardeningTests(unittest.TestCase):
    def _authorized_replace(self, root: Path):
        target = root / "file.txt"
        target.write_bytes(b"old")
        proposal = prepare_replace_proposal(
            workspace=root,
            target="file.txt",
            content=b"new",
        )
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
        )
        lifecycle = ActionLifecycle(proposal, ApprovalMode.RISKY)
        lifecycle.apply_receipt(receipt, authority=authority)
        return target, authority, receipt, lifecycle

    def test_exact_destination_pin_failure_does_not_consume_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target, authority, receipt, lifecycle = self._authorized_replace(root)
            with mock.patch(
                "codexia_manual_agent.mutation.metadata_executor._win_pin_exact_replace_target",
                side_effect=WorkspaceMutationBoundaryError("pin unavailable"),
            ):
                with self.assertRaisesRegex(WorkspaceMutationBoundaryError, "pin unavailable"):
                    WindowsMetadataReplaceExecutor().execute(
                        lifecycle,
                        authority=authority,
                    )
            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertEqual(target.read_bytes(), b"old")

    def test_unpreservable_metadata_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target, authority, receipt, lifecycle = self._authorized_replace(root)
            with mock.patch(
                "codexia_manual_agent.mutation.metadata_executor.capture_windows_replace_metadata_fd",
                side_effect=WorkspaceMutationBoundaryError("unsupported metadata"),
            ):
                with self.assertRaisesRegex(WorkspaceMutationBoundaryError, "unsupported metadata"):
                    WindowsMetadataReplaceExecutor().execute(
                        lifecycle,
                        authority=authority,
                    )
            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertEqual(target.read_bytes(), b"old")

    def test_plain_replace_preserves_bound_windows_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "file.txt"
            target.write_bytes(b"old")
            before = capture_windows_replace_binding(target)
            result = __import__(
                "codexia_manual_agent.application.mutate_workspace",
                fromlist=["MutateWorkspaceService"],
            ).MutateWorkspaceService().run(
                workspace=root,
                operation=MutationOperation.REPLACE,
                target="file.txt",
                content=b"new",
                approved=True,
            )
            after = capture_windows_replace_binding(target)
            self.assertTrue(result.observation.applied)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(after, before)

    def test_named_stream_target_fails_before_receipt_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target, authority, receipt, lifecycle = self._authorized_replace(root)
            try:
                with open(f"{target}:secret", "wb") as handle:
                    handle.write(b"secret")
            except OSError:
                self.skipTest("Named data streams are unavailable on this filesystem")
            with self.assertRaisesRegex(
                WorkspaceMutationBoundaryError,
                "named data streams",
            ):
                WorkspaceMutationExecutor().execute(lifecycle, authority=authority)
            self.assertFalse(authority.is_consumed(receipt))
            self.assertEqual(lifecycle.phase, ActionPhase.AUTHORIZED)
            self.assertEqual(target.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
