from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.session_events import SqliteSessionEventStore


class SqliteResourceLifetimeTests(unittest.TestCase):
    def test_store_connection_scope_closes_underlying_sqlite_handle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = SqliteSessionEventStore(Path(raw) / "events.sqlite3")
            scoped = store._connect()
            with scoped as connection:
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
