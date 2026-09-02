from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.authority import ApprovalMode, LocalApprovalAuthority
from codexia_manual_agent.session_events import (
    ActionRecoveryState,
    DurableAuthorizationConsumptionRegistry,
    EventKind,
    RecoveryDisposition,
    SessionEventStateError,
    SqliteSessionEventStore,
)
from tests.test_session_events_authority import _proposal, _session_payload


def _budgets() -> dict[str, int]:
    return {
        "max_turns": 8,
        "max_tool_calls": 8,
        "max_response_chars": 32768,
        "max_total_model_chars": 131072,
        "max_observation_chars": 131072,
    }


class RecoveryHardeningTests(unittest.TestCase):
    def _new_store(self, root: Path):
        workspace = root / "workspace"
        workspace.mkdir()
        session_id = str(uuid4())
        store = SqliteSessionEventStore(root / "events.sqlite3")
        payload = _session_payload(workspace)
        payload["provider"] = "provider-a"
        store.start_session(session_id=session_id, payload=payload)
        return workspace, session_id, store

    def test_provider_identity_substitution_is_rejected_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, session_id, store = self._new_store(root)
            run_id = str(uuid4())
            store.append(
                session_id,
                EventKind.RUN_STARTED,
                {"run_id": run_id, "task": "Inspect", "budgets": _budgets()},
            )
            with self.assertRaises(SessionEventStateError):
                store.append(
                    session_id,
                    EventKind.MODEL_REQUEST_STARTED,
                    {
                        "run_id": run_id,
                        "request_id": str(uuid4()),
                        "provider": "provider-b",
                        "prompt": "prompt",
                        "system": None,
                        "conversation": None,
                    },
                )
            self.assertEqual(len(store.load_events(session_id)), 2)

    def test_response_cannot_switch_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, session_id, store = self._new_store(root)
            run_id = str(uuid4())
            first_request = str(uuid4())
            second_request = str(uuid4())
            conversation_a = {
                "conversation_id": "conversation-a",
                "message_id": "message-1",
                "parent_message_id": None,
                "finish_reason": None,
            }
            store.append(
                session_id,
                EventKind.RUN_STARTED,
                {"run_id": run_id, "task": "Inspect", "budgets": _budgets()},
            )
            store.append(
                session_id,
                EventKind.MODEL_REQUEST_STARTED,
                {
                    "run_id": run_id,
                    "request_id": first_request,
                    "provider": "provider-a",
                    "prompt": "one",
                    "system": "system",
                    "conversation": None,
                },
            )
            store.append(
                session_id,
                EventKind.MODEL_RESPONSE_RECORDED,
                {
                    "run_id": run_id,
                    "request_id": first_request,
                    "provider": "provider-a",
                    "response_text": "first",
                    "conversation": conversation_a,
                    "model": None,
                    "reasoning_effort": None,
                    "metrics": {},
                },
            )
            store.append(
                session_id,
                EventKind.MODEL_REQUEST_STARTED,
                {
                    "run_id": run_id,
                    "request_id": second_request,
                    "provider": "provider-a",
                    "prompt": "two",
                    "system": None,
                    "conversation": conversation_a,
                },
            )
            store.append(
                session_id,
                EventKind.MODEL_RESPONSE_RECORDED,
                {
                    "run_id": run_id,
                    "request_id": second_request,
                    "provider": "provider-a",
                    "response_text": "second",
                    "conversation": {
                        "conversation_id": "conversation-b",
                        "message_id": "message-2",
                        "parent_message_id": "message-1",
                        "finish_reason": None,
                    },
                    "model": None,
                    "reasoning_effort": None,
                    "metrics": {},
                },
            )
            with self.assertRaises(SessionEventStateError):
                store.recover(session_id)

    def test_authorized_unconsumed_action_blocks_provider_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace, session_id, store = self._new_store(root)
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

            recovery = store.recover(session_id)
            self.assertIs(recovery.disposition, RecoveryDisposition.INTERRUPTED)
            self.assertEqual(
                recovery.interruption_reason,
                "authorized_action_pending_execution",
            )
            self.assertIs(
                recovery.actions[0].state,
                ActionRecoveryState.AUTHORIZED_UNCONSUMED,
            )
            self.assertFalse(recovery.can_resume_provider)

    def test_session_completed_rejects_unresolved_authority_before_append(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace, session_id, store = self._new_store(root)
            proposal = _proposal(workspace)
            store.record_proposal(session_id, proposal)
            with self.assertRaises(SessionEventStateError):
                store.append(
                    session_id,
                    EventKind.SESSION_COMPLETED,
                    {"status": "completed", "detail": None},
                )
            self.assertNotEqual(
                store.load_events(session_id)[-1].kind,
                EventKind.SESSION_COMPLETED,
            )


if __name__ == "__main__":
    unittest.main()
