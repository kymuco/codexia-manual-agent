from __future__ import annotations

from codexia_manual_agent.evidence_core.models import (
    EVIDENCE_REF_RECORDED_EVENT,
    EvidenceRef,
    InvalidEvidenceRef,
)
from codexia_manual_agent.work_core import WorkEvent


class EvidenceProjectionError(RuntimeError):
    """Durable Work chronology violates Gen2 evidence-reference semantics."""


def project_evidence_refs(
    events: tuple[WorkEvent, ...],
) -> tuple[EvidenceRef, ...]:
    refs: list[EvidenceRef] = []
    seen_ids: set[str] = set()

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind != EVIDENCE_REF_RECORDED_EVENT:
            continue

        payload = event.to_dict()["payload"]
        if (
            not isinstance(payload, dict)
            or set(payload) != {"evidence_ref"}
            or not isinstance(payload["evidence_ref"], dict)
        ):
            raise EvidenceProjectionError(
                "evidence.ref-recorded payload is not exact"
            )
        try:
            evidence = EvidenceRef.from_dict(payload["evidence_ref"])
        except (InvalidEvidenceRef, KeyError, TypeError, ValueError) as exc:
            raise EvidenceProjectionError(
                "evidence.ref-recorded contains invalid EvidenceRef"
            ) from exc

        if evidence.evidence_id in seen_ids:
            raise EvidenceProjectionError(
                "EvidenceRef identity was durably recorded twice in one Work"
            )
        seen_ids.add(evidence.evidence_id)
        refs.append(evidence)

    return tuple(refs)


def project_evidence_ref(
    events: tuple[WorkEvent, ...],
    evidence_id: str,
) -> EvidenceRef:
    for evidence in project_evidence_refs(events):
        if evidence.evidence_id == evidence_id:
            return evidence
    raise EvidenceProjectionError(f"Unknown EvidenceRef: {evidence_id}")
