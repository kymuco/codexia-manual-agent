from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping
from uuid import uuid4

from codexia_manual_agent.work.admission import (
    ContinuationAdmission,
    ContinuationDecision,
    ContinuationProposal,
)
from codexia_manual_agent.work.contracts import (
    MAX_ACTOR_CHARS,
    MAX_ATTENTION_REASON_CHARS,
    MAX_ATTENTION_RESPONSE_CHARS,
    AttentionAssessment,
    AttentionUrgency,
    InvalidWorkRecordError,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
    _bounded_text,
    _digest,
    _exact_keys,
    _new_timestamp,
    _normalize_actor_kind,
    _normalize_attention_urgency,
    _optional_bounded_text,
    _validate_digest,
    _validate_timestamp,
    _validate_uuid,
)

ATTENTION_CONSTRAINT_CHECK_SCHEMA_VERSION = 1
DYNAMIC_ATTENTION_CONTEXT_SCHEMA_VERSION = 1
DYNAMIC_ATTENTION_DECISION_SCHEMA_VERSION = 1
MAX_DYNAMIC_ATTENTION_BASIS = 64


class AttentionReversibility(StrEnum):
    """How difficult the contemplated work is to undo semantically."""

    REVERSIBLE = "reversible"
    BOUNDED = "bounded"
    COSTLY = "costly"
    IRREVERSIBLE = "irreversible"


class AttentionTrajectoryImpact(StrEnum):
    """How broadly the contemplated work may change the delegated trajectory."""

    ROUTINE = "routine"
    LOCAL = "local"
    MATERIAL = "material"
    DIRECTIONAL = "directional"


class AttentionAlternativeShape(StrEnum):
    """Whether meaningful alternatives exist at the current checkpoint."""

    NONE = "none"
    EQUIVALENT = "equivalent"
    MATERIAL = "material"


class AttentionConstraintStatus(StrEnum):
    """Codexia's explicit evaluation of one HUMAN attention constraint."""

    CLEAR = "clear"
    TRIGGERED = "triggered"
    UNCERTAIN = "uncertain"


class AttentionDisposition(StrEnum):
    """Attention-only recommendation; never an execution permission."""

    KEEP_MOVING = "keep_moving"
    ASK_HUMAN = "ask_human"


class AttentionOverride(StrEnum):
    """Hard source that forces human attention despite cognitive preference."""

    NONE = "none"
    EXPLICIT_HUMAN_CONSTRAINT = "explicit_human_constraint"
    ADMISSION_REQUIRES_HUMAN = "admission_requires_human"


