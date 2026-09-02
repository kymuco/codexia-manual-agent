from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codexia_manual_agent.adapters.json_session_store import JsonSessionStore
from codexia_manual_agent.application.run_session import RunSessionService
from codexia_manual_agent.application.session_queries import SessionQueryService
from codexia_manual_agent.domain.errors import InvalidSessionIdError, SessionNotFoundError


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = JsonSessionStore(self.root / "state")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_start_persists_read_only_manifest(self) -> None:
        manifest = RunSessionService(self.store).start(workspace=self.workspace)
        self.assertEqual(manifest.mode, "read-only")
        self.assertEqual(manifest.provider, "unconfigured")
        self.assertEqual(manifest.capabilities, ("read_workspace",))
        loaded = self.store.load(manifest.session_id)
        self.assertEqual(loaded, manifest)

    def test_list_orders_newest_first(self) -> None:
        service = RunSessionService(self.store)
        first = service.start(workspace=self.workspace, title="first")
        second = service.start(workspace=self.workspace, title="second")
        # The store orders by persisted creation time. Give the fixture distinct
        # timestamps instead of depending on host clock resolution between starts.
        first = replace(first, created_at="2026-08-06T00:00:00+00:00")
        second = replace(second, created_at="2026-08-06T00:00:01+00:00")
        self.store.save(first)
        self.store.save(second)

        listed = SessionQueryService(self.store).list()
        self.assertEqual({item.session_id for item in listed}, {first.session_id, second.session_id})
        self.assertEqual(listed[0].title, "second")

    def test_loads_m1_0_schema_without_transport_fields(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        self.store.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "session_id": session_id,
            "created_at": "2026-08-06T00:00:00+00:00",
            "workspace": str(self.workspace),
            "prompt_version": "v0.3",
            "mode": "read-only",
            "capabilities": ["read_workspace"],
            "provider": "unconfigured",
            "title": None,
        }
        (self.store.directory / f"{session_id}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        loaded = self.store.load(session_id)
        self.assertEqual(loaded.schema_version, 1)
        self.assertIsNone(loaded.conversation)
        self.assertEqual(loaded.turn_count, 0)
        self.assertEqual(loaded.tool_call_count, 0)

    def test_missing_session_is_explicit(self) -> None:
        with self.assertRaises(SessionNotFoundError):
            self.store.load("00000000-0000-0000-0000-000000000000")

    def test_invalid_session_id_cannot_escape_store(self) -> None:
        with self.assertRaises(InvalidSessionIdError):
            self.store.load("../../escape")


if __name__ == "__main__":
    unittest.main()
