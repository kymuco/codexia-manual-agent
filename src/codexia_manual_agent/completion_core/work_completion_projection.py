from __future__ import annotations

import hmac

from codexia_manual_agent.completion_core.models import CompletionClaim
from codexia_manual_agent.completion_core.projection import (
    project_admitted_completion_claim,
)
from codexia_manual_agent.completion_core.work_completion import (
    InvalidWorkCompletion,
    WorkCompletion,
)
from codexia_manual_agent.work_core import (
    WORK_COMPLETED_EVENT,
    WorkEvent,
)


class WorkCompletionProjectionError(RuntimeError):
    """Durable Work chronology violates Gen2 WorkCompletion semantics."""


def _completion_record(event: WorkEvent) -> WorkCompletion:
    payload = event.to_dict()["payload"]
    if (
        not isinstance(payload, dict)
        or set(payload) != {"work_completion"}
        or not isinstance(payload["work_completion"], dict)
    ):
        raise WorkCompletionProjectionError(
            "work.completed payload is not exact WorkCompletion"
        )
    try:
        return WorkCompletion.from_dict(payload["work_completion"])
    except (InvalidWorkCompletion, KeyError, TypeError, ValueError) as exc:
        raise WorkCompletionProjectionError(
            "work.completed contains invalid WorkCompletion"
        ) from exc


def project_work_completion(
    events: tuple[WorkEvent, ...],
) -> WorkCompletion | None:
    """Recover the one semantic WorkCompletion from canonical chronology."""

    completion: WorkCompletion | None = None
    by_sequence = {event.sequence: event for event in events}

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind != WORK_COMPLETED_EVENT:
            continue
        if completion is not None:
            raise WorkCompletionProjectionError(
                "Work chronology contains more than one WorkCompletion"
            )

        candidate = _completion_record(event)
        if event.event_id != candidate.completion_id:
            raise WorkCompletionProjectionError(
                "work.completed event identity differs from WorkCompletion"
            )
        if event.created_at != candidate.created_at:
            raise WorkCompletionProjectionError(
                "work.completed timestamp differs from WorkCompletion"
            )
        if event.work_id != candidate.work_id:
            raise WorkCompletionProjectionError(
                "work.completed crossed Work identity"
            )
        if event.sequence != candidate.claim_admission_sequence + 1:
            raise WorkCompletionProjectionError(
                "work.completed does not immediately follow claim admission"
            )
        if (
            event.previous_event_digest
            != candidate.claim_admission_event_digest
        ):
            raise WorkCompletionProjectionError(
                "work.completed changed claim-admission chronology"
            )

        admission_event = by_sequence.get(candidate.claim_admission_sequence)
        if admission_event is None:
            raise WorkCompletionProjectionError(
                "WorkCompletion claim admission event is missing"
            )
        if admission_event.event_id != candidate.claim_id:
            raise WorkCompletionProjectionError(
                "WorkCompletion changed admitted CompletionClaim identity"
            )
        if (
            admission_event.event_digest
            != candidate.claim_admission_event_digest
        ):
            raise WorkCompletionProjectionError(
                "WorkCompletion changed claim-admission event digest"
            )

        try:
            claim: CompletionClaim = project_admitted_completion_claim(
                events[: candidate.claim_admission_sequence],
                candidate.claim_id,
            )
        except RuntimeError as exc:
            raise WorkCompletionProjectionError(
                "WorkCompletion references unknown admitted CompletionClaim"
            ) from exc

        if not hmac.compare_digest(
            claim.claim_digest,
            candidate.claim_digest,
        ):
            raise WorkCompletionProjectionError(
                "WorkCompletion changed CompletionClaim digest"
            )
        if claim.work_id != candidate.work_id:
            raise WorkCompletionProjectionError(
                "WorkCompletion CompletionClaim crossed Work identity"
            )
        if not hmac.compare_digest(
            claim.work_digest,
            candidate.work_digest,
        ):
            raise WorkCompletionProjectionError(
                "WorkCompletion changed Work binding"
            )

        completion = candidate

    return completion
