from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.session_events import (
    EventKind,
    SessionEventIntegrityError,
    SessionEventStateError,
    SqliteSessionEventStore,
)


def _session_payload(workspace: Path) -> dict[str, object]:
    return {
        "workspace": str(workspace),
        "prompt_version": "v0.3",
        "mode": "read-only",
        "capabilities": ["read_workspace"],
        "provider": "test-provider",
        "title": None,
        "model": None,
        "reasoning_effort": None,
    }


def _budgets() -> dict[str, int]:
    return {
        "max_turns": 8,
        "max_tool_calls": 8,
        "max_response_chars": 32768,
        "max_total_model_chars": 131072,
        "max_observation_chars": 131072,
    }


class SqliteSessionEventStoreTests(unittest.TestCase):
    def test_first_event_and_restart_round_trip_preserve_exact_chain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            db = root / "state" / "events.sqlite3"
            session_id = str(uuid4())
            store = SqliteSessionEventStore(db)
            first = store.start_session(
                session_id=session_id,
                payload=_session_payload(workspace),
            )
            second = store.append(
                session_id,
                EventKind.RUN_STARTED,
                {"run_id": str(uuid4()), "task": "Inspect", "budgets": _budgets()},
            )

            reopened = SqliteSessionEventStore(db)
            events = reopened.load_events(session_id)
            self.assertEqual(events, (first, second))
            self.assertEqual(events[1].sequence, 1)
            self.assertEqual(events[1].previous_event_digest, events[0].event_digest)

    def test_concurrent_run_starts_publish_exactly_one_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            store = SqliteSessionEventStore(root / "events.sqlite3")
            session_id = str(uuid4())
            store.start_session(session_id=session_id, payload=_session_payload(workspace))
            barrier = threading.Barrier(9)
            successes: list[str] = []
            errors: list[BaseException] = []
            result_lock = threading.Lock()

            def worker() -> None:
                run_id = str(uuid4())
                barrier.wait()
                try:
                    store.append(
                        session_id,
                        EventKind.RUN_STARTED,
                        {"run_id": run_id, "task": "child", "budgets": _budgets()},
                    )
                except BaseException as exc:
                    with result_lock:
                        errors.append(exc)
                else:
                    with result_lock:
                        successes.append(run_id)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

            self.assertEqual(len(successes), 1)
            self.assertEqual(len(errors), 7)
            self.assertTrue(
                all(isinstance(error, SessionEventStateError) for error in errors)
            )
            events = store.load_events(session_id)
            self.assertEqual([event.sequence for event in events], [0, 1])
            self.assertEqual(events[1].kind, EventKind.RUN_STARTED)
            self.assertEqual(events[1].payload["run_id"], successes[0])

            store.append(
                session_id,
                EventKind.RUN_INTERRUPTED,
                {
                    "run_id": successes[0],
                    "reason": "test_cleanup",
                    "detail": "no provider call occurred",
                    "request_id": None,
                },
            )
            next_run = str(uuid4())
            store.append(
                session_id,
                EventKind.RUN_STARTED,
                {"run_id": next_run, "task": "next", "budgets": _budgets()},
            )
            self.assertEqual(store.load_events(session_id)[-1].payload["run_id"], next_run)

    def test_direct_database_payload_tamper_is_detected_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            db = root / "events.sqlite3"
            session_id = str(uuid4())
            store = SqliteSessionEventStore(db)
            store.start_session(session_id=session_id, payload=_session_payload(workspace))
            store.append(
                session_id,
                EventKind.RUN_STARTED,
                {"run_id": str(uuid4()), "task": "Inspect", "budgets": _budgets()},
            )

            with closing(sqlite3.connect(db)) as connection:
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE session_id = ? AND sequence = 1",
                    (
                        '{"run_id":"00000000-0000-0000-0000-000000000000",'
                        '"task":"tampered","budgets":{"max_turns":8,'
                        '"max_tool_calls":8,"max_response_chars":32768,'
                        '"max_total_model_chars":131072,'
                        '"max_observation_chars":131072}}',
                        session_id,
                    ),
                )
                connection.commit()

            with self.assertRaises(SessionEventIntegrityError):
                store.load_events(session_id)

    def test_session_completed_makes_chronology_append_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            store = SqliteSessionEventStore(root / "events.sqlite3")
            session_id = str(uuid4())
            store.start_session(session_id=session_id, payload=_session_payload(workspace))
            store.append(
                session_id,
                EventKind.SESSION_COMPLETED,
                {"status": "completed", "detail": None},
            )
            with self.assertRaises(SessionEventStateError):
                store.append(
                    session_id,
                    EventKind.RUN_STARTED,
                    {"run_id": str(uuid4()), "task": "late", "budgets": _budgets()},
                )


if __name__ == "__main__":
    unittest.main()
