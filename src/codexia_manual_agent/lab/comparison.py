from __future__ import annotations

import hmac
import json
import math
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

from codexia_manual_agent.lab.errors import (
    EvidenceBindingError,
    InvalidLabRecordError,
    LabIdentityConflictError,
    LabPersistenceError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
)
from codexia_manual_agent.lab.models import ExperimentManifest, Hypothesis
from codexia_manual_agent.lab.registry import (
    LabRegistryEventKind,
    LabRegistryRecovery,
    SqliteLabRegistry,
)


COMPARISON_POLICY_SCHEMA_VERSION = 1
FROZEN_COMPARISON_SCHEMA_VERSION = 1
MAX_COMPARISON_JSON_CHARS = 131_072
MAX_COMPARISON_NAME_CHARS = 128
MAX_COMPARISON_SEEDS = 64
MAX_TIMESTAMP_CHARS = 64
MAX_SIGNED_64 = 9_223_372_036_854_775_807

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


class ComparisonDirection(StrEnum):
    LOWER_IS_BETTER = "lower_is_better"
    HIGHER_IS_BETTER = "higher_is_better"


class ComparisonAggregation(StrEnum):
    MEAN = "mean"


class ComparisonMissingPolicy(StrEnum):
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


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
        raise InvalidLabRecordError("Comparison payload is not canonical JSON") from exc
    if len(encoded) > MAX_COMPARISON_JSON_CHARS:
        raise InvalidLabRecordError("Comparison payload exceeds its byte budget")
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


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidLabRecordError(f"{field_name} must be bounded canonical ISO-8601")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be canonical ISO-8601") from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidLabRecordError(f"{field_name} must be canonical ISO-8601")
    return value


def _validate_name(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field_name} has an invalid identifier")
    return value


def _validate_unit(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > MAX_COMPARISON_NAME_CHARS
        or "\x00" in value
    ):
        raise InvalidLabRecordError("metric_unit is invalid")
    return value


