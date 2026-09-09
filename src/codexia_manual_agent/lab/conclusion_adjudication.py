from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from codexia_manual_agent.lab.comparison import FrozenComparisonPolicy
from codexia_manual_agent.lab.comparison_result import ComparisonOutcome, ComparisonResult
from codexia_manual_agent.lab.errors import EvidenceBindingError, InvalidLabRecordError
from codexia_manual_agent.lab.models import (
    ConclusionVerdict,
    ExperimentManifest,
    Hypothesis,
)


ADJUDICATED_CONCLUSION_SCHEMA_VERSION = 1
MAX_ADJUDICATED_CONCLUSION_JSON_CHARS = 131_072
MAX_ADJUDICATED_CONCLUSION_SUMMARY_CHARS = 1_024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ConclusionScope(StrEnum):
    FROZEN_COMPARISON_POLICY_V1 = "frozen_comparison_policy.v1"


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidLabRecordError(
            "Adjudicated conclusion payload is not canonical JSON"
        ) from exc
    if len(encoded) > MAX_ADJUDICATED_CONCLUSION_JSON_CHARS:
        raise InvalidLabRecordError("Adjudicated conclusion exceeds its byte budget")
    return encoded


def _digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidLabRecordError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise InvalidLabRecordError(
            f"{label} keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidLabRecordError(
            f"{field_name} must use lowercase hyphenated UUID form"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _same_hypothesis(manifest: ExperimentManifest, hypothesis: Hypothesis) -> bool:
    return (
        manifest.hypothesis_id == hypothesis.hypothesis_id
        and hmac.compare_digest(
            manifest.hypothesis_digest,
            hypothesis.hypothesis_digest,
        )
    )


def _mapped_verdict(outcome: ComparisonOutcome | str) -> ConclusionVerdict:
    try:
        normalized = ComparisonOutcome(outcome)
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError("Unsupported comparison outcome for adjudication") from exc
    return ConclusionVerdict(normalized.value)


def _summary(outcome: ComparisonOutcome | str) -> str:
    normalized = ComparisonOutcome(outcome)
    if normalized is ComparisonOutcome.SUPPORTED:
        return (
            "The verified evidence satisfies the exact frozen comparison policy for "
            "this hypothesis; support is limited to that declared policy and evidence."
        )
    if normalized is ComparisonOutcome.REFUTED:
        return (
            "The verified evidence does not satisfy the exact frozen comparison policy "
            "for this hypothesis; refutation is limited to that declared policy and evidence."
        )
    return (
        "The exact frozen comparison policy is inconclusive under its precommitted "
        "evidence rule; no supported/refuted policy-scoped conclusion is admitted."
    )


def _conclusion_id(result_digest: str) -> str:
    _validate_digest(result_digest, "result_digest")
    return str(
        uuid5(
            NAMESPACE_URL,
            f"codexia.m4.5.adjudicated-conclusion.v1:{result_digest}",
        )
    )


@dataclass(frozen=True, slots=True, init=False)
class AdjudicatedConclusion:
    schema_version: int
    conclusion_id: str
    scope: ConclusionScope
    hypothesis_id: str
    hypothesis_digest: str
    baseline_experiment_id: str
    baseline_manifest_digest: str
    candidate_experiment_id: str
    candidate_manifest_digest: str
    policy_id: str
    policy_digest: str
    freeze_digest: str
    result_id: str
    result_digest: str
    comparison_outcome: ComparisonOutcome
    verdict: ConclusionVerdict
    summary: str
    conclusion_digest: str

    def __init__(self) -> None:
        raise TypeError(
            "AdjudicatedConclusion is derived state; use create() or the strict decoder"
        )

    @classmethod
    def create(
        cls,
        *,
        hypothesis: Hypothesis,
        baseline_manifest: ExperimentManifest,
        candidate_manifest: ExperimentManifest,
        frozen: FrozenComparisonPolicy,
        result: ComparisonResult,
    ) -> "AdjudicatedConclusion":
        if not isinstance(hypothesis, Hypothesis):
            raise TypeError("hypothesis must be a Hypothesis")
        if not isinstance(baseline_manifest, ExperimentManifest):
            raise TypeError("baseline_manifest must be an ExperimentManifest")
        if not isinstance(candidate_manifest, ExperimentManifest):
            raise TypeError("candidate_manifest must be an ExperimentManifest")
        if not isinstance(frozen, FrozenComparisonPolicy):
            raise TypeError("frozen must be a FrozenComparisonPolicy")
        if not isinstance(result, ComparisonResult):
            raise TypeError("result must be a ComparisonResult")
        if not _same_hypothesis(baseline_manifest, hypothesis):
            raise EvidenceBindingError(
                "Baseline manifest is not bound to the exact adjudicated hypothesis"
            )
        if not _same_hypothesis(candidate_manifest, hypothesis):
            raise EvidenceBindingError(
                "Candidate manifest is not bound to the exact adjudicated hypothesis"
            )

        policy = frozen.policy
        if (
            policy.hypothesis_id != hypothesis.hypothesis_id
            or not hmac.compare_digest(
                policy.hypothesis_digest,
                hypothesis.hypothesis_digest,
            )
        ):
            raise EvidenceBindingError(
                "Frozen comparison policy is not bound to the exact adjudicated hypothesis"
            )
        if (
            policy.baseline_experiment_id != baseline_manifest.experiment_id
            or not hmac.compare_digest(
                policy.baseline_manifest_digest,
                baseline_manifest.manifest_digest,
            )
        ):
            raise EvidenceBindingError(
                "Baseline manifest does not match the frozen comparison policy"
            )
        if (
            policy.candidate_experiment_id != candidate_manifest.experiment_id
            or not hmac.compare_digest(
                policy.candidate_manifest_digest,
                candidate_manifest.manifest_digest,
            )
        ):
            raise EvidenceBindingError(
                "Candidate manifest does not match the frozen comparison policy"
            )
        if (
            result.policy_id != policy.policy_id
            or not hmac.compare_digest(result.policy_digest, policy.policy_digest)
            or not hmac.compare_digest(result.freeze_digest, frozen.freeze_digest)
        ):
            raise EvidenceBindingError(
                "Comparison result is not bound to the exact frozen policy"
            )
        if (
            result.baseline_experiment_id != baseline_manifest.experiment_id
            or result.candidate_experiment_id != candidate_manifest.experiment_id
        ):
            raise EvidenceBindingError(
                "Comparison result arms do not match the adjudicated manifests"
            )

        outcome = ComparisonOutcome(result.outcome)
        verdict = _mapped_verdict(outcome)
        summary = _summary(outcome)
        if len(summary) > MAX_ADJUDICATED_CONCLUSION_SUMMARY_CHARS:
            raise InvalidLabRecordError("Generated adjudication summary exceeds its budget")
        conclusion_id = _conclusion_id(result.result_digest)
        base = {
            "schema_version": ADJUDICATED_CONCLUSION_SCHEMA_VERSION,
            "conclusion_id": conclusion_id,
            "scope": ConclusionScope.FROZEN_COMPARISON_POLICY_V1.value,
            "hypothesis_id": hypothesis.hypothesis_id,
            "hypothesis_digest": hypothesis.hypothesis_digest,
            "baseline_experiment_id": baseline_manifest.experiment_id,
            "baseline_manifest_digest": baseline_manifest.manifest_digest,
            "candidate_experiment_id": candidate_manifest.experiment_id,
            "candidate_manifest_digest": candidate_manifest.manifest_digest,
            "policy_id": policy.policy_id,
            "policy_digest": policy.policy_digest,
            "freeze_digest": frozen.freeze_digest,
            "result_id": result.result_id,
            "result_digest": result.result_digest,
            "comparison_outcome": outcome.value,
            "verdict": verdict.value,
            "summary": summary,
        }
        return _construct_adjudicated_conclusion(
            {
                **base,
                "conclusion_digest": _digest(base),
            }
        )

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != ADJUDICATED_CONCLUSION_SCHEMA_VERSION
        ):
            raise InvalidLabRecordError(
                "Unsupported M4.5 adjudicated conclusion schema version"
            )
        _validate_uuid(self.conclusion_id, "conclusion_id")
        try:
            scope = ConclusionScope(self.scope)
        except (TypeError, ValueError) as exc:
            raise InvalidLabRecordError("Unsupported adjudicated conclusion scope") from exc
        if scope is not ConclusionScope.FROZEN_COMPARISON_POLICY_V1:
            raise InvalidLabRecordError("M4.5.1 admits only frozen comparison policy scope")
        object.__setattr__(self, "scope", scope)
        _validate_uuid(self.hypothesis_id, "hypothesis_id")
        _validate_digest(self.hypothesis_digest, "hypothesis_digest")
        _validate_uuid(self.baseline_experiment_id, "baseline_experiment_id")
        _validate_digest(self.baseline_manifest_digest, "baseline_manifest_digest")
        _validate_uuid(self.candidate_experiment_id, "candidate_experiment_id")
        _validate_digest(self.candidate_manifest_digest, "candidate_manifest_digest")
        if self.baseline_experiment_id == self.candidate_experiment_id:
            raise EvidenceBindingError("Adjudicated comparison arms must remain distinct")
        _validate_uuid(self.policy_id, "policy_id")
        _validate_digest(self.policy_digest, "policy_digest")
        _validate_digest(self.freeze_digest, "freeze_digest")
        _validate_uuid(self.result_id, "result_id")
        _validate_digest(self.result_digest, "result_digest")
        if self.conclusion_id != _conclusion_id(self.result_digest):
            raise InvalidLabRecordError(
                "conclusion_id does not match the exact comparison result"
            )
        try:
            outcome = ComparisonOutcome(self.comparison_outcome)
            verdict = ConclusionVerdict(self.verdict)
        except (TypeError, ValueError) as exc:
            raise InvalidLabRecordError(
                "Unsupported adjudicated comparison outcome/verdict"
            ) from exc
        object.__setattr__(self, "comparison_outcome", outcome)
        object.__setattr__(self, "verdict", verdict)
        if verdict is not _mapped_verdict(outcome):
            raise InvalidLabRecordError(
                "Adjudicated verdict does not match the exact comparison outcome"
            )
        expected_summary = _summary(outcome)
        if self.summary != expected_summary:
            raise InvalidLabRecordError(
                "Adjudicated summary is not the canonical policy-scoped wording"
            )
        if len(self.summary) > MAX_ADJUDICATED_CONCLUSION_SUMMARY_CHARS:
            raise InvalidLabRecordError("Adjudicated summary exceeds its budget")
        _validate_digest(self.conclusion_digest, "conclusion_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.conclusion_digest):
            raise InvalidLabRecordError(
                "Adjudicated conclusion digest does not match exact payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "conclusion_id": self.conclusion_id,
            "scope": self.scope.value,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_digest": self.hypothesis_digest,
            "baseline_experiment_id": self.baseline_experiment_id,
            "baseline_manifest_digest": self.baseline_manifest_digest,
            "candidate_experiment_id": self.candidate_experiment_id,
            "candidate_manifest_digest": self.candidate_manifest_digest,
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "freeze_digest": self.freeze_digest,
            "result_id": self.result_id,
            "result_digest": self.result_digest,
            "comparison_outcome": self.comparison_outcome.value,
            "verdict": self.verdict.value,
            "summary": self.summary,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "conclusion_digest": self.conclusion_digest}


def _construct_adjudicated_conclusion(
    value: Mapping[str, Any],
) -> AdjudicatedConclusion:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "conclusion_id",
            "scope",
            "hypothesis_id",
            "hypothesis_digest",
            "baseline_experiment_id",
            "baseline_manifest_digest",
            "candidate_experiment_id",
            "candidate_manifest_digest",
            "policy_id",
            "policy_digest",
            "freeze_digest",
            "result_id",
            "result_digest",
            "comparison_outcome",
            "verdict",
            "summary",
            "conclusion_digest",
        },
        "adjudicated conclusion",
    )
    instance = object.__new__(AdjudicatedConclusion)
    for field_name, field_value in data.items():
        object.__setattr__(instance, field_name, field_value)
    instance.__post_init__()
    return instance


def adjudicated_conclusion_from_dict(value: Any) -> AdjudicatedConclusion:
    if not isinstance(value, Mapping):
        raise InvalidLabRecordError("adjudicated conclusion must be an object")
    return _construct_adjudicated_conclusion(value)
