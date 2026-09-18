from __future__ import annotations

import hmac
from dataclasses import replace
from typing import Any

from codexia_manual_agent.work.contracts import (
    InvalidWorkRecordError,
    WorkActorKind,
    WorkStatement,
    _exact_keys,
    _validate_digest,
)
from codexia_manual_agent.work.supervisor import (
    BackgroundWorkSupervisor,
    SupervisorEventKind,
    SupervisorIntegrityError,
    SupervisorStateError,
    SupervisorStatus,
    SupervisorWorkSnapshot,
)

_ORIGINAL_APPLY_EVENT = BackgroundWorkSupervisor._apply_event


def _record_pilot_human_answer(
    self: BackgroundWorkSupervisor,
    work_id: str,
    *,
    answer: WorkStatement,
) -> SupervisorWorkSnapshot:
    """Resume exact WAITING_HUMAN work from an explicit pilot human surface.

    The answer is evidence for the next cognition checkpoint only. It does not
    admit a continuation and grants no execution/provider authority.
    """

    snapshot = self.recover(work_id)
    if snapshot.status is not SupervisorStatus.WAITING_HUMAN:
        raise SupervisorStateError(
            "A pilot human answer requires exact WAITING_HUMAN supervisor state"
        )
    if not isinstance(answer, WorkStatement):
        raise InvalidWorkRecordError("answer must be a WorkStatement")
    if answer.author_kind is not WorkActorKind.HUMAN:
        raise InvalidWorkRecordError(
            "Pilot human answer must preserve explicit HUMAN authorship"
        )
    if snapshot.last_attention is None or not snapshot.last_attention.needs_human:
        raise SupervisorIntegrityError(
            "WAITING_HUMAN work lost its exact attention decision"
        )
    return self._append(
        snapshot,
        SupervisorEventKind.EXTERNAL_OBSERVED,
        {
            "pilot_human_answer": answer.to_dict(),
            "waiting_sequence": snapshot.last_sequence,
            "waiting_event_digest": snapshot.last_event_digest,
            "attention_decision_digest": snapshot.last_attention.decision_digest,
        },
    )


def _apply_event_with_pilot_human_answer(
    self: BackgroundWorkSupervisor,
    snapshot: SupervisorWorkSnapshot | None,
    event: Any,
) -> SupervisorWorkSnapshot:
    if (
        event.kind is SupervisorEventKind.EXTERNAL_OBSERVED
        and isinstance(event.payload, dict)
        and "pilot_human_answer" in event.payload
    ):
        if snapshot is None:
            raise SupervisorIntegrityError(
                "Pilot human answer cannot appear before work registration"
            )
        if snapshot.status is not SupervisorStatus.WAITING_HUMAN:
            raise SupervisorIntegrityError(
                "Pilot human answer requires WAITING_HUMAN state"
            )
        if snapshot.last_attention is None or not snapshot.last_attention.needs_human:
            raise SupervisorIntegrityError(
                "Pilot human answer lost its exact human-attention checkpoint"
            )
        try:
            value = _exact_keys(
                event.payload,
                {
                    "pilot_human_answer",
                    "waiting_sequence",
                    "waiting_event_digest",
                    "attention_decision_digest",
                },
                "Supervisor pilot human-answer payload",
            )
            answer = WorkStatement.from_dict(value["pilot_human_answer"])
            if answer.author_kind is not WorkActorKind.HUMAN:
                raise InvalidWorkRecordError(
                    "Pilot human answer must preserve HUMAN authorship"
                )
            if (
                type(value["waiting_sequence"]) is not int
                or value["waiting_sequence"] != snapshot.last_sequence
            ):
                raise InvalidWorkRecordError(
                    "Pilot human answer does not bind exact waiting sequence"
                )
            _validate_digest(value["waiting_event_digest"], "waiting_event_digest")
            _validate_digest(
                value["attention_decision_digest"],
                "attention_decision_digest",
            )
            if not hmac.compare_digest(
                value["waiting_event_digest"],
                snapshot.last_event_digest,
            ):
                raise InvalidWorkRecordError(
                    "Pilot human answer does not bind exact waiting event"
                )
            if not hmac.compare_digest(
                value["attention_decision_digest"],
                snapshot.last_attention.decision_digest,
            ):
                raise InvalidWorkRecordError(
                    "Pilot human answer does not bind exact attention decision"
                )
        except InvalidWorkRecordError as exc:
            raise SupervisorIntegrityError(
                "Pilot human-answer event failed exact binding validation"
            ) from exc
        return replace(
            snapshot,
            status=SupervisorStatus.READY,
            last_proposal=None,
            last_admission=None,
            last_attention=None,
            pending_dispatch=None,
            in_flight_claim_id=None,
            last_sequence=event.sequence,
            last_event_digest=event.event_digest,
        )

    return _ORIGINAL_APPLY_EVENT(self, snapshot, event)


setattr(
    BackgroundWorkSupervisor,
    "record_pilot_human_answer",
    _record_pilot_human_answer,
)
setattr(
    BackgroundWorkSupervisor,
    "_apply_event",
    _apply_event_with_pilot_human_answer,
)
