from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.authority import ActionProposal, ApprovalMode, LocalApprovalAuthority
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.session_events import (
    ActionRecoveryState,
    DurableAuthorizationConsumptionRegistry,
    EventKind,
    RecoveryDisposition,
    SqliteSessionEventStore,
    recover_session,
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


def _budgets() -> dict[str, int]:
    return {
        "max_turns": 8,
        "max_tool_calls": 8,
        "max_response_chars": 32768,
        "max_total_model_chars": 131072,
        "max_observation_chars": 131072,
    }


class SessionRecoveryTests(unittest.TestCase):
    def _store(self, root: Path):
        workspace = root / "workspace"
        workspace.mkdir()
        session_id = str(uuid4())
        store = SqliteSessionEventStore(root / "events.sqlite3")
        store.start_session(session_id=session_id, payload=_session_payload(workspace))
        return workspace, session_id, store

    def _recover(self, store: SqliteSessionEventStore, session_id: str):
        return recover_session(
            store.load_events(session_id),
            consumed_authorizations=store.consumed_authorizations(session_id),
        )

    def test_provider_request_without_response_recovers_unknown_outcome_and_never_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, session_id, store = self._store(root)
            run_id = str(uuid4())
            request_id = str(uuid4())
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
                    "request_id": request_id,
                    "provider": "test-provider",
                    "prompt": "prompt",
                    "system": "system",
                    "conversation": None,
                },
            )

            recovered = self._recover(store, session_id)
            self.assertIs(recovered.disposition, RecoveryDisposition.UNKNOWN_PROVIDER_OUTCOME)
            self.assertFalse(recovered.can_resume_provider)
            self.assertEqual(recovered.open_provider_request_ids, (request_id,))
            self.assertEqual(recovered.interruption_reason, "unknown_provider_outcome")

    def test_exact_response_and_tool_observation_replay_conversation_and_counters(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, session_id, store = self._store(root)
            run_id = str(uuid4())
            request_id = str(uuid4())
            conversation = {
                "conversation_id": "conversation-1",
                "message_id": "message-2",
                "parent_message_id": "message-1",
                "finish_reason": "stop",
            }
            observation_json = json.dumps(
                {
                    "request_id": "tool-1",
                    "tool": "read_file",
                    "success": True,
                    "data": {"text": "exact output"},
                    "error": None,
                },
                sort_keys=True,
            )
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
                    "request_id": request_id,
                    "provider": "test-provider",
                    "prompt": "prompt",
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
                    "conversation": conversation,
                    "model": "test-model",
                    "reasoning_effort": "high",
                    "metrics": {},
                },
            )
            store.append(
                session_id,
                EventKind.TOOL_OBSERVATION_RECORDED,
                {
                    "run_id": run_id,
                    "request_id": "tool-1",
                    "tool": "read_file",
                    "observation_json": observation_json,
                },
            )

            recovered_mid_run = self._recover(store, session_id)
            self.assertIs(recovered_mid_run.disposition, RecoveryDisposition.INTERRUPTED)
            self.assertEqual(recovered_mid_run.turns, 1)
            self.assertEqual(recovered_mid_run.tool_calls, 1)
            self.assertEqual(recovered_mid_run.model_chars, len("response"))
            self.assertEqual(recovered_mid_run.tool_observation_json, (observation_json,))
            self.assertIsNotNone(recovered_mid_run.latest_conversation)
            self.assertEqual(
                recovered_mid_run.latest_conversation.conversation_id,
                "conversation-1",
            )

            store.append(
                session_id,
                EventKind.RUN_COMPLETED,
                {
                    "run_id": run_id,
                    "status": "completed",
                    "final_text": "done",
                    "turns": 1,
                    "tool_calls": 1,
                    "model_chars": len("response"),
                    "conversation": conversation,
                    "model": "test-model",
                    "reasoning_effort": "high",
                    "error": None,
                },
            )
            recovered = self._recover(store, session_id)
            self.assertIs(recovered.disposition, RecoveryDisposition.RESUMABLE)
            self.assertEqual(recovered.turns, 1)
            self.assertEqual(recovered.tool_calls, 1)
            self.assertEqual(recovered.model_chars, len("response"))

    def test_consumed_receipt_without_execution_record_never_recovers_as_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace, session_id, store = self._store(root)
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
            authority.consume(proposal, receipt, mode=ApprovalMode.ALWAYS)

            recovered = self._recover(store, session_id)
            self.assertIs(
                recovered.disposition,
                RecoveryDisposition.BLOCKED_CONSUMED_AUTHORITY,
            )
            self.assertEqual(len(recovered.actions), 1)
            self.assertIs(
                recovered.actions[0].state,
                ActionRecoveryState.CONSUMED_NOT_EXECUTION_RECORDED,
            )
            self.assertFalse(recovered.can_resume_provider)

    def test_execution_and_observation_recover_without_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace, session_id, store = self._store(root)
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
            authority.consume(proposal, receipt, mode=ApprovalMode.ALWAYS)
            store.record_execution(
                session_id,
                proposal=proposal,
                receipt=receipt,
                execution_id="execution-1",
            )
            interrupted = self._recover(store, session_id)
            self.assertIs(interrupted.disposition, RecoveryDisposition.INTERRUPTED)
            self.assertIs(interrupted.actions[0].state, ActionRecoveryState.EXECUTED)

            store.record_observation(
                session_id,
                proposal=proposal,
                execution_id="execution-1",
                observation_id="observation-1",
            )
            recovered = self._recover(store, session_id)
            self.assertIs(recovered.actions[0].state, ActionRecoveryState.OBSERVED)
            self.assertEqual(recovered.actions[0].observation_id, "observation-1")
            self.assertTrue(authority.is_consumed(receipt))


if __name__ == "__main__":
    unittest.main()
