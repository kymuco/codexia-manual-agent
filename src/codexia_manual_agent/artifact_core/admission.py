from __future__ import annotations

import hmac

from codexia_manual_agent.artifact_core.models import (
    ARTIFACT_REF_RECORDED_EVENT,
    ArtifactRef,
)
from codexia_manual_agent.artifact_core.projection import (
    project_artifact_ref,
    project_artifact_refs,
)
from codexia_manual_agent.work_core import WorkSnapshot, WorkState, WorkStore


class ArtifactAdmissionError(RuntimeError):
    """Base failure for durable Gen2 ArtifactRef admission."""


class ArtifactBindingError(ArtifactAdmissionError):
    """Artifact recording changed exact canonical Work binding."""


class ArtifactStateError(ArtifactAdmissionError):
    """ArtifactRef cannot be recorded from the supplied Work state."""


class ArtifactIdentityConflictError(ArtifactAdmissionError):
    """One ArtifactRef identity was reused for different exact semantics."""


class ArtifactAdmission:
    """Record content-bound ArtifactRef relations in canonical Work chronology.

    This boundary does not materialize bytes, verify locator reachability, grant
    authority, interpret evidence, or decide completion. The host/product that
    has an ArtifactRef chooses when to record that immutable reference.
    """

    def __init__(self, store: WorkStore) -> None:
        self._store = store

    def record(
        self,
        snapshot: WorkSnapshot,
        artifact: ArtifactRef,
    ) -> ArtifactRef:
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if not isinstance(artifact, ArtifactRef):
            raise TypeError("artifact must be ArtifactRef")

        events = self._store.events(snapshot.work.work_id)
        current = self._store.snapshot(snapshot.work.work_id)
        if current.work.work_id != snapshot.work.work_id:
            raise ArtifactBindingError("ArtifactRef crossed Work identity")
        if not hmac.compare_digest(
            current.work.work_digest,
            snapshot.work.work_digest,
        ):
            raise ArtifactBindingError("ArtifactRef changed Work binding")

        for existing in project_artifact_refs(events):
            if existing.artifact_id != artifact.artifact_id:
                continue
            if existing == artifact:
                return existing
            raise ArtifactIdentityConflictError(
                "ArtifactRef identity is already bound to different exact semantics"
            )

        if snapshot.state is not WorkState.ACTIVE:
            raise ArtifactStateError(
                "ArtifactRef cannot be recorded on terminal Work"
            )

        event = snapshot.next_event(
            kind=ARTIFACT_REF_RECORDED_EVENT,
            payload={"artifact_ref": artifact.to_dict()},
        )
        self._store.append(
            snapshot.work.work_id,
            expected_revision=snapshot.revision,
            event=event,
        )
        return project_artifact_ref(
            self._store.events(snapshot.work.work_id),
            artifact.artifact_id,
        )