def _validate_effect(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidLabRecordError("minimum_effect must be a finite positive number")
    if isinstance(value, int):
        if not -MAX_SIGNED_64 - 1 <= value <= MAX_SIGNED_64:
            raise InvalidLabRecordError("minimum_effect integer exceeds signed 64-bit budget")
    elif not math.isfinite(value):
        raise InvalidLabRecordError("minimum_effect must be finite")
    if value <= 0:
        raise InvalidLabRecordError("minimum_effect must be greater than zero")
    return value


def _validate_seeds(value: Any) -> tuple[int, ...]:
    if not isinstance(value, tuple):
        raise InvalidLabRecordError("seeds must be a canonical tuple")
    if not value or len(value) > MAX_COMPARISON_SEEDS:
        raise InvalidLabRecordError("seeds must contain 1..64 entries")
    normalized: list[int] = []
    for seed in value:
        if (
            type(seed) is not int
            or not -MAX_SIGNED_64 - 1 <= seed <= MAX_SIGNED_64
        ):
            raise InvalidLabRecordError("each comparison seed must be a signed 64-bit integer")
        normalized.append(seed)
    if len(set(normalized)) != len(normalized):
        raise InvalidLabRecordError("comparison seeds must be unique")
    return tuple(normalized)


def _new_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _same_hypothesis(manifest: ExperimentManifest, hypothesis: Hypothesis) -> bool:
    return (
        manifest.hypothesis_id == hypothesis.hypothesis_id
        and hmac.compare_digest(
            manifest.hypothesis_digest,
            hypothesis.hypothesis_digest,
        )
    )


@dataclass(frozen=True, slots=True)
class ComparisonPolicy:
    schema_version: int
    policy_id: str
    created_at: str
    hypothesis_id: str
    hypothesis_digest: str
    baseline_experiment_id: str
    baseline_manifest_digest: str
    candidate_experiment_id: str
    candidate_manifest_digest: str
    metric_name: str
    metric_unit: str | None
    direction: ComparisonDirection
    minimum_effect: int | float
    seeds: tuple[int, ...]
    aggregation: ComparisonAggregation
    missing_run_policy: ComparisonMissingPolicy
    policy_digest: str

    @classmethod
    def create(
        cls,
        *,
        hypothesis: Hypothesis,
        baseline_manifest: ExperimentManifest,
        candidate_manifest: ExperimentManifest,
        metric_name: str,
        metric_unit: str | None,
        direction: ComparisonDirection | str,
        minimum_effect: int | float,
        seeds: tuple[int, ...],
        aggregation: ComparisonAggregation | str = ComparisonAggregation.MEAN,
        missing_run_policy: ComparisonMissingPolicy | str = ComparisonMissingPolicy.INCONCLUSIVE,
        policy_id: str | None = None,
        created_at: str | None = None,
    ) -> "ComparisonPolicy":
        if not isinstance(hypothesis, Hypothesis):
            raise TypeError("hypothesis must be a Hypothesis")
        if not isinstance(baseline_manifest, ExperimentManifest):
            raise TypeError("baseline_manifest must be an ExperimentManifest")
        if not isinstance(candidate_manifest, ExperimentManifest):
            raise TypeError("candidate_manifest must be an ExperimentManifest")
        if not _same_hypothesis(baseline_manifest, hypothesis):
            raise EvidenceBindingError(
                "Baseline manifest is not bound to the exact comparison hypothesis"
            )
        if not _same_hypothesis(candidate_manifest, hypothesis):
            raise EvidenceBindingError(
                "Candidate manifest is not bound to the exact comparison hypothesis"
            )
        if baseline_manifest.experiment_id == candidate_manifest.experiment_id:
            raise EvidenceBindingError("Comparison arms must use distinct experiments")
        try:
            normalized_direction = ComparisonDirection(direction)
            normalized_aggregation = ComparisonAggregation(aggregation)
            normalized_missing = ComparisonMissingPolicy(missing_run_policy)
        except (TypeError, ValueError) as exc:
            raise InvalidLabRecordError("Unsupported comparison policy enum value") from exc
        _validate_name(metric_name, "metric_name")
        _validate_unit(metric_unit)
        _validate_effect(minimum_effect)
        normalized_seeds = _validate_seeds(seeds)
        policy_id = policy_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(policy_id, "policy_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": COMPARISON_POLICY_SCHEMA_VERSION,
            "policy_id": policy_id,
            "created_at": created_at,
            "hypothesis_id": hypothesis.hypothesis_id,
            "hypothesis_digest": hypothesis.hypothesis_digest,
            "baseline_experiment_id": baseline_manifest.experiment_id,
            "baseline_manifest_digest": baseline_manifest.manifest_digest,
            "candidate_experiment_id": candidate_manifest.experiment_id,
            "candidate_manifest_digest": candidate_manifest.manifest_digest,
            "metric_name": metric_name,
            "metric_unit": metric_unit,
            "direction": normalized_direction.value,
            "minimum_effect": minimum_effect,
            "seeds": list(normalized_seeds),
            "aggregation": normalized_aggregation.value,
            "missing_run_policy": normalized_missing.value,
        }
        return cls(
            schema_version=COMPARISON_POLICY_SCHEMA_VERSION,
            policy_id=policy_id,
            created_at=created_at,
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_digest=hypothesis.hypothesis_digest,
            baseline_experiment_id=baseline_manifest.experiment_id,
            baseline_manifest_digest=baseline_manifest.manifest_digest,
            candidate_experiment_id=candidate_manifest.experiment_id,
            candidate_manifest_digest=candidate_manifest.manifest_digest,
            metric_name=metric_name,
            metric_unit=metric_unit,
            direction=normalized_direction,
            minimum_effect=minimum_effect,
            seeds=normalized_seeds,
            aggregation=normalized_aggregation,
            missing_run_policy=normalized_missing,
            policy_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != COMPARISON_POLICY_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M4.4 comparison policy schema version")
        _validate_uuid(self.policy_id, "policy_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.hypothesis_id, "hypothesis_id")
        _validate_digest(self.hypothesis_digest, "hypothesis_digest")
        _validate_uuid(self.baseline_experiment_id, "baseline_experiment_id")
        _validate_digest(self.baseline_manifest_digest, "baseline_manifest_digest")
        _validate_uuid(self.candidate_experiment_id, "candidate_experiment_id")
        _validate_digest(self.candidate_manifest_digest, "candidate_manifest_digest")
        if self.baseline_experiment_id == self.candidate_experiment_id:
            raise EvidenceBindingError("Comparison arms must use distinct experiments")
        try:
            direction = ComparisonDirection(self.direction)
            aggregation = ComparisonAggregation(self.aggregation)
            missing = ComparisonMissingPolicy(self.missing_run_policy)
        except (TypeError, ValueError) as exc:
            raise InvalidLabRecordError("Unsupported comparison policy enum value") from exc
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "aggregation", aggregation)
        object.__setattr__(self, "missing_run_policy", missing)
        _validate_name(self.metric_name, "metric_name")
        _validate_unit(self.metric_unit)
        _validate_effect(self.minimum_effect)
        _validate_seeds(self.seeds)
        _validate_digest(self.policy_digest, "policy_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.policy_digest):
            raise InvalidLabRecordError("Comparison policy digest does not match exact payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "created_at": self.created_at,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_digest": self.hypothesis_digest,
            "baseline_experiment_id": self.baseline_experiment_id,
            "baseline_manifest_digest": self.baseline_manifest_digest,
            "candidate_experiment_id": self.candidate_experiment_id,
            "candidate_manifest_digest": self.candidate_manifest_digest,
            "metric_name": self.metric_name,
            "metric_unit": self.metric_unit,
            "direction": self.direction.value,
            "minimum_effect": self.minimum_effect,
            "seeds": list(self.seeds),
            "aggregation": self.aggregation.value,
            "missing_run_policy": self.missing_run_policy.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "policy_digest": self.policy_digest}


@dataclass(frozen=True, slots=True)
class FrozenComparisonPolicy:
    schema_version: int
    policy: ComparisonPolicy
    frozen_at: str
    baseline_event_id: str
    baseline_event_sequence: int
    baseline_event_digest: str
    candidate_event_id: str
    candidate_event_sequence: int
    candidate_event_digest: str
    freeze_digest: str

    @classmethod
    def create(
        cls,
        *,
        policy: ComparisonPolicy,
        baseline_event_id: str,
        baseline_event_sequence: int,
        baseline_event_digest: str,
        candidate_event_id: str,
        candidate_event_sequence: int,
        candidate_event_digest: str,
        frozen_at: str | None = None,
    ) -> "FrozenComparisonPolicy":
        if not isinstance(policy, ComparisonPolicy):
            raise TypeError("policy must be a ComparisonPolicy")
        frozen_at = frozen_at or _new_timestamp()
        _validate_timestamp(frozen_at, "frozen_at")
        _validate_uuid(baseline_event_id, "baseline_event_id")
        _validate_uuid(candidate_event_id, "candidate_event_id")
        if type(baseline_event_sequence) is not int or baseline_event_sequence < 0:
            raise InvalidLabRecordError("baseline_event_sequence must be non-negative")
        if type(candidate_event_sequence) is not int or candidate_event_sequence < 0:
            raise InvalidLabRecordError("candidate_event_sequence must be non-negative")
        _validate_digest(baseline_event_digest, "baseline_event_digest")
        _validate_digest(candidate_event_digest, "candidate_event_digest")
        base = {
            "schema_version": FROZEN_COMPARISON_SCHEMA_VERSION,
            "policy": policy.to_dict(),
            "frozen_at": frozen_at,
            "baseline_event_id": baseline_event_id,
            "baseline_event_sequence": baseline_event_sequence,
            "baseline_event_digest": baseline_event_digest,
            "candidate_event_id": candidate_event_id,
            "candidate_event_sequence": candidate_event_sequence,
            "candidate_event_digest": candidate_event_digest,
        }
        return cls(
            schema_version=FROZEN_COMPARISON_SCHEMA_VERSION,
            policy=policy,
            frozen_at=frozen_at,
            baseline_event_id=baseline_event_id,
            baseline_event_sequence=baseline_event_sequence,
            baseline_event_digest=baseline_event_digest,
            candidate_event_id=candidate_event_id,
            candidate_event_sequence=candidate_event_sequence,
            candidate_event_digest=candidate_event_digest,
            freeze_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != FROZEN_COMPARISON_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M4.4 frozen policy schema version")
        if not isinstance(self.policy, ComparisonPolicy):
            raise TypeError("policy must be a ComparisonPolicy")
        _validate_timestamp(self.frozen_at, "frozen_at")
        _validate_uuid(self.baseline_event_id, "baseline_event_id")
        _validate_uuid(self.candidate_event_id, "candidate_event_id")
        if type(self.baseline_event_sequence) is not int or self.baseline_event_sequence < 0:
            raise InvalidLabRecordError("baseline_event_sequence must be non-negative")
        if type(self.candidate_event_sequence) is not int or self.candidate_event_sequence < 0:
            raise InvalidLabRecordError("candidate_event_sequence must be non-negative")
        _validate_digest(self.baseline_event_digest, "baseline_event_digest")
        _validate_digest(self.candidate_event_digest, "candidate_event_digest")
        _validate_digest(self.freeze_digest, "freeze_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.freeze_digest):
            raise InvalidLabRecordError("Frozen comparison digest does not match exact payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy": self.policy.to_dict(),
            "frozen_at": self.frozen_at,
            "baseline_event_id": self.baseline_event_id,
            "baseline_event_sequence": self.baseline_event_sequence,
            "baseline_event_digest": self.baseline_event_digest,
            "candidate_event_id": self.candidate_event_id,
            "candidate_event_sequence": self.candidate_event_sequence,
            "candidate_event_digest": self.candidate_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "freeze_digest": self.freeze_digest}


def comparison_policy_from_dict(value: Any) -> ComparisonPolicy:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "policy_id",
            "created_at",
            "hypothesis_id",
            "hypothesis_digest",
            "baseline_experiment_id",
            "baseline_manifest_digest",
            "candidate_experiment_id",
            "candidate_manifest_digest",
            "metric_name",
            "metric_unit",
            "direction",
            "minimum_effect",
            "seeds",
            "aggregation",
            "missing_run_policy",
            "policy_digest",
        },
        "comparison policy",
    )
    seeds = data["seeds"]
    if not isinstance(seeds, list):
        raise InvalidLabRecordError("comparison policy seeds must decode from a JSON array")
    return ComparisonPolicy(
        schema_version=data["schema_version"],
        policy_id=data["policy_id"],
        created_at=data["created_at"],
        hypothesis_id=data["hypothesis_id"],
        hypothesis_digest=data["hypothesis_digest"],
        baseline_experiment_id=data["baseline_experiment_id"],
        baseline_manifest_digest=data["baseline_manifest_digest"],
        candidate_experiment_id=data["candidate_experiment_id"],
        candidate_manifest_digest=data["candidate_manifest_digest"],
        metric_name=data["metric_name"],
        metric_unit=data["metric_unit"],
        direction=data["direction"],
        minimum_effect=data["minimum_effect"],
        seeds=tuple(seeds),
        aggregation=data["aggregation"],
        missing_run_policy=data["missing_run_policy"],
        policy_digest=data["policy_digest"],
    )


def frozen_comparison_policy_from_dict(value: Any) -> FrozenComparisonPolicy:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "policy",
            "frozen_at",
            "baseline_event_id",
            "baseline_event_sequence",
            "baseline_event_digest",
            "candidate_event_id",
            "candidate_event_sequence",
            "candidate_event_digest",
            "freeze_digest",
        },
        "frozen comparison policy",
    )
    return FrozenComparisonPolicy(
        schema_version=data["schema_version"],
        policy=comparison_policy_from_dict(data["policy"]),
        frozen_at=data["frozen_at"],
        baseline_event_id=data["baseline_event_id"],
        baseline_event_sequence=data["baseline_event_sequence"],
        baseline_event_digest=data["baseline_event_digest"],
        candidate_event_id=data["candidate_event_id"],
        candidate_event_sequence=data["candidate_event_sequence"],
        candidate_event_digest=data["candidate_event_digest"],
        freeze_digest=data["freeze_digest"],
    )


class SqliteComparisonRegistry:
    """Durably freezes M4.4 comparison criteria before either arm has run evidence."""

    def __init__(self, lab_registry: SqliteLabRegistry) -> None:
        if not isinstance(lab_registry, SqliteLabRegistry):
            raise TypeError("lab_registry must be a SqliteLabRegistry")
        self._lab = lab_registry
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
            raise LabPersistenceError("SQLite comparison-policy operation failed") from exc

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lab_comparison_policy_freezes (
                    policy_id TEXT PRIMARY KEY,
                    policy_digest TEXT NOT NULL UNIQUE,
                    baseline_experiment_id TEXT NOT NULL,
                    candidate_experiment_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    freeze_digest TEXT NOT NULL,
                    FOREIGN KEY (baseline_experiment_id)
                        REFERENCES lab_registry_experiments(experiment_id),
                    FOREIGN KEY (candidate_experiment_id)
                        REFERENCES lab_registry_experiments(experiment_id)
                )
                """
            )

    def register_policy(self, policy: ComparisonPolicy) -> FrozenComparisonPolicy:
        if not isinstance(policy, ComparisonPolicy):
            raise TypeError("policy must be a ComparisonPolicy")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM lab_comparison_policy_freezes WHERE policy_id = ?",
                (policy.policy_id,),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                recovered = self.recover_policy(policy.policy_id)
                if recovered.policy != policy:
                    raise LabIdentityConflictError(
                        "Comparison policy id is already bound to another exact policy"
                    )
                return recovered
            digest_owner = connection.execute(
                "SELECT policy_id FROM lab_comparison_policy_freezes WHERE policy_digest = ?",
                (policy.policy_digest,),
            ).fetchone()
            if digest_owner is not None:
                connection.execute("ROLLBACK")
                raise LabIdentityConflictError(
                    "Exact comparison policy digest is already frozen under another id"
                )

            baseline = self._lab.recover_experiment(policy.baseline_experiment_id)
            candidate = self._lab.recover_experiment(policy.candidate_experiment_id)
            self._validate_policy_dependencies(
                policy,
                baseline,
                candidate,
                require_pre_evidence=True,
            )
            baseline_event = baseline.events[0]
            candidate_event = candidate.events[0]
            frozen = FrozenComparisonPolicy.create(
                policy=policy,
                baseline_event_id=baseline_event.event_id,
                baseline_event_sequence=baseline_event.sequence,
                baseline_event_digest=baseline_event.event_digest,
                candidate_event_id=candidate_event.event_id,
                candidate_event_sequence=candidate_event.sequence,
                candidate_event_digest=candidate_event.event_digest,
            )
            raw = _canonical_json(frozen.to_dict())
            connection.execute(
                """
                INSERT INTO lab_comparison_policy_freezes(
                    policy_id,
                    policy_digest,
                    baseline_experiment_id,
                    candidate_experiment_id,
                    payload_json,
                    freeze_digest
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    policy.policy_id,
                    policy.policy_digest,
                    policy.baseline_experiment_id,
                    policy.candidate_experiment_id,
                    raw,
                    frozen.freeze_digest,
                ),
            )
            connection.execute("COMMIT")
        return self.recover_policy(policy.policy_id)

    def recover_policy(self, policy_id: str) -> FrozenComparisonPolicy:
        _validate_uuid(policy_id, "policy_id")
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM lab_comparison_policy_freezes WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise InvalidLabRecordError("Unknown frozen comparison policy")
            raw = row["payload_json"]
            if not isinstance(raw, str) or len(raw) > MAX_COMPARISON_JSON_CHARS:
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted comparison policy JSON is invalid"
                )
            try:
                value = json.loads(raw)
                frozen = frozen_comparison_policy_from_dict(value)
                canonical = _canonical_json(frozen.to_dict())
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
                    "Persisted comparison policy failed validation"
                ) from exc
            if canonical != raw:
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted comparison policy JSON is not canonical"
                )
            if (
                row["policy_id"] != frozen.policy.policy_id
                or row["policy_digest"] != frozen.policy.policy_digest
                or row["baseline_experiment_id"] != frozen.policy.baseline_experiment_id
                or row["candidate_experiment_id"] != frozen.policy.candidate_experiment_id
                or row["freeze_digest"] != frozen.freeze_digest
            ):
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted comparison policy indexes disagree with payload"
                )
            connection.execute("COMMIT")

        try:
            baseline = self._lab.recover_experiment(frozen.policy.baseline_experiment_id)
            candidate = self._lab.recover_experiment(frozen.policy.candidate_experiment_id)
            self._validate_policy_dependencies(
                frozen.policy,
                baseline,
                candidate,
                require_pre_evidence=False,
            )
            self._validate_freeze_anchor(frozen, baseline, candidate)
        except (
            InvalidLabRecordError,
            EvidenceBindingError,
            LabRegistryStateError,
        ) as exc:
            raise LabPersistenceIntegrityError(
                "Frozen comparison policy no longer matches durable lab state"
            ) from exc
        return frozen

    @staticmethod
    def _validate_policy_dependencies(
        policy: ComparisonPolicy,
        baseline: LabRegistryRecovery,
        candidate: LabRegistryRecovery,
        *,
        require_pre_evidence: bool,
    ) -> None:
        for label, recovery, experiment_id, manifest_digest in (
            (
                "baseline",
                baseline,
                policy.baseline_experiment_id,
                policy.baseline_manifest_digest,
            ),
            (
                "candidate",
                candidate,
                policy.candidate_experiment_id,
                policy.candidate_manifest_digest,
            ),
        ):
            if recovery.experiment_id != experiment_id:
                raise EvidenceBindingError(f"{label} recovery resolved another experiment")
            if (
                recovery.hypothesis.hypothesis_id != policy.hypothesis_id
                or not hmac.compare_digest(
                    recovery.hypothesis.hypothesis_digest,
                    policy.hypothesis_digest,
                )
                or recovery.manifest.hypothesis_id != policy.hypothesis_id
                or not hmac.compare_digest(
                    recovery.manifest.hypothesis_digest,
                    policy.hypothesis_digest,
                )
            ):
                raise EvidenceBindingError(
                    f"{label} experiment is not bound to the exact policy hypothesis"
                )
            if not hmac.compare_digest(
                recovery.manifest.manifest_digest,
                manifest_digest,
            ):
                raise EvidenceBindingError(
                    f"{label} experiment does not match the exact policy manifest"
                )
            if require_pre_evidence:
                if recovery.experiment_sealed:
                    raise LabRegistryStateError(
                        f"Cannot freeze comparison after {label} experiment is sealed"
                    )
                if recovery.runs:
                    raise LabRegistryStateError(
                        f"Cannot freeze comparison after {label} has a registered run"
                    )
                if (
                    len(recovery.events) != 1
                    or recovery.events[0].kind is not LabRegistryEventKind.EXPERIMENT_REGISTERED
                ):
                    raise LabRegistryStateError(
                        f"{label} comparison arm is not at the pre-run experiment state"
                    )

    @staticmethod
    def _validate_freeze_anchor(
        frozen: FrozenComparisonPolicy,
        baseline: LabRegistryRecovery,
        candidate: LabRegistryRecovery,
    ) -> None:
        for label, recovery, event_id, sequence, event_digest in (
            (
                "baseline",
                baseline,
                frozen.baseline_event_id,
                frozen.baseline_event_sequence,
                frozen.baseline_event_digest,
            ),
            (
                "candidate",
                candidate,
                frozen.candidate_event_id,
                frozen.candidate_event_sequence,
                frozen.candidate_event_digest,
            ),
        ):
            if not recovery.events:
                raise LabPersistenceIntegrityError(
                    f"{label} experiment lost its registration chronology"
                )
            first = recovery.events[0]
            if (
                first.kind is not LabRegistryEventKind.EXPERIMENT_REGISTERED
                or first.event_id != event_id
                or first.sequence != sequence
                or not hmac.compare_digest(first.event_digest, event_digest)
            ):
                raise LabPersistenceIntegrityError(
                    f"{label} pre-evidence freeze anchor no longer matches M4.2 chronology"
                )
