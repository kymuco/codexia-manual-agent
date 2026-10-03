from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid5

from codexia_manual_agent.artifact_core import (
    ArtifactAdmission,
    ArtifactRef,
    project_artifact_refs,
)
from codexia_manual_agent.attention_core import project_attention_responses
from codexia_manual_agent.evidence_core import (
    EvidenceAdmission,
    EvidenceRef,
    project_evidence_refs,
)
from codexia_manual_agent.pack_core import project_workflow_pack_binding
from codexia_manual_agent.role_core import (
    ContextProjection,
    RoleBinding,
    RoleRunState,
    project_role_runs,
)
from codexia_manual_agent.work_core import WorkEvent, WorkState, WorkStore
from codexia_manual_agent.workflow_core import project_workflow_runs
from codexia_manual_agent.workflow_runtime.research_v1 import (
    CRITIC_ROLE_ID,
    CRITIQUE_EVIDENCE_KIND,
    EVIDENCE_SUFFICIENCY_EVIDENCE_KIND,
    INITIAL_EVIDENCE_KIND,
    NO_MATERIAL_UNCERTAINTY_EVIDENCE_KIND,
    OBJECTIVE_COVERAGE_EVIDENCE_KIND,
    RESEARCHER_ROLE_ID,
    REVISER_ROLE_ID,
    REVISION_EVIDENCE_KIND,
    STAGE_CRITIQUE,
    STAGE_INITIAL,
    STAGE_REVISION,
    STAGE_SYNTHESIS,
    SYNTHESIS_EVIDENCE_KIND,
    SYNTHESIZER_ROLE_ID,
    ResearchRoleOutput,
    research_context_text,
    research_pack_binding,
    research_role_instructions,
    research_workflow_binding,
)

_MATERIAL_NAMESPACE = UUID("08f80958-2577-5be0-bf41-b5fe76566f4c")

_ROLE_META = {
    RESEARCHER_ROLE_ID: (STAGE_INITIAL, INITIAL_EVIDENCE_KIND),
    CRITIC_ROLE_ID: (STAGE_CRITIQUE, CRITIQUE_EVIDENCE_KIND),
    REVISER_ROLE_ID: (STAGE_REVISION, REVISION_EVIDENCE_KIND),
    SYNTHESIZER_ROLE_ID: (STAGE_SYNTHESIS, SYNTHESIS_EVIDENCE_KIND),
}


class ResearchWorkMaterializationError(RuntimeError):
    pass


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


