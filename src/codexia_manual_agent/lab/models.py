from __future__ import annotations

import hmac
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

from codexia_manual_agent.lab.errors import EvidenceBindingError, InvalidLabRecordError


LAB_SCHEMA_VERSION = 1
MAX_LAB_TEXT_CHARS = 8_192
MAX_LAB_NAME_CHARS = 128
MAX_PARAMETER_BYTES = 65_536
MAX_EVIDENCE_RECORDS = 256
MAX_ARTIFACT_PATH_CHARS = 512
MAX_MEDIA_TYPE_CHARS = 255
MAX_PARAMETER_DEPTH = 32
MAX_SIGNED_64 = 9_223_372_036_854_775_807
MAX_TIMESTAMP_CHARS = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


class ConclusionVerdict(StrEnum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_schema_version(value: Any, label: str) -> int:
    if type(value) is not int or value != LAB_SCHEMA_VERSION:
        raise InvalidLabRecordError(f"Unsupported M4.1 {label} schema version")
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidLabRecordError(f"{field_name} must be a UUID")
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be a UUID") from exc
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidLabRecordError(
            f"{field_name} must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise InvalidLabRecordError(f"{field_name} must include a timezone")
    if parsed.isoformat() != value:
        raise InvalidLabRecordError(f"{field_name} must be canonical ISO-8601")
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _bounded_text(value: Any, *, field_name: str, max_chars: int = MAX_LAB_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise InvalidLabRecordError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized or normalized != value or len(value) > max_chars or "\x00" in value:
        raise InvalidLabRecordError(f"{field_name} is empty, non-canonical, or exceeds its budget")
    return value


def _optional_bounded_text(value: Any, *, field_name: str, max_chars: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name=field_name, max_chars=max_chars)


def _validate_name(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field_name} has an invalid lab identifier")
    return value


def _validate_metric_value(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidLabRecordError("metric value must be a finite number")
    if isinstance(value, int) and not -MAX_SIGNED_64 - 1 <= value <= MAX_SIGNED_64:
        raise InvalidLabRecordError("integer metric value exceeds the signed 64-bit budget")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidLabRecordError("metric value must be a finite number")
    return value


def _freeze_json(value: Any, *, path: str = "parameters", depth: int = 0) -> Any:
    if depth > MAX_PARAMETER_DEPTH:
        raise InvalidLabRecordError(f"{path} exceeds the M4.1 nesting-depth budget")
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if not -MAX_SIGNED_64 - 1 <= value <= MAX_SIGNED_64:
            raise InvalidLabRecordError(f"{path} integer exceeds the signed 64-bit budget")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidLabRecordError(f"{path} cannot contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidLabRecordError(f"{path} object keys must be strings")
            normalized[key] = _freeze_json(item, path=f"{path}.{key}", depth=depth + 1)
        return MappingProxyType(dict(sorted(normalized.items())))
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, path=f"{path}[]", depth=depth + 1)
            for item in value
        )
    raise InvalidLabRecordError(f"{path} must be JSON-compatible")


def _canonical_parameters(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidLabRecordError("parameters must be an object")
    frozen = _freeze_json(value)
    encoded = _canonical_json(frozen).encode("utf-8")
    if len(encoded) > MAX_PARAMETER_BYTES:
        raise InvalidLabRecordError("parameters exceed the M4.1 byte budget")
    return frozen


def _canonical_artifact_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ARTIFACT_PATH_CHARS:
        raise InvalidLabRecordError("artifact logical_path is invalid")
    if "\\" in value or "\x00" in value:
        raise InvalidLabRecordError("artifact logical_path must use canonical POSIX separators")
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise InvalidLabRecordError("artifact logical_path must be a normalized relative path")
    canonical = path.as_posix()
    if canonical != value:
        raise InvalidLabRecordError("artifact logical_path must be canonical")
    return value


def _new_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Hypothesis:
    schema_version: int
    hypothesis_id: str
    created_at: str
    statement: str
    falsification_criterion: str
    hypothesis_digest: str

    @classmethod
    def create(
        cls,
        *,
        statement: str,
        falsification_criterion: str,
        hypothesis_id: str | None = None,
        created_at: str | None = None,
    ) -> "Hypothesis":
        hypothesis_id = hypothesis_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(hypothesis_id, "hypothesis_id")
        _validate_timestamp(created_at, "created_at")
        _bounded_text(statement, field_name="statement")
        _bounded_text(falsification_criterion, field_name="falsification_criterion")
        base = {
            "schema_version": LAB_SCHEMA_VERSION,
            "hypothesis_id": hypothesis_id,
            "created_at": created_at,
            "statement": statement,
            "falsification_criterion": falsification_criterion,
        }
        return cls(**base, hypothesis_digest=_digest(base))

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version, "hypothesis")
        _validate_uuid(self.hypothesis_id, "hypothesis_id")
        _validate_timestamp(self.created_at, "created_at")
        _bounded_text(self.statement, field_name="statement")
        _bounded_text(self.falsification_criterion, field_name="falsification_criterion")
        _validate_digest(self.hypothesis_digest, "hypothesis_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.hypothesis_digest):
            raise InvalidLabRecordError("Hypothesis digest does not match the exact payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "created_at": self.created_at,
            "statement": self.statement,
            "falsification_criterion": self.falsification_criterion,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "hypothesis_digest": self.hypothesis_digest}


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    schema_version: int
    experiment_id: str
    created_at: str
    hypothesis_id: str
    hypothesis_digest: str
    procedure: str
    parameters: Mapping[str, Any]
    manifest_digest: str

    @classmethod
    def create(
        cls,
        *,
        hypothesis: Hypothesis,
        procedure: str,
        parameters: Mapping[str, Any] | None = None,
        experiment_id: str | None = None,
        created_at: str | None = None,
    ) -> "ExperimentManifest":
        if not isinstance(hypothesis, Hypothesis):
            raise TypeError("hypothesis must be a Hypothesis")
        experiment_id = experiment_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(experiment_id, "experiment_id")
        _validate_timestamp(created_at, "created_at")
        _bounded_text(procedure, field_name="procedure")
        normalized_parameters = _canonical_parameters(
            {} if parameters is None else parameters
        )
        base = {
            "schema_version": LAB_SCHEMA_VERSION,
            "experiment_id": experiment_id,
            "created_at": created_at,
            "hypothesis_id": hypothesis.hypothesis_id,
            "hypothesis_digest": hypothesis.hypothesis_digest,
            "procedure": procedure,
            "parameters": normalized_parameters,
        }
        return cls(**base, manifest_digest=_digest(base))

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version, "experiment-manifest")
        _validate_uuid(self.experiment_id, "experiment_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.hypothesis_id, "hypothesis_id")
        _validate_digest(self.hypothesis_digest, "hypothesis_digest")
        _bounded_text(self.procedure, field_name="procedure")
        parameters = _canonical_parameters(self.parameters)
        object.__setattr__(self, "parameters", parameters)
        _validate_digest(self.manifest_digest, "manifest_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.manifest_digest):
            raise InvalidLabRecordError("Experiment manifest digest does not match the exact payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "created_at": self.created_at,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_digest": self.hypothesis_digest,
            "procedure": self.procedure,
            "parameters": self.parameters,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = _thaw_json(self._payload())
        payload["manifest_digest"] = self.manifest_digest
        return payload


@dataclass(frozen=True, slots=True)
class ExperimentRun:
    schema_version: int
    run_id: str
    created_at: str
    experiment_id: str
    manifest_digest: str
    ordinal: int
    seed: int | None
    run_digest: str

    @classmethod
    def create(
        cls,
        *,
        manifest: ExperimentManifest,
        ordinal: int,
        seed: int | None = None,
        run_id: str | None = None,
        created_at: str | None = None,
    ) -> "ExperimentRun":
        if not isinstance(manifest, ExperimentManifest):
            raise TypeError("manifest must be an ExperimentManifest")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 0
            or ordinal > MAX_SIGNED_64
        ):
            raise InvalidLabRecordError(
                "run ordinal must fit a non-negative signed 64-bit integer"
            )
        if seed is not None and (
            not isinstance(seed, int)
            or isinstance(seed, bool)
            or not -MAX_SIGNED_64 - 1 <= seed <= MAX_SIGNED_64
        ):
            raise InvalidLabRecordError(
                "run seed must fit a signed 64-bit integer or be null"
            )
        run_id = run_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(run_id, "run_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": LAB_SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": created_at,
            "experiment_id": manifest.experiment_id,
            "manifest_digest": manifest.manifest_digest,
            "ordinal": ordinal,
            "seed": seed,
        }
        return cls(**base, run_digest=_digest(base))

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version, "experiment-run")
        _validate_uuid(self.run_id, "run_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.experiment_id, "experiment_id")
        _validate_digest(self.manifest_digest, "manifest_digest")
        if (
            not isinstance(self.ordinal, int)
            or isinstance(self.ordinal, bool)
            or self.ordinal < 0
            or self.ordinal > MAX_SIGNED_64
        ):
            raise InvalidLabRecordError(
                "run ordinal must fit a non-negative signed 64-bit integer"
            )
        if self.seed is not None and (
            not isinstance(self.seed, int)
            or isinstance(self.seed, bool)
            or not -MAX_SIGNED_64 - 1 <= self.seed <= MAX_SIGNED_64
        ):
            raise InvalidLabRecordError(
                "run seed must fit a signed 64-bit integer or be null"
            )
        _validate_digest(self.run_digest, "run_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.run_digest):
            raise InvalidLabRecordError("Experiment run digest does not match the exact payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "experiment_id": self.experiment_id,
            "manifest_digest": self.manifest_digest,
            "ordinal": self.ordinal,
            "seed": self.seed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "run_digest": self.run_digest}


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    schema_version: int
    artifact_id: str
    created_at: str
    run_id: str
    run_digest: str
    manifest_digest: str
    logical_path: str
    size_bytes: int
    sha256: str
    media_type: str | None
    artifact_digest: str

    @classmethod
    def create(
        cls,
        *,
        run: ExperimentRun,
        logical_path: str,
        size_bytes: int,
        sha256_digest: str,
        media_type: str | None = None,
        artifact_id: str | None = None,
        created_at: str | None = None,
    ) -> "ArtifactRecord":
        if not isinstance(run, ExperimentRun):
            raise TypeError("run must be an ExperimentRun")
        _canonical_artifact_path(logical_path)
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or size_bytes > MAX_SIGNED_64
        ):
            raise InvalidLabRecordError(
                "artifact size_bytes must fit a non-negative signed 64-bit integer"
            )
        _validate_digest(sha256_digest, "sha256")
        _optional_bounded_text(
            media_type,
            field_name="media_type",
            max_chars=MAX_MEDIA_TYPE_CHARS,
        )
        artifact_id = artifact_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(artifact_id, "artifact_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": LAB_SCHEMA_VERSION,
            "artifact_id": artifact_id,
            "created_at": created_at,
            "run_id": run.run_id,
            "run_digest": run.run_digest,
            "manifest_digest": run.manifest_digest,
            "logical_path": logical_path,
            "size_bytes": size_bytes,
            "sha256": sha256_digest,
            "media_type": media_type,
        }
        return cls(**base, artifact_digest=_digest(base))

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version, "artifact")
        _validate_uuid(self.artifact_id, "artifact_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.run_id, "run_id")
        _validate_digest(self.run_digest, "run_digest")
        _validate_digest(self.manifest_digest, "manifest_digest")
        _canonical_artifact_path(self.logical_path)
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
            or self.size_bytes > MAX_SIGNED_64
        ):
            raise InvalidLabRecordError(
                "artifact size_bytes must fit a non-negative signed 64-bit integer"
            )
        _validate_digest(self.sha256, "sha256")
        _optional_bounded_text(
            self.media_type,
            field_name="media_type",
            max_chars=MAX_MEDIA_TYPE_CHARS,
        )
        _validate_digest(self.artifact_digest, "artifact_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.artifact_digest):
            raise InvalidLabRecordError("Artifact digest does not match the exact payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "run_digest": self.run_digest,
            "manifest_digest": self.manifest_digest,
            "logical_path": self.logical_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "media_type": self.media_type,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "artifact_digest": self.artifact_digest}


@dataclass(frozen=True, slots=True)
class MetricRecord:
    schema_version: int
    metric_id: str
    created_at: str
    run_id: str
    run_digest: str
    manifest_digest: str
    name: str
    value: int | float
    unit: str | None
    metric_digest: str

    @classmethod
    def create(
        cls,
        *,
        run: ExperimentRun,
        name: str,
        value: int | float,
        unit: str | None = None,
        metric_id: str | None = None,
        created_at: str | None = None,
    ) -> "MetricRecord":
        if not isinstance(run, ExperimentRun):
            raise TypeError("run must be an ExperimentRun")
        _validate_name(name, "metric name")
        _validate_metric_value(value)
        _optional_bounded_text(unit, field_name="unit", max_chars=MAX_LAB_NAME_CHARS)
        metric_id = metric_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(metric_id, "metric_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": LAB_SCHEMA_VERSION,
            "metric_id": metric_id,
            "created_at": created_at,
            "run_id": run.run_id,
            "run_digest": run.run_digest,
            "manifest_digest": run.manifest_digest,
            "name": name,
            "value": value,
            "unit": unit,
        }
        return cls(**base, metric_digest=_digest(base))

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version, "metric")
        _validate_uuid(self.metric_id, "metric_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.run_id, "run_id")
        _validate_digest(self.run_digest, "run_digest")
        _validate_digest(self.manifest_digest, "manifest_digest")
        _validate_name(self.name, "metric name")
        _validate_metric_value(self.value)
        _optional_bounded_text(self.unit, field_name="unit", max_chars=MAX_LAB_NAME_CHARS)
        _validate_digest(self.metric_digest, "metric_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.metric_digest):
            raise InvalidLabRecordError("Metric digest does not match the exact payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "metric_id": self.metric_id,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "run_digest": self.run_digest,
            "manifest_digest": self.manifest_digest,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "metric_digest": self.metric_digest}


def _canonical_evidence_digests(
    records: Iterable[Any],
    *,
    expected_type: type[MetricRecord] | type[ArtifactRecord],
    manifest_digest: str,
    field_name: str,
) -> tuple[str, ...]:
    digests: list[str] = []
    for record in records:
        if len(digests) >= MAX_EVIDENCE_RECORDS:
            raise InvalidLabRecordError(
                f"{field_name} exceeds the M4.1 evidence-count budget"
            )
        if not isinstance(record, expected_type):
            raise TypeError(f"{field_name} contains an unsupported evidence record")
        if isinstance(record, MetricRecord):
            digest = record.metric_digest
        else:
            digest = record.artifact_digest
        if not hmac.compare_digest(record.manifest_digest, manifest_digest):
            raise EvidenceBindingError(
                f"{field_name} contains evidence from another manifest"
            )
        digests.append(digest)
    if len(set(digests)) != len(digests):
        raise EvidenceBindingError(f"{field_name} contains duplicate evidence")
    return tuple(sorted(digests))


@dataclass(frozen=True, slots=True)
class Conclusion:
    schema_version: int
    conclusion_id: str
    created_at: str
    hypothesis_id: str
    hypothesis_digest: str
    experiment_id: str
    manifest_digest: str
    verdict: ConclusionVerdict
    summary: str
    metric_digests: tuple[str, ...]
    artifact_digests: tuple[str, ...]
    conclusion_digest: str

    @classmethod
    def create(
        cls,
        *,
        hypothesis: Hypothesis,
        manifest: ExperimentManifest,
        verdict: ConclusionVerdict | str,
        summary: str,
        metrics: Iterable[MetricRecord] = (),
        artifacts: Iterable[ArtifactRecord] = (),
        conclusion_id: str | None = None,
        created_at: str | None = None,
    ) -> "Conclusion":
        if not isinstance(hypothesis, Hypothesis):
            raise TypeError("hypothesis must be a Hypothesis")
        if not isinstance(manifest, ExperimentManifest):
            raise TypeError("manifest must be an ExperimentManifest")
        if (
            manifest.hypothesis_id != hypothesis.hypothesis_id
            or not hmac.compare_digest(
                manifest.hypothesis_digest,
                hypothesis.hypothesis_digest,
            )
        ):
            raise EvidenceBindingError("Manifest is not bound to the exact hypothesis")
        try:
            normalized_verdict = ConclusionVerdict(verdict)
        except (TypeError, ValueError) as exc:
            raise InvalidLabRecordError("Unsupported conclusion verdict") from exc
        _bounded_text(summary, field_name="summary")
        metric_digests = _canonical_evidence_digests(
            metrics,
            expected_type=MetricRecord,
            manifest_digest=manifest.manifest_digest,
            field_name="metrics",
        )
        artifact_digests = _canonical_evidence_digests(
            artifacts,
            expected_type=ArtifactRecord,
            manifest_digest=manifest.manifest_digest,
            field_name="artifacts",
        )
        if (
            normalized_verdict in {ConclusionVerdict.SUPPORTED, ConclusionVerdict.REFUTED}
            and not metric_digests
            and not artifact_digests
        ):
            raise EvidenceBindingError(
                "Supported/refuted conclusions require at least one bound evidence record"
            )
        conclusion_id = conclusion_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        _validate_uuid(conclusion_id, "conclusion_id")
        _validate_timestamp(created_at, "created_at")
        base = {
            "schema_version": LAB_SCHEMA_VERSION,
            "conclusion_id": conclusion_id,
            "created_at": created_at,
            "hypothesis_id": hypothesis.hypothesis_id,
            "hypothesis_digest": hypothesis.hypothesis_digest,
            "experiment_id": manifest.experiment_id,
            "manifest_digest": manifest.manifest_digest,
            "verdict": normalized_verdict.value,
            "summary": summary,
            "metric_digests": metric_digests,
            "artifact_digests": artifact_digests,
        }
        return cls(
            schema_version=LAB_SCHEMA_VERSION,
            conclusion_id=conclusion_id,
            created_at=created_at,
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_digest=hypothesis.hypothesis_digest,
            experiment_id=manifest.experiment_id,
            manifest_digest=manifest.manifest_digest,
            verdict=normalized_verdict,
            summary=summary,
            metric_digests=metric_digests,
            artifact_digests=artifact_digests,
            conclusion_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version, "conclusion")
        _validate_uuid(self.conclusion_id, "conclusion_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.hypothesis_id, "hypothesis_id")
        _validate_digest(self.hypothesis_digest, "hypothesis_digest")
        _validate_uuid(self.experiment_id, "experiment_id")
        _validate_digest(self.manifest_digest, "manifest_digest")
        try:
            verdict = ConclusionVerdict(self.verdict)
        except (TypeError, ValueError) as exc:
            raise InvalidLabRecordError("Unsupported conclusion verdict") from exc
        object.__setattr__(self, "verdict", verdict)
        _bounded_text(self.summary, field_name="summary")
        for field_name in ("metric_digests", "artifact_digests"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple):
                raise InvalidLabRecordError(f"{field_name} must be a canonical tuple")
            if len(values) > MAX_EVIDENCE_RECORDS:
                raise InvalidLabRecordError(
                    f"{field_name} exceeds the evidence-count budget"
                )
            if tuple(sorted(values)) != values or len(set(values)) != len(values):
                raise InvalidLabRecordError(
                    f"{field_name} must be sorted and duplicate-free"
                )
            for digest in values:
                _validate_digest(digest, field_name)
        if (
            verdict in {ConclusionVerdict.SUPPORTED, ConclusionVerdict.REFUTED}
            and not self.metric_digests
            and not self.artifact_digests
        ):
            raise EvidenceBindingError(
                "Supported/refuted conclusions require at least one bound evidence digest"
            )
        _validate_digest(self.conclusion_digest, "conclusion_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.conclusion_digest):
            raise InvalidLabRecordError("Conclusion digest does not match the exact payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "conclusion_id": self.conclusion_id,
            "created_at": self.created_at,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_digest": self.hypothesis_digest,
            "experiment_id": self.experiment_id,
            "manifest_digest": self.manifest_digest,
            "verdict": self.verdict.value,
            "summary": self.summary,
            "metric_digests": self.metric_digests,
            "artifact_digests": self.artifact_digests,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = _thaw_json(self._payload())
        payload["conclusion_digest"] = self.conclusion_digest
        return payload