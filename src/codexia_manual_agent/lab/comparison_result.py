from __future__ import annotations

import hmac
import json
import math
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from codexia_manual_agent.lab.comparison import (
    ComparisonDirection,
    ComparisonMissingPolicy,
    FrozenComparisonPolicy,
    SqliteComparisonRegistry,
)
from codexia_manual_agent.lab.errors import (
    EvidenceBindingError,
    InvalidLabRecordError,
    LabIdentityConflictError,
    LabPersistenceError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
)
from codexia_manual_agent.lab.governed_python import (
    PhysicalEvidenceRecovery,
    SqlitePhysicalEvidenceRegistry,
)
from codexia_manual_agent.lab.registry import (
    LabRegistryEventKind,
    LabRegistryRecovery,
    RegisteredRunSnapshot,
    SqliteLabRegistry,
)


COMPARISON_RESULT_SCHEMA_VERSION = 1
COMPARISON_EVIDENCE_SCHEMA_VERSION = 1
MAX_COMPARISON_RESULT_JSON_CHARS = 262_144
MAX_RATIONAL_TEXT_CHARS = 2_048
MAX_SIGNED_64 = 9_223_372_036_854_775_807

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ComparisonOutcome(StrEnum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


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
        raise InvalidLabRecordError("Comparison result payload is not canonical JSON") from exc
    if len(encoded) > MAX_COMPARISON_RESULT_JSON_CHARS:
        raise InvalidLabRecordError("Comparison result payload exceeds its byte budget")
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


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LabPersistenceIntegrityError(
                f"Persisted comparison result JSON contains duplicate key: {key}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise LabPersistenceIntegrityError(
        f"Persisted comparison result contains non-finite constant: {value}"
    )


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


def _validate_seed(value: Any, field_name: str) -> int:
    if type(value) is not int or not -MAX_SIGNED_64 - 1 <= value <= MAX_SIGNED_64:
        raise InvalidLabRecordError(f"{field_name} must be a signed 64-bit integer")
    return value


def _validate_metric_value(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidLabRecordError("comparison evidence metric must be a finite number")
    if isinstance(value, int):
        if not -MAX_SIGNED_64 - 1 <= value <= MAX_SIGNED_64:
            raise InvalidLabRecordError(
                "comparison evidence integer metric exceeds signed 64-bit budget"
            )
    elif not math.isfinite(value):
        raise InvalidLabRecordError("comparison evidence metric must be finite")
    return value


def _fraction(value: int | float) -> Fraction:
    _validate_metric_value(value)
    if type(value) is int:
        return Fraction(value)
    return Fraction.from_float(value)


def _rational_text(value: Fraction) -> str:
    text = str(value)
    if len(text) > MAX_RATIONAL_TEXT_CHARS:
        raise InvalidLabRecordError("Exact comparison rational exceeds its text budget")
    return text


def _validate_rational_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_RATIONAL_TEXT_CHARS:
        raise InvalidLabRecordError(f"{field_name} must be bounded exact rational text")
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise InvalidLabRecordError(f"{field_name} is not an exact rational") from exc
    if str(parsed) != value:
        raise InvalidLabRecordError(f"{field_name} is not canonical rational text")
    return value


def _optional_rational_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _validate_rational_text(value, field_name)


def _validate_missing_seeds(value: Any, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, tuple):
        raise InvalidLabRecordError(f"{field_name} must be a canonical tuple")
    normalized = tuple(_validate_seed(item, field_name) for item in value)
    if len(set(normalized)) != len(normalized):
        raise InvalidLabRecordError(f"{field_name} must be duplicate-free")
    return normalized


def _comparison_result_id(freeze_digest: str) -> str:
    _validate_digest(freeze_digest, "freeze_digest")
    return str(uuid5(NAMESPACE_URL, f"codexia.m4.4.comparison-result.v1:{freeze_digest}"))


@dataclass(frozen=True, slots=True)
class ComparisonEvidenceEntry:
    schema_version: int
    run_id: str
    run_digest: str
    ordinal: int
    seed: int
    metric_id: str
    metric_digest: str
    metric_value: int | float
    artifact_id: str
    artifact_digest: str
    physical_receipt_id: str
    physical_receipt_digest: str
    entry_digest: str

    @classmethod
    def create(
        cls,
        *,
        run: RegisteredRunSnapshot,
        physical: PhysicalEvidenceRecovery,
    ) -> "ComparisonEvidenceEntry":
        if not isinstance(run, RegisteredRunSnapshot):
            raise TypeError("run must be a RegisteredRunSnapshot")
        if not isinstance(physical, PhysicalEvidenceRecovery):
            raise TypeError("physical must be a PhysicalEvidenceRecovery")
        record = run.run
        if record.seed is None:
            raise EvidenceBindingError("M4.4 comparison runs require an exact declared seed")
        if physical.run.run.to_dict() != record.to_dict():
            raise EvidenceBindingError(
                "Physical evidence recovery does not bind the exact comparison run"
            )
        base = {
            "schema_version": COMPARISON_EVIDENCE_SCHEMA_VERSION,
            "run_id": record.run_id,
            "run_digest": record.run_digest,
            "ordinal": record.ordinal,
            "seed": record.seed,
            "metric_id": physical.metric.metric_id,
            "metric_digest": physical.metric.metric_digest,
            "metric_value": physical.metric.value,
            "artifact_id": physical.artifact.artifact_id,
            "artifact_digest": physical.artifact.artifact_digest,
            "physical_receipt_id": physical.receipt.receipt_id,
            "physical_receipt_digest": physical.receipt.receipt_digest,
        }
        return cls(**base, entry_digest=_digest(base))

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != COMPARISON_EVIDENCE_SCHEMA_VERSION
        ):
            raise InvalidLabRecordError("Unsupported M4.4 comparison evidence schema version")
        _validate_uuid(self.run_id, "run_id")
        _validate_digest(self.run_digest, "run_digest")
        if type(self.ordinal) is not int or not 0 <= self.ordinal <= MAX_SIGNED_64:
            raise InvalidLabRecordError("comparison evidence ordinal is invalid")
        _validate_seed(self.seed, "seed")
        _validate_uuid(self.metric_id, "metric_id")
        _validate_digest(self.metric_digest, "metric_digest")
        _validate_metric_value(self.metric_value)
        _validate_uuid(self.artifact_id, "artifact_id")
        _validate_digest(self.artifact_digest, "artifact_digest")
        _validate_uuid(self.physical_receipt_id, "physical_receipt_id")
        _validate_digest(self.physical_receipt_digest, "physical_receipt_digest")
        _validate_digest(self.entry_digest, "entry_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.entry_digest):
            raise InvalidLabRecordError(
                "Comparison evidence entry digest does not match exact payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "run_digest": self.run_digest,
            "ordinal": self.ordinal,
            "seed": self.seed,
            "metric_id": self.metric_id,
            "metric_digest": self.metric_digest,
            "metric_value": self.metric_value,
            "artifact_id": self.artifact_id,
            "artifact_digest": self.artifact_digest,
            "physical_receipt_id": self.physical_receipt_id,
            "physical_receipt_digest": self.physical_receipt_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "entry_digest": self.entry_digest}


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    schema_version: int
    result_id: str
    policy_id: str
    policy_digest: str
    freeze_digest: str
    baseline_experiment_id: str
    baseline_head_event_id: str
    baseline_head_sequence: int
    baseline_head_digest: str
    candidate_experiment_id: str
    candidate_head_event_id: str
    candidate_head_sequence: int
    candidate_head_digest: str
    baseline_evidence: tuple[ComparisonEvidenceEntry, ...]
    candidate_evidence: tuple[ComparisonEvidenceEntry, ...]
    missing_baseline_seeds: tuple[int, ...]
    missing_candidate_seeds: tuple[int, ...]
    baseline_mean: str | None
    candidate_mean: str | None
    effect: str | None
    outcome: ComparisonOutcome
    result_digest: str

    @classmethod
    def create(
        cls,
        *,
        frozen: FrozenComparisonPolicy,
        baseline: LabRegistryRecovery,
        candidate: LabRegistryRecovery,
        baseline_evidence: tuple[ComparisonEvidenceEntry, ...],
        candidate_evidence: tuple[ComparisonEvidenceEntry, ...],
        missing_baseline_seeds: tuple[int, ...],
        missing_candidate_seeds: tuple[int, ...],
        baseline_mean: str | None,
        candidate_mean: str | None,
        effect: str | None,
        outcome: ComparisonOutcome | str,
    ) -> "ComparisonResult":
        if not isinstance(frozen, FrozenComparisonPolicy):
            raise TypeError("frozen must be a FrozenComparisonPolicy")
        if not isinstance(baseline, LabRegistryRecovery) or not isinstance(
            candidate, LabRegistryRecovery
        ):
            raise TypeError("comparison arms must be LabRegistryRecovery objects")
        if not baseline.events or not candidate.events:
            raise EvidenceBindingError("Comparison arms lack durable chronology")
        try:
            normalized_outcome = ComparisonOutcome(outcome)
        except (TypeError, ValueError) as exc:
            raise InvalidLabRecordError("Unsupported comparison outcome") from exc
        baseline_head = baseline.events[-1]
        candidate_head = candidate.events[-1]
        result_id = _comparison_result_id(frozen.freeze_digest)
        base = {
            "schema_version": COMPARISON_RESULT_SCHEMA_VERSION,
            "result_id": result_id,
            "policy_id": frozen.policy.policy_id,
            "policy_digest": frozen.policy.policy_digest,
            "freeze_digest": frozen.freeze_digest,
            "baseline_experiment_id": baseline.experiment_id,
            "baseline_head_event_id": baseline_head.event_id,
            "baseline_head_sequence": baseline_head.sequence,
            "baseline_head_digest": baseline_head.event_digest,
            "candidate_experiment_id": candidate.experiment_id,
            "candidate_head_event_id": candidate_head.event_id,
            "candidate_head_sequence": candidate_head.sequence,
            "candidate_head_digest": candidate_head.event_digest,
            "baseline_evidence": [item.to_dict() for item in baseline_evidence],
            "candidate_evidence": [item.to_dict() for item in candidate_evidence],
            "missing_baseline_seeds": list(missing_baseline_seeds),
            "missing_candidate_seeds": list(missing_candidate_seeds),
            "baseline_mean": baseline_mean,
            "candidate_mean": candidate_mean,
            "effect": effect,
            "outcome": normalized_outcome.value,
        }
        return cls(
            schema_version=COMPARISON_RESULT_SCHEMA_VERSION,
            result_id=result_id,
            policy_id=frozen.policy.policy_id,
            policy_digest=frozen.policy.policy_digest,
            freeze_digest=frozen.freeze_digest,
            baseline_experiment_id=baseline.experiment_id,
            baseline_head_event_id=baseline_head.event_id,
            baseline_head_sequence=baseline_head.sequence,
            baseline_head_digest=baseline_head.event_digest,
            candidate_experiment_id=candidate.experiment_id,
            candidate_head_event_id=candidate_head.event_id,
            candidate_head_sequence=candidate_head.sequence,
            candidate_head_digest=candidate_head.event_digest,
            baseline_evidence=baseline_evidence,
            candidate_evidence=candidate_evidence,
            missing_baseline_seeds=missing_baseline_seeds,
            missing_candidate_seeds=missing_candidate_seeds,
            baseline_mean=baseline_mean,
            candidate_mean=candidate_mean,
            effect=effect,
            outcome=normalized_outcome,
            result_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != COMPARISON_RESULT_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M4.4 comparison result schema version")
        _validate_uuid(self.result_id, "result_id")
        _validate_uuid(self.policy_id, "policy_id")
        _validate_digest(self.policy_digest, "policy_digest")
        _validate_digest(self.freeze_digest, "freeze_digest")
        if self.result_id != _comparison_result_id(self.freeze_digest):
            raise InvalidLabRecordError("result_id does not match the exact frozen policy")
        _validate_uuid(self.baseline_experiment_id, "baseline_experiment_id")
        _validate_uuid(self.baseline_head_event_id, "baseline_head_event_id")
        if type(self.baseline_head_sequence) is not int or self.baseline_head_sequence < 0:
            raise InvalidLabRecordError("baseline_head_sequence must be non-negative")
        _validate_digest(self.baseline_head_digest, "baseline_head_digest")
        _validate_uuid(self.candidate_experiment_id, "candidate_experiment_id")
        _validate_uuid(self.candidate_head_event_id, "candidate_head_event_id")
        if type(self.candidate_head_sequence) is not int or self.candidate_head_sequence < 0:
            raise InvalidLabRecordError("candidate_head_sequence must be non-negative")
        _validate_digest(self.candidate_head_digest, "candidate_head_digest")
        for field_name in ("baseline_evidence", "candidate_evidence"):
            entries = getattr(self, field_name)
            if not isinstance(entries, tuple) or any(
                not isinstance(item, ComparisonEvidenceEntry) for item in entries
            ):
                raise InvalidLabRecordError(f"{field_name} must be a canonical evidence tuple")
            ordinals = tuple(item.ordinal for item in entries)
            if ordinals != tuple(sorted(ordinals)) or len(set(ordinals)) != len(ordinals):
                raise InvalidLabRecordError(
                    f"{field_name} must be strictly ordered by unique ordinal"
                )
        _validate_missing_seeds(self.missing_baseline_seeds, "missing_baseline_seeds")
        _validate_missing_seeds(self.missing_candidate_seeds, "missing_candidate_seeds")
        _optional_rational_text(self.baseline_mean, "baseline_mean")
        _optional_rational_text(self.candidate_mean, "candidate_mean")
        _optional_rational_text(self.effect, "effect")
        try:
            outcome = ComparisonOutcome(self.outcome)
        except (TypeError, ValueError) as exc:
            raise InvalidLabRecordError("Unsupported comparison outcome") from exc
        object.__setattr__(self, "outcome", outcome)
        has_missing = bool(self.missing_baseline_seeds or self.missing_candidate_seeds)
        if outcome is ComparisonOutcome.INCONCLUSIVE:
            if not has_missing or any(
                value is not None
                for value in (self.baseline_mean, self.candidate_mean, self.effect)
            ):
                raise InvalidLabRecordError(
                    "Inconclusive comparison must bind missing evidence and no partial aggregate"
                )
        elif has_missing or any(
            value is None for value in (self.baseline_mean, self.candidate_mean, self.effect)
        ):
            raise InvalidLabRecordError(
                "Supported/refuted comparison requires complete evidence and aggregates"
            )
        _validate_digest(self.result_digest, "result_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.result_digest):
            raise InvalidLabRecordError("Comparison result digest does not match exact payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "result_id": self.result_id,
            "policy_id": self.policy_id,
            "policy_digest": self.policy_digest,
            "freeze_digest": self.freeze_digest,
            "baseline_experiment_id": self.baseline_experiment_id,
            "baseline_head_event_id": self.baseline_head_event_id,
            "baseline_head_sequence": self.baseline_head_sequence,
            "baseline_head_digest": self.baseline_head_digest,
            "candidate_experiment_id": self.candidate_experiment_id,
            "candidate_head_event_id": self.candidate_head_event_id,
            "candidate_head_sequence": self.candidate_head_sequence,
            "candidate_head_digest": self.candidate_head_digest,
            "baseline_evidence": [item.to_dict() for item in self.baseline_evidence],
            "candidate_evidence": [item.to_dict() for item in self.candidate_evidence],
            "missing_baseline_seeds": list(self.missing_baseline_seeds),
            "missing_candidate_seeds": list(self.missing_candidate_seeds),
            "baseline_mean": self.baseline_mean,
            "candidate_mean": self.candidate_mean,
            "effect": self.effect,
            "outcome": self.outcome.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "result_digest": self.result_digest}


def comparison_evidence_entry_from_dict(value: Any) -> ComparisonEvidenceEntry:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "run_id",
            "run_digest",
            "ordinal",
            "seed",
            "metric_id",
            "metric_digest",
            "metric_value",
            "artifact_id",
            "artifact_digest",
            "physical_receipt_id",
            "physical_receipt_digest",
            "entry_digest",
        },
        "comparison evidence entry",
    )
    return ComparisonEvidenceEntry(**data)


def comparison_result_from_dict(value: Any) -> ComparisonResult:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "result_id",
            "policy_id",
            "policy_digest",
            "freeze_digest",
            "baseline_experiment_id",
            "baseline_head_event_id",
            "baseline_head_sequence",
            "baseline_head_digest",
            "candidate_experiment_id",
            "candidate_head_event_id",
            "candidate_head_sequence",
            "candidate_head_digest",
            "baseline_evidence",
            "candidate_evidence",
            "missing_baseline_seeds",
            "missing_candidate_seeds",
            "baseline_mean",
            "candidate_mean",
            "effect",
            "outcome",
            "result_digest",
        },
        "comparison result",
    )
    baseline_evidence = data["baseline_evidence"]
    candidate_evidence = data["candidate_evidence"]
    missing_baseline = data["missing_baseline_seeds"]
    missing_candidate = data["missing_candidate_seeds"]
    if not isinstance(baseline_evidence, list) or not isinstance(candidate_evidence, list):
        raise InvalidLabRecordError("comparison evidence must decode from JSON arrays")
    if not isinstance(missing_baseline, list) or not isinstance(missing_candidate, list):
        raise InvalidLabRecordError("missing comparison seeds must decode from JSON arrays")
    return ComparisonResult(
        schema_version=data["schema_version"],
        result_id=data["result_id"],
        policy_id=data["policy_id"],
        policy_digest=data["policy_digest"],
        freeze_digest=data["freeze_digest"],
        baseline_experiment_id=data["baseline_experiment_id"],
        baseline_head_event_id=data["baseline_head_event_id"],
        baseline_head_sequence=data["baseline_head_sequence"],
        baseline_head_digest=data["baseline_head_digest"],
        candidate_experiment_id=data["candidate_experiment_id"],
        candidate_head_event_id=data["candidate_head_event_id"],
        candidate_head_sequence=data["candidate_head_sequence"],
        candidate_head_digest=data["candidate_head_digest"],
        baseline_evidence=tuple(
            comparison_evidence_entry_from_dict(item) for item in baseline_evidence
        ),
        candidate_evidence=tuple(
            comparison_evidence_entry_from_dict(item) for item in candidate_evidence
        ),
        missing_baseline_seeds=tuple(missing_baseline),
        missing_candidate_seeds=tuple(missing_candidate),
        baseline_mean=data["baseline_mean"],
        candidate_mean=data["candidate_mean"],
        effect=data["effect"],
        outcome=data["outcome"],
        result_digest=data["result_digest"],
    )


