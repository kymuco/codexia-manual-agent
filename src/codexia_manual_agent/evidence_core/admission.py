from __future__ import annotations

import hmac

from codexia_manual_agent.evidence_core.models import (
    EVIDENCE_REF_RECORDED_EVENT,
    EvidenceRef,
)
from codexia_manual_agent.evidence_core.projection import (
    project_evidence_ref,
    project_evidence_refs,
)
from codexia_manual_agent.work_core import WorkSnapshot, WorkState, WorkStore


class EvidenceAdmissionError(RuntimeError):
    """Base failure for durable Gen2 EvidenceRef admission."""


class EvidenceBindingError(EvidenceAdmissionError):
    """Evidence recording changed exact canonical Work binding."""


class EvidenceStateError(EvidenceAdmissionError):
    """EvidenceRef cannot be recorded from the supplied Work state."""


class EvidenceIdentityConflictError(EvidenceAdmissionError):
    """One EvidenceRef identity was reused for different exact semantics."""


class EvidenceAdmission:
    """Record EvidenceRef relevance in canonical Work chronology.

    This boundary does not dereference or verify evidence, grant authority,
    interpret domain validity, decide sufficiency, or decide completion.
    The host/product chooses when an already-formed immutable EvidenceRef is
    semantically relevant enough to record for one Work.
    """

    def __init__(self, store: WorkStore) -> None:
        self._store = store

    def record(
        self,
        snapshot: WorkSnapshot,
        evidence: EvidenceRef,
    ) -> EvidenceRef:
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if not isinstance(evidence, EvidenceRef):
            raise TypeError("evidence must be EvidenceRef")

        events = self._store.events(snapshot.work.work_id)
        current = self._store.snapshot(snapshot.work.work_id)
        if current.work.work_id != snapshot.work.work_id:
            raise EvidenceBindingError("EvidenceRef crossed Work identity")
        if not hmac.compare_digest(
            current.work.work_digest,
            snapshot.work.work_digest,
        ):
            raise EvidenceBindingError("EvidenceRef changed Work binding")

        for existing in project_evidence_refs(events):
            if existing.evidence_id != evidence.evidence_id:
                continue
            if existing == evidence:
                return existing
            raise EvidenceIdentityConflictError(
                "EvidenceRef identity is already bound to different exact semantics"
            )

        if snapshot.state is not WorkState.ACTIVE:
            raise EvidenceStateError(
                "EvidenceRef cannot be recorded on terminal Work"
            )

        event = snapshot.next_event(
            kind=EVIDENCE_REF_RECORDED_EVENT,
            payload={"evidence_ref": evidence.to_dict()},
        )
        self._store.append(
            snapshot.work.work_id,
            expected_revision=snapshot.revision,
            event=event,
        )
        return project_evidence_ref(
            self._store.events(snapshot.work.work_id),
            evidence.evidence_id,
        )
