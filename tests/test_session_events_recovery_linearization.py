from __future__ import annotations

import unittest
from uuid import uuid4

from codexia_manual_agent.session_events import (
    EventKind,
    SessionEventReceipt,
    SessionEventStateError,
    recover_session,
)


def _session_started(session_id: str) -> SessionEventReceipt:
    return SessionEventReceipt.create(
        session_id=session_id,
        sequence=0,
        kind=EventKind.SESSION_STARTED,
        payload={
            "workspace": "C:/m3-recovery-test",
            "prompt_version": "v0.3",
            "mode": "read-only",
            "capabilities": ["read_workspace"],
            "provider": "test-provider",
            "title": None,
            "model": None,
            "reasoning_effort": None,
        },
        previous_event_digest=None,
    )


def _append(
    events: list[SessionEventReceipt],
    kind: EventKind,
    payload: dict[str, object],
) -> None:
    previous = events[-1]
    events.append(
        SessionEventReceipt.create(
            session_id=previous.session_id,
            sequence=len(events),
            kind=kind,
            payload=payload,
            previous_event_digest=previous.event_digest,
        )
    )


def _budgets() -> dict[str, int]:
    return {
        "max_turns": 8,
        "max_tool_calls": 8,
        "max_response_chars": 32768,
        "max_total_model_chars": 131072,
        "max_observation_chars": 131072,
    }


class PureRecoveryLinearizationTests(unittest.TestCase):
    def test_direct_hash_chronology_rejects_overlapping_runs(self) -> None:
        session_id = str(uuid4())
        events = [_session_started(session_id)]
        _append(
            events,
            EventKind.RUN_STARTED,
            {"run_id": str(uuid4()), "task": "first", "budgets": _budgets()},
        )
        _append(
            events,
            EventKind.RUN_STARTED,
            {"run_id": str(uuid4()), "task": "second", "budgets": _budgets()},
        )

        with self.assertRaises(SessionEventStateError):
            recover_session(tuple(events), consumed_authorizations={})

    def test_unresolved_provider_outcome_blocks_later_run_in_pure_recovery(self) -> None:
        session_id = str(uuid4())
        run_id = str(uuid4())
        request_id = str(uuid4())
        events = [_session_started(session_id)]
        _append(
            events,
            EventKind.RUN_STARTED,
            {"run_id": run_id, "task": "first", "budgets": _budgets()},
        )
        _append(
            events,
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
        _append(
            events,
            EventKind.RUN_INTERRUPTED,
            {
                "run_id": run_id,
                "reason": "provider_error",
                "detail": "remote outcome unknown",
                "request_id": request_id,
            },
        )
        _append(
            events,
            EventKind.RUN_STARTED,
            {"run_id": str(uuid4()), "task": "must-not-start", "budgets": _budgets()},
        )

        with self.assertRaises(SessionEventStateError):
            recover_session(tuple(events), consumed_authorizations={})


if __name__ == "__main__":
    unittest.main()
