from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from codexia_manual_agent.attention_core import AttentionNeed
from codexia_manual_agent.completion_core import (
    CompletionClaim,
    CompletionCriterionContext,
    CompletionCriterionResult,
)
from codexia_manual_agent.pack_core import (
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
)
from codexia_manual_agent.role_core import (
    ContextProjection,
    RoleBinding,
    RoleRun,
    RoleRunState,
)
from codexia_manual_agent.workflow_core import WorkflowBinding
from codexia_manual_agent.workflow_runtime.boundary import (
    WorkflowImplementationStateError,
    WorkflowStepContext,
)

RESEARCH_WORKFLOW_ID = "codexia:research-work"
RESEARCH_WORKFLOW_VERSION = "1.0.0"
RESEARCH_PACK_ID = "codexia:research-work-pack"
RESEARCH_PACK_VERSION = "1.0.0"

RESEARCHER_ROLE_ID = "codexia:research-researcher"
CRITIC_ROLE_ID = "codexia:research-critic"
REVISER_ROLE_ID = "codexia:research-reviser"
SYNTHESIZER_ROLE_ID = "codexia:research-synthesizer"
RESEARCH_ROLE_VERSION = "1.0.0"

STAGE_INITIAL = "initial"
STAGE_CRITIQUE = "critique"
STAGE_REVISION = "revision"
STAGE_SYNTHESIS = "synthesis"

RESEARCH_COMPLETION_SUMMARY = (
    "Research Work has evidence-backed critique, revision, and final synthesis."
)

INITIAL_EVIDENCE_KIND = "codexia.research.initial-output.v1"
CRITIQUE_EVIDENCE_KIND = "codexia.research.critique-output.v1"
REVISION_EVIDENCE_KIND = "codexia.research.revision-output.v1"
SYNTHESIS_EVIDENCE_KIND = "codexia.research.synthesis-output.v1"
OBJECTIVE_COVERAGE_EVIDENCE_KIND = "codexia.research.objective-coverage.complete.v1"
EVIDENCE_SUFFICIENCY_EVIDENCE_KIND = "codexia.research.evidence-sufficiency.sufficient.v1"
NO_MATERIAL_UNCERTAINTY_EVIDENCE_KIND = (
    "codexia.research.material-uncertainty.none.v1"
)

_ROLE_SEQUENCE = (
    (STAGE_INITIAL, RESEARCHER_ROLE_ID),
    (STAGE_CRITIQUE, CRITIC_ROLE_ID),
    (STAGE_REVISION, REVISER_ROLE_ID),
    (STAGE_SYNTHESIS, SYNTHESIZER_ROLE_ID),
)

_REQUIRED_EVIDENCE_KINDS = frozenset(
    {
        INITIAL_EVIDENCE_KIND,
        CRITIQUE_EVIDENCE_KIND,
        REVISION_EVIDENCE_KIND,
        SYNTHESIS_EVIDENCE_KIND,
        OBJECTIVE_COVERAGE_EVIDENCE_KIND,
        EVIDENCE_SUFFICIENCY_EVIDENCE_KIND,
        NO_MATERIAL_UNCERTAINTY_EVIDENCE_KIND,
    }
)

