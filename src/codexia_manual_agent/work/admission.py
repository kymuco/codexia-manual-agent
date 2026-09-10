from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4

from codexia_manual_agent.work.contracts import (
    MAX_ACTOR_CHARS,
    MAX_WORK_TEXT_CHARS,
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
    _optional_bounded_text,
    _validate_digest,
    _validate_timestamp,
    _validate_uuid,
)

CONTINUATION_PROPOSAL_SCHEMA_VERSION = 1
CONTINUATION_ADMISSION_SCHEMA_VERSION = 1

MAX_CONTINUATION_REASON_CHARS = 8_192
MAX_CONTINUATION_RESPONSE_CHARS = 8_192


class ContinuationDecision(StrEnum):
    ADMIT = "admit"
    REVISE = "revise"
    REJECT = "reject"
    ASK_HUMAN = "ask_human"


class ContinuationFit(StrEnum):
    ALIGNED = "aligned"
    MISALIGNED = "misaligned"
    UNCERTAIN = "uncertain"


class ContinuationEvidenceFit(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"
    NOT_REQUIRED = "not_required"


def _normalize_decision(
    value: ContinuationDecision | str,
) -> ContinuationDecision:
    try:
        return ContinuationDecision(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported continuation decision") from exc


def _normalize_fit(value: ContinuationFit | str) -> ContinuationFit:
    try:
        return ContinuationFit(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported continuation fit") from exc


def _normalize_evidence_fit(
    value: ContinuationEvidenceFit | str,
) -> ContinuationEvidenceFit:
    try:
        return ContinuationEvidenceFit(value)
    except (TypeError, ValueError) as exc:
        raise InvalidWorkRecordError("Unsupported continuation evidence fit") from exc


@dataclass(frozen=True, slots=True)
class ContinuationProposal:
    """Non-human proposal for the next unit of delegated work."""

    schema_version: int
    proposal_id: str
    created_at: str
    handoff_id: str
    handoff_digest: str
    interpretation_id: str
    interpretation_digest: str
    checkpoint_digest: str
    statement: WorkStatement
    proposal_digest: str

    @classmethod
    def create(
        cls,
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        checkpoint_digest: str,
        statement: WorkStatement,
        proposal_id: str | None = None,
        created_at: str | None = None,
    ) -> "ContinuationProposal":
        if not isinstance(handoff, WorkHandoff):
            raise InvalidWorkRecordError("handoff must be a WorkHandoff")
        if not isinstance(interpretation, WorkIntentInterpretation):
            raise InvalidWorkRecordError(
                "interpretation must be a WorkIntentInterpretation"
            )
        interpretation.assert_binds(handoff)
        _validate_digest(checkpoint_digest, "checkpoint_digest")
        if not isinstance(statement, WorkStatement):
            raise InvalidWorkRecordError("statement must be a WorkStatement")
        if statement.author_kind not in {
            WorkActorKind.WORKER,
            WorkActorKind.CODEXIA,
        }:
            raise InvalidWorkRecordError(
                "Continuation proposal must preserve worker or Codexia authorship"
            )
        proposal_id = proposal_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        base = {
            "schema_version": CONTINUATION_PROPOSAL_SCHEMA_VERSION,
            "proposal_id": proposal_id,
            "created_at": created_at,
            "handoff_id": handoff.handoff_id,
            "handoff_digest": handoff.handoff_digest,
            "interpretation_id": interpretation.interpretation_id,
            "interpretation_digest": interpretation.interpretation_digest,
            "checkpoint_digest": checkpoint_digest,
            "statement": statement.to_dict(),
        }
        return cls(
            schema_version=CONTINUATION_PROPOSAL_SCHEMA_VERSION,
            proposal_id=proposal_id,
            created_at=created_at,
            handoff_id=handoff.handoff_id,
            handoff_digest=handoff.handoff_digest,
            interpretation_id=interpretation.interpretation_id,
            interpretation_digest=interpretation.interpretation_digest,
            checkpoint_digest=checkpoint_digest,
            statement=statement,
            proposal_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != CONTINUATION_PROPOSAL_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported continuation proposal schema")
        _validate_uuid(self.proposal_id, "proposal_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.handoff_id, "handoff_id")
        _validate_digest(self.handoff_digest, "handoff_digest")
        _validate_uuid(self.interpretation_id, "interpretation_id")
        _validate_digest(self.interpretation_digest, "interpretation_digest")
        _validate_digest(self.checkpoint_digest, "checkpoint_digest")
        if not isinstance(self.statement, WorkStatement):
            raise InvalidWorkRecordError("statement must be a WorkStatement")
        if self.statement.author_kind not in {
            WorkActorKind.WORKER,
            WorkActorKind.CODEXIA,
        }:
            raise InvalidWorkRecordError(
                "Continuation proposal must preserve worker or Codexia authorship"
            )
        _validate_digest(self.proposal_digest, "proposal_digest")
        if not hmac.compare_digest(self.proposal_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "Continuation proposal digest does not match its exact payload"
            )

    def assert_binds(
        self,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
    ) -> None:
        interpretation.assert_binds(handoff)
        if (
            self.handoff_id != handoff.handoff_id
            or not hmac.compare_digest(self.handoff_digest, handoff.handoff_digest)
            or self.interpretation_id != interpretation.interpretation_id
            or not hmac.compare_digest(
                self.interpretation_digest,
                interpretation.interpretation_digest,
            )
        ):
            raise InvalidWorkRecordError(
                "Continuation proposal does not bind the exact work interpretation"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "created_at": self.created_at,
            "handoff_id": self.handoff_id,
            "handoff_digest": self.handoff_digest,
            "interpretation_id": self.interpretation_id,
            "interpretation_digest": self.interpretation_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "statement": self.statement.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "proposal_digest": self.proposal_digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ContinuationProposal":
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "proposal_id",
                "created_at",
                "handoff_id",
                "handoff_digest",
                "interpretation_id",
                "interpretation_digest",
                "checkpoint_digest",
                "statement",
                "proposal_digest",
            },
            "ContinuationProposal",
        )
        return cls(
            schema_version=value["schema_version"],
            proposal_id=value["proposal_id"],
            created_at=value["created_at"],
            handoff_id=value["handoff_id"],
            handoff_digest=value["handoff_digest"],
            interpretation_id=value["interpretation_id"],
            interpretation_digest=value["interpretation_digest"],
            checkpoint_digest=value["checkpoint_digest"],
            statement=WorkStatement.from_dict(value["statement"]),
            proposal_digest=value["proposal_digest"],
        )


@dataclass(frozen=True, slots=True)
class ContinuationAdmission:
    """Codexia judgment that classifies a proposal without granting authority."""

    schema_version: int
    admission_id: str
    created_at: str
    handoff_id: str
    handoff_digest: str
    interpretation_id: str
    interpretation_digest: str
    proposal_id: str
    proposal_digest: str
    checkpoint_digest: str
    assessor_kind: WorkActorKind
    assessor: str
    objective_fit: ContinuationFit
    constraint_fit: ContinuationFit
    scope_fit: ContinuationFit
    depth_fit: ContinuationFit
    evidence_fit: ContinuationEvidenceFit
    material_human_choice: bool
    decision: ContinuationDecision
    reason: str
    revision_request: str | None
    requested_human_response: str | None
    admission_digest: str

    @classmethod
    def evaluate(
        cls,
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        proposal: ContinuationProposal,
        assessor_kind: WorkActorKind | str,
        assessor: str,
        objective_fit: ContinuationFit | str,
        constraint_fit: ContinuationFit | str,
        scope_fit: ContinuationFit | str,
        depth_fit: ContinuationFit | str,
        evidence_fit: ContinuationEvidenceFit | str,
        material_human_choice: bool,
        reason: str,
        revision_request: str | None = None,
        requested_human_response: str | None = None,
        admission_id: str | None = None,
        created_at: str | None = None,
    ) -> "ContinuationAdmission":
        if not isinstance(handoff, WorkHandoff):
            raise InvalidWorkRecordError("handoff must be a WorkHandoff")
        if not isinstance(interpretation, WorkIntentInterpretation):
            raise InvalidWorkRecordError(
                "interpretation must be a WorkIntentInterpretation"
            )
        if not isinstance(proposal, ContinuationProposal):
            raise InvalidWorkRecordError("proposal must be a ContinuationProposal")
        proposal.assert_binds(handoff, interpretation)
        kind = _normalize_actor_kind(assessor_kind)
        if kind is not WorkActorKind.CODEXIA:
            raise InvalidWorkRecordError(
                "Continuation admission must preserve explicit Codexia authorship"
            )
        assessor = _bounded_text(assessor, "assessor", MAX_ACTOR_CHARS)
        objective_fit = _normalize_fit(objective_fit)
        constraint_fit = _normalize_fit(constraint_fit)
        scope_fit = _normalize_fit(scope_fit)
        depth_fit = _normalize_fit(depth_fit)
        evidence_fit = _normalize_evidence_fit(evidence_fit)
        if type(material_human_choice) is not bool:
            raise InvalidWorkRecordError("material_human_choice must be boolean")
        reason = _bounded_text(
            reason,
            "reason",
            MAX_CONTINUATION_REASON_CHARS,
        )
        revision_request = _optional_bounded_text(
            revision_request,
            "revision_request",
            MAX_CONTINUATION_RESPONSE_CHARS,
        )
        requested_human_response = _optional_bounded_text(
            requested_human_response,
            "requested_human_response",
            MAX_CONTINUATION_RESPONSE_CHARS,
        )
        decision = cls._decide(
            objective_fit=objective_fit,
            constraint_fit=constraint_fit,
            scope_fit=scope_fit,
            depth_fit=depth_fit,
            evidence_fit=evidence_fit,
            material_human_choice=material_human_choice,
        )
        cls._validate_decision_payload(
            decision=decision,
            revision_request=revision_request,
            requested_human_response=requested_human_response,
        )
        admission_id = admission_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        base = {
            "schema_version": CONTINUATION_ADMISSION_SCHEMA_VERSION,
            "admission_id": admission_id,
            "created_at": created_at,
            "handoff_id": handoff.handoff_id,
            "handoff_digest": handoff.handoff_digest,
            "interpretation_id": interpretation.interpretation_id,
            "interpretation_digest": interpretation.interpretation_digest,
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "checkpoint_digest": proposal.checkpoint_digest,
            "assessor_kind": kind.value,
            "assessor": assessor,
            "objective_fit": objective_fit.value,
            "constraint_fit": constraint_fit.value,
            "scope_fit": scope_fit.value,
            "depth_fit": depth_fit.value,
            "evidence_fit": evidence_fit.value,
            "material_human_choice": material_human_choice,
            "decision": decision.value,
            "reason": reason,
            "revision_request": revision_request,
            "requested_human_response": requested_human_response,
        }
        return cls(
            schema_version=CONTINUATION_ADMISSION_SCHEMA_VERSION,
            admission_id=admission_id,
            created_at=created_at,
            handoff_id=handoff.handoff_id,
            handoff_digest=handoff.handoff_digest,
            interpretation_id=interpretation.interpretation_id,
            interpretation_digest=interpretation.interpretation_digest,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            checkpoint_digest=proposal.checkpoint_digest,
            assessor_kind=kind,
            assessor=assessor,
            objective_fit=objective_fit,
            constraint_fit=constraint_fit,
            scope_fit=scope_fit,
            depth_fit=depth_fit,
            evidence_fit=evidence_fit,
            material_human_choice=material_human_choice,
            decision=decision,
            reason=reason,
            revision_request=revision_request,
            requested_human_response=requested_human_response,
            admission_digest=_digest(base),
        )

    @staticmethod
    def _decide(
        *,
        objective_fit: ContinuationFit,
        constraint_fit: ContinuationFit,
        scope_fit: ContinuationFit,
        depth_fit: ContinuationFit,
        evidence_fit: ContinuationEvidenceFit,
        material_human_choice: bool,
    ) -> ContinuationDecision:
        if (
            objective_fit is ContinuationFit.MISALIGNED
            or constraint_fit is ContinuationFit.MISALIGNED
        ):
            return ContinuationDecision.REJECT
        if (
            objective_fit is ContinuationFit.UNCERTAIN
            or constraint_fit is ContinuationFit.UNCERTAIN
        ):
            return ContinuationDecision.ASK_HUMAN
        if scope_fit is ContinuationFit.MISALIGNED:
            if material_human_choice:
                return ContinuationDecision.ASK_HUMAN
            return ContinuationDecision.REJECT
        if scope_fit is ContinuationFit.UNCERTAIN or material_human_choice:
            return ContinuationDecision.ASK_HUMAN
        if depth_fit is not ContinuationFit.ALIGNED:
            return ContinuationDecision.REVISE
        if evidence_fit in {
            ContinuationEvidenceFit.UNSUPPORTED,
            ContinuationEvidenceFit.UNCERTAIN,
        }:
            return ContinuationDecision.REVISE
        return ContinuationDecision.ADMIT

    @staticmethod
    def _validate_decision_payload(
        *,
        decision: ContinuationDecision,
        revision_request: str | None,
        requested_human_response: str | None,
    ) -> None:
        if decision is ContinuationDecision.REVISE:
            if revision_request is None:
                raise InvalidWorkRecordError(
                    "REVISE admission requires a bounded revision request"
                )
            if requested_human_response is not None:
                raise InvalidWorkRecordError(
                    "REVISE admission cannot request a human response"
                )
            return
        if decision is ContinuationDecision.ASK_HUMAN:
            if requested_human_response is None:
                raise InvalidWorkRecordError(
                    "ASK_HUMAN admission requires a bounded human response request"
                )
            if revision_request is not None:
                raise InvalidWorkRecordError(
                    "ASK_HUMAN admission cannot also issue a worker revision"
                )
            return
        if revision_request is not None or requested_human_response is not None:
            raise InvalidWorkRecordError(
                "ADMIT/REJECT admission cannot carry follow-up request text"
            )

    def __post_init__(self) -> None:
        if self.schema_version != CONTINUATION_ADMISSION_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported continuation admission schema")
        _validate_uuid(self.admission_id, "admission_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.handoff_id, "handoff_id")
        _validate_digest(self.handoff_digest, "handoff_digest")
        _validate_uuid(self.interpretation_id, "interpretation_id")
        _validate_digest(self.interpretation_digest, "interpretation_digest")
        _validate_uuid(self.proposal_id, "proposal_id")
        _validate_digest(self.proposal_digest, "proposal_digest")
        _validate_digest(self.checkpoint_digest, "checkpoint_digest")
        kind = _normalize_actor_kind(self.assessor_kind)
        if kind is not WorkActorKind.CODEXIA:
            raise InvalidWorkRecordError(
                "Continuation admission must preserve explicit Codexia authorship"
            )
        object.__setattr__(self, "assessor_kind", kind)
        object.__setattr__(
            self,
            "assessor",
            _bounded_text(self.assessor, "assessor", MAX_ACTOR_CHARS),
        )
        for field_name in (
            "objective_fit",
            "constraint_fit",
            "scope_fit",
            "depth_fit",
        ):
            object.__setattr__(self, field_name, _normalize_fit(getattr(self, field_name)))
        object.__setattr__(
            self,
            "evidence_fit",
            _normalize_evidence_fit(self.evidence_fit),
        )
        if type(self.material_human_choice) is not bool:
            raise InvalidWorkRecordError("material_human_choice must be boolean")
        decision = _normalize_decision(self.decision)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(
            self,
            "reason",
            _bounded_text(
                self.reason,
                "reason",
                MAX_CONTINUATION_REASON_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "revision_request",
            _optional_bounded_text(
                self.revision_request,
                "revision_request",
                MAX_CONTINUATION_RESPONSE_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "requested_human_response",
            _optional_bounded_text(
                self.requested_human_response,
                "requested_human_response",
                MAX_CONTINUATION_RESPONSE_CHARS,
            ),
        )
        expected = self._decide(
            objective_fit=self.objective_fit,
            constraint_fit=self.constraint_fit,
            scope_fit=self.scope_fit,
            depth_fit=self.depth_fit,
            evidence_fit=self.evidence_fit,
            material_human_choice=self.material_human_choice,
        )
        if decision is not expected:
            raise InvalidWorkRecordError(
                "Continuation decision does not match its exact admission criteria"
            )
        self._validate_decision_payload(
            decision=decision,
            revision_request=self.revision_request,
            requested_human_response=self.requested_human_response,
        )
        _validate_digest(self.admission_digest, "admission_digest")
        if not hmac.compare_digest(self.admission_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "Continuation admission digest does not match its exact judgment"
            )

    @property
    def admitted(self) -> bool:
        return self.decision is ContinuationDecision.ADMIT

    def assert_binds(
        self,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        proposal: ContinuationProposal,
    ) -> None:
        proposal.assert_binds(handoff, interpretation)
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
                self.checkpoint_digest,
                proposal.checkpoint_digest,
            )
        ):
            raise InvalidWorkRecordError(
                "Continuation admission does not bind the exact proposal checkpoint"
            )

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "admission_id": self.admission_id,
            "created_at": self.created_at,
            "handoff_id": self.handoff_id,
            "handoff_digest": self.handoff_digest,
            "interpretation_id": self.interpretation_id,
            "interpretation_digest": self.interpretation_digest,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "assessor_kind": self.assessor_kind.value,
            "assessor": self.assessor,
            "objective_fit": self.objective_fit.value,
            "constraint_fit": self.constraint_fit.value,
            "scope_fit": self.scope_fit.value,
            "depth_fit": self.depth_fit.value,
            "evidence_fit": self.evidence_fit.value,
            "material_human_choice": self.material_human_choice,
            "decision": self.decision.value,
            "reason": self.reason,
            "revision_request": self.revision_request,
            "requested_human_response": self.requested_human_response,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "admission_digest": self.admission_digest}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ContinuationAdmission":
        value = _exact_keys(
            payload,
            {
                "schema_version",
                "admission_id",
                "created_at",
                "handoff_id",
                "handoff_digest",
                "interpretation_id",
                "interpretation_digest",
                "proposal_id",
                "proposal_digest",
                "checkpoint_digest",
                "assessor_kind",
                "assessor",
                "objective_fit",
                "constraint_fit",
                "scope_fit",
                "depth_fit",
                "evidence_fit",
                "material_human_choice",
                "decision",
                "reason",
                "revision_request",
                "requested_human_response",
                "admission_digest",
            },
            "ContinuationAdmission",
        )
        return cls(
            schema_version=value["schema_version"],
            admission_id=value["admission_id"],
            created_at=value["created_at"],
            handoff_id=value["handoff_id"],
            handoff_digest=value["handoff_digest"],
            interpretation_id=value["interpretation_id"],
            interpretation_digest=value["interpretation_digest"],
            proposal_id=value["proposal_id"],
            proposal_digest=value["proposal_digest"],
            checkpoint_digest=value["checkpoint_digest"],
            assessor_kind=value["assessor_kind"],
            assessor=value["assessor"],
            objective_fit=value["objective_fit"],
            constraint_fit=value["constraint_fit"],
            scope_fit=value["scope_fit"],
            depth_fit=value["depth_fit"],
            evidence_fit=value["evidence_fit"],
            material_human_choice=value["material_human_choice"],
            decision=value["decision"],
            reason=value["reason"],
            revision_request=value["revision_request"],
            requested_human_response=value["requested_human_response"],
            admission_digest=value["admission_digest"],
        )
