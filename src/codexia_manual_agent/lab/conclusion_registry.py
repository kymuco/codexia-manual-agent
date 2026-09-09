from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from codexia_manual_agent.lab.comparison import SqliteComparisonRegistry
from codexia_manual_agent.lab.comparison_result import SqliteComparisonResultRegistry
from codexia_manual_agent.lab.conclusion_adjudication import (
    MAX_ADJUDICATED_CONCLUSION_JSON_CHARS,
    AdjudicatedConclusion,
    adjudicated_conclusion_from_dict,
)
from codexia_manual_agent.lab.errors import (
    EvidenceBindingError,
    InvalidLabRecordError,
    LabIdentityConflictError,
    LabPersistenceError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
)
from codexia_manual_agent.lab.registry import SqliteLabRegistry


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidLabRecordError("Duplicate JSON key in adjudicated conclusion")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise InvalidLabRecordError(f"Non-finite JSON constant is not admitted: {value}")


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


class SqliteAdjudicatedConclusionRegistry:
    """Durable M4.5.2 cache whose authority remains exact M4.4 recovery."""

    def __init__(
        self,
        comparison_registry: SqliteComparisonRegistry,
        comparison_result_registry: SqliteComparisonResultRegistry,
        lab_registry: SqliteLabRegistry,
    ) -> None:
        if not isinstance(comparison_registry, SqliteComparisonRegistry):
            raise TypeError("comparison_registry must be a SqliteComparisonRegistry")
        if not isinstance(comparison_result_registry, SqliteComparisonResultRegistry):
            raise TypeError(
                "comparison_result_registry must be a SqliteComparisonResultRegistry"
            )
        if not isinstance(lab_registry, SqliteLabRegistry):
            raise TypeError("lab_registry must be a SqliteLabRegistry")
        paths = {
            comparison_registry.database_path.resolve(),
            comparison_result_registry.database_path.resolve(),
            lab_registry.database_path.resolve(),
        }
        if len(paths) != 1:
            raise ValueError("M4.5.2 registries must share one SQLite trust domain")
        self._comparisons = comparison_registry
        self._results = comparison_result_registry
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
            raise LabPersistenceError("SQLite adjudicated-conclusion operation failed") from exc

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lab_adjudicated_conclusions (
                    policy_id TEXT PRIMARY KEY,
                    conclusion_id TEXT NOT NULL UNIQUE,
                    conclusion_digest TEXT NOT NULL,
                    result_id TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (policy_id)
                        REFERENCES lab_comparison_policy_freezes(policy_id),
                    FOREIGN KEY (result_id)
                        REFERENCES lab_comparison_results(result_id)
                )
                """
            )

    def publish(self, policy_id: str) -> AdjudicatedConclusion:
        """Derive from authoritative M4.4 state and publish exactly that conclusion."""

        _validate_uuid(policy_id, "policy_id")
        expected = self._derive(policy_id)
        raw = _canonical_json(expected.to_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM lab_adjudicated_conclusions WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["conclusion_id"] != expected.conclusion_id
                    or row["conclusion_digest"] != expected.conclusion_digest
                    or row["result_id"] != expected.result_id
                    or row["result_digest"] != expected.result_digest
                    or row["payload_json"] != raw
                ):
                    connection.execute("ROLLBACK")
                    raise LabIdentityConflictError(
                        "Frozen comparison already has a different durable conclusion"
                    )
                connection.execute("COMMIT")
                return self.recover(policy_id)
            connection.execute(
                """
                INSERT INTO lab_adjudicated_conclusions(
                    policy_id, conclusion_id, conclusion_digest,
                    result_id, result_digest, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_id,
                    expected.conclusion_id,
                    expected.conclusion_digest,
                    expected.result_id,
                    expected.result_digest,
                    raw,
                ),
            )
            connection.execute("COMMIT")
        return self.recover(policy_id)

    def recover(self, policy_id: str) -> AdjudicatedConclusion:
        """Validate persisted cache, then return fresh deterministic M4.4 derivation."""

        _validate_uuid(policy_id, "policy_id")
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM lab_adjudicated_conclusions WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise InvalidLabRecordError("Unknown durable adjudicated conclusion")
            raw = row["payload_json"]
            if not isinstance(raw, str) or len(raw) > MAX_ADJUDICATED_CONCLUSION_JSON_CHARS:
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted adjudicated conclusion JSON is invalid"
                )
            try:
                value = json.loads(
                    raw,
                    object_pairs_hook=_reject_duplicate_pairs,
                    parse_constant=_reject_constant,
                )
                persisted = adjudicated_conclusion_from_dict(value)
                canonical = _canonical_json(persisted.to_dict())
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
                    "Persisted adjudicated conclusion failed validation"
                ) from exc
            if canonical != raw:
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted adjudicated conclusion JSON is not canonical"
                )
            if (
                row["policy_id"] != persisted.policy_id
                or row["conclusion_id"] != persisted.conclusion_id
                or row["conclusion_digest"] != persisted.conclusion_digest
                or row["result_id"] != persisted.result_id
                or row["result_digest"] != persisted.result_digest
            ):
                connection.execute("ROLLBACK")
                raise LabPersistenceIntegrityError(
                    "Persisted adjudicated conclusion indexes disagree with payload"
                )
            _validate_digest(row["conclusion_digest"], "conclusion_digest")
            _validate_digest(row["result_digest"], "result_digest")
            connection.execute("COMMIT")

        try:
            expected = self._derive(policy_id)
        except LabPersistenceIntegrityError:
            raise
        except (
            InvalidLabRecordError,
            EvidenceBindingError,
            LabRegistryStateError,
        ) as exc:
            raise LabPersistenceIntegrityError(
                "Durable conclusion dependencies no longer reproduce the conclusion"
            ) from exc
        if expected.to_dict() != persisted.to_dict():
            raise LabPersistenceIntegrityError(
                "Persisted adjudicated conclusion differs from deterministic recomputation"
            )
        return expected

    def _derive(self, policy_id: str) -> AdjudicatedConclusion:
        frozen = self._comparisons.recover_policy(policy_id)
        result = self._results.recover_result(policy_id)
        policy = frozen.policy
        baseline = self._lab.recover_experiment(policy.baseline_experiment_id)
        candidate = self._lab.recover_experiment(policy.candidate_experiment_id)
        if baseline.hypothesis.to_dict() != candidate.hypothesis.to_dict():
            raise LabPersistenceIntegrityError(
                "Comparison arms disagree on the exact adjudicated hypothesis"
            )
        return AdjudicatedConclusion.create(
            hypothesis=baseline.hypothesis,
            baseline_manifest=baseline.manifest,
            candidate_manifest=candidate.manifest,
            frozen=frozen,
            result=result,
        )
