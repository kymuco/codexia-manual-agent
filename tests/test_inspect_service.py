from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.adapters.filesystem_workspace import FilesystemWorkspace
from codexia_manual_agent.application.inspect_workspace import InspectWorkspaceService
from codexia_manual_agent.domain.models import ToolName, ToolRequest


class InspectWorkspaceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "note.txt").write_bytes(b"alpha\nbeta\n")
        self.service = InspectWorkspaceService(FilesystemWorkspace(self.root))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_read_observation_contains_exact_text(self) -> None:
        result = self.service.execute(
            ToolRequest("r1", ToolName.READ_FILE, {"path": "note.txt"})
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["text"], "alpha\nbeta\n")

    def test_missing_required_argument_is_failure_observation(self) -> None:
        result = self.service.execute(
            ToolRequest("r2", ToolName.READ_FILE, {})
        )
        self.assertFalse(result.success)
        self.assertIn("path", result.error or "")

    def test_git_status_rejects_model_arguments(self) -> None:
        result = self.service.execute(
            ToolRequest("r3", ToolName.GIT_STATUS, {"command": "not-allowed"})
        )
        self.assertFalse(result.success)
        self.assertIn("does not accept arguments", result.error or "")


if __name__ == "__main__":
    unittest.main()
