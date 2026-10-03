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
RESEARCH_WORKFLOW_VERSION = "1.1.0"
RESEARCH_PACK_ID = "codexia:research-work-pack"
RESEARCH_PACK_VERSION = "1.1.0"

RESEARCHER_ROLE_ID = "codexia:research-researcher"
CRITIC_ROLE_ID = "codexia:research-critic"
REVISER_ROLE_ID = "codexia:research-reviser"
SYNTHESIZER_ROLE_ID = "codexia:research-synthesizer"
RESEARCH_ROLE_VERSION = "1.1.0"

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
EVIDENCE_SUFFICIENCY_EVIDENCE_KIND = (
    "codexia.research.evidence-sufficiency.sufficient.v1"
)
NO_MATERIAL_UNCERTAINTY_EVIDENCE_KIND = "codexia.research.material-uncertainty.none.v1"

_ROLE_SEQUENCE = (
    (STAGE_INITIAL, RESEARCHER_ROLE_ID),
    (STAGE_CRITIQUE, CRITIC_ROLE_ID),
    (STAGE_REVISION, REVISER_ROLE_ID),
    (STAGE_SYNTHESIS, SYNTHESIZER_ROLE_ID),
)


RESEARCH_CONTROL_START = "<<<CODEXIA_CONTROL_V1>>>"
RESEARCH_CONTROL_END = "<<<END_CODEXIA_CONTROL_V1>>>"


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
        "Develop an initial research analysis of the delegated objective as "
        "ordinary text. Identify claims, support, caveats, and uncertainty. "
        "Do not wrap the research content in a JSON envelope. If and only if a "
        "material human-owned objective or scope choice is required, append this "
        "small control trailer after the research content:\n"
        f"{RESEARCH_CONTROL_START}\n"
        '{"schema_version":1,"needs_human":true,'
        '"human_question":"...","human_reason":"..."}\n'
        f"{RESEARCH_CONTROL_END}\n"
        "If no genuine human judgment is needed, omit the control trailer."
    ),
    CRITIC_ROLE_ID: (
        "Critically challenge the initial research analysis as ordinary text. "
        "Identify weak support, contradictions, omissions, and uncertainty. "
        "Do not wrap the critique in a JSON envelope and do not ask the human "
        "to schedule ordinary research iteration."
    ),
    REVISER_ROLE_ID: (
        "Revise the research analysis in response to the critique and any "
        "durable human clarification. Return the revised analysis as ordinary "
        "text, strengthening or explicitly qualifying weak claims. Do not wrap "
        "the revision in a JSON envelope."
    ),
    SYNTHESIZER_ROLE_ID: (
        "Synthesize the revised research into final Markdown-ready content. "
        "Write the useful final artifact first as ordinary Markdown, not inside "
        "a JSON envelope. Then append exactly one small control trailer:\n"
        f"{RESEARCH_CONTROL_START}\n"
        '{"schema_version":1,"objective_coverage_complete":true,'
        '"evidence_sufficient":true,"material_uncertainty_resolved":true}\n'
        f"{RESEARCH_CONTROL_END}\n"
        "Set those three booleans according to the synthesis. The trailer is "
        "workflow control metadata and is not part of the final artifact."
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
        if self.stage != STAGE_INITIAL and self.needs_human:
            raise ValueError("only initial research may request human judgment")
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

        judgments = (
            self.objective_coverage,
            self.evidence_sufficiency,
            self.material_unresolved_uncertainty,
        )
        if self.stage == STAGE_SYNTHESIS:
            all_missing = all(value is None for value in judgments)
            all_boolean = all(type(value) is bool for value in judgments)
            if not (all_missing or all_boolean):
                raise TypeError(
                    "synthesis completion judgments must be all booleans or all null"
                )
        elif any(value is not None for value in judgments):
            raise ValueError(
                "completion judgments are only valid for synthesis output"
            )

    def to_text(self) -> str:
        content = self.content.strip()
        payload: dict[str, Any] | None = None
        if self.stage == STAGE_INITIAL and self.needs_human:
            payload = {
                "schema_version": 1,
                "needs_human": True,
                "human_question": self.human_question,
                "human_reason": self.human_reason,
            }
        elif (
            self.stage == STAGE_SYNTHESIS
            and type(self.objective_coverage) is bool
            and type(self.evidence_sufficiency) is bool
            and type(self.material_unresolved_uncertainty) is bool
        ):
            payload = {
                "schema_version": 1,
                "objective_coverage_complete": self.objective_coverage,
                "evidence_sufficient": self.evidence_sufficiency,
                "material_uncertainty_resolved": (
                    not self.material_unresolved_uncertainty
                ),
            }

        if payload is None:
            return content
        return (
            f"{content}\n\n{RESEARCH_CONTROL_START}\n"
            f"{_canonical_json(payload)}\n"
            f"{RESEARCH_CONTROL_END}"
        )

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        expected_stage: str | None = None,
    ) -> ResearchRoleOutput:
        if expected_stage not in {item[0] for item in _ROLE_SEQUENCE}:
            raise ValueError("expected research stage is required")
        if type(text) is not str or not text.strip():
            raise ValueError("research role output must contain text")

        stage = expected_stage
        content = text.strip()
        payload: dict[str, Any] | None = None
        if stage in {STAGE_INITIAL, STAGE_SYNTHESIS}:
            content, payload = _split_control_trailer(content)

        needs_human = False
        human_question = None
        human_reason = None
        coverage = None
        sufficient = None
        unresolved = None

        if stage == STAGE_INITIAL and _control_schema_is_supported(payload):
            requested = payload.get("needs_human")
            if requested is True:
                question = payload.get("human_question")
                reason = payload.get("human_reason")
                if (
                    type(question) is str
                    and question.strip()
                    and type(reason) is str
                    and reason.strip()
                ):
                    needs_human = True
                    human_question = question.strip()
                    human_reason = reason.strip()
        elif stage == STAGE_SYNTHESIS and _control_schema_is_supported(payload):
            raw_coverage = payload.get("objective_coverage_complete")
            raw_sufficient = payload.get("evidence_sufficient")
            raw_resolved = payload.get("material_uncertainty_resolved")
            if all(
                type(value) is bool
                for value in (raw_coverage, raw_sufficient, raw_resolved)
            ):
                coverage = raw_coverage
                sufficient = raw_sufficient
                unresolved = not raw_resolved

        return cls(
            schema_version=1,
            stage=stage,
            content=content,
            needs_human=needs_human,
            human_question=human_question,
            human_reason=human_reason,
            objective_coverage=coverage,
            evidence_sufficiency=sufficient,
            material_unresolved_uncertainty=unresolved,
        )


