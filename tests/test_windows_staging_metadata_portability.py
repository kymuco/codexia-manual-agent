from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.application.mutate_workspace import MutateWorkspaceService
from codexia_manual_agent.mutation import MutationOperation, MutationTerminationReason
from codexia_manual_agent.mutation.windows_metadata import capture_windows_replace_binding


@unittest.skipUnless(os.name == "nt", "Windows staging metadata portability regression")
class WindowsStagingMetadataPortabilityTests(unittest.TestCase):
    def test_same_parent_replace_preserves_exact_approved_security_binding(self) -> None:
        """A normal same-parent stage must not perturb an already matching inherited DACL."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "file.txt"
            target.write_bytes(b"old")
            approved_binding = capture_windows_replace_binding(target)

            result = MutateWorkspaceService().run(
                workspace=root,
                operation=MutationOperation.REPLACE,
                target="file.txt",
                content=b"new",
                approved=True,
            )

            self.assertTrue(result.observation.applied, result.observation.error)
            self.assertEqual(
                result.observation.termination_reason,
                MutationTerminationReason.APPLIED,
            )
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(capture_windows_replace_binding(target), approved_binding)


if __name__ == "__main__":
    unittest.main()
