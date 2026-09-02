from __future__ import annotations

from typing import Any, Mapping

from codexia_manual_agent.lab.errors import InvalidLabRecordError
from codexia_manual_agent.lab.models import (
    ArtifactRecord,
    Conclusion,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    MetricRecord,
)


def _exact_keys(
    value: Any,
    expected: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidLabRecordError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise InvalidLabRecordError(
            f"{label} keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _digest_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidLabRecordError(f"{field_name} must be an array")
    return tuple(value)


def hypothesis_from_dict(value: Any) -> Hypothesis:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "hypothesis_id",
            "created_at",
            "statement",
            "falsification_criterion",
            "hypothesis_digest",
        },
        "hypothesis",
    )
    return Hypothesis(
        schema_version=payload["schema_version"],
        hypothesis_id=payload["hypothesis_id"],
        created_at=payload["created_at"],
        statement=payload["statement"],
        falsification_criterion=payload["falsification_criterion"],
        hypothesis_digest=payload["hypothesis_digest"],
    )


def experiment_manifest_from_dict(value: Any) -> ExperimentManifest:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "experiment_id",
            "created_at",
            "hypothesis_id",
            "hypothesis_digest",
            "procedure",
            "parameters",
            "manifest_digest",
        },
        "experiment manifest",
    )
    return ExperimentManifest(
        schema_version=payload["schema_version"],
        experiment_id=payload["experiment_id"],
        created_at=payload["created_at"],
        hypothesis_id=payload["hypothesis_id"],
        hypothesis_digest=payload["hypothesis_digest"],
        procedure=payload["procedure"],
        parameters=payload["parameters"],
        manifest_digest=payload["manifest_digest"],
    )


def experiment_run_from_dict(value: Any) -> ExperimentRun:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "run_id",
            "created_at",
            "experiment_id",
            "manifest_digest",
            "ordinal",
            "seed",
            "run_digest",
        },
        "experiment run",
    )
    return ExperimentRun(
        schema_version=payload["schema_version"],
        run_id=payload["run_id"],
        created_at=payload["created_at"],
        experiment_id=payload["experiment_id"],
        manifest_digest=payload["manifest_digest"],
        ordinal=payload["ordinal"],
        seed=payload["seed"],
        run_digest=payload["run_digest"],
    )


def artifact_record_from_dict(value: Any) -> ArtifactRecord:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "artifact_id",
            "created_at",
            "run_id",
            "run_digest",
            "manifest_digest",
            "logical_path",
            "size_bytes",
            "sha256",
            "media_type",
            "artifact_digest",
        },
        "artifact record",
    )
    return ArtifactRecord(
        schema_version=payload["schema_version"],
        artifact_id=payload["artifact_id"],
        created_at=payload["created_at"],
        run_id=payload["run_id"],
        run_digest=payload["run_digest"],
        manifest_digest=payload["manifest_digest"],
        logical_path=payload["logical_path"],
        size_bytes=payload["size_bytes"],
        sha256=payload["sha256"],
        media_type=payload["media_type"],
        artifact_digest=payload["artifact_digest"],
    )


def metric_record_from_dict(value: Any) -> MetricRecord:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "metric_id",
            "created_at",
            "run_id",
            "run_digest",
            "manifest_digest",
            "name",
            "value",
            "unit",
            "metric_digest",
        },
        "metric record",
    )
    return MetricRecord(
        schema_version=payload["schema_version"],
        metric_id=payload["metric_id"],
        created_at=payload["created_at"],
        run_id=payload["run_id"],
        run_digest=payload["run_digest"],
        manifest_digest=payload["manifest_digest"],
        name=payload["name"],
        value=payload["value"],
        unit=payload["unit"],
        metric_digest=payload["metric_digest"],
    )


def conclusion_from_dict(value: Any) -> Conclusion:
    payload = _exact_keys(
        value,
        {
            "schema_version",
            "conclusion_id",
            "created_at",
            "hypothesis_id",
            "hypothesis_digest",
            "experiment_id",
            "manifest_digest",
            "verdict",
            "summary",
            "metric_digests",
            "artifact_digests",
            "conclusion_digest",
        },
        "conclusion",
    )
    return Conclusion(
        schema_version=payload["schema_version"],
        conclusion_id=payload["conclusion_id"],
        created_at=payload["created_at"],
        hypothesis_id=payload["hypothesis_id"],
        hypothesis_digest=payload["hypothesis_digest"],
        experiment_id=payload["experiment_id"],
        manifest_digest=payload["manifest_digest"],
        verdict=payload["verdict"],
        summary=payload["summary"],
        metric_digests=_digest_tuple(payload["metric_digests"], "metric_digests"),
        artifact_digests=_digest_tuple(
            payload["artifact_digests"],
            "artifact_digests",
        ),
        conclusion_digest=payload["conclusion_digest"],
    )