def _canonical_digest(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha_bytes(raw)


@dataclass(frozen=True, slots=True)
class ResearchMaterializationResult:
    evidence_refs: tuple[EvidenceRef, ...]
    artifact_refs: tuple[ArtifactRef, ...]


class ResearchInstructionsMaterialPort:
    def resolve(self, binding: RoleBinding) -> str:
        text = research_role_instructions(binding)
        if _sha_text(text) != binding.instructions_digest:
            raise ResearchWorkMaterializationError(
                "research RoleBinding instructions digest drifted"
            )
        return text


class ResearchContextMaterialPort:
    """Reconstruct exact Role context from durable Work history only."""

    def __init__(self, *, store: WorkStore, work_id: str) -> None:
        self._store = store
        self._work_id = work_id

    def resolve(self, projection: ContextProjection) -> str:
        events = self._store.events(self._work_id)
        snapshot = self._store.snapshot(self._work_id)
        if snapshot.revision != len(events):
            raise ResearchWorkMaterializationError(
                "research context read crossed Work chronology"
            )
        matches = tuple(
            role
            for role in project_role_runs(events)
            if role.run.context == projection
        )
        if len(matches) != 1:
            raise ResearchWorkMaterializationError(
                "ContextProjection does not identify one research RoleRun"
            )
        role = matches[0]
        meta = _ROLE_META.get(role.run.binding.role_id)
        if meta is None:
            raise ResearchWorkMaterializationError(
                "ContextProjection belongs to a non-research RoleBinding"
            )
        stage, _kind = meta
        prefix = events[: role.run.start_revision]
        prior_roles = project_role_runs(prefix)
        prior_outputs_list: list[str] = []
        for item in prior_roles:
            if (
                item.state is not RoleRunState.COMPLETED
                or item.output_text is None
            ):
                continue
            prior_meta = _ROLE_META.get(item.run.binding.role_id)
            if prior_meta is None:
                raise ResearchWorkMaterializationError(
                    "research context contains role outside Pack stages"
                )
            prior_stage, _prior_kind = prior_meta
            prior_outputs_list.append(
                ResearchRoleOutput.parse(
                    item.output_text,
                    expected_stage=prior_stage,
                ).content
            )
        prior_outputs = tuple(prior_outputs_list)
        responses = tuple(
            item.response_text
            for item in project_attention_responses(prefix)
        )
        evidence = tuple(
            item.to_dict() for item in project_evidence_refs(prefix)
        )
        artifacts = tuple(
            item.to_dict() for item in project_artifact_refs(prefix)
        )
        text = research_context_text(
            objective=snapshot.work.objective,
            stage=stage,
            prior_outputs=prior_outputs,
            human_responses=responses,
            evidence_refs=evidence,
            artifact_refs=artifacts,
        )
        if _sha_text(text) != projection.content_digest:
            raise ResearchWorkMaterializationError(
                "reconstructed research context changed ContextProjection digest"
            )
        return text


class ResearchWorkMaterializer:
    """Project durable cognition outcomes into standard Work material refs."""

    def __init__(self, store: WorkStore) -> None:
        self._store = store

    def materialize(self, work_id: str) -> ResearchMaterializationResult:
        events = self._store.events(work_id)
        snapshot = self._store.snapshot(work_id)
        if snapshot.state is not WorkState.ACTIVE:
            raise ResearchWorkMaterializationError(
                "research materialization requires active Work"
            )
        workflows = project_workflow_runs(events)
        if len(workflows) != 1:
            raise ResearchWorkMaterializationError(
                "research Work must contain one WorkflowRun"
            )
        workflow = workflows[0]
        if workflow.run.binding != research_workflow_binding():
            raise ResearchWorkMaterializationError(
                "Work is not bound to Research Work Pack workflow"
            )
        pin = project_workflow_pack_binding(
            events,
            workflow.run.workflow_run_id,
        )
        if pin is None or pin.pack != research_pack_binding():
            raise ResearchWorkMaterializationError(
                "Research Work Pack pin is missing or changed"
            )

        event_by_id = {event.event_id: event for event in events}
        for role in project_role_runs(events):
            if role.state is not RoleRunState.COMPLETED:
                continue
            meta = _ROLE_META.get(role.run.binding.role_id)
            if meta is None:
                raise ResearchWorkMaterializationError(
                    "research Work contains role outside Pack stages"
                )
            stage, evidence_kind = meta
            if role.terminal_event_id is None or role.output_text is None:
                raise ResearchWorkMaterializationError(
                    "completed research RoleRun lacks terminal output"
                )
            terminal = event_by_id.get(role.terminal_event_id)
            if terminal is None:
                raise ResearchWorkMaterializationError(
                    "research RoleRun terminal event is unavailable"
                )
            parsed = ResearchRoleOutput.parse(
                role.output_text,
                expected_stage=stage,
            )
            locator = f"work-event://{work_id}/{terminal.event_id}"
            evidence = EvidenceRef.create(
                evidence_id=terminal.event_id,
                evidence_digest=terminal.event_digest,
                evidence_kind=evidence_kind,
                locator=locator,
            )
            EvidenceAdmission(self._store).record(
                self._store.snapshot(work_id),
                evidence,
            )

            if stage == STAGE_SYNTHESIS:
                self._materialize_synthesis(
                    work_id=work_id,
                    terminal=terminal,
                    output=parsed,
                )

        return ResearchMaterializationResult(
            evidence_refs=project_evidence_refs(self._store.events(work_id)),
            artifact_refs=project_artifact_refs(self._store.events(work_id)),
        )

    def _materialize_synthesis(
        self,
        *,
        work_id: str,
        terminal: WorkEvent,
        output: ResearchRoleOutput,
    ) -> None:
        raw = output.content.encode("utf-8")
        artifact = ArtifactRef.create(
            artifact_id=str(
                uuid5(_MATERIAL_NAMESPACE, f"artifact:{terminal.event_id}")
            ),
            content_sha256=_sha_bytes(raw),
            size_bytes=len(raw),
            locator=(
                f"work-event://{work_id}/{terminal.event_id}#content"
            ),
            media_type="text/markdown",
        )
        ArtifactAdmission(self._store).record(
            self._store.snapshot(work_id),
            artifact,
        )

        dimensions = (
            (
                "objective_coverage",
                output.objective_coverage,
                OBJECTIVE_COVERAGE_EVIDENCE_KIND,
                True,
            ),
            (
                "evidence_sufficiency",
                output.evidence_sufficiency,
                EVIDENCE_SUFFICIENCY_EVIDENCE_KIND,
                True,
            ),
            (
                "material_unresolved_uncertainty",
                output.material_unresolved_uncertainty,
                NO_MATERIAL_UNCERTAINTY_EVIDENCE_KIND,
                False,
            ),
        )
        for field, value, kind, required in dimensions:
            if value is not required:
                continue
            evidence_id = str(
                uuid5(
                    _MATERIAL_NAMESPACE,
                    f"assessment:{terminal.event_id}:{field}:{value}",
                )
            )
            digest = _canonical_digest(
                {
                    "schema_version": 1,
                    "source_event_digest": terminal.event_digest,
                    "field": field,
                    "value": value,
                }
            )
            assessment = EvidenceRef.create(
                evidence_id=evidence_id,
                evidence_digest=digest,
                evidence_kind=kind,
                locator=(
                    f"work-event://{work_id}/{terminal.event_id}#{field}"
                ),
            )
            EvidenceAdmission(self._store).record(
                self._store.snapshot(work_id),
                assessment,
            )
