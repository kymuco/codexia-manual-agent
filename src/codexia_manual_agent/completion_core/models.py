from __future__ import annotations

import hmac
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from codexia_manual_agent.artifact_core import ArtifactRef
from codexia_manual_agent.evidence_core import EvidenceRef
from codexia_manual_agent.completion_core.work_completion_ref import (
    InvalidWorkCompletionRef,
    WorkCompletionRef,
)
from codexia_manual_agent.pack_core import PackWorkflowBinding
from codexia_manual_agent.work_core import WorkSnapshot, WorkState
from codexia_manual_agent.workflow_core import WorkflowRunSnapshot, WorkflowRunState

COMPLETION_CLAIM_SCHEMA_VERSION = 2
COMPLETION_CLAIM_LEGACY_SCHEMA_VERSION = 1
_COMPLETION_CLAIM_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
COMPLETION_CLAIM_ADMITTED_EVENT = "completion.claim-admitted"
MAX_COMPLETION_SUMMARY_CHARS = 16_384
MAX_TIMESTAMP_CHARS = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidCompletionClaim(ValueError):
    """Raised when a Gen2 CompletionClaim is structurally invalid."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidCompletionClaim(
            "CompletionClaim is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidCompletionClaim(
            f"{field_name} must be a canonical UUID"
        )
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidCompletionClaim(
            f"{field_name} must be a canonical UUID"
        ) from exc
    if str(parsed) != value:
        raise InvalidCompletionClaim(
            f"{field_name} must be lowercase hyphenated UUID"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidCompletionClaim(
            f"{field_name} must be lowercase SHA-256 hex"
        )
    return value


def _timestamp(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidCompletionClaim(
            "created_at must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidCompletionClaim(
            "created_at must be canonical ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidCompletionClaim(
            "created_at must be canonical ISO-8601"
        )
    return value


def _new_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _summary(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidCompletionClaim("summary must be text")
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > MAX_COMPLETION_SUMMARY_CHARS
    ):
        raise InvalidCompletionClaim(
            "summary must be non-empty canonical trimmed text "
            f"within {MAX_COMPLETION_SUMMARY_CHARS} characters"
        )
    return value


def _artifact_basis(values: Iterable[ArtifactRef]) -> tuple[ArtifactRef, ...]:
    items = tuple(values)
    if any(not isinstance(item, ArtifactRef) for item in items):
        raise TypeError("artifact_refs must contain ArtifactRef values")
    ordered = tuple(sorted(items, key=lambda item: item.artifact_id))
    ids = [item.artifact_id for item in ordered]
    if len(set(ids)) != len(ids):
        raise InvalidCompletionClaim(
            "artifact_refs cannot repeat ArtifactRef identity"
        )
    return ordered


def _evidence_basis(values: Iterable[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    items = tuple(values)
    if any(not isinstance(item, EvidenceRef) for item in items):
        raise TypeError("evidence_refs must contain EvidenceRef values")
    ordered = tuple(sorted(items, key=lambda item: item.evidence_id))
    ids = [item.evidence_id for item in ordered]
    if len(set(ids)) != len(ids):
        raise InvalidCompletionClaim(
            "evidence_refs cannot repeat EvidenceRef identity"
        )
    return ordered


def _child_completion_basis(
    values: Iterable[WorkCompletionRef],
) -> tuple[WorkCompletionRef, ...]:
    items = tuple(values)
    if any(not isinstance(item, WorkCompletionRef) for item in items):
        raise TypeError(
            "child_completion_refs must contain WorkCompletionRef values"
        )
    ordered = tuple(sorted(items, key=lambda item: item.work_id))
    work_ids = [item.work_id for item in ordered]
    if len(set(work_ids)) != len(work_ids):
        raise InvalidCompletionClaim(
            "child_completion_refs cannot repeat child Work identity"
        )
    return ordered


@dataclass(frozen=True, slots=True)
class CompletionClaim:
    """Immutable Codexia semantic assertion that one Work objective is satisfied.

    A CompletionClaim is not a Work state transition, completion admission,
    evidence verdict, execution authority, cleanup proof, or child-terminal
    judgment. It records the exact Work/Workflow/Pack checkpoint and the exact
    ArtifactRef/EvidenceRef subset presented as the claim basis.

    Canonical admission must later prove that the referenced
    PackWorkflowBinding and basis records are admitted in the same Work
    chronology and that Pack/domain completion criteria accept them. Creating
    this detached record grants none of those semantics.
    """

    schema_version: int
    claim_id: str
    created_at: str
    work_id: str
    work_digest: str
    work_revision: int
    work_event_digest: str
    workflow_run_id: str
    workflow_run_digest: str
    pack_binding_digest: str
    pack_workflow_binding_id: str
    pack_workflow_binding_digest: str
    summary: str
    artifact_refs: tuple[ArtifactRef, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    child_completion_refs: tuple[WorkCompletionRef, ...]
    claim_digest: str

    @classmethod
    def create(
        cls,
        *,
        snapshot: WorkSnapshot,
        workflow: WorkflowRunSnapshot,
        pack_binding: PackWorkflowBinding,
        summary: str,
        artifact_refs: Iterable[ArtifactRef] = (),
        evidence_refs: Iterable[EvidenceRef] = (),
        child_completion_refs: Iterable[WorkCompletionRef] = (),
        claim_id: str | None = None,
        created_at: str | None = None,
    ) -> CompletionClaim:
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        if snapshot.state is not WorkState.ACTIVE:
            raise InvalidCompletionClaim(
                "CompletionClaim requires active Work"
            )
        if snapshot.last_event_digest is None:
            raise InvalidCompletionClaim(
                "CompletionClaim requires non-empty Work chronology"
            )
        if not isinstance(workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise InvalidCompletionClaim(
                "CompletionClaim requires active WorkflowRun"
            )
        if not isinstance(pack_binding, PackWorkflowBinding):
            raise TypeError("pack_binding must be PackWorkflowBinding")

        run = workflow.run
        if run.work_id != snapshot.work.work_id:
            raise InvalidCompletionClaim(
                "WorkflowRun belongs to another Work"
            )
        if not hmac.compare_digest(
            run.work_digest,
            snapshot.work.work_digest,
        ):
            raise InvalidCompletionClaim(
                "WorkflowRun changed Work binding"
            )
        if pack_binding.work_id != snapshot.work.work_id:
            raise InvalidCompletionClaim(
                "Pack binding belongs to another Work"
            )
        if not hmac.compare_digest(
            pack_binding.work_digest,
            snapshot.work.work_digest,
        ):
            raise InvalidCompletionClaim(
                "Pack binding changed Work binding"
            )
        if pack_binding.workflow_run_id != run.workflow_run_id:
            raise InvalidCompletionClaim(
                "Pack binding belongs to another WorkflowRun"
            )
        if not hmac.compare_digest(
            pack_binding.workflow_run_digest,
            run.run_digest,
        ):
            raise InvalidCompletionClaim(
                "Pack binding changed WorkflowRun binding"
            )
        if snapshot.revision <= pack_binding.start_revision:
            raise InvalidCompletionClaim(
                "CompletionClaim checkpoint precedes durable Pack pin"
            )

        artifacts = _artifact_basis(artifact_refs)
        evidence = _evidence_basis(evidence_refs)
        child_completions = _child_completion_basis(
            child_completion_refs
        )
        summary = _summary(summary)
        claim_id = claim_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(claim_id, "claim_id")
        _timestamp(created_at)

        base = {
            "schema_version": COMPLETION_CLAIM_SCHEMA_VERSION,
            "claim_id": claim_id,
            "created_at": created_at,
            "work_id": snapshot.work.work_id,
            "work_digest": snapshot.work.work_digest,
            "work_revision": snapshot.revision,
            "work_event_digest": snapshot.last_event_digest,
            "workflow_run_id": run.workflow_run_id,
            "workflow_run_digest": run.run_digest,
            "pack_binding_digest": pack_binding.pack.binding_digest,
            "pack_workflow_binding_id": pack_binding.binding_id,
            "pack_workflow_binding_digest": pack_binding.pin_digest,
            "summary": summary,
            "artifact_refs": [item.to_dict() for item in artifacts],
            "evidence_refs": [item.to_dict() for item in evidence],
            "child_completion_refs": [
                item.to_dict() for item in child_completions
            ],
        }
        return cls(
            schema_version=COMPLETION_CLAIM_SCHEMA_VERSION,
            claim_id=claim_id,
            created_at=created_at,
            work_id=snapshot.work.work_id,
            work_digest=snapshot.work.work_digest,
            work_revision=snapshot.revision,
            work_event_digest=snapshot.last_event_digest,
            workflow_run_id=run.workflow_run_id,
            workflow_run_digest=run.run_digest,
            pack_binding_digest=pack_binding.pack.binding_digest,
            pack_workflow_binding_id=pack_binding.binding_id,
            pack_workflow_binding_digest=pack_binding.pin_digest,
            summary=summary,
            artifact_refs=artifacts,
            evidence_refs=evidence,
            child_completion_refs=child_completions,
            claim_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version
            not in _COMPLETION_CLAIM_SUPPORTED_SCHEMA_VERSIONS
        ):
            raise InvalidCompletionClaim(
                "Unsupported CompletionClaim schema"
            )
        _validate_uuid(self.claim_id, "claim_id")
        _timestamp(self.created_at)
        _validate_uuid(self.work_id, "work_id")
        _validate_digest(self.work_digest, "work_digest")
        if type(self.work_revision) is not int or self.work_revision <= 0:
            raise InvalidCompletionClaim(
                "work_revision must be a positive integer"
            )
        _validate_digest(self.work_event_digest, "work_event_digest")
        _validate_uuid(self.workflow_run_id, "workflow_run_id")
        _validate_digest(
            self.workflow_run_digest,
            "workflow_run_digest",
        )
        _validate_digest(
            self.pack_binding_digest,
            "pack_binding_digest",
        )
        _validate_uuid(
            self.pack_workflow_binding_id,
            "pack_workflow_binding_id",
        )
        _validate_digest(
            self.pack_workflow_binding_digest,
            "pack_workflow_binding_digest",
        )
        _summary(self.summary)

        artifacts = _artifact_basis(self.artifact_refs)
        evidence = _evidence_basis(self.evidence_refs)
        child_completions = _child_completion_basis(
            self.child_completion_refs
        )
        if artifacts != tuple(self.artifact_refs):
            raise InvalidCompletionClaim(
                "artifact_refs must be canonical sorted"
            )
        if evidence != tuple(self.evidence_refs):
            raise InvalidCompletionClaim(
                "evidence_refs must be canonical sorted"
            )
        if child_completions != tuple(self.child_completion_refs):
            raise InvalidCompletionClaim(
                "child_completion_refs must be canonical sorted"
            )
        if (
            self.schema_version == COMPLETION_CLAIM_LEGACY_SCHEMA_VERSION
            and child_completions
        ):
            raise InvalidCompletionClaim(
                "CompletionClaim v1 cannot contain child_completion_refs"
            )
        object.__setattr__(self, "artifact_refs", artifacts)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(
            self,
            "child_completion_refs",
            child_completions,
        )

        _validate_digest(self.claim_digest, "claim_digest")
        if not hmac.compare_digest(
            self.claim_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidCompletionClaim(
                "CompletionClaim digest mismatch"
            )

    def _base_dict(self) -> dict[str, Any]:
        base = {
            "schema_version": self.schema_version,
            "claim_id": self.claim_id,
            "created_at": self.created_at,
            "work_id": self.work_id,
            "work_digest": self.work_digest,
            "work_revision": self.work_revision,
            "work_event_digest": self.work_event_digest,
            "workflow_run_id": self.workflow_run_id,
            "workflow_run_digest": self.workflow_run_digest,
            "pack_binding_digest": self.pack_binding_digest,
            "pack_workflow_binding_id": self.pack_workflow_binding_id,
            "pack_workflow_binding_digest": self.pack_workflow_binding_digest,
            "summary": self.summary,
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }
        if self.schema_version >= COMPLETION_CLAIM_SCHEMA_VERSION:
            base["child_completion_refs"] = [
                item.to_dict() for item in self.child_completion_refs
            ]
        return base

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "claim_digest": self.claim_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CompletionClaim:
        if not isinstance(value, Mapping):
            raise InvalidCompletionClaim(
                "CompletionClaim must be a mapping"
            )
        schema_version = value.get("schema_version")
        if type(schema_version) is not int:
            raise InvalidCompletionClaim(
                "Unsupported CompletionClaim schema"
            )

        common = {
            "schema_version",
            "claim_id",
            "created_at",
            "work_id",
            "work_digest",
            "work_revision",
            "work_event_digest",
            "workflow_run_id",
            "workflow_run_digest",
            "pack_binding_digest",
            "pack_workflow_binding_id",
            "pack_workflow_binding_digest",
            "summary",
            "artifact_refs",
            "evidence_refs",
            "claim_digest",
        }
        if schema_version == COMPLETION_CLAIM_LEGACY_SCHEMA_VERSION:
            expected = common
        elif schema_version == COMPLETION_CLAIM_SCHEMA_VERSION:
            expected = common | {"child_completion_refs"}
        else:
            raise InvalidCompletionClaim(
                "Unsupported CompletionClaim schema"
            )
        if set(value) != expected:
            raise InvalidCompletionClaim(
                "CompletionClaim keys are not exact"
            )

        raw_artifacts = value["artifact_refs"]
        raw_evidence = value["evidence_refs"]
        raw_child_completions = (
            []
            if schema_version == COMPLETION_CLAIM_LEGACY_SCHEMA_VERSION
            else value["child_completion_refs"]
        )
        if not isinstance(raw_artifacts, list):
            raise InvalidCompletionClaim(
                "artifact_refs must be a list"
            )
        if not isinstance(raw_evidence, list):
            raise InvalidCompletionClaim(
                "evidence_refs must be a list"
            )
        if not isinstance(raw_child_completions, list):
            raise InvalidCompletionClaim(
                "child_completion_refs must be a list"
            )
        try:
            child_completion_refs = tuple(
                WorkCompletionRef.from_dict(item)
                for item in raw_child_completions
            )
        except (
            InvalidWorkCompletionRef,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise InvalidCompletionClaim(
                "child_completion_refs contains invalid WorkCompletionRef"
            ) from exc

        return cls(
            schema_version=schema_version,
            claim_id=value["claim_id"],
            created_at=value["created_at"],
            work_id=value["work_id"],
            work_digest=value["work_digest"],
            work_revision=value["work_revision"],
            work_event_digest=value["work_event_digest"],
            workflow_run_id=value["workflow_run_id"],
            workflow_run_digest=value["workflow_run_digest"],
            pack_binding_digest=value["pack_binding_digest"],
            pack_workflow_binding_id=value["pack_workflow_binding_id"],
            pack_workflow_binding_digest=value["pack_workflow_binding_digest"],
            summary=value["summary"],
            artifact_refs=tuple(
                ArtifactRef.from_dict(item)
                for item in raw_artifacts
            ),
            evidence_refs=tuple(
                EvidenceRef.from_dict(item)
                for item in raw_evidence
            ),
            child_completion_refs=child_completion_refs,
            claim_digest=value["claim_digest"],
        )