_INSTRUCTIONS = {
    RESEARCHER_ROLE_ID: (
        "Develop an initial research analysis of the delegated objective. "
        "Return exactly one codexia.research.role-output.v1 JSON object for "
        "stage=initial. Identify claims and uncertainty in content. Set "
        "needs_human=true only when a material objective/scope choice cannot "
        "be resolved from supplied context; otherwise keep it false."
    ),
    CRITIC_ROLE_ID: (
        "Critically challenge the initial research analysis. Return exactly "
        "one codexia.research.role-output.v1 JSON object for stage=critique. "
        "Identify weak support, contradictions, omissions, and uncertainty. "
        "Do not ask the human to schedule ordinary research iteration."
    ),
    REVISER_ROLE_ID: (
        "Revise the research analysis in response to the critique and any "
        "durable human clarification. Return exactly one "
        "codexia.research.role-output.v1 JSON object for stage=revision. "
        "Strengthen or explicitly qualify weak claims."
    ),
    SYNTHESIZER_ROLE_ID: (
        "Synthesize the revised research into final Markdown-ready content. "
        "Return exactly one codexia.research.role-output.v1 JSON object for "
        "stage=synthesis and explicitly judge objective_coverage, "
        "evidence_sufficiency, and material_unresolved_uncertainty."
    ),
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class ResearchRoleOutput:
    schema_version: int
    stage: str
    content: str
    needs_human: bool
    human_question: str | None
    human_reason: str | None
    objective_coverage: bool | None
    evidence_sufficiency: bool | None
    material_unresolved_uncertainty: bool | None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported research role-output schema")
        if self.stage not in {item[0] for item in _ROLE_SEQUENCE}:
            raise ValueError("unsupported research role-output stage")
        if (
            type(self.content) is not str
            or not self.content.strip()
            or len(self.content) > 100_000
        ):
            raise ValueError("research role output content must be bounded text")
        if type(self.needs_human) is not bool:
            raise TypeError("needs_human must be bool")
        if self.needs_human:
            if (
                type(self.human_question) is not str
                or not self.human_question.strip()
                or type(self.human_reason) is not str
                or not self.human_reason.strip()
            ):
                raise ValueError(
                    "human question/reason are required when needs_human=true"
                )
        elif self.human_question is not None or self.human_reason is not None:
            raise ValueError(
                "human question/reason must be null when needs_human=false"
            )
        if self.stage == STAGE_SYNTHESIS:
            for name, value in (
                ("objective_coverage", self.objective_coverage),
                ("evidence_sufficiency", self.evidence_sufficiency),
                (
                    "material_unresolved_uncertainty",
                    self.material_unresolved_uncertainty,
                ),
            ):
                if type(value) is not bool:
                    raise TypeError(f"{name} must be bool for synthesis")
        elif any(
            value is not None
            for value in (
                self.objective_coverage,
                self.evidence_sufficiency,
                self.material_unresolved_uncertainty,
            )
        ):
            raise ValueError(
                "completion judgments are only valid for synthesis output"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "content": self.content,
            "needs_human": self.needs_human,
            "human_question": self.human_question,
            "human_reason": self.human_reason,
            "objective_coverage": self.objective_coverage,
            "evidence_sufficiency": self.evidence_sufficiency,
            "material_unresolved_uncertainty": (
                self.material_unresolved_uncertainty
            ),
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        expected_stage: str | None = None,
    ) -> ResearchRoleOutput:
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("research role output is not valid JSON") from exc
        expected = {
            "schema_version",
            "stage",
            "content",
            "needs_human",
            "human_question",
            "human_reason",
            "objective_coverage",
            "evidence_sufficiency",
            "material_unresolved_uncertainty",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("research role output keys are not exact")
        result = cls(**value)
        if expected_stage is not None and result.stage != expected_stage:
            raise ValueError(
                "research role output stage differs from expected stage"
            )
        return result


def research_workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=RESEARCH_WORKFLOW_ID,
        version=RESEARCH_WORKFLOW_VERSION,
        definition_digest=_sha("research-workflow-v1"),
    )


def research_role_bindings() -> tuple[RoleBinding, ...]:
    return tuple(
        RoleBinding.create(
            role_id=role_id,
            version=RESEARCH_ROLE_VERSION,
            instructions_digest=_sha(_INSTRUCTIONS[role_id]),
        )
        for _stage, role_id in _ROLE_SEQUENCE
    )


def research_pack_binding() -> PackBinding:
    workflow = research_workflow_binding()
    members = [
        PackMemberBinding.create(
            kind=PackMemberKind.WORKFLOW,
            semantic_id=workflow.workflow_id,
            version=workflow.version,
            binding_digest=workflow.binding_digest,
        )
    ]
    for role in research_role_bindings():
        members.append(
            PackMemberBinding.create(
                kind=PackMemberKind.ROLE,
                semantic_id=role.role_id,
                version=role.version,
                binding_digest=role.binding_digest,
            )
        )
    return PackBinding.create(
        pack_id=RESEARCH_PACK_ID,
        version=RESEARCH_PACK_VERSION,
        definition_digest=_sha("research-work-pack-v1"),
        members=members,
    )


def research_role_instructions(binding: RoleBinding) -> str:
    for role in research_role_bindings():
        if role == binding:
            return _INSTRUCTIONS[role.role_id]
    raise KeyError("unknown Research Work Pack RoleBinding")


def _role_output(snapshot, expected_stage: str) -> ResearchRoleOutput:
    if snapshot.state is not RoleRunState.COMPLETED:
        raise WorkflowImplementationStateError(
            f"research role {expected_stage} is not completed"
        )
    if snapshot.output_text is None:
        raise WorkflowImplementationStateError(
            f"research role {expected_stage} has no output"
        )
    try:
        return ResearchRoleOutput.parse(
            snapshot.output_text,
            expected_stage=expected_stage,
        )
    except ValueError as exc:
        raise WorkflowImplementationStateError(
            f"research role {expected_stage} output is invalid"
        ) from exc


def research_context_text(
    *,
    objective: str,
    stage: str,
    prior_outputs: tuple[str, ...],
    human_responses: tuple[str, ...],
    evidence_refs: tuple[dict[str, Any], ...] = (),
    artifact_refs: tuple[dict[str, Any], ...] = (),
) -> str:
    return _canonical_json(
        {
            "schema_version": 1,
            "stage": stage,
            "objective": objective,
            "prior_outputs": list(prior_outputs),
            "human_responses": list(human_responses),
            "evidence_refs": list(evidence_refs),
            "artifact_refs": list(artifact_refs),
        }
    )


class ResearchWorkflowImplementationV1:
    def __init__(self) -> None:
        self._binding = research_workflow_binding()
        self._roles = {
            role.role_id: role for role in research_role_bindings()
        }

    @property
    def binding(self) -> WorkflowBinding:
        return self._binding

    def propose(self, context: WorkflowStepContext):
        if context.workflow.run.binding != self._binding:
            raise WorkflowImplementationStateError(
                "research workflow received another WorkflowBinding"
            )
        if len(context.roles) > len(_ROLE_SEQUENCE):
            raise WorkflowImplementationStateError(
                "research workflow has too many RoleRuns"
            )

        parsed: list[ResearchRoleOutput] = []
        prior_text: list[str] = []
        for index, snapshot in enumerate(context.roles):
            stage, role_id = _ROLE_SEQUENCE[index]
            if snapshot.run.binding != self._roles[role_id]:
                raise WorkflowImplementationStateError(
                    "research RoleRun order/binding changed"
                )
            output = _role_output(snapshot, stage)
            if index > 0 and output.needs_human:
                raise WorkflowImplementationStateError(
                    "Research Work Pack v1 only allows human judgment after "
                    "initial research"
                )
            parsed.append(output)
            assert snapshot.output_text is not None
            prior_text.append(snapshot.output_text)

        if parsed:
            initial = parsed[0]
            if initial.needs_human:
                if len(context.attentions) > 1:
                    raise WorkflowImplementationStateError(
                        "research workflow has multiple AttentionNeed records"
                    )
                if not context.attentions:
                    return AttentionNeed.create(
                        workflow=context.workflow,
                        snapshot=context.work,
                        question=initial.human_question or "Research clarification?",
                        reason=initial.human_reason or "Human judgment is required.",
                    )
                attention = context.attentions[0]
                responses = tuple(
                    response
                    for response in context.attention_responses
                    if response.attention_id == attention.attention_id
                )
                if not responses:
                    return None
                if len(responses) != 1:
                    raise WorkflowImplementationStateError(
                        "research AttentionNeed has ambiguous multiple responses"
                    )
            elif context.attentions or context.attention_responses:
                raise WorkflowImplementationStateError(
                    "research workflow has unexpected human-attention chronology"
                )

        if len(context.roles) < len(_ROLE_SEQUENCE):
            stage, role_id = _ROLE_SEQUENCE[len(context.roles)]
            context_text = research_context_text(
                objective=context.work.work.objective,
                stage=stage,
                prior_outputs=tuple(prior_text),
                human_responses=tuple(
                    response.response_text
                    for response in context.attention_responses
                ),
                evidence_refs=tuple(
                    evidence.to_dict() for evidence in context.evidence_refs
                ),
                artifact_refs=tuple(
                    artifact.to_dict() for artifact in context.artifacts
                ),
            )
            projection = ContextProjection.create(
                content_digest=_sha(context_text)
            )
            return RoleRun.create(
                workflow=context.workflow,
                snapshot=context.work,
                binding=self._roles[role_id],
                context=projection,
            )

        synthesis = parsed[-1]
        if synthesis.stage != STAGE_SYNTHESIS:
            raise WorkflowImplementationStateError(
                "research workflow lacks synthesis output"
            )
        if not (
            synthesis.objective_coverage is True
            and synthesis.evidence_sufficiency is True
            and synthesis.material_unresolved_uncertainty is False
        ):
            return None

        kinds = {item.evidence_kind for item in context.evidence_refs}
        if not _REQUIRED_EVIDENCE_KINDS.issubset(kinds):
            return None
        synthesis_roles = [
            item
            for item in context.evidence_refs
            if item.evidence_kind == SYNTHESIS_EVIDENCE_KIND
        ]
        if len(synthesis_roles) != 1:
            raise WorkflowImplementationStateError(
                "research synthesis evidence is not exact"
            )
        synthesis_event_id = synthesis_roles[0].evidence_id
        artifacts = tuple(
            artifact
            for artifact in context.artifacts
            if artifact.locator
            == (
                f"work-event://{context.work.work.work_id}/"
                f"{synthesis_event_id}#content"
            )
        )
        if len(artifacts) != 1:
            return None

        return CompletionClaim.create(
            snapshot=context.work,
            workflow=context.workflow,
            pack_binding=context.pack_binding,
            summary=RESEARCH_COMPLETION_SUMMARY,
            artifact_refs=artifacts,
            evidence_refs=context.evidence_refs,
        )


class ResearchCompletionCriterionV1:
    def __init__(self) -> None:
        self._binding = research_workflow_binding()

    @property
    def binding(self) -> WorkflowBinding:
        return self._binding

    def evaluate(
        self,
        context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        claim = context.claim
        if claim.summary != RESEARCH_COMPLETION_SUMMARY:
            return CompletionCriterionResult(
                accepted=False,
                reason="Research completion summary changed Pack semantics.",
            )
        if len(claim.artifact_refs) != 1:
            return CompletionCriterionResult(
                accepted=False,
                reason="Research completion requires one final synthesis artifact.",
            )
        artifact = claim.artifact_refs[0]
        if artifact.media_type != "text/markdown":
            return CompletionCriterionResult(
                accepted=False,
                reason="Research synthesis artifact must be Markdown text.",
            )

        by_kind: dict[str, list] = {}
        for evidence in claim.evidence_refs:
            by_kind.setdefault(evidence.evidence_kind, []).append(evidence)
        if any(len(by_kind.get(kind, ())) != 1 for kind in _REQUIRED_EVIDENCE_KINDS):
            return CompletionCriterionResult(
                accepted=False,
                reason="Research completion lacks exact required evidence basis.",
            )

        synthesis = by_kind[SYNTHESIS_EVIDENCE_KIND][0]
        expected_evidence_locator = (
            f"work-event://{claim.work_id}/{synthesis.evidence_id}"
        )
        if synthesis.locator != expected_evidence_locator:
            return CompletionCriterionResult(
                accepted=False,
                reason="Synthesis evidence locator changed durable role provenance.",
            )
        expected_artifact_locator = expected_evidence_locator + "#content"
        if artifact.locator != expected_artifact_locator:
            return CompletionCriterionResult(
                accepted=False,
                reason="Final artifact is not bound to synthesis role output.",
            )

        return CompletionCriterionResult(
            accepted=True,
            reason=(
                "Research completion has critique, revision, final synthesis, "
                "objective coverage, evidence sufficiency, and no material "
                "unresolved uncertainty."
            ),
        )


RESEARCH_REQUIRED_EVIDENCE_KINDS = tuple(sorted(_REQUIRED_EVIDENCE_KINDS))
