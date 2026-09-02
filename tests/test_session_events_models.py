from __future__ import annotations

import unittest
from dataclasses import replace
from uuid import uuid4

from codexia_manual_agent.authority import (
    ActionProposal,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.session_events import (
    EventKind,
    SessionEventIntegrityError,
    SessionEventReceipt,
)


class SessionEventReceiptTests(unittest.TestCase):
    def _session_payload(self) -> dict[str, object]:
        return {
            "workspace": "/tmp/workspace",
            "prompt_version": "v0.3",
            "mode": "read-only",
            "capabilities": ["read_workspace"],
            "provider": "test-provider",
            "title": None,
            "model": None,
            "reasoning_effort": None,
        }

    def test_sequence_zero_is_session_started_and_digest_bound(self) -> None:
        session_id = str(uuid4())
        event = SessionEventReceipt.create(
            session_id=session_id,
            sequence=0,
            kind=EventKind.SESSION_STARTED,
            payload=self._session_payload(),
            previous_event_digest=None,
        )
        self.assertEqual(event.session_id, session_id)
        self.assertEqual(event.sequence, 0)
        self.assertEqual(len(event.event_digest), 64)

        with self.assertRaises(SessionEventIntegrityError):
            replace(event, payload={**self._session_payload(), "provider": "changed"})
        with self.assertRaises(SessionEventIntegrityError):
            SessionEventReceipt.create(
                session_id=session_id,
                sequence=0,
                kind=EventKind.RUN_STARTED,
                payload={
                    "run_id": str(uuid4()),
                    "task": "task",
                    "budgets": {
                        "max_turns": 1,
                        "max_tool_calls": 1,
                        "max_response_chars": 100,
                        "max_total_model_chars": 100,
                        "max_observation_chars": 100,
                    },
                },
                previous_event_digest=None,
            )

    def test_event_chain_binds_previous_digest(self) -> None:
        session_id = str(uuid4())
        first = SessionEventReceipt.create(
            session_id=session_id,
            sequence=0,
            kind=EventKind.SESSION_STARTED,
            payload=self._session_payload(),
            previous_event_digest=None,
        )
        second = SessionEventReceipt.create(
            session_id=session_id,
            sequence=1,
            kind=EventKind.RUN_STARTED,
            payload={
                "run_id": str(uuid4()),
                "task": "Inspect",
                "budgets": {
                    "max_turns": 2,
                    "max_tool_calls": 2,
                    "max_response_chars": 1000,
                    "max_total_model_chars": 2000,
                    "max_observation_chars": 1000,
                },
            },
            previous_event_digest=first.event_digest,
        )
        self.assertEqual(second.previous_event_digest, first.event_digest)
        with self.assertRaises(SessionEventIntegrityError):
            replace(second, previous_event_digest="0" * 64)

    def test_event_kind_rejects_extra_generic_payload_fields(self) -> None:
        with self.assertRaises(SessionEventIntegrityError):
            SessionEventReceipt.create(
                session_id=str(uuid4()),
                sequence=0,
                kind=EventKind.SESSION_STARTED,
                payload={**self._session_payload(), "generic": {"authority": True}},
                previous_event_digest=None,
            )

    def test_authority_events_revalidate_existing_proposal_and_receipt_digests(self) -> None:
        proposal = ActionProposal.create(
            capability=Capability.WRITE_WORKSPACE,
            action="workspace.patch.v1",
            workspace_root="/tmp/workspace",
            parameters={"target": "a.txt"},
        )
        receipt = LocalApprovalAuthority().decide(
            proposal,
            mode=ApprovalMode.ALWAYS,
            approved=True,
            actor="human",
        )
        session_id = str(uuid4())
        first = SessionEventReceipt.create(
            session_id=session_id,
            sequence=0,
            kind=EventKind.SESSION_STARTED,
            payload=self._session_payload(),
            previous_event_digest=None,
        )
        proposed = SessionEventReceipt.create(
            session_id=session_id,
            sequence=1,
            kind=EventKind.ACTION_PROPOSED,
            payload={"proposal": proposal.to_dict()},
            previous_event_digest=first.event_digest,
        )
        recorded = SessionEventReceipt.create(
            session_id=session_id,
            sequence=2,
            kind=EventKind.AUTHORIZATION_RECORDED,
            payload={"receipt": receipt.to_dict()},
            previous_event_digest=proposed.event_digest,
        )
        self.assertEqual(recorded.payload["receipt"]["receipt_id"], receipt.receipt_id)

        tampered = receipt.to_dict()
        tampered["actor"] = "not-the-recorded-actor"
        with self.assertRaises(SessionEventIntegrityError):
            SessionEventReceipt.create(
                session_id=session_id,
                sequence=3,
                kind=EventKind.AUTHORIZATION_RECORDED,
                payload={"receipt": tampered},
                previous_event_digest=recorded.event_digest,
            )


if __name__ == "__main__":
    unittest.main()
