from __future__ import annotations

from codexia_manual_agent.completion_core.models import (
    COMPLETION_CLAIM_ADMITTED_EVENT,
    CompletionClaim,
    InvalidCompletionClaim,
)
from codexia_manual_agent.work_core import WorkEvent


class CompletionProjectionError(RuntimeError):
    """Durable Work chronology violates Gen2 completion-claim admission."""


def project_admitted_completion_claims(
    events: tuple[WorkEvent, ...],
) -> tuple[CompletionClaim, ...]:
    claims: list[CompletionClaim] = []
    seen_ids: set[str] = set()

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind != COMPLETION_CLAIM_ADMITTED_EVENT:
            continue

        payload = event.to_dict()["payload"]
        if (
            not isinstance(payload, dict)
            or set(payload) != {"completion_claim"}
            or not isinstance(payload["completion_claim"], dict)
        ):
            raise CompletionProjectionError(
                "completion.claim-admitted payload is not exact"
            )
        try:
            claim = CompletionClaim.from_dict(payload["completion_claim"])
        except (InvalidCompletionClaim, KeyError, TypeError, ValueError) as exc:
            raise CompletionProjectionError(
                "completion.claim-admitted contains invalid CompletionClaim"
            ) from exc

        if claim.claim_id in seen_ids:
            raise CompletionProjectionError(
                "CompletionClaim identity was durably admitted twice in one Work"
            )
        if event.event_id != claim.claim_id:
            raise CompletionProjectionError(
                "completion claim admission event identity differs from claim"
            )
        if event.work_id != claim.work_id:
            raise CompletionProjectionError(
                "completion claim admission crossed Work identity"
            )
        if event.sequence != claim.work_revision + 1:
            raise CompletionProjectionError(
                "completion claim admission does not bind exact Work revision"
            )
        if event.previous_event_digest != claim.work_event_digest:
            raise CompletionProjectionError(
                "completion claim admission changed prior Work chronology"
            )

        seen_ids.add(claim.claim_id)
        claims.append(claim)

    return tuple(claims)


def project_admitted_completion_claim(
    events: tuple[WorkEvent, ...],
    claim_id: str,
) -> CompletionClaim:
    for claim in project_admitted_completion_claims(events):
        if claim.claim_id == claim_id:
            return claim
    raise CompletionProjectionError(
        f"Unknown admitted CompletionClaim: {claim_id}"
    )
