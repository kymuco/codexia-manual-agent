from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.session_events import (
    RecoveryDisposition,
    SqliteSessionEventStore,
)


class _TracingStore(SqliteSessionEventStore):
    def __init__(self, database_path: Path) -> None:
        self.connect_calls = 0
        self.statements: list[str] = []
        super().__init__(database_path)

    def _connect(self):
        connection = super()._connect()
        self.connect_calls += 1
        connection.set_trace_callback(self.statements.append)
        return connection

    def reset_trace(self) -> None:
        self.connect_calls = 0
        self.statements.clear()


def _session_payload(workspace: Path) -> dict[str, object]:
    return {
        "workspace": str(workspace.resolve()),
        "prompt_version": "v0.3",
        "mode": "read-only",
        "capabilities": ["read_workspace"],
        "provider": "test-provider",
        "title": None,
        "model": None,
        "reasoning_effort": None,
    }


class SnapshotReadTests(unittest.TestCase):
    def test_load_events_reads_session_events_and_consumption_rows_in_one_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            store = _TracingStore(root / "events.sqlite3")
            session_id = str(uuid4())
            store.start_session(session_id=session_id, payload=_session_payload(workspace))

            store.reset_trace()
            events = store.load_events(session_id)

            self.assertEqual(len(events), 1)
            self.assertEqual(store.connect_calls, 1)
            self.assertTrue(store.statements)
            self.assertEqual(store.statements[0].strip().upper(), "BEGIN")
            joined = "\n".join(store.statements).lower()
            self.assertIn("from sessions", joined)
            self.assertIn("from events", joined)
            self.assertIn("from consumed_authorizations", joined)

    def test_recover_uses_one_snapshot_connection_instead_of_composing_separate_reads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            store = _TracingStore(root / "events.sqlite3")
            session_id = str(uuid4())
            store.start_session(session_id=session_id, payload=_session_payload(workspace))

            store.reset_trace()
            recovery = store.recover(session_id)

            self.assertIs(recovery.disposition, RecoveryDisposition.RESUMABLE)
            self.assertEqual(store.connect_calls, 1)
            self.assertTrue(store.statements)
            self.assertEqual(store.statements[0].strip().upper(), "BEGIN")

    def test_consumed_authorizations_uses_the_same_single_snapshot_reader(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            store = _TracingStore(root / "events.sqlite3")
            session_id = str(uuid4())
            store.start_session(session_id=session_id, payload=_session_payload(workspace))

            store.reset_trace()
            consumed = store.consumed_authorizations(session_id)

            self.assertEqual(consumed, {})
            self.assertEqual(store.connect_calls, 1)
            self.assertTrue(store.statements)
            self.assertEqual(store.statements[0].strip().upper(), "BEGIN")

    def test_is_authorization_consumed_reads_row_and_event_from_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = _TracingStore(root / "events.sqlite3")

            store.reset_trace()
            consumed = store.is_authorization_consumed(str(uuid4()))

            self.assertFalse(consumed)
            self.assertEqual(store.connect_calls, 1)
            self.assertTrue(store.statements)
            self.assertEqual(store.statements[0].strip().upper(), "BEGIN")
            joined = "\n".join(store.statements).lower()
            self.assertIn("from consumed_authorizations", joined)
            self.assertIn("from events", joined)


if __name__ == "__main__":
    unittest.main()