def _normalize_reversibility(
    value: AttentionReversibility | str,
) -> AttentionReversibility:
    try:
        return AttentionReversibility(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported attention reversibility") from exc


def _normalize_trajectory_impact(
    value: AttentionTrajectoryImpact | str,
) -> AttentionTrajectoryImpact:
    try:
        return AttentionTrajectoryImpact(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported attention trajectory impact") from exc


def _normalize_alternative_shape(
    value: AttentionAlternativeShape | str,
) -> AttentionAlternativeShape:
    try:
        return AttentionAlternativeShape(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported attention alternative shape") from exc


def _normalize_constraint_status(
    value: AttentionConstraintStatus | str,
) -> AttentionConstraintStatus:
    try:
        return AttentionConstraintStatus(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported attention constraint status") from exc


def _normalize_disposition(
    value: AttentionDisposition | str,
) -> AttentionDisposition:
    try:
        return AttentionDisposition(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported attention disposition") from exc


def _normalize_override(value: AttentionOverride | str) -> AttentionOverride:
    try:
        return AttentionOverride(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported attention override") from exc


def _normalize_admission_decision(
    value: ContinuationDecision | str,
) -> ContinuationDecision:
    try:
        return ContinuationDecision(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported bound admission decision") from exc


@dataclass(frozen=True, slots=True)
class AttentionConstraintCheck:
    """Complete, digest-bound evaluation of one explicit HUMAN attention rule."""

    schema_version: int
    statement_digest: str
    status: AttentionConstraintStatus
    reason: str
    check_digest: str

    @classmethod
    def create(
        cls,
        *,
        constraint: WorkStatement,
        status: AttentionConstraintStatus | str,
        reason: str,
    ) -> AttentionConstraintCheck:
        if not isinstance(constraint, WorkStatement):
            raise InvalidWorkRecordError("constraint must be a WorkStatement")
        if constraint.author_kind is not WorkActorKind.HUMAN:
            raise InvalidWorkRecordError(
                "Attention constraint check must preserve HUMAN source provenance"
            )
        status = _normalize_constraint_status(status)
        reason = _bounded_text(reason, "reason", MAX_ATTENTION_REASON_CHARS)
        base = {
            "schema_version": ATTENTION_CONSTRAINT_CHECK_SCHEMA_VERSION,
            "statement_digest": constraint.statement_digest,
            "status": status.value,
            "reason": reason,
        }
        return cls(
            schema_version=ATTENTION_CONSTRAINT_CHECK_SCHEMA_VERSION,
            statement_digest=constraint.statement_digest,
            status=status,
            reason=reason,
            check_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != ATTENTION_CONSTRAINT_CHECK_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported attention constraint check schema")
        _validate_digest(self.statement_digest, "statement_digest")
        object.__setattr__(
            self,
            "status",
            _normalize_constraint_status(self.status),
        )
        object.__setattr__(
            self,
            "reason",
            _bounded_text(self.reason, "reason", MAX_ATTENTION_REASON_CHARS),
        )
        _validate_digest(self.check_digest, "check_digest")
        if not hmac.compare_digest(self.check_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "Attention constraint check digest does not match its exact judgment"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "statement_digest": self.statement_digest,
            "status": self.status.value,
            "reason": self.reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "check_digest": self.check_digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AttentionConstraintCheck:
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "statement_digest",
                "status",
                "reason",
                "check_digest",
            },
            "AttentionConstraintCheck",
        )
        return cls(
            schema_version=value["schema_version"],
            statement_digest=value["statement_digest"],
            status=value["status"],
            reason=value["reason"],
            check_digest=value["check_digest"],
        )


@dataclass(frozen=True, slots=True)
class DynamicAttentionContext:
    """Exact Codexia-authored context for deciding whether human attention is needed."""

    schema_version: int
    context_id: str
    created_at: str
    handoff_id: str
    handoff_digest: str
    interpretation_id: str
    interpretation_digest: str
    proposal_id: str
    proposal_digest: str
    proposal_statement_digest: str
    admission_id: str
    admission_digest: str
    admission_decision: ContinuationDecision
    checkpoint_digest: str
    context_builder_kind: WorkActorKind
    context_builder: str
    basis_statements: tuple[WorkStatement, ...]
    reversibility: AttentionReversibility
    trajectory_impact: AttentionTrajectoryImpact
    alternatives: AttentionAlternativeShape
    attention_constraint_checks: tuple[AttentionConstraintCheck, ...]
    context_digest: str

    @classmethod
    def create(
        cls,
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        proposal: ContinuationProposal,
        admission: ContinuationAdmission,
        context_builder_kind: WorkActorKind | str,
        context_builder: str,
        basis_statements: Iterable[WorkStatement],
        reversibility: AttentionReversibility | str,
        trajectory_impact: AttentionTrajectoryImpact | str,
        alternatives: AttentionAlternativeShape | str,
        attention_constraint_checks: Iterable[AttentionConstraintCheck] = (),
        context_id: str | None = None,
        created_at: str | None = None,
    ) -> DynamicAttentionContext:
        if not isinstance(handoff, WorkHandoff):
            raise InvalidWorkRecordError("handoff must be a WorkHandoff")
        if not isinstance(interpretation, WorkIntentInterpretation):
            raise InvalidWorkRecordError(
                "interpretation must be a WorkIntentInterpretation"
            )
        if not isinstance(proposal, ContinuationProposal):
            raise InvalidWorkRecordError("proposal must be a ContinuationProposal")
        if not isinstance(admission, ContinuationAdmission):
            raise InvalidWorkRecordError("admission must be a ContinuationAdmission")
        admission.assert_binds(handoff, interpretation, proposal)

        builder_kind = _normalize_actor_kind(context_builder_kind)
        if builder_kind is not WorkActorKind.CODEXIA:
            raise InvalidWorkRecordError(
                "Dynamic attention context must preserve explicit Codexia authorship"
            )
        context_builder = _bounded_text(
            context_builder,
            "context_builder",
            MAX_ACTOR_CHARS,
        )
        basis = tuple(basis_statements)
        checks = tuple(attention_constraint_checks)
        cls._validate_basis(proposal, basis)
        cls._validate_constraint_coverage(handoff, checks)

        reversibility = _normalize_reversibility(reversibility)
        trajectory_impact = _normalize_trajectory_impact(trajectory_impact)
        alternatives = _normalize_alternative_shape(alternatives)
        context_id = context_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        base = {
            "schema_version": DYNAMIC_ATTENTION_CONTEXT_SCHEMA_VERSION,
            "context_id": context_id,
            "created_at": created_at,
            "handoff_id": handoff.handoff_id,
            "handoff_digest": handoff.handoff_digest,
            "interpretation_id": interpretation.interpretation_id,
            "interpretation_digest": interpretation.interpretation_digest,
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "proposal_statement_digest": proposal.statement.statement_digest,
            "admission_id": admission.admission_id,
            "admission_digest": admission.admission_digest,
            "admission_decision": admission.decision.value,
            "checkpoint_digest": proposal.checkpoint_digest,
            "context_builder_kind": builder_kind.value,
            "context_builder": context_builder,
            "basis_statements": [item.to_dict() for item in basis],
            "reversibility": reversibility.value,
            "trajectory_impact": trajectory_impact.value,
            "alternatives": alternatives.value,
            "attention_constraint_checks": [item.to_dict() for item in checks],
        }
        return cls(
            schema_version=DYNAMIC_ATTENTION_CONTEXT_SCHEMA_VERSION,
            context_id=context_id,
            created_at=created_at,
            handoff_id=handoff.handoff_id,
            handoff_digest=handoff.handoff_digest,
            interpretation_id=interpretation.interpretation_id,
            interpretation_digest=interpretation.interpretation_digest,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            proposal_statement_digest=proposal.statement.statement_digest,
            admission_id=admission.admission_id,
            admission_digest=admission.admission_digest,
            admission_decision=admission.decision,
            checkpoint_digest=proposal.checkpoint_digest,
            context_builder_kind=builder_kind,
            context_builder=context_builder,
            basis_statements=basis,
            reversibility=reversibility,
            trajectory_impact=trajectory_impact,
            alternatives=alternatives,
            attention_constraint_checks=checks,
            context_digest=_digest(base),
        )

    @staticmethod
    def _validate_basis(
        proposal: ContinuationProposal,
        basis: tuple[WorkStatement, ...],
    ) -> None:
        if not basis or len(basis) > MAX_DYNAMIC_ATTENTION_BASIS:
            raise InvalidWorkRecordError(
                "Dynamic attention requires a bounded non-empty statement basis"
            )
        if any(not isinstance(item, WorkStatement) for item in basis):
            raise InvalidWorkRecordError(
                "Dynamic attention basis must contain WorkStatement records"
            )
        digests = tuple(item.statement_digest for item in basis)
        if len(set(digests)) != len(digests):
            raise InvalidWorkRecordError(
                "Dynamic attention basis must not contain duplicate statements"
            )
        if proposal.statement.statement_digest not in digests:
            raise InvalidWorkRecordError(
                "Dynamic attention basis must include the exact continuation proposal"
            )

    @staticmethod
    def _validate_constraint_coverage(
        handoff: WorkHandoff,
        checks: tuple[AttentionConstraintCheck, ...],
    ) -> None:
        if any(not isinstance(item, AttentionConstraintCheck) for item in checks):
            raise InvalidWorkRecordError(
                "attention_constraint_checks must contain AttentionConstraintCheck records"
            )
        check_digests = tuple(item.statement_digest for item in checks)
        if len(set(check_digests)) != len(check_digests):
            raise InvalidWorkRecordError(
                "Attention constraints must be evaluated exactly once"
            )
        required = {item.statement_digest for item in handoff.attention_constraints}
        actual = set(check_digests)
        if actual != required:
            raise InvalidWorkRecordError(
                "Dynamic attention must explicitly evaluate every exact attention constraint"
            )

    def __post_init__(self) -> None:
        if self.schema_version != DYNAMIC_ATTENTION_CONTEXT_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported dynamic attention context schema")
        _validate_uuid(self.context_id, "context_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.handoff_id, "handoff_id")
        _validate_digest(self.handoff_digest, "handoff_digest")
        _validate_uuid(self.interpretation_id, "interpretation_id")
        _validate_digest(self.interpretation_digest, "interpretation_digest")
        _validate_uuid(self.proposal_id, "proposal_id")
        _validate_digest(self.proposal_digest, "proposal_digest")
        _validate_digest(self.proposal_statement_digest, "proposal_statement_digest")
        _validate_uuid(self.admission_id, "admission_id")
        _validate_digest(self.admission_digest, "admission_digest")
        object.__setattr__(
            self,
            "admission_decision",
            _normalize_admission_decision(self.admission_decision),
        )
        _validate_digest(self.checkpoint_digest, "checkpoint_digest")
        builder_kind = _normalize_actor_kind(self.context_builder_kind)
        if builder_kind is not WorkActorKind.CODEXIA:
            raise InvalidWorkRecordError(
                "Dynamic attention context must preserve explicit Codexia authorship"
            )
        object.__setattr__(self, "context_builder_kind", builder_kind)
        object.__setattr__(
            self,
            "context_builder",
            _bounded_text(self.context_builder, "context_builder", MAX_ACTOR_CHARS),
        )

        if not isinstance(self.basis_statements, tuple):
            raise InvalidWorkRecordError("basis_statements must be a tuple")
        if not self.basis_statements or len(self.basis_statements) > MAX_DYNAMIC_ATTENTION_BASIS:
            raise InvalidWorkRecordError(
                "Dynamic attention requires a bounded non-empty statement basis"
            )
        if any(not isinstance(item, WorkStatement) for item in self.basis_statements):
            raise InvalidWorkRecordError(
                "Dynamic attention basis must contain WorkStatement records"
            )
        basis_digests = tuple(item.statement_digest for item in self.basis_statements)
        if len(set(basis_digests)) != len(basis_digests):
            raise InvalidWorkRecordError(
                "Dynamic attention basis must not contain duplicate statements"
            )
        if self.proposal_statement_digest not in set(basis_digests):
            raise InvalidWorkRecordError(
                "Dynamic attention context lost its exact proposal statement"
            )

        object.__setattr__(
            self,
            "reversibility",
            _normalize_reversibility(self.reversibility),
        )
        object.__setattr__(
            self,
            "trajectory_impact",
            _normalize_trajectory_impact(self.trajectory_impact),
        )
        object.__setattr__(
            self,
            "alternatives",
            _normalize_alternative_shape(self.alternatives),
        )
        if not isinstance(self.attention_constraint_checks, tuple):
            raise InvalidWorkRecordError("attention_constraint_checks must be a tuple")
        if any(
            not isinstance(item, AttentionConstraintCheck)
            for item in self.attention_constraint_checks
        ):
            raise InvalidWorkRecordError(
                "attention_constraint_checks must contain AttentionConstraintCheck records"
            )
        check_statement_digests = tuple(
            item.statement_digest for item in self.attention_constraint_checks
        )
        if len(set(check_statement_digests)) != len(check_statement_digests):
            raise InvalidWorkRecordError(
                "Attention constraints must be evaluated exactly once"
            )
        _validate_digest(self.context_digest, "context_digest")
        if not hmac.compare_digest(self.context_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "Dynamic attention context digest does not match its exact evidence"
            )

    @property
    def has_attention_constraint_blocker(self) -> bool:
        return any(
            item.status
            in {AttentionConstraintStatus.TRIGGERED, AttentionConstraintStatus.UNCERTAIN}
            for item in self.attention_constraint_checks
        )

    def assert_binds(
        self,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        proposal: ContinuationProposal,
        admission: ContinuationAdmission,
    ) -> None:
        admission.assert_binds(handoff, interpretation, proposal)
        if (
            self.handoff_id != handoff.handoff_id
            or not hmac.compare_digest(self.handoff_digest, handoff.handoff_digest)
            or self.interpretation_id != interpretation.interpretation_id
            or not hmac.compare_digest(
                self.interpretation_digest,
                interpretation.interpretation_digest,
            )
            or self.proposal_id != proposal.proposal_id
            or not hmac.compare_digest(self.proposal_digest, proposal.proposal_digest)
            or not hmac.compare_digest(
                self.proposal_statement_digest,
                proposal.statement.statement_digest,
            )
            or self.admission_id != admission.admission_id
            or not hmac.compare_digest(self.admission_digest, admission.admission_digest)
            or self.admission_decision is not admission.decision
            or not hmac.compare_digest(
                self.checkpoint_digest,
                proposal.checkpoint_digest,
            )
        ):
            raise InvalidWorkRecordError(
                "Dynamic attention context does not bind the exact admitted work checkpoint"
            )
        basis_digests = {item.statement_digest for item in self.basis_statements}
        if proposal.statement.statement_digest not in basis_digests:
            raise InvalidWorkRecordError(
                "Dynamic attention context lost the exact continuation proposal"
            )
        self._validate_constraint_coverage(handoff, self.attention_constraint_checks)

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "created_at": self.created_at,
            "handoff_id": self.handoff_id,
            "handoff_digest": self.handoff_digest,
            "interpretation_id": self.interpretation_id,
            "interpretation_digest": self.interpretation_digest,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "proposal_statement_digest": self.proposal_statement_digest,
            "admission_id": self.admission_id,
            "admission_digest": self.admission_digest,
            "admission_decision": self.admission_decision.value,
            "checkpoint_digest": self.checkpoint_digest,
            "context_builder_kind": self.context_builder_kind.value,
            "context_builder": self.context_builder,
            "basis_statements": [item.to_dict() for item in self.basis_statements],
            "reversibility": self.reversibility.value,
            "trajectory_impact": self.trajectory_impact.value,
            "alternatives": self.alternatives.value,
            "attention_constraint_checks": [
                item.to_dict() for item in self.attention_constraint_checks
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "context_digest": self.context_digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DynamicAttentionContext:
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "context_id",
                "created_at",
                "handoff_id",
                "handoff_digest",
                "interpretation_id",
                "interpretation_digest",
                "proposal_id",
                "proposal_digest",
                "proposal_statement_digest",
                "admission_id",
                "admission_digest",
                "admission_decision",
                "checkpoint_digest",
                "context_builder_kind",
                "context_builder",
                "basis_statements",
                "reversibility",
                "trajectory_impact",
                "alternatives",
                "attention_constraint_checks",
                "context_digest",
            },
            "DynamicAttentionContext",
        )
        basis = value["basis_statements"]
        checks = value["attention_constraint_checks"]
        if not isinstance(basis, list):
            raise InvalidWorkRecordError(
                "basis_statements must decode from a JSON array"
            )
        if not isinstance(checks, list):
            raise InvalidWorkRecordError(
                "attention_constraint_checks must decode from a JSON array"
            )
        return cls(
            schema_version=value["schema_version"],
            context_id=value["context_id"],
            created_at=value["created_at"],
            handoff_id=value["handoff_id"],
            handoff_digest=value["handoff_digest"],
            interpretation_id=value["interpretation_id"],
            interpretation_digest=value["interpretation_digest"],
            proposal_id=value["proposal_id"],
            proposal_digest=value["proposal_digest"],
            proposal_statement_digest=value["proposal_statement_digest"],
            admission_id=value["admission_id"],
            admission_digest=value["admission_digest"],
            admission_decision=value["admission_decision"],
            checkpoint_digest=value["checkpoint_digest"],
            context_builder_kind=value["context_builder_kind"],
            context_builder=value["context_builder"],
            basis_statements=tuple(WorkStatement.from_dict(item) for item in basis),
            reversibility=value["reversibility"],
            trajectory_impact=value["trajectory_impact"],
            alternatives=value["alternatives"],
            attention_constraint_checks=tuple(
                AttentionConstraintCheck.from_dict(item) for item in checks
            ),
            context_digest=value["context_digest"],
        )


@dataclass(frozen=True, slots=True)
class DynamicAttentionDecision:
    """Codexia attention decision bound to exact context; never action authority."""

    schema_version: int
    decision_id: str
    created_at: str
    context: DynamicAttentionContext
    cognitive_disposition: AttentionDisposition
    override: AttentionOverride
    assessment: AttentionAssessment
    decision_digest: str

    @classmethod
    def evaluate(
        cls,
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        proposal: ContinuationProposal,
        admission: ContinuationAdmission,
        context: DynamicAttentionContext,
        assessor_kind: WorkActorKind | str,
        assessor: str,
        cognitive_disposition: AttentionDisposition | str,
        confidence_basis_points: int,
        urgency: AttentionUrgency | str,
        reason: str,
        requested_response: str | None = None,
        decision_id: str | None = None,
        created_at: str | None = None,
    ) -> DynamicAttentionDecision:
        if not isinstance(context, DynamicAttentionContext):
            raise InvalidWorkRecordError("context must be a DynamicAttentionContext")
        context.assert_binds(handoff, interpretation, proposal, admission)
        kind = _normalize_actor_kind(assessor_kind)
        if kind is not WorkActorKind.CODEXIA:
            raise InvalidWorkRecordError(
                "Dynamic attention decision must preserve explicit Codexia authorship"
            )
        assessor = _bounded_text(assessor, "assessor", MAX_ACTOR_CHARS)
        cognitive_disposition = _normalize_disposition(cognitive_disposition)
        urgency = _normalize_attention_urgency(urgency)
        reason = _bounded_text(reason, "reason", MAX_ATTENTION_REASON_CHARS)
        requested_response = _optional_bounded_text(
            requested_response,
            "requested_response",
            MAX_ATTENTION_RESPONSE_CHARS,
        )
        override = cls._derive_override(context)
        needs_human = (
            override is not AttentionOverride.NONE
            or cognitive_disposition is AttentionDisposition.ASK_HUMAN
        )
        assessment = AttentionAssessment.create(
            handoff=handoff,
            interpretation=interpretation,
            checkpoint_digest=context.checkpoint_digest,
            assessor_kind=kind,
            assessor=assessor,
            needs_human=needs_human,
            confidence_basis_points=confidence_basis_points,
            urgency=urgency,
            reason=reason,
            requested_response=requested_response,
        )
        decision_id = decision_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        base = {
            "schema_version": DYNAMIC_ATTENTION_DECISION_SCHEMA_VERSION,
            "decision_id": decision_id,
            "created_at": created_at,
            "context": context.to_dict(),
            "cognitive_disposition": cognitive_disposition.value,
            "override": override.value,
            "assessment": assessment.to_dict(),
        }
        return cls(
            schema_version=DYNAMIC_ATTENTION_DECISION_SCHEMA_VERSION,
            decision_id=decision_id,
            created_at=created_at,
            context=context,
            cognitive_disposition=cognitive_disposition,
            override=override,
            assessment=assessment,
            decision_digest=_digest(base),
        )

    @staticmethod
    def _derive_override(context: DynamicAttentionContext) -> AttentionOverride:
        if context.has_attention_constraint_blocker:
            return AttentionOverride.EXPLICIT_HUMAN_CONSTRAINT
        if context.admission_decision is ContinuationDecision.ASK_HUMAN:
            return AttentionOverride.ADMISSION_REQUIRES_HUMAN
        return AttentionOverride.NONE

    def __post_init__(self) -> None:
        if self.schema_version != DYNAMIC_ATTENTION_DECISION_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported dynamic attention decision schema")
        _validate_uuid(self.decision_id, "decision_id")
        _validate_timestamp(self.created_at, "created_at")
        if not isinstance(self.context, DynamicAttentionContext):
            raise InvalidWorkRecordError("context must be a DynamicAttentionContext")
        object.__setattr__(
            self,
            "cognitive_disposition",
            _normalize_disposition(self.cognitive_disposition),
        )
        object.__setattr__(self, "override", _normalize_override(self.override))
        if not isinstance(self.assessment, AttentionAssessment):
            raise InvalidWorkRecordError("assessment must be an AttentionAssessment")
        if self.assessment.assessor_kind is not WorkActorKind.CODEXIA:
            raise InvalidWorkRecordError(
                "Dynamic attention assessment must preserve Codexia authorship"
            )
        expected_override = self._derive_override(self.context)
        if self.override is not expected_override:
            raise InvalidWorkRecordError(
                "Dynamic attention override does not match its exact context"
            )
        expected_needs_human = (
            expected_override is not AttentionOverride.NONE
            or self.cognitive_disposition is AttentionDisposition.ASK_HUMAN
        )
        if self.assessment.needs_human is not expected_needs_human:
            raise InvalidWorkRecordError(
                "Dynamic attention outcome does not match cognition plus hard constraints"
            )
        if (
            self.assessment.handoff_id != self.context.handoff_id
            or not hmac.compare_digest(
                self.assessment.handoff_digest,
                self.context.handoff_digest,
            )
            or self.assessment.interpretation_id != self.context.interpretation_id
            or not hmac.compare_digest(
                self.assessment.interpretation_digest,
                self.context.interpretation_digest,
            )
            or not hmac.compare_digest(
                self.assessment.checkpoint_digest,
                self.context.checkpoint_digest,
            )
        ):
            raise InvalidWorkRecordError(
                "Dynamic attention assessment does not bind the exact attention context"
            )
        _validate_digest(self.decision_digest, "decision_digest")
        if not hmac.compare_digest(self.decision_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "Dynamic attention decision digest does not match its exact judgment"
            )

    @property
    def disposition(self) -> AttentionDisposition:
        if self.assessment.needs_human:
            return AttentionDisposition.ASK_HUMAN
        return AttentionDisposition.KEEP_MOVING

    @property
    def needs_human(self) -> bool:
        return self.assessment.needs_human

    def assert_binds(self, context: DynamicAttentionContext) -> None:
        if not isinstance(context, DynamicAttentionContext):
            raise InvalidWorkRecordError("context must be a DynamicAttentionContext")
        if (
            self.context.context_id != context.context_id
            or not hmac.compare_digest(
                self.context.context_digest,
                context.context_digest,
            )
        ):
            raise InvalidWorkRecordError(
                "Dynamic attention decision does not bind the exact context"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "created_at": self.created_at,
            "context": self.context.to_dict(),
            "cognitive_disposition": self.cognitive_disposition.value,
            "override": self.override.value,
            "assessment": self.assessment.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "decision_digest": self.decision_digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DynamicAttentionDecision:
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "decision_id",
                "created_at",
                "context",
                "cognitive_disposition",
                "override",
                "assessment",
                "decision_digest",
            },
            "DynamicAttentionDecision",
        )
        return cls(
            schema_version=value["schema_version"],
            decision_id=value["decision_id"],
            created_at=value["created_at"],
            context=DynamicAttentionContext.from_dict(value["context"]),
            cognitive_disposition=value["cognitive_disposition"],
            override=value["override"],
            assessment=AttentionAssessment.from_dict(value["assessment"]),
            decision_digest=value["decision_digest"],
        )
