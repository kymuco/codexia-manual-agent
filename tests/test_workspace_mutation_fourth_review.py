from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.application.mutate_workspace import MutateWorkspaceService
from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError
from codexia_manual_agent.mutation import MutationOperation, MutationTerminationReason
from codexia_manual_agent.mutation import parent_anchor as parent_anchor_module
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget


class FourthReviewWorkspaceMutationTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX descriptor regression")
    def test_enter_releases_posix_pin_when_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            anchor = PinnedMutationTarget(
                root=root,
                parent=root,
                target_name="target.txt",
            )
            captured_fd: int | None = None

            def fail_verification() -> None:
                nonlocal captured_fd
                captured_fd = anchor._dir_fd
                raise WorkspaceMutationBoundaryError("simulated parent race")

            with mock.patch.object(
                anchor,
                "verify_parent_identity",
                side_effect=fail_verification,
            ):
                with self.assertRaises(WorkspaceMutationBoundaryError):
                    anchor.__enter__()

            self.assertIsNotNone(captured_fd)
            self.assertIsNone(anchor._dir_fd)
            with self.assertRaises(OSError):
                os.fstat(captured_fd)

    def test_enter_releases_all_windows_pins_when_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            anchor = PinnedMutationTarget(
                root=root,
                parent=root,
                target_name="target.txt",
            )

            def fake_pin_windows_chain() -> None:
                anchor._windows_handles = [101, 202, 303]

            with (
                mock.patch.object(parent_anchor_module.os, "name", "nt"),
                mock.patch.object(
                    anchor,
                    "_pin_windows_chain",
                    side_effect=fake_pin_windows_chain,
                ),
                mock.patch.object(
                    anchor,
                    "verify_parent_identity",
                    side_effect=WorkspaceMutationBoundaryError("simulated parent race"),
                ),
                mock.patch.object(
                    parent_anchor_module,
                    "_win_close_handle",
                ) as close_handle,
            ):
                with self.assertRaises(WorkspaceMutationBoundaryError):
                    anchor.__enter__()

            self.assertEqual(anchor._windows_handles, [])
            self.assertEqual(
                [call.args[0] for call in close_handle.call_args_list],
                [303, 202, 101],
            )

    @unittest.skipUnless(os.name == "nt", "Windows file-attribute regression")
    def test_windows_published_targets_do_not_inherit_staging_attributes(self) -> None:
        import ctypes

        FILE_ATTRIBUTE_HIDDEN = 0x00000002
        FILE_ATTRIBUTE_TEMPORARY = 0x00000100
        INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "visible.txt"
            service = MutateWorkspaceService()

            created = service.run(
                workspace=root,
                operation=MutationOperation.CREATE,
                target="visible.txt",
                content=b"created\n",
                approved=True,
            )
            self.assertTrue(created.observation.applied)
            self.assertEqual(
                created.observation.termination_reason,
                MutationTerminationReason.APPLIED,
            )

            get_attributes = ctypes.WinDLL("kernel32", use_last_error=True).GetFileAttributesW
            get_attributes.argtypes = [ctypes.c_wchar_p]
            get_attributes.restype = ctypes.c_uint32

            attrs = get_attributes(str(target))
            self.assertNotEqual(attrs, INVALID_FILE_ATTRIBUTES)
            self.assertEqual(attrs & FILE_ATTRIBUTE_HIDDEN, 0)
            self.assertEqual(attrs & FILE_ATTRIBUTE_TEMPORARY, 0)

            replaced = service.run(
                workspace=root,
                operation=MutationOperation.REPLACE,
                target="visible.txt",
                content=b"replaced\n",
                approved=True,
            )
            self.assertTrue(replaced.observation.applied)
            self.assertEqual(
                replaced.observation.termination_reason,
                MutationTerminationReason.APPLIED,
            )

            attrs = get_attributes(str(target))
            self.assertNotEqual(attrs, INVALID_FILE_ATTRIBUTES)
            self.assertEqual(attrs & FILE_ATTRIBUTE_HIDDEN, 0)
            self.assertEqual(attrs & FILE_ATTRIBUTE_TEMPORARY, 0)


if __name__ == "__main__":
    unittest.main()
