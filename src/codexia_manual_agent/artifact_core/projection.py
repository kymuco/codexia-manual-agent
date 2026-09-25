from __future__ import annotations

from codexia_manual_agent.artifact_core.models import (
    ARTIFACT_REF_RECORDED_EVENT,
    ArtifactRef,
    InvalidArtifactRef,
)
from codexia_manual_agent.work_core import WorkEvent


class ArtifactProjectionError(RuntimeError):
    """Durable Work chronology violates Gen2 artifact-reference semantics."""


def project_artifact_refs(
    events: tuple[WorkEvent, ...],
) -> tuple[ArtifactRef, ...]:
    refs: list[ArtifactRef] = []
    seen_ids: set[str] = set()

    for event in events:
        if not isinstance(event, WorkEvent):
            raise TypeError("events must contain WorkEvent values")
        if event.kind != ARTIFACT_REF_RECORDED_EVENT:
            continue

        payload = event.to_dict()["payload"]
        if (
            not isinstance(payload, dict)
            or set(payload) != {"artifact_ref"}
            or not isinstance(payload["artifact_ref"], dict)
        ):
            raise ArtifactProjectionError(
                "artifact.ref-recorded payload is not exact"
            )
        try:
            artifact = ArtifactRef.from_dict(payload["artifact_ref"])
        except (InvalidArtifactRef, KeyError, TypeError, ValueError) as exc:
            raise ArtifactProjectionError(
                "artifact.ref-recorded contains invalid ArtifactRef"
            ) from exc

        if artifact.artifact_id in seen_ids:
            raise ArtifactProjectionError(
                "ArtifactRef identity was durably recorded twice in one Work"
            )
        seen_ids.add(artifact.artifact_id)
        refs.append(artifact)

    return tuple(refs)


def project_artifact_ref(
    events: tuple[WorkEvent, ...],
    artifact_id: str,
) -> ArtifactRef:
    for artifact in project_artifact_refs(events):
        if artifact.artifact_id == artifact_id:
            return artifact
    raise ArtifactProjectionError(f"Unknown ArtifactRef: {artifact_id}")
