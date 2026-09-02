from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.authority import (
    ActionProposal,
    ApprovalMode,
    AuthorizationDecision,
    AuthorizationReceipt,
    AuthorizationSource,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import AuthorizationConsumedError
from codexia_manual_agent.session_events import (
    DurableAuthorizationConsumptionRegistry,
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


def _proposal(workspace: Path) -> ActionProposal:
    return ActionProposal.create(
        capability=Capability.WRITE_WORKSPACE,
        action="workspace.patch.v1",
        workspace_root=str(workspace),
        parameters={"target": "a.txt"},
    )


class DurableAuthorizationConsumptionTests(unittest.TestCase):
    def _prepared(self, root: Path):
        workspace = root / "workspace"
        workspace.mkdir()
        db = root / "state" / "events.sqlite3"
        session_id = str(uuid4())
        store = SqliteSessionEventStore(db)
        store.start_session(session_id=session_id, payload=_session_payload(workspace))
        proposal = _proposal(workspace)
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
        return workspace, db, session_id, store, authority, proposal, receipt

    def test_consumed_receipt_stays_consumed_after_store_and_authority_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, db, session_id, store, authority, proposal, receipt = self._prepared(root)
            authority.consume(proposal, receipt, mode=ApprovalMode.ALWAYS)
            self.assertTrue(authority.is_consumed(receipt))

            reopened = SqliteSessionEventStore(db)
            restarted_authority = LocalApprovalAuthority(
                consumption_registry=DurableAuthorizationConsumptionRegistry(
                    reopened,
                    session_id=session_id,
                )
            )
            self.assertTrue(restarted_authority.is_consumed(receipt))
            with self.assertRaises(AuthorizationConsumedError):
                restarted_authority.consume(proposal, receipt, mode=ApprovalMode.ALWAYS)

            events = reopened.load_events(session_id)
            consumed = [
                event for event in events if event.kind is EventKind.AUTHORIZATION_CONSUMED
            ]
            self.assertEqual(len(consumed), 1)
            self.assertEqual(consumed[0].payload["receipt_id"], receipt.receipt_id)

    def test_unrecorded_receipt_cannot_be_consumed_into_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            session_id = str(uuid4())
            store = SqliteSessionEventStore(root / "events.sqlite3")
            store.start_session(session_id=session_id, payload=_session_payload(workspace))
            proposal = _proposal(workspace)
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
            before = store.load_events(session_id)
            with self.assertRaises(SessionEventStateError):
                authority.consume(proposal, receipt, mode=ApprovalMode.ALWAYS)
            self.assertEqual(store.load_events(session_id), before)
            self.assertFalse(store.is_authorization_consumed(receipt.receipt_id))

    def test_same_receipt_id_with_different_valid_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, _, session_id, store, authority, proposal, recorded = self._prepared(root)
            substitute = AuthorizationReceipt.issue(
                proposal=proposal,
                decision=AuthorizationDecision.ALLOW,
                mode=ApprovalMode.ALWAYS,
                source=AuthorizationSource.HUMAN,
                actor="different-human",
                receipt_id=recorded.receipt_id,
            )
            self.assertNotEqual(substitute.receipt_digest, recorded.receipt_digest)
            authority.verify_authorization(proposal, substitute, mode=ApprovalMode.ALWAYS)
            with self.assertRaises(SessionEventStateError):
                authority.consume(proposal, substitute, mode=ApprovalMode.ALWAYS)
            self.assertFalse(store.is_authorization_consumed(recorded.receipt_id))
            self.assertFalse(
                any(
                    event.kind is EventKind.AUTHORIZATION_CONSUMED
                    for event in store.load_events(session_id)
                )
            )

    def test_concurrent_double_consumption_has_exactly_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, db, session_id, _, _, proposal, receipt = self._prepared(root)
            barrier = threading.Barrier(3)
            outcomes: list[str] = []
            lock = threading.Lock()

            def worker() -> None:
                store = SqliteSessionEventStore(db)
                authority = LocalApprovalAuthority(
                    consumption_registry=DurableAuthorizationConsumptionRegistry(
                        store,
                        session_id=session_id,
                    )
                )
                barrier.wait()
                try:
                    authority.consume(proposal, receipt, mode=ApprovalMode.ALWAYS)
                except AuthorizationConsumedError:
                    outcome = "consumed"
                else:
                    outcome = "winner"
                with lock:
                    outcomes.append(outcome)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())

            self.assertEqual(outcomes.count("winner"), 1)
            self.assertEqual(outcomes.count("consumed"), 1)
            events = SqliteSessionEventStore(db).load_events(session_id)
            self.assertEqual(
                sum(event.kind is EventKind.AUTHORIZATION_CONSUMED for event in events),
                1,
            )


if __name__ == "__main__":
    unittest.main()
