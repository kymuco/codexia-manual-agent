from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.session_events import (
    EventKind,
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


class RuntimeIdentifierReuseTests(unittest.TestCase):
    def _store(self, root: Path) -> tuple[str, SqliteSessionEventStore]:
        workspace = root / "workspace"
        workspace.mkdir()
        session_id = str(uuid4())
        store = SqliteSessionEventStore(root / "events.sqlite3")
        store.start_session(session_id=session_id, payload=_session_payload(workspace))
        return session_id, store

    def test_completed_run_id_cannot_be_reused_by_writer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session_id, store = self._store(Path(raw))
            run_id = str(uuid4())
            store.append(
                session_id,
                EventKind.RUN_STARTED,
                {"run_id": run_id, "task": "first", "budgets": _budgets()},
            )
            store.append(
                session_id,
                EventKind.RUN_COMPLETED,
                {
                    "run_id": run_id,
                    "status": "completed",
                    "final_text": "done",
                    "turns": 0,
                    "tool_calls": 0,
                    "model_chars": 0,
                    "conversation": None,
                    "model": None,
                    "reasoning_effort": None,
                    "error": None,
                },
            )
            before = store.load_events(session_id)

            with self.assertRaisesRegex(SessionEventStateError, "run_id was reused"):
                store.append(
                    session_id,
                    EventKind.RUN_STARTED,
                    {"run_id": run_id, "task": "second", "budgets": _budgets()},
                )

            self.assertEqual(store.load_events(session_id), before)

    def test_resolved_provider_request_id_cannot_be_reused_by_writer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session_id, store = self._store(Path(raw))
            run_id = str(uuid4())
            request_id = str(uuid4())
            store.append(
                session_id,
                EventKind.RUN_STARTED,
                {"run_id": run_id, "task": "inspect", "budgets": _budgets()},
            )
            store.append(
                session_id,
                EventKind.MODEL_REQUEST_STARTED,
                {
                    "run_id": run_id,
                    "request_id": request_id,
                    "provider": "test-provider",
                    "prompt": "first",
                    "system": None,
                    "conversation": None,
                },
            )
            store.append(
                session_id,
                EventKind.MODEL_RESPONSE_RECORDED,
                {
                    "run_id": run_id,
                    "request_id": request_id,
                    "provider": "test-provider",
                    "response_text": "response",
                    "conversation": None,
                    "model": None,
                    "reasoning_effort": None,
                    "metrics": {},
                },
            )
            before = store.load_events(session_id)

            with self.assertRaisesRegex(
                SessionEventStateError,
                "Provider request id was reused",
            ):
                store.append(
                    session_id,
                    EventKind.MODEL_REQUEST_STARTED,
                    {
                        "run_id": run_id,
                        "request_id": request_id,
                        "provider": "test-provider",
                        "prompt": "second",
                        "system": None,
                        "conversation": None,
                    },
                )

            self.assertEqual(store.load_events(session_id), before)


if __name__ == "__main__":
    unittest.main()