class SqliteComparisonResultRegistry:
    """Evaluates one frozen M4.4 policy over complete sealed M4.3 physical evidence."""

    def __init__(
        self,
        comparison_registry: SqliteComparisonRegistry,
        lab_registry: SqliteLabRegistry,
        physical_registry: SqlitePhysicalEvidenceRegistry,
    ) -> None:
        if not isinstance(comparison_registry, SqliteComparisonRegistry):
            raise TypeError("comparison_registry must be a SqliteComparisonRegistry")
        if not isinstance(lab_registry, SqliteLabRegistry):
            raise TypeError("lab_registry must be a SqliteLabRegistry")
        if not isinstance(physical_registry, SqlitePhysicalEvidenceRegistry):
            raise TypeError("physical_registry must be a SqlitePhysicalEvidenceRegistry")
        paths = {
            comparison_registry.database_path.resolve(),
            lab_registry.database_path.resolve(),
            physical_registry.database_path.resolve(),
        }
        if len(paths) != 1:
            raise ValueError("M4.4.2 registries must share one SQLite trust domain")
        self._comparisons = comparison_registry
        self._lab = lab_registry
        self._physical = physical_registry
        self._database_path = lab_registry.database_path
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    @contextmanager
    def _connect(self):
        try:
            with closing(
                sqlite3.connect(
                    self._database_path,
                    timeout=30.0,
                    isolation_level=None,
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 30000")
                yield connection
        except sqlite3.Error as exc:
            raise LabPersistenceError("SQLite comparison-result operation failed") from exc

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lab_comparison_results (
                    policy_id TEXT PRIMARY KEY,
                    result_id TEXT NOT NULL UNIQUE,
                    result_digest TEXT NOT NULL,
                    baseline_head_digest TEXT NOT NULL,
                    candidate_head_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (policy_id)
                        REFERENCES lab_comparison_policy_freezes(policy_id)
                )
                """
            )

    def evaluate(self, policy_id: str) -> ComparisonResult:
        _validate_uuid(policy_id, "policy_id")
        frozen = self._comparisons.recover_policy(policy_id)
        result = self._compute(frozen)
        raw = _canonical_json(result.to_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM lab_comparison_results WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["result_id"] != result.result_id
                    or row["result_digest"] != result.result_digest
                    or row["payload_json"] != raw
                ):
                    connection.execute("ROLLBACK")
                    raise LabIdentityConflictError(
                        "Frozen comparison already has a different durable result"
                    )
                connection.execute("COMMIT")
                return self.recover_result(policy_id)
            connection.execute(
                """
                INSERT INTO lab_comparison_results(
                    policy_id, result_id, result_digest,
                    baseline_head_digest, candidate_head_digest, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_id,
                    result.result_id,
                    result.result_digest,
                    result.baseline_head_digest,
                    result.candidate_head_digest,
                    raw,
                ),
            )
            connection.execute("COMMIT")
        return self.recover_result(policy_id)

    def recover_result(self, policy_id: str) -> ComparisonResult:
        _validate_uuid(policy_id, "policy_id")
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM lab_comparison_results WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise InvalidLabRecordError("Unknown durable comparison result")
            raw = row["payload_json"]
            if not isinstance(raw, str) or len(raw) > MAX_COMPARISON_RESULT_JSON_CHARS:
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError("Persisted comparison result JSON is invalid")
            try:
                value = json.loads(
                    raw,
                    object_pairs_hook=_reject_duplicate_pairs,
                    parse_constant=_reject_constant,
                )
                result = comparison_result_from_dict(value)
                canonical = _canonical_json(result.to_dict())
            except LabPersistenceIntegrityError:
                connection.execute("ROLLBACK")
                raise
            except (
                InvalidLabRecordError,
                EvidenceBindingError,
                TypeError,
                ValueError,
                RecursionError,
                json.JSONDecodeError,
            ) as exc:
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted comparison result failed validation"
                ) from exc
            if canonical != raw:
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted comparison result JSON is not canonical"
                )
            if (
                row["policy_id"] != result.policy_id
                or row["result_id"] != result.result_id
                or row["result_digest"] != result.result_digest
                or row["baseline_head_digest"] != result.baseline_head_digest
                or row["candidate_head_digest"] != result.candidate_head_digest
            ):
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted comparison result indexes disagree with payload"
                )
            connection.execute("COMMIT")

        try:
            frozen = self._comparisons.recover_policy(policy_id)
            expected = self._compute(frozen)
        except LabPersistenceIntegrityError:
            raise
        except (InvalidLabRecordError, EvidenceBindingError, LabRegistryStateError) as exc:
            raise LabPersistenceIntegrityError(
                "Durable comparison dependencies no longer reproduce the result"
            ) from exc
        if expected.to_dict() != result.to_dict():
            raise LabPersistenceIntegrityError(
                "Persisted comparison result differs from deterministic recomputation"
            )
        return result

    def _compute(self, frozen: FrozenComparisonPolicy) -> ComparisonResult:
        policy = frozen.policy
        baseline = self._lab.recover_experiment(policy.baseline_experiment_id)
        candidate = self._lab.recover_experiment(policy.candidate_experiment_id)
        self._validate_final_arm(
            "baseline", baseline, policy.baseline_manifest_digest
        )
        self._validate_final_arm(
            "candidate", candidate, policy.candidate_manifest_digest
        )

        baseline_entries, baseline_missing = self._collect_arm(
            baseline,
            policy.seeds,
            metric_name=policy.metric_name,
            metric_unit=policy.metric_unit,
        )
        candidate_entries, candidate_missing = self._collect_arm(
            candidate,
            policy.seeds,
            metric_name=policy.metric_name,
            metric_unit=policy.metric_unit,
        )
        has_missing = bool(baseline_missing or candidate_missing)
        if has_missing:
            if policy.missing_run_policy is ComparisonMissingPolicy.ERROR:
                raise LabRegistryStateError(
                    "Frozen comparison requires complete physical evidence for every declared seed"
                )
            return ComparisonResult.create(
                frozen=frozen,
                baseline=baseline,
                candidate=candidate,
                baseline_evidence=baseline_entries,
                candidate_evidence=candidate_entries,
                missing_baseline_seeds=baseline_missing,
                missing_candidate_seeds=candidate_missing,
                baseline_mean=None,
                candidate_mean=None,
                effect=None,
                outcome=ComparisonOutcome.INCONCLUSIVE,
            )

        baseline_fraction = sum(
            (_fraction(item.metric_value) for item in baseline_entries),
            Fraction(0),
        ) / len(baseline_entries)
        candidate_fraction = sum(
            (_fraction(item.metric_value) for item in candidate_entries),
            Fraction(0),
        ) / len(candidate_entries)
        if policy.direction is ComparisonDirection.LOWER_IS_BETTER:
            effect_fraction = baseline_fraction - candidate_fraction
        else:
            effect_fraction = candidate_fraction - baseline_fraction
        threshold = _fraction(policy.minimum_effect)
        outcome = (
            ComparisonOutcome.SUPPORTED
            if effect_fraction >= threshold
            else ComparisonOutcome.REFUTED
        )
        return ComparisonResult.create(
            frozen=frozen,
            baseline=baseline,
            candidate=candidate,
            baseline_evidence=baseline_entries,
            candidate_evidence=candidate_entries,
            missing_baseline_seeds=(),
            missing_candidate_seeds=(),
            baseline_mean=_rational_text(baseline_fraction),
            candidate_mean=_rational_text(candidate_fraction),
            effect=_rational_text(effect_fraction),
            outcome=outcome,
        )

    @staticmethod
    def _validate_final_arm(
        label: str,
        recovery: LabRegistryRecovery,
        manifest_digest: str,
    ) -> None:
        if not recovery.experiment_sealed:
            raise LabRegistryStateError(
                f"M4.4.2 requires the {label} experiment to be irreversibly sealed"
            )
        if not hmac.compare_digest(recovery.manifest.manifest_digest, manifest_digest):
            raise EvidenceBindingError(
                f"{label} experiment no longer matches the frozen exact manifest"
            )
        if not recovery.events or recovery.events[-1].kind is not LabRegistryEventKind.EXPERIMENT_SEALED:
            raise LabPersistenceIntegrityError(
                f"{label} sealed experiment lacks a terminal experiment_sealed event"
            )
        if any(not snapshot.evidence_sealed for snapshot in recovery.runs.values()):
            raise LabPersistenceIntegrityError(
                f"{label} sealed experiment contains an unsealed run"
            )

    def _collect_arm(
        self,
        recovery: LabRegistryRecovery,
        seeds: tuple[int, ...],
        *,
        metric_name: str,
        metric_unit: str | None,
    ) -> tuple[tuple[ComparisonEvidenceEntry, ...], tuple[int, ...]]:
        by_ordinal: dict[int, RegisteredRunSnapshot] = {}
        for snapshot in recovery.runs.values():
            run = snapshot.run
            if run.ordinal >= len(seeds):
                raise EvidenceBindingError(
                    "Sealed comparison arm contains an undeclared extra run ordinal"
                )
            expected_seed = seeds[run.ordinal]
            if run.seed != expected_seed:
                raise EvidenceBindingError(
                    "Sealed comparison run seed does not match its frozen ordinal/seed plan"
                )
            if run.ordinal in by_ordinal:
                raise LabPersistenceIntegrityError(
                    "Sealed comparison arm contains duplicate run ordinals"
                )
            by_ordinal[run.ordinal] = snapshot

        entries: list[ComparisonEvidenceEntry] = []
        missing: list[int] = []
        for ordinal, seed in enumerate(seeds):
            snapshot = by_ordinal.get(ordinal)
            if snapshot is None:
                missing.append(seed)
                continue
            if not self._physical_receipt_exists(snapshot.run.run_id):
                missing.append(seed)
                continue
            physical = self._physical.recover(snapshot.run.run_id)
            if physical.metric.name != metric_name or physical.metric.unit != metric_unit:
                raise EvidenceBindingError(
                    "Verified physical metric does not match the frozen comparison metric"
                )
            entries.append(ComparisonEvidenceEntry.create(run=snapshot, physical=physical))
        return tuple(entries), tuple(missing)

    def _physical_receipt_exists(self, run_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id FROM lab_physical_evidence WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return row is not None
