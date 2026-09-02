from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.authority import (
    ActionProposal,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.session_events import (
    DurableAuthorizationConsumptionRegistry,
    EventKind,
    SessionEventIntegrityError,
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


class ConsumptionIntegrityTests(unittest.TestCase):
    def _consumed(self, root: Path):
        workspace = root / "workspace"
        workspace.mkdir()
        db = root / "events.sqlite3"
        session_id = str(uuid4())
        store = SqliteSessionEventStore(db)
        store.start_session(session_id=session_id, payload=_session_payload(workspace))
        proposal = ActionProposal.create(
            capability=Capability.WRITE_WORKSPACE,
            action="workspace.patch.v1",
            workspace_root=str(workspace),
            parameters={"target": "a.txt"},
        )
        store.record_proposal(session_id, proposal)
        authority = LocalApprovalAuthority(
            consumption_registry=DurableAuthorizationConsumptionRegistry(
                store,
                session_id=session_id,
            )
        )
        receipt = authority.decide(
            proposal,
            mode=ApprovalMode.ALWAYS,
            approved=True,
            actor="human",
        )
        store.record_authorization(session_id, receipt)
        authority.consume(proposal, receipt, mode=ApprovalMode.ALWAYS)
        return db, session_id, store, authority, proposal, receipt

    def test_missing_consumed_row_blocks_second_consume_before_new_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db, session_id, store, authority, proposal, receipt = self._consumed(root)
            before_count = len(
                [
                    event
                    for event in store.load_events(session_id)
                    if event.kind.value == "authorization_consumed"
                ]
            )
            with closing(sqlite3.connect(db)) as connection:
                connection.execute(
                    "DELETE FROM consumed_authorizations WHERE receipt_id = ?",
                    (receipt.receipt_id,),
                )
                connection.commit()

            with self.assertRaises(SessionEventIntegrityError):
                authority.consume(proposal, receipt, mode=ApprovalMode.ALWAYS)
            with closing(sqlite3.connect(db)) as connection:
                after_count = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE kind = ?",
                    ("authorization_consumed",),
                ).fetchone()[0]
            self.assertEqual(after_count, before_count)

    def test_missing_consumption_event_blocks_execution_even_when_row_remains(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db, session_id, store, _, proposal, receipt = self._consumed(root)
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                event_id = connection.execute(
                    "SELECT consumed_event_id FROM consumed_authorizations WHERE receipt_id = ?",
                    (receipt.receipt_id,),
                ).fetchone()[0]
                connection.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
                connection.commit()

            with self.assertRaises(SessionEventIntegrityError):
                store.record_execution(
                    session_id,
                    proposal=proposal,
                    receipt=receipt,
                    execution_id="must-not-publish",
                )
            with closing(sqlite3.connect(db)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE kind = ?",
                    ("action_executed",),
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_missing_consumed_row_blocks_observation_before_new_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db, session_id, store, _, proposal, receipt = self._consumed(root)
            store.record_execution(
                session_id,
                proposal=proposal,
                receipt=receipt,
                execution_id="execution-1",
            )
            with closing(sqlite3.connect(db)) as connection:
                connection.execute(
                    "DELETE FROM consumed_authorizations WHERE receipt_id = ?",
                    (receipt.receipt_id,),
                )
                connection.commit()

            with self.assertRaises(SessionEventIntegrityError):
                store.record_observation(
                    session_id,
                    proposal=proposal,
                    execution_id="execution-1",
                    observation_id="must-not-publish",
                )
            with closing(sqlite3.connect(db)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE kind = ?",
                    ("action_observed",),
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_consumption_corruption_blocks_session_completion_before_new_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db, session_id, store, _, proposal, receipt = self._consumed(root)
            store.record_execution(
                session_id,
                proposal=proposal,
                receipt=receipt,
                execution_id="execution-1",
            )
            store.record_observation(
                session_id,
                proposal=proposal,
                execution_id="execution-1",
                observation_id="observation-1",
            )
            with closing(sqlite3.connect(db)) as connection:
                connection.execute(
                    "DELETE FROM consumed_authorizations WHERE receipt_id = ?",
                    (receipt.receipt_id,),
                )
                connection.commit()

            with self.assertRaises(SessionEventIntegrityError):
                store.append(
                    session_id,
                    EventKind.SESSION_COMPLETED,
                    {"status": "completed", "detail": None},
                )
            with closing(sqlite3.connect(db)) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE kind = ?",
                    ("session_completed",),
                ).fetchone()[0]
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