def _control_schema_is_supported(payload: dict[str, Any] | None) -> bool:
    return payload is not None and payload.get("schema_version") == 1


def _split_control_trailer(text: str) -> tuple[str, dict[str, Any] | None]:
    stripped = text.strip()
    if not stripped.endswith(RESEARCH_CONTROL_END):
        return stripped, None

    end = len(stripped) - len(RESEARCH_CONTROL_END)
    start = stripped.rfind(RESEARCH_CONTROL_START, 0, end)
    if start < 0:
        return stripped, None

    content = stripped[:start].rstrip()
    if not content:
        raise ValueError("research role output content must be non-empty")
    raw = stripped[start + len(RESEARCH_CONTROL_START) : end].strip()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return content, None
    if not isinstance(payload, dict):
        return content, None
    return content, payload


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
        self._roles = {role.role_id: role for role in research_role_bindings()}

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
            prior_text.append(output.content)

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
                    response.response_text for response in context.attention_responses
                ),
                evidence_refs=tuple(
                    evidence.to_dict() for evidence in context.evidence_refs
                ),
                artifact_refs=tuple(
                    artifact.to_dict() for artifact in context.artifacts
                ),
            )
            projection = ContextProjection.create(content_digest=_sha(context_text))
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
                f"work-event://{context.work.work.work_id}/{synthesis_event_id}#content"
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
