from __future__ import annotations

import hmac
import json
import math
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from codexia_manual_agent.lab.errors import (
    EvidenceBindingError,
    InvalidLabRecordError,
    LabError,
    LabIdentityConflictError,
    LabPersistenceError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
)
from codexia_manual_agent.lab.models import (
    ArtifactRecord,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    MetricRecord,
)
from codexia_manual_agent.lab.serialization import (
    artifact_record_from_dict,
    experiment_manifest_from_dict,
    experiment_run_from_dict,
    hypothesis_from_dict,
    metric_record_from_dict,
)


LAB_REGISTRY_EVENT_SCHEMA_VERSION = 1
MAX_LAB_REGISTRY_EVENT_PAYLOAD_BYTES = 2_097_152
MAX_LAB_REGISTRY_RAW_JSON_CHARS = MAX_LAB_REGISTRY_EVENT_PAYLOAD_BYTES
MAX_LAB_REGISTRY_EVENT_DEPTH = 40
MAX_LAB_REGISTRY_TIMESTAMP_CHARS = 64
MAX_SIGNED_64 = 9_223_372_036_854_775_807

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LabRegistryEventKind(StrEnum):
    EXPERIMENT_REGISTERED = "experiment_registered"
    RUN_REGISTERED = "run_registered"
    METRIC_REGISTERED = "metric_registered"
    ARTIFACT_REGISTERED = "artifact_registered"
    RUN_SEALED = "run_sealed"
    EXPERIMENT_SEALED = "experiment_sealed"


def _normalize_event_kind(value: Any) -> LabRegistryEventKind:
    try:
        return LabRegistryEventKind(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError("Unknown M4.2 lab registry event kind") from exc


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LabPersistenceIntegrityError(
                f"Persisted lab registry JSON contains duplicate key: {key}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise LabPersistenceIntegrityError(
        f"Persisted lab registry JSON contains non-finite constant: {value}"
    )


def _load_json(raw: Any) -> Any:
    if not isinstance(raw, str):
        raise LabPersistenceIntegrityError("Persisted lab registry JSON must be text")
    if len(raw) > MAX_LAB_REGISTRY_RAW_JSON_CHARS:
        raise LabPersistenceIntegrityError(
            "Persisted lab registry raw JSON exceeds the pre-parse character budget"
        )
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except LabPersistenceIntegrityError:
        raise
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise LabPersistenceIntegrityError(
            "Persisted lab registry event payload is not valid JSON"
        ) from exc


def _json_compatible(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_LAB_REGISTRY_EVENT_DEPTH:
        raise InvalidLabRecordError("Lab registry event payload exceeds nesting budget")
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if not -MAX_SIGNED_64 - 1 <= value <= MAX_SIGNED_64:
            raise InvalidLabRecordError(
                "Lab registry event integer exceeds signed 64-bit budget"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidLabRecordError(
                "Lab registry event payload cannot contain NaN or infinity"
            )
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidLabRecordError("Lab registry event object keys must be strings")
            normalized[key] = _json_compatible(item, depth=depth + 1)
        return dict(sorted(normalized.items()))
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item, depth=depth + 1) for item in value]
    raise InvalidLabRecordError(
        f"Lab registry event payload must be JSON-compatible, got {type(value).__name__}"
    )


def _freeze_event_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_event_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_event_value(item) for item in value)
    return value


def canonical_registry_json(value: Any) -> str:
    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _load_canonical_payload(raw: Any) -> Mapping[str, Any]:
    payload = _load_json(raw)
    if not isinstance(payload, Mapping):
        raise LabPersistenceIntegrityError(
            "Persisted lab registry event payload must be an object"
        )
    try:
        canonical = canonical_registry_json(payload)
    except LabError as exc:
        raise LabPersistenceIntegrityError(
            f"Persisted lab registry JSON violates canonical bounds: {exc}"
        ) from exc
    if canonical != raw:
        raise LabPersistenceIntegrityError(
            "Persisted lab registry event payload is not canonical JSON"
        )
    return payload


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
        raise InvalidLabRecordError(f"{field_name} must be a UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be a UUID") from exc
    if str(parsed) != value:
        raise InvalidLabRecordError(
            f"{field_name} must use canonical lowercase hyphenated UUID form"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _validate_persisted_digest(value: Any, field_name: str) -> str:
    try:
        return _validate_digest(value, field_name)
    except InvalidLabRecordError as exc:
        raise LabPersistenceIntegrityError(
            f"Persisted {field_name} is not canonical SHA-256 hex"
        ) from exc


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_LAB_REGISTRY_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidLabRecordError(
            f"{field_name} must be bounded canonical ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidLabRecordError(
            f"{field_name} must be canonical timezone-aware ISO-8601"
        )
    return value


def _validate_sequence(value: Any) -> int:
    if type(value) is not int or value < 0 or value > MAX_SIGNED_64:
        raise InvalidLabRecordError(
            "Lab registry event sequence must be a non-negative signed 64-bit integer"
        )
    return value


def _validate_event_link(sequence: int, previous_event_digest: Any) -> None:
    if sequence == 0:
        if previous_event_digest is not None:
            raise InvalidLabRecordError(
                "Sequence-zero lab registry event cannot have a previous digest"
            )
        return
    if previous_event_digest is None:
        raise InvalidLabRecordError(
            "Non-first lab registry event requires a previous digest"
        )
    _validate_digest(previous_event_digest, "previous_event_digest")


def _validate_experiment_pair(
    hypothesis: Hypothesis,
    manifest: ExperimentManifest,
) -> None:
    if (
        manifest.hypothesis_id != hypothesis.hypothesis_id
        or not hmac.compare_digest(
            manifest.hypothesis_digest,
            hypothesis.hypothesis_digest,
        )
    ):
        raise EvidenceBindingError(
            "Experiment manifest is not bound to the exact durable hypothesis"
        )


def validate_lab_registry_event_payload(
    kind: LabRegistryEventKind | str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    normalized_kind = _normalize_event_kind(kind)

    if normalized_kind is LabRegistryEventKind.EXPERIMENT_REGISTERED:
        value = _exact_keys(
            payload,
            {"hypothesis", "manifest"},
            normalized_kind.value,
        )
        hypothesis = hypothesis_from_dict(value["hypothesis"])
        manifest = experiment_manifest_from_dict(value["manifest"])
        _validate_uuid(hypothesis.hypothesis_id, "hypothesis.hypothesis_id")
        _validate_uuid(manifest.experiment_id, "manifest.experiment_id")
        _validate_uuid(manifest.hypothesis_id, "manifest.hypothesis_id")
        _validate_experiment_pair(hypothesis, manifest)
    elif normalized_kind is LabRegistryEventKind.RUN_REGISTERED:
        value = _exact_keys(payload, {"run"}, normalized_kind.value)
        run = experiment_run_from_dict(value["run"])
        _validate_uuid(run.run_id, "run.run_id")
        _validate_uuid(run.experiment_id, "run.experiment_id")
    elif normalized_kind is LabRegistryEventKind.METRIC_REGISTERED:
        value = _exact_keys(payload, {"metric"}, normalized_kind.value)
        metric = metric_record_from_dict(value["metric"])
        _validate_uuid(metric.metric_id, "metric.metric_id")
        _validate_uuid(metric.run_id, "metric.run_id")
    elif normalized_kind is LabRegistryEventKind.ARTIFACT_REGISTERED:
        value = _exact_keys(payload, {"artifact"}, normalized_kind.value)
        artifact = artifact_record_from_dict(value["artifact"])
        _validate_uuid(artifact.artifact_id, "artifact.artifact_id")
        _validate_uuid(artifact.run_id, "artifact.run_id")
    elif normalized_kind is LabRegistryEventKind.RUN_SEALED:
        value = _exact_keys(
            payload,
            {"run_id", "run_digest"},
            normalized_kind.value,
        )
        _validate_uuid(value["run_id"], "run_id")
        _validate_digest(value["run_digest"], "run_digest")
    elif normalized_kind is LabRegistryEventKind.EXPERIMENT_SEALED:
        value = _exact_keys(payload, {"manifest_digest"}, normalized_kind.value)
        _validate_digest(value["manifest_digest"], "manifest_digest")
    else:  # pragma: no cover - enum exhaustiveness
        raise InvalidLabRecordError("Unknown M4.2 lab registry event kind")

    normalized = _json_compatible(payload)
    encoded = canonical_registry_json(normalized).encode("utf-8")
    if len(encoded) > MAX_LAB_REGISTRY_EVENT_PAYLOAD_BYTES:
        raise InvalidLabRecordError("Lab registry event payload exceeds M4.2 byte budget")
    assert isinstance(normalized, Mapping)
    frozen = _freeze_event_value(normalized)
    assert isinstance(frozen, Mapping)
    return frozen


def _event_digest(payload: Mapping[str, Any]) -> str:
    return sha256(canonical_registry_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LabRegistryEventReceipt:
    schema_version: int
    event_id: str
    experiment_id: str
    sequence: int
    created_at: str
    kind: LabRegistryEventKind
    payload: Mapping[str, Any]
    previous_event_digest: str | None
    event_digest: str

    @classmethod
    def create(
        cls,
        *,
        experiment_id: str,
        sequence: int,
        kind: LabRegistryEventKind | str,
        payload: Mapping[str, Any],
        previous_event_digest: str | None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> "LabRegistryEventReceipt":
        event_id = event_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        _validate_uuid(event_id, "event_id")
        _validate_uuid(experiment_id, "experiment_id")
        _validate_sequence(sequence)
        _validate_timestamp(created_at, "created_at")
        _validate_event_link(sequence, previous_event_digest)
        normalized_kind = _normalize_event_kind(kind)
        normalized_payload = validate_lab_registry_event_payload(
            normalized_kind,
            payload,
        )
        base = {
            "schema_version": LAB_REGISTRY_EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "experiment_id": experiment_id,
            "sequence": sequence,
            "created_at": created_at,
            "kind": normalized_kind.value,
            "payload": normalized_payload,
            "previous_event_digest": previous_event_digest,
        }
        return cls(
            schema_version=LAB_REGISTRY_EVENT_SCHEMA_VERSION,
            event_id=event_id,
            experiment_id=experiment_id,
            sequence=sequence,
            created_at=created_at,
            kind=normalized_kind,
            payload=normalized_payload,
            previous_event_digest=previous_event_digest,
            event_digest=_event_digest(base),
        )

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != LAB_REGISTRY_EVENT_SCHEMA_VERSION
        ):
            raise InvalidLabRecordError("Unsupported M4.2 registry event schema version")
        _validate_uuid(self.event_id, "event_id")
        _validate_uuid(self.experiment_id, "experiment_id")
        _validate_sequence(self.sequence)
        _validate_timestamp(self.created_at, "created_at")
        _validate_event_link(self.sequence, self.previous_event_digest)
        kind = _normalize_event_kind(self.kind)
        payload = validate_lab_registry_event_payload(kind, self.payload)
        _validate_digest(self.event_digest, "event_digest")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "payload", payload)
        if not hmac.compare_digest(
            _event_digest(self._base_payload()),
            self.event_digest,
        ):
            raise InvalidLabRecordError(
                "Lab registry event digest does not match the exact payload"
            )

    def _base_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "experiment_id": self.experiment_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "kind": self.kind.value,
            "payload": self.payload,
            "previous_event_digest": self.previous_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        value = _json_compatible(self._base_payload())
        value["event_digest"] = self.event_digest
        return value


@dataclass(slots=True)
class _RunReplayState:
    run: ExperimentRun
    sealed: bool = False
    metrics: dict[str, MetricRecord] = field(default_factory=dict)
    artifacts: dict[str, ArtifactRecord] = field(default_factory=dict)
    metric_names: dict[str, str] = field(default_factory=dict)
    artifact_paths: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class _ExperimentReplayState:
    experiment_id: str
    hypothesis: Hypothesis
    manifest: ExperimentManifest
    sealed: bool = False
    runs: dict[str, _RunReplayState] = field(default_factory=dict)
    run_ordinals: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _RegistrationDiscoverySnapshot:
    rows_by_kind: Mapping[LabRegistryEventKind, tuple[sqlite3.Row, ...]]
    hypothesis_owners: Mapping[str, Mapping[str, str]]
    record_roots: Mapping[str, Mapping[str, frozenset[str]]]
    experiment_states: Mapping[str, _ExperimentReplayState]


@dataclass(frozen=True, slots=True)
class _DerivedIndexSnapshot:
    hypothesis_roots: tuple[sqlite3.Row, ...]
    hypothesis_row: sqlite3.Row | None
    run_rows: Mapping[str, tuple[sqlite3.Row, ...]]
    metric_rows: Mapping[str, tuple[sqlite3.Row, ...]]
    artifact_rows: Mapping[str, tuple[sqlite3.Row, ...]]
    metric_scoped_rows: Mapping[tuple[str, str], tuple[sqlite3.Row, ...]]
    artifact_scoped_rows: Mapping[tuple[str, str], tuple[sqlite3.Row, ...]]
    experiment_run_rows: Mapping[str, tuple[sqlite3.Row, ...]]
    experiment_metric_rows: Mapping[str, tuple[sqlite3.Row, ...]]
    experiment_artifact_rows: Mapping[str, tuple[sqlite3.Row, ...]]


def _require_experiment_open(state: _ExperimentReplayState) -> None:
    if state.sealed:
        raise LabRegistryStateError("Experiment registry is sealed")


def _require_run_open(run_state: _RunReplayState) -> None:
    if run_state.sealed:
        raise LabRegistryStateError("Run evidence registry is sealed")


def apply_lab_registry_event(
    state: _ExperimentReplayState | None,
    kind: LabRegistryEventKind | str,
    payload: Mapping[str, Any],
    *,
    experiment_id: str,
) -> _ExperimentReplayState:
    normalized_kind = _normalize_event_kind(kind)
    value = validate_lab_registry_event_payload(normalized_kind, payload)

    if normalized_kind is LabRegistryEventKind.EXPERIMENT_REGISTERED:
        if state is not None:
            raise LabRegistryStateError("experiment_registered can appear only once")
        hypothesis = hypothesis_from_dict(value["hypothesis"])
        manifest = experiment_manifest_from_dict(value["manifest"])
        _validate_experiment_pair(hypothesis, manifest)
        if manifest.experiment_id != experiment_id:
            raise EvidenceBindingError(
                "Registered experiment manifest does not bind the exact registry root"
            )
        return _ExperimentReplayState(
            experiment_id=experiment_id,
            hypothesis=hypothesis,
            manifest=manifest,
        )

    if state is None:
        raise LabRegistryStateError(
            "Lab registry chronology must start with experiment_registered"
        )
    if state.experiment_id != experiment_id:
        raise EvidenceBindingError("Lab registry event experiment differs from replay root")

    if normalized_kind is LabRegistryEventKind.RUN_REGISTERED:
        _require_experiment_open(state)
        run = experiment_run_from_dict(value["run"])
        if run.experiment_id != state.experiment_id:
            raise EvidenceBindingError("Run references another experiment")
        if not hmac.compare_digest(run.manifest_digest, state.manifest.manifest_digest):
            raise EvidenceBindingError("Run does not bind the exact durable manifest")
        if run.run_id in state.runs:
            raise LabIdentityConflictError("Run id is already registered")
        if run.ordinal in state.run_ordinals:
            raise LabIdentityConflictError("Run ordinal is already registered for experiment")
        state.runs[run.run_id] = _RunReplayState(run=run)
        state.run_ordinals[run.ordinal] = run.run_id

    elif normalized_kind is LabRegistryEventKind.METRIC_REGISTERED:
        metric = metric_record_from_dict(value["metric"])
        try:
            run_state = state.runs[metric.run_id]
        except KeyError as exc:
            raise EvidenceBindingError("Metric references an unknown durable run") from exc
        _require_experiment_open(state)
        _require_run_open(run_state)
        if not hmac.compare_digest(metric.run_digest, run_state.run.run_digest):
            raise EvidenceBindingError("Metric does not bind the exact durable run")
        if not hmac.compare_digest(
            metric.manifest_digest,
            state.manifest.manifest_digest,
        ):
            raise EvidenceBindingError("Metric does not bind the exact durable manifest")
        if metric.metric_id in run_state.metrics:
            raise LabIdentityConflictError("Metric id is already registered")
        if metric.name in run_state.metric_names:
            raise LabIdentityConflictError("Metric name is already registered for run")
        run_state.metrics[metric.metric_id] = metric
        run_state.metric_names[metric.name] = metric.metric_id

    elif normalized_kind is LabRegistryEventKind.ARTIFACT_REGISTERED:
        artifact = artifact_record_from_dict(value["artifact"])
        try:
            run_state = state.runs[artifact.run_id]
        except KeyError as exc:
            raise EvidenceBindingError("Artifact references an unknown durable run") from exc
        _require_experiment_open(state)
        _require_run_open(run_state)
        if not hmac.compare_digest(artifact.run_digest, run_state.run.run_digest):
            raise EvidenceBindingError("Artifact does not bind the exact durable run")
        if not hmac.compare_digest(
            artifact.manifest_digest,
            state.manifest.manifest_digest,
        ):
            raise EvidenceBindingError("Artifact does not bind the exact durable manifest")
        if artifact.artifact_id in run_state.artifacts:
            raise LabIdentityConflictError("Artifact id is already registered")
        if artifact.logical_path in run_state.artifact_paths:
            raise LabIdentityConflictError(
                "Artifact logical path is already registered for run"
            )
        run_state.artifacts[artifact.artifact_id] = artifact
        run_state.artifact_paths[artifact.logical_path] = artifact.artifact_id

    elif normalized_kind is LabRegistryEventKind.RUN_SEALED:
        _require_experiment_open(state)
        run_id = value["run_id"]
        try:
            run_state = state.runs[run_id]
        except KeyError as exc:
            raise EvidenceBindingError("Cannot seal an unknown durable run") from exc
        if not hmac.compare_digest(value["run_digest"], run_state.run.run_digest):
            raise EvidenceBindingError("Run seal does not bind the exact durable run")
        if run_state.sealed:
            raise LabRegistryStateError("Run evidence registry is already sealed")
        run_state.sealed = True

    elif normalized_kind is LabRegistryEventKind.EXPERIMENT_SEALED:
        _require_experiment_open(state)
        if not hmac.compare_digest(
            value["manifest_digest"],
            state.manifest.manifest_digest,
        ):
            raise EvidenceBindingError(
                "Experiment seal does not bind the exact durable manifest"
            )
        if any(not run_state.sealed for run_state in state.runs.values()):
            raise LabRegistryStateError(
                "Experiment cannot be sealed while a run evidence registry is open"
            )
        state.sealed = True

    return state


@dataclass(frozen=True, slots=True)
class RegisteredRunSnapshot:
    run: ExperimentRun
    evidence_sealed: bool
    metrics: Mapping[str, MetricRecord]
    artifacts: Mapping[str, ArtifactRecord]

    def metric(self, metric_id: str) -> MetricRecord:
        try:
            return self.metrics[metric_id]
        except KeyError as exc:
            raise InvalidLabRecordError("Unknown metric id") from exc

    def artifact(self, artifact_id: str) -> ArtifactRecord:
        try:
            return self.artifacts[artifact_id]
        except KeyError as exc:
            raise InvalidLabRecordError("Unknown artifact id") from exc


@dataclass(frozen=True, slots=True)
class LabRegistryRecovery:
    experiment_id: str
    hypothesis: Hypothesis
    manifest: ExperimentManifest
    experiment_sealed: bool
    events: tuple[LabRegistryEventReceipt, ...]
    runs: Mapping[str, RegisteredRunSnapshot]

    def run(self, run_id: str) -> RegisteredRunSnapshot:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise InvalidLabRecordError("Unknown run id") from exc

    def metric(self, metric_id: str) -> MetricRecord:
        for snapshot in self.runs.values():
            if metric_id in snapshot.metrics:
                return snapshot.metrics[metric_id]
        raise InvalidLabRecordError("Unknown metric id")

    def artifact(self, artifact_id: str) -> ArtifactRecord:
        for snapshot in self.runs.values():
            if artifact_id in snapshot.artifacts:
                return snapshot.artifacts[artifact_id]
        raise InvalidLabRecordError("Unknown artifact id")


def _public_recovery(
    state: _ExperimentReplayState,
    events: tuple[LabRegistryEventReceipt, ...],
) -> LabRegistryRecovery:
    runs: dict[str, RegisteredRunSnapshot] = {}
    for run_id, run_state in state.runs.items():
        runs[run_id] = RegisteredRunSnapshot(
            run=run_state.run,
            evidence_sealed=run_state.sealed,
            metrics=MappingProxyType(dict(run_state.metrics)),
            artifacts=MappingProxyType(dict(run_state.artifacts)),
        )
    return LabRegistryRecovery(
        experiment_id=state.experiment_id,
        hypothesis=state.hypothesis,
        manifest=state.manifest,
        experiment_sealed=state.sealed,
        events=events,
        runs=MappingProxyType(runs),
    )


_RecordDecoder = Callable[[Any], Any]

_RECORD_LOOKUP_SPECS: dict[
    str,
    tuple[LabRegistryEventKind, str, str, _RecordDecoder],
] = {
    "run": (
        LabRegistryEventKind.RUN_REGISTERED,
        "run",
        "run_id",
        experiment_run_from_dict,
    ),
    "metric": (
        LabRegistryEventKind.METRIC_REGISTERED,
        "metric",
        "metric_id",
        metric_record_from_dict,
    ),
    "artifact": (
        LabRegistryEventKind.ARTIFACT_REGISTERED,
        "artifact",
        "artifact_id",
        artifact_record_from_dict,
    ),
}


class SqliteLabRegistry:
    """Authoritative append-only M4.2 experiment/run/evidence registry."""

    def __init__(self, database_path: str | Path) -> None:
        if str(database_path) == ":memory:":
            raise LabPersistenceError(
                "Durable lab registry requires a filesystem-backed SQLite database path"
            )
        self._database_path = Path(database_path)
        try:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LabPersistenceError(
                "Could not create durable lab registry parent directory"
            ) from exc
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _sqlite_connection(self):
        try:
            with closing(self._connect()) as connection:
                yield connection
        except sqlite3.Error as exc:
            raise LabPersistenceError("SQLite lab registry operation failed") from exc

    def _initialize(self) -> None:
        with self._sqlite_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lab_registry_hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    hypothesis_digest TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lab_registry_experiments (
                    experiment_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL,
                    hypothesis_digest TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    head_sequence INTEGER NOT NULL,
                    head_event_digest TEXT,
                    registered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lab_registry_events (
                    experiment_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_event_digest TEXT,
                    event_digest TEXT NOT NULL,
                    PRIMARY KEY (experiment_id, sequence),
                    FOREIGN KEY (experiment_id)
                        REFERENCES lab_registry_experiments(experiment_id)
                );

                CREATE TABLE IF NOT EXISTS lab_registry_runs (
                    run_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    run_digest TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    UNIQUE (experiment_id, ordinal),
                    FOREIGN KEY (experiment_id)
                        REFERENCES lab_registry_experiments(experiment_id)
                );

                CREATE TABLE IF NOT EXISTS lab_registry_metrics (
                    metric_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    run_digest TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    metric_digest TEXT NOT NULL,
                    name TEXT NOT NULL,
                    UNIQUE (run_id, name),
                    FOREIGN KEY (experiment_id)
                        REFERENCES lab_registry_experiments(experiment_id),
                    FOREIGN KEY (run_id)
                        REFERENCES lab_registry_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS lab_registry_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    run_digest TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    artifact_digest TEXT NOT NULL,
                    logical_path TEXT NOT NULL,
                    UNIQUE (run_id, logical_path),
                    FOREIGN KEY (experiment_id)
                        REFERENCES lab_registry_experiments(experiment_id),
                    FOREIGN KEY (run_id)
                        REFERENCES lab_registry_runs(run_id)
                );
                """
            )

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("ROLLBACK")

    def register_experiment(
        self,
        hypothesis: Hypothesis,
        manifest: ExperimentManifest,
    ) -> LabRegistryRecovery:
        if not isinstance(hypothesis, Hypothesis):
            raise TypeError("hypothesis must be a Hypothesis")
        if not isinstance(manifest, ExperimentManifest):
            raise TypeError("manifest must be an ExperimentManifest")
        _validate_experiment_pair(hypothesis, manifest)
        receipt = LabRegistryEventReceipt.create(
            experiment_id=manifest.experiment_id,
            sequence=0,
            kind=LabRegistryEventKind.EXPERIMENT_REGISTERED,
            payload={
                "hypothesis": hypothesis.to_dict(),
                "manifest": manifest.to_dict(),
            },
            previous_event_digest=None,
        )

        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing_root = self._uuid_identity_row(
                    connection,
                    table="lab_registry_experiments",
                    id_column="experiment_id",
                    record_id=manifest.experiment_id,
                    record_label="Experiment",
                )
                if existing_root is not None:
                    self._load_experiment(connection, manifest.experiment_id)
                    raise LabIdentityConflictError("Experiment id is already registered")
                self._audit_new_experiment_namespace(connection, manifest.experiment_id)
                self._admit_hypothesis_identity(connection, hypothesis)
                try:
                    connection.execute(
                        """
                        INSERT INTO lab_registry_experiments(
                            experiment_id, hypothesis_id, hypothesis_digest,
                            manifest_digest, head_sequence, head_event_digest,
                            registered_at
                        ) VALUES (?, ?, ?, ?, -1, NULL, ?)
                        """,
                        (
                            manifest.experiment_id,
                            hypothesis.hypothesis_id,
                            hypothesis.hypothesis_digest,
                            manifest.manifest_digest,
                            receipt.created_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise LabPersistenceError(
                        "Could not publish durable lab experiment root"
                    ) from exc
                state = apply_lab_registry_event(
                    None,
                    receipt.kind,
                    receipt.payload,
                    experiment_id=manifest.experiment_id,
                )
                self._insert_event(
                    connection,
                    receipt,
                    old_sequence=-1,
                    old_digest=None,
                )
                connection.execute("COMMIT")
                return _public_recovery(state, (receipt,))
            except Exception:
                self._rollback(connection)
                raise

    def register_run(self, run: ExperimentRun) -> LabRegistryRecovery:
        if not isinstance(run, ExperimentRun):
            raise TypeError("run must be an ExperimentRun")
        _validate_uuid(run.run_id, "run.run_id")
        _validate_uuid(run.experiment_id, "run.experiment_id")
        return self._append_record_event(
            experiment_id=run.experiment_id,
            kind=LabRegistryEventKind.RUN_REGISTERED,
            payload={"run": run.to_dict()},
        )

    def register_metric(self, metric: MetricRecord) -> LabRegistryRecovery:
        if not isinstance(metric, MetricRecord):
            raise TypeError("metric must be a MetricRecord")
        _validate_uuid(metric.metric_id, "metric.metric_id")
        _validate_uuid(metric.run_id, "metric.run_id")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                experiment_id = self._experiment_for_record(
                    connection,
                    record_type="run",
                    record_id=metric.run_id,
                    unknown_message="Unknown run id",
                )
                recovery = self._append_loaded_event(
                    connection,
                    experiment_id=experiment_id,
                    kind=LabRegistryEventKind.METRIC_REGISTERED,
                    payload={"metric": metric.to_dict()},
                )
                connection.execute("COMMIT")
                return recovery
            except Exception:
                self._rollback(connection)
                raise

    def register_artifact(self, artifact: ArtifactRecord) -> LabRegistryRecovery:
        if not isinstance(artifact, ArtifactRecord):
            raise TypeError("artifact must be an ArtifactRecord")
        _validate_uuid(artifact.artifact_id, "artifact.artifact_id")
        _validate_uuid(artifact.run_id, "artifact.run_id")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                experiment_id = self._experiment_for_record(
                    connection,
                    record_type="run",
                    record_id=artifact.run_id,
                    unknown_message="Unknown run id",
                )
                recovery = self._append_loaded_event(
                    connection,
                    experiment_id=experiment_id,
                    kind=LabRegistryEventKind.ARTIFACT_REGISTERED,
                    payload={"artifact": artifact.to_dict()},
                )
                connection.execute("COMMIT")
                return recovery
            except Exception:
                self._rollback(connection)
                raise

    def seal_run(self, run_id: str, run_digest: str) -> LabRegistryRecovery:
        _validate_uuid(run_id, "run_id")
        _validate_digest(run_digest, "run_digest")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                experiment_id = self._experiment_for_record(
                    connection,
                    record_type="run",
                    record_id=run_id,
                    unknown_message="Unknown run id",
                )
                recovery = self._append_loaded_event(
                    connection,
                    experiment_id=experiment_id,
                    kind=LabRegistryEventKind.RUN_SEALED,
                    payload={"run_id": run_id, "run_digest": run_digest},
                )
                connection.execute("COMMIT")
                return recovery
            except Exception:
                self._rollback(connection)
                raise

    def seal_experiment(
        self,
        experiment_id: str,
        manifest_digest: str,
    ) -> LabRegistryRecovery:
        _validate_uuid(experiment_id, "experiment_id")
        _validate_digest(manifest_digest, "manifest_digest")
        return self._append_record_event(
            experiment_id=experiment_id,
            kind=LabRegistryEventKind.EXPERIMENT_SEALED,
            payload={"manifest_digest": manifest_digest},
        )

    def recover_experiment(self, experiment_id: str) -> LabRegistryRecovery:
        _validate_uuid(experiment_id, "experiment_id")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN")
                state, events = self._load_experiment(connection, experiment_id)
                connection.execute("COMMIT")
                return _public_recovery(state, events)
            except Exception:
                self._rollback(connection)
                raise

    def recover_for_run(self, run_id: str) -> LabRegistryRecovery:
        _validate_uuid(run_id, "run_id")
        return self._recover_for_record(
            record_type="run",
            record_id=run_id,
            unknown_message="Unknown run id",
        )

    def recover_for_metric(self, metric_id: str) -> LabRegistryRecovery:
        _validate_uuid(metric_id, "metric_id")
        return self._recover_for_record(
            record_type="metric",
            record_id=metric_id,
            unknown_message="Unknown metric id",
        )

    def recover_for_artifact(self, artifact_id: str) -> LabRegistryRecovery:
        _validate_uuid(artifact_id, "artifact_id")
        return self._recover_for_record(
            record_type="artifact",
            record_id=artifact_id,
            unknown_message="Unknown artifact id",
        )

    def _append_record_event(
        self,
        *,
        experiment_id: str,
        kind: LabRegistryEventKind,
        payload: Mapping[str, Any],
    ) -> LabRegistryRecovery:
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                recovery = self._append_loaded_event(
                    connection,
                    experiment_id=experiment_id,
                    kind=kind,
                    payload=payload,
                )
                connection.execute("COMMIT")
                return recovery
            except Exception:
                self._rollback(connection)
                raise

    def _append_loaded_event(
        self,
        connection: sqlite3.Connection,
        *,
        experiment_id: str,
        kind: LabRegistryEventKind,
        payload: Mapping[str, Any],
    ) -> LabRegistryRecovery:
        discovery = self._registration_discovery_snapshot(connection)
        state, events = self._load_experiment(
            connection,
            experiment_id,
            discovery=discovery,
        )
        previous = events[-1]
        receipt = LabRegistryEventReceipt.create(
            experiment_id=experiment_id,
            sequence=previous.sequence + 1,
            kind=kind,
            payload=payload,
            previous_event_digest=previous.event_digest,
        )
        self._audit_authoritative_record_identity(
            connection,
            receipt,
            discovery=discovery,
        )
        state = apply_lab_registry_event(
            state,
            receipt.kind,
            receipt.payload,
            experiment_id=experiment_id,
        )
        self._insert_event(
            connection,
            receipt,
            old_sequence=previous.sequence,
            old_digest=previous.event_digest,
        )
        self._insert_derived_index(connection, receipt)
        return _public_recovery(state, events + (receipt,))

    def _recover_for_record(
        self,
        *,
        record_type: str,
        record_id: str,
        unknown_message: str,
    ) -> LabRegistryRecovery:
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN")
                experiment_id = self._experiment_for_record(
                    connection,
                    record_type=record_type,
                    record_id=record_id,
                    unknown_message=unknown_message,
                )
                state, events = self._load_experiment(connection, experiment_id)
                connection.execute("COMMIT")
                return _public_recovery(state, events)
            except Exception:
                self._rollback(connection)
                raise

    def _experiment_for_record(
        self,
        connection: sqlite3.Connection,
        *,
        record_type: str,
        record_id: str,
        unknown_message: str,
    ) -> str:
        if record_type == "run":
            table, id_column = "lab_registry_runs", "run_id"
        elif record_type == "metric":
            table, id_column = "lab_registry_metrics", "metric_id"
        elif record_type == "artifact":
            table, id_column = "lab_registry_artifacts", "artifact_id"
        else:  # pragma: no cover - private call-site exhaustiveness
            raise AssertionError("Unsupported lab registry record type")

        row = self._uuid_identity_row(
            connection,
            table=table,
            id_column=id_column,
            record_id=record_id,
            record_label=record_type,
        )
        if row is not None:
            owner_experiment_id = str(row["experiment_id"])
            owner_root = self._uuid_identity_row(
                connection,
                table="lab_registry_experiments",
                id_column="experiment_id",
                record_id=owner_experiment_id,
                record_label="Experiment",
            )
            if owner_root is None:
                raise LabPersistenceIntegrityError(
                    f"Derived {record_type} navigation index references an unknown experiment root"
                )
            return owner_experiment_id

        discovery = self._registration_discovery_snapshot(connection)
        chronology_root = self._chronology_root_for_record(
            connection,
            record_type=record_type,
            record_id=record_id,
            discovery=discovery,
        )
        if chronology_root is None:
            raise InvalidLabRecordError(unknown_message)

        try:
            self._load_experiment(
                connection,
                chronology_root,
                discovery=discovery,
            )
        except LabPersistenceIntegrityError:
            raise
        except InvalidLabRecordError as exc:
            raise LabPersistenceIntegrityError(
                f"Authoritative {record_type} registration references an unknown experiment root"
            ) from exc
        raise LabPersistenceIntegrityError(
            f"Derived {record_type} navigation index is missing for durable chronology"
        )

    @classmethod
    def _experiment_root_snapshot(
        cls,
        connection: sqlite3.Connection,
        experiment_ids: set[str],
    ) -> Mapping[str, sqlite3.Row]:
        if not experiment_ids:
            return MappingProxyType({})
        for experiment_id in experiment_ids:
            try:
                requested_uuid = UUID(experiment_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise LabPersistenceIntegrityError(
                    "Persisted experiment lookup identity is not a UUID"
                ) from exc
            if str(requested_uuid) != experiment_id:
                raise LabPersistenceIntegrityError(
                    "Persisted experiment lookup identity is noncanonical"
                )

        rows = connection.execute("SELECT * FROM lab_registry_experiments").fetchall()
        selected: dict[str, sqlite3.Row] = {}
        for row in rows:
            persisted_id = row["experiment_id"]
            if not isinstance(persisted_id, str):
                raise LabPersistenceIntegrityError(
                    "Persisted experiment identity is not text"
                )
            try:
                canonical_id = str(UUID(persisted_id))
            except (TypeError, ValueError, AttributeError) as exc:
                raise LabPersistenceIntegrityError(
                    "Persisted experiment identity is not a UUID"
                ) from exc
            if canonical_id not in experiment_ids:
                continue
            if persisted_id != canonical_id:
                raise LabPersistenceIntegrityError(
                    "Persisted experiment identity uses a noncanonical UUID alias"
                )
            if canonical_id in selected:
                raise LabPersistenceIntegrityError(
                    "Persisted experiment identity contains duplicate exact rows"
                )
            selected[canonical_id] = row
        return MappingProxyType(selected)

    @classmethod
    def _registration_discovery_snapshot(
        cls,
        connection: sqlite3.Connection,
    ) -> _RegistrationDiscoverySnapshot:
        rows = connection.execute(
            """
            SELECT experiment_id, sequence, event_id, created_at, kind,
                   payload_json, previous_event_digest, event_digest
            FROM lab_registry_events
            ORDER BY experiment_id ASC, sequence ASC
            """
        ).fetchall()
        validated: dict[str, list[tuple[sqlite3.Row, LabRegistryEventReceipt]]] = {}
        for row in rows:
            persisted_kind = row["kind"]
            if not isinstance(persisted_kind, str):
                raise LabPersistenceIntegrityError(
                    "Persisted lab registry event kind is not text"
                )
            payload = _load_canonical_payload(row["payload_json"])
            try:
                receipt = LabRegistryEventReceipt(
                    schema_version=LAB_REGISTRY_EVENT_SCHEMA_VERSION,
                    event_id=row["event_id"],
                    experiment_id=row["experiment_id"],
                    sequence=row["sequence"],
                    created_at=row["created_at"],
                    kind=persisted_kind,
                    payload=payload,
                    previous_event_digest=row["previous_event_digest"],
                    event_digest=row["event_digest"],
                )
            except LabPersistenceIntegrityError:
                raise
            except LabError as exc:
                raise LabPersistenceIntegrityError(
                    f"Persisted lab registry event is invalid: {exc}"
                ) from exc
            validated.setdefault(receipt.experiment_id, []).append((row, receipt))

        experiment_roots = cls._experiment_root_snapshot(connection, set(validated))
        rows_by_kind: dict[LabRegistryEventKind, list[sqlite3.Row]] = {
            kind: [] for kind in LabRegistryEventKind
        }
        hypothesis_owners: dict[str, dict[str, str]] = {}
        record_roots: dict[str, dict[str, set[str]]] = {
            "run": {},
            "metric": {},
            "artifact": {},
        }
        experiment_states: dict[str, _ExperimentReplayState] = {}

        for experiment_id, event_rows in validated.items():
            root_row = experiment_roots.get(experiment_id)
            if root_row is None:
                raise LabPersistenceIntegrityError(
                    "Authoritative lab registry event references an unknown experiment root"
                )
            head_sequence = root_row["head_sequence"]
            head_digest = root_row["head_event_digest"]
            if (
                type(head_sequence) is not int
                or head_sequence < 0
                or not isinstance(head_digest, str)
            ):
                raise LabPersistenceIntegrityError(
                    "Durable lab experiment head is malformed during registration discovery"
                )
            head_digest = _validate_persisted_digest(
                head_digest,
                "head_event_digest",
            )
            root_hypothesis_digest = _validate_persisted_digest(
                root_row["hypothesis_digest"],
                "root hypothesis_digest during registration discovery",
            )
            root_manifest_digest = _validate_persisted_digest(
                root_row["manifest_digest"],
                "root manifest_digest during registration discovery",
            )
            if len(event_rows) != head_sequence + 1:
                raise LabPersistenceIntegrityError(
                    "Durable lab experiment head sequence disagrees with event count during registration discovery"
                )

            previous_digest: str | None = None
            for expected_sequence, (_, receipt) in enumerate(event_rows):
                if receipt.sequence != expected_sequence:
                    raise LabPersistenceIntegrityError(
                        "Durable lab registry event sequence is not contiguous during registration discovery"
                    )
                if receipt.previous_event_digest != previous_digest:
                    raise LabPersistenceIntegrityError(
                        "Durable lab registry hash-chain previous digest mismatch during registration discovery"
                    )
                previous_digest = receipt.event_digest

            tail = event_rows[-1][1]
            if (
                tail.sequence != head_sequence
                or not hmac.compare_digest(tail.event_digest, head_digest)
            ):
                raise LabPersistenceIntegrityError(
                    "Durable lab experiment head digest disagrees with event tail during registration discovery"
                )
            if root_row["registered_at"] != event_rows[0][1].created_at:
                raise LabPersistenceIntegrityError(
                    "Durable lab experiment registration metadata disagrees with chronology during registration discovery"
                )

            replay_state: _ExperimentReplayState | None = None
            for _, receipt in event_rows:
                try:
                    replay_state = apply_lab_registry_event(
                        replay_state,
                        receipt.kind,
                        receipt.payload,
                        experiment_id=experiment_id,
                    )
                except LabPersistenceIntegrityError:
                    raise
                except LabError as exc:
                    raise LabPersistenceIntegrityError(
                        "Persisted lab registry chronology is semantically invalid during registration discovery"
                    ) from exc
            if replay_state is None:
                raise LabPersistenceIntegrityError(
                    "Durable lab registry replay produced no experiment during registration discovery"
                )
            if (
                replay_state.hypothesis.hypothesis_id != root_row["hypothesis_id"]
                or not hmac.compare_digest(
                    replay_state.hypothesis.hypothesis_digest,
                    root_hypothesis_digest,
                )
                or not hmac.compare_digest(
                    replay_state.manifest.manifest_digest,
                    root_manifest_digest,
                )
            ):
                raise LabPersistenceIntegrityError(
                    "Durable lab experiment identity metadata disagrees with chronology during registration discovery"
                )
            experiment_states[experiment_id] = replay_state

            owners = hypothesis_owners.setdefault(
                replay_state.hypothesis.hypothesis_id,
                {},
            )
            if experiment_id in owners:
                raise LabPersistenceIntegrityError(
                    "Authoritative hypothesis owner has multiple experiment registrations"
                )
            owners[experiment_id] = replay_state.hypothesis.hypothesis_digest

            for run_id, run_state in replay_state.runs.items():
                record_roots["run"].setdefault(run_id, set()).add(experiment_id)
                for metric_id in run_state.metrics:
                    record_roots["metric"].setdefault(metric_id, set()).add(
                        experiment_id
                    )
                for artifact_id in run_state.artifacts:
                    record_roots["artifact"].setdefault(artifact_id, set()).add(
                        experiment_id
                    )

            for row, receipt in event_rows:
                rows_by_kind[receipt.kind].append(row)

        return _RegistrationDiscoverySnapshot(
            rows_by_kind=MappingProxyType(
                {kind: tuple(kind_rows) for kind, kind_rows in rows_by_kind.items()}
            ),
            hypothesis_owners=MappingProxyType(
                {
                    hypothesis_id: MappingProxyType(dict(owners))
                    for hypothesis_id, owners in hypothesis_owners.items()
                }
            ),
            record_roots=MappingProxyType(
                {
                    record_type: MappingProxyType(
                        {
                            record_id: frozenset(roots)
                            for record_id, roots in identities.items()
                        }
                    )
                    for record_type, identities in record_roots.items()
                }
            ),
            experiment_states=MappingProxyType(dict(experiment_states)),
        )

    @classmethod
    def _event_rows_for_kind(
        cls,
        connection: sqlite3.Connection,
        kind: LabRegistryEventKind,
        *,
        discovery: _RegistrationDiscoverySnapshot | None = None,
    ) -> tuple[sqlite3.Row, ...]:
        snapshot = discovery or cls._registration_discovery_snapshot(connection)
        return snapshot.rows_by_kind.get(kind, ())

    @classmethod
    def _chronology_root_for_record(
        cls,
        connection: sqlite3.Connection,
        *,
        record_type: str,
        record_id: str,
        discovery: _RegistrationDiscoverySnapshot | None = None,
    ) -> str | None:
        if record_type not in _RECORD_LOOKUP_SPECS:
            raise AssertionError("Unsupported lab registry record type")
        snapshot = discovery or cls._registration_discovery_snapshot(connection)
        roots = snapshot.record_roots[record_type].get(record_id, frozenset())
        if len(roots) > 1:
            raise LabPersistenceIntegrityError(
                f"Durable {record_type} identity appears in multiple experiment chronologies"
            )
        return next(iter(roots), None)

    @staticmethod
    def _uuid_identity_rows(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        record_id: str,
        record_label: str,
    ) -> tuple[sqlite3.Row, ...]:
        try:
            requested_uuid = UUID(record_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise LabPersistenceIntegrityError(
                f"Persisted {record_label} lookup identity is not a UUID"
            ) from exc
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        exact_rows: list[sqlite3.Row] = []
        for row in rows:
            persisted_id = row[id_column]
            if persisted_id == record_id:
                exact_rows.append(row)
                continue
            if not isinstance(persisted_id, str):
                raise LabPersistenceIntegrityError(
                    f"Persisted {record_label} identity is not text"
                )
            try:
                semantically_equal = UUID(persisted_id) == requested_uuid
            except (TypeError, ValueError, AttributeError) as exc:
                raise LabPersistenceIntegrityError(
                    f"Persisted {record_label} identity is not a UUID"
                ) from exc
            if semantically_equal:
                raise LabPersistenceIntegrityError(
                    f"Persisted {record_label} identity uses a noncanonical UUID alias"
                )
        return tuple(exact_rows)

    @classmethod
    def _uuid_identity_row(
        cls,
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        record_id: str,
        record_label: str,
    ) -> sqlite3.Row | None:
        rows = cls._uuid_identity_rows(
            connection,
            table=table,
            id_column=id_column,
            record_id=record_id,
            record_label=record_label,
        )
        if len(rows) > 1:
            raise LabPersistenceIntegrityError(
                f"Persisted {record_label} identity contains duplicate exact rows"
            )
        return rows[0] if rows else None

    @classmethod
    def _audit_experiment_identity_aliases(
        cls,
        connection: sqlite3.Connection,
        experiment_id: str,
    ) -> None:
        for table in (
            "lab_registry_experiments",
            "lab_registry_events",
            "lab_registry_runs",
            "lab_registry_metrics",
            "lab_registry_artifacts",
        ):
            cls._uuid_identity_rows(
                connection,
                table=table,
                id_column="experiment_id",
                record_id=experiment_id,
                record_label="experiment",
            )

    @classmethod
    def _audit_new_experiment_namespace(
        cls,
        connection: sqlite3.Connection,
        experiment_id: str,
    ) -> None:
        cls._audit_experiment_identity_aliases(connection, experiment_id)
        for table in (
            "lab_registry_events",
            "lab_registry_runs",
            "lab_registry_metrics",
            "lab_registry_artifacts",
        ):
            rows = cls._uuid_identity_rows(
                connection,
                table=table,
                id_column="experiment_id",
                record_id=experiment_id,
                record_label="experiment",
            )
            if rows:
                raise LabPersistenceIntegrityError(
                    "Unregistered experiment id already owns persisted registry rows"
                )

    @classmethod
    def _authoritative_hypothesis_roots(
        cls,
        connection: sqlite3.Connection,
        hypothesis_id: str,
        *,
        discovery: _RegistrationDiscoverySnapshot | None = None,
    ) -> dict[str, str]:
        try:
            requested_uuid = UUID(hypothesis_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise LabPersistenceIntegrityError(
                "Persisted hypothesis ownership lookup identity is not a UUID"
            ) from exc
        if str(requested_uuid) != hypothesis_id:
            raise LabPersistenceIntegrityError(
                "Persisted hypothesis ownership lookup identity is noncanonical"
            )
        snapshot = discovery or cls._registration_discovery_snapshot(connection)
        return dict(snapshot.hypothesis_owners.get(hypothesis_id, {}))

    def _admit_hypothesis_identity(
        self,
        connection: sqlite3.Connection,
        hypothesis: Hypothesis,
    ) -> None:
        discovery = self._registration_discovery_snapshot(connection)
        authoritative_owners = self._authoritative_hypothesis_roots(
            connection,
            hypothesis.hypothesis_id,
            discovery=discovery,
        )
        authoritative_roots = set(authoritative_owners)

        roots = self._uuid_identity_rows(
            connection,
            table="lab_registry_experiments",
            id_column="hypothesis_id",
            record_id=hypothesis.hypothesis_id,
            record_label="hypothesis root",
        )
        derived_root_ids = {str(root["experiment_id"]) for root in roots}
        index_row = self._uuid_identity_row(
            connection,
            table="lab_registry_hypotheses",
            id_column="hypothesis_id",
            record_id=hypothesis.hypothesis_id,
            record_label="hypothesis index",
        )

        if authoritative_roots:
            digest_conflict = False
            owner_states: list[_ExperimentReplayState] = []
            for experiment_id in sorted(authoritative_roots):
                state = discovery.experiment_states.get(experiment_id)
                if state is None:
                    raise LabPersistenceIntegrityError(
                        "Authoritative hypothesis owner references an unknown experiment root"
                    )
                owner_states.append(state)
                if not hmac.compare_digest(
                    state.hypothesis.hypothesis_digest,
                    hypothesis.hypothesis_digest,
                ):
                    digest_conflict = True

            derived = self._derived_index_snapshot(connection, tuple(owner_states))
            for state in owner_states:
                self._verify_derived_indexes(
                    connection,
                    state,
                    discovery=discovery,
                    derived=derived,
                )

            if derived_root_ids != authoritative_roots:
                raise LabPersistenceIntegrityError(
                    "Derived hypothesis ownership disagrees with authoritative chronology"
                )
            if digest_conflict:
                raise LabIdentityConflictError(
                    "Hypothesis id is already bound to another digest"
                )
            return

        if roots:
            raise LabPersistenceIntegrityError(
                "Derived hypothesis owner exists without authoritative chronology"
            )
        if index_row is not None:
            raise LabPersistenceIntegrityError(
                "Derived hypothesis index exists without a durable experiment chronology"
            )
        try:
            connection.execute(
                """
                INSERT INTO lab_registry_hypotheses(hypothesis_id, hypothesis_digest)
                VALUES (?, ?)
                """,
                (hypothesis.hypothesis_id, hypothesis.hypothesis_digest),
            )
        except sqlite3.IntegrityError as exc:
            raise LabPersistenceError(
                "Could not publish durable hypothesis identity"
            ) from exc

    def _load_experiment(
        self,
        connection: sqlite3.Connection,
        experiment_id: str,
        *,
        discovery: _RegistrationDiscoverySnapshot | None = None,
        derived: _DerivedIndexSnapshot | None = None,
        verify_derived: bool = True,
    ) -> tuple[_ExperimentReplayState, tuple[LabRegistryEventReceipt, ...]]:
        self._audit_experiment_identity_aliases(connection, experiment_id)
        root_row = self._uuid_identity_row(
            connection,
            table="lab_registry_experiments",
            id_column="experiment_id",
            record_id=experiment_id,
            record_label="experiment",
        )
        if root_row is None:
            raise InvalidLabRecordError("Unknown experiment id")

        rows = connection.execute(
            """
            SELECT sequence, event_id, created_at, kind, payload_json,
                   previous_event_digest, event_digest
            FROM lab_registry_events
            WHERE experiment_id = ?
            ORDER BY sequence ASC
            """,
            (experiment_id,),
        ).fetchall()
        if not rows:
            raise LabPersistenceIntegrityError(
                "Durable lab experiment has no event chronology"
            )

        head_sequence = root_row["head_sequence"]
        head_digest = root_row["head_event_digest"]
        if (
            type(head_sequence) is not int
            or head_sequence < 0
            or not isinstance(head_digest, str)
        ):
            raise LabPersistenceIntegrityError("Durable lab experiment head is malformed")
        head_digest = _validate_persisted_digest(head_digest, "head_event_digest")
        root_hypothesis_digest = _validate_persisted_digest(
            root_row["hypothesis_digest"],
            "root hypothesis_digest",
        )
        root_manifest_digest = _validate_persisted_digest(
            root_row["manifest_digest"],
            "root manifest_digest",
        )
        if len(rows) != head_sequence + 1:
            raise LabPersistenceIntegrityError(
                "Durable lab experiment head sequence disagrees with event count"
            )

        events: list[LabRegistryEventReceipt] = []
        state: _ExperimentReplayState | None = None
        previous_digest: str | None = None
        for expected_sequence, row in enumerate(rows):
            if row["sequence"] != expected_sequence:
                raise LabPersistenceIntegrityError(
                    "Durable lab registry event sequence is not contiguous"
                )
            payload = _load_canonical_payload(row["payload_json"])
            try:
                receipt = LabRegistryEventReceipt(
                    schema_version=LAB_REGISTRY_EVENT_SCHEMA_VERSION,
                    event_id=row["event_id"],
                    experiment_id=experiment_id,
                    sequence=row["sequence"],
                    created_at=row["created_at"],
                    kind=row["kind"],
                    payload=payload,
                    previous_event_digest=row["previous_event_digest"],
                    event_digest=row["event_digest"],
                )
                if receipt.previous_event_digest != previous_digest:
                    raise LabPersistenceIntegrityError(
                        "Durable lab registry hash-chain previous digest mismatch"
                    )
                state = apply_lab_registry_event(
                    state,
                    receipt.kind,
                    receipt.payload,
                    experiment_id=experiment_id,
                )
            except LabPersistenceIntegrityError:
                raise
            except LabError as exc:
                raise LabPersistenceIntegrityError(
                    f"Persisted lab registry chronology is semantically invalid: {exc}"
                ) from exc
            events.append(receipt)
            previous_digest = receipt.event_digest

        if state is None:
            raise LabPersistenceIntegrityError(
                "Durable lab registry replay produced no experiment"
            )
        if (
            events[-1].sequence != head_sequence
            or not hmac.compare_digest(events[-1].event_digest, head_digest)
        ):
            raise LabPersistenceIntegrityError(
                "Durable lab experiment head digest disagrees with event tail"
            )
        if root_row["registered_at"] != events[0].created_at:
            raise LabPersistenceIntegrityError(
                "Durable lab experiment registration metadata disagrees with chronology"
            )
        if (
            state.hypothesis.hypothesis_id != root_row["hypothesis_id"]
            or not hmac.compare_digest(
                state.hypothesis.hypothesis_digest,
                root_hypothesis_digest,
            )
            or not hmac.compare_digest(
                state.manifest.manifest_digest,
                root_manifest_digest,
            )
        ):
            raise LabPersistenceIntegrityError(
                "Durable lab experiment identity metadata disagrees with chronology"
            )
        if verify_derived:
            self._verify_derived_indexes(
                connection,
                state,
                discovery=discovery,
                derived=derived,
            )
        return state, tuple(events)

    @classmethod
    def _derived_index_snapshot(
        cls,
        connection: sqlite3.Connection,
        state: _ExperimentReplayState | tuple[_ExperimentReplayState, ...],
    ) -> _DerivedIndexSnapshot:
        states = (state,) if isinstance(state, _ExperimentReplayState) else state
        if not states:
            raise AssertionError("Derived index snapshot requires at least one replay state")
        hypothesis_id = states[0].hypothesis.hypothesis_id
        if any(item.hypothesis.hypothesis_id != hypothesis_id for item in states):
            raise AssertionError("Derived index snapshot states must share one hypothesis")

        experiment_rows = connection.execute(
            "SELECT * FROM lab_registry_experiments"
        ).fetchall()
        hypothesis_rows = connection.execute(
            "SELECT * FROM lab_registry_hypotheses"
        ).fetchall()
        all_run_rows = connection.execute("SELECT * FROM lab_registry_runs").fetchall()
        all_metric_rows = connection.execute(
            "SELECT * FROM lab_registry_metrics"
        ).fetchall()
        all_artifact_rows = connection.execute(
            "SELECT * FROM lab_registry_artifacts"
        ).fetchall()

        def exact_uuid_rows(
            rows: list[sqlite3.Row],
            *,
            id_column: str,
            record_id: str,
            record_label: str,
        ) -> tuple[sqlite3.Row, ...]:
            try:
                requested_uuid = UUID(record_id)
            except (TypeError, ValueError, AttributeError) as exc:
                raise LabPersistenceIntegrityError(
                    f"Persisted {record_label} lookup identity is not a UUID"
                ) from exc
            exact_rows: list[sqlite3.Row] = []
            for row in rows:
                persisted_id = row[id_column]
                if persisted_id == record_id:
                    exact_rows.append(row)
                    continue
                if not isinstance(persisted_id, str):
                    raise LabPersistenceIntegrityError(
                        f"Persisted {record_label} identity is not text"
                    )
                try:
                    semantically_equal = UUID(persisted_id) == requested_uuid
                except (TypeError, ValueError, AttributeError) as exc:
                    raise LabPersistenceIntegrityError(
                        f"Persisted {record_label} identity is not a UUID"
                    ) from exc
                if semantically_equal:
                    raise LabPersistenceIntegrityError(
                        f"Persisted {record_label} identity uses a noncanonical UUID alias"
                    )
            return tuple(exact_rows)

        def primary_rows(
            rows: list[sqlite3.Row],
            *,
            id_column: str,
            requested_ids: set[str],
            record_label: str,
        ) -> Mapping[str, tuple[sqlite3.Row, ...]]:
            if not requested_ids:
                return MappingProxyType({})
            selected: dict[str, list[sqlite3.Row]] = {
                record_id: [] for record_id in requested_ids
            }
            for row in rows:
                persisted_id = row[id_column]
                if not isinstance(persisted_id, str):
                    raise LabPersistenceIntegrityError(
                        f"Persisted {record_label} identity is not text"
                    )
                try:
                    canonical_id = str(UUID(persisted_id))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise LabPersistenceIntegrityError(
                        f"Persisted {record_label} identity is not a UUID"
                    ) from exc
                if canonical_id not in requested_ids:
                    continue
                if persisted_id != canonical_id:
                    raise LabPersistenceIntegrityError(
                        f"Persisted {record_label} identity uses a noncanonical UUID alias"
                    )
                selected[canonical_id].append(row)
            for exact_rows in selected.values():
                if len(exact_rows) > 1:
                    raise LabPersistenceIntegrityError(
                        f"Persisted {record_label} identity contains duplicate exact rows"
                    )
            return MappingProxyType(
                {
                    record_id: tuple(exact_rows)
                    for record_id, exact_rows in selected.items()
                }
            )

        def scoped_rows(
            rows: list[sqlite3.Row],
            *,
            uuid_column: str,
            scope_column: str,
            requested_keys: set[tuple[str, str]],
            record_label: str,
        ) -> Mapping[tuple[str, str], tuple[sqlite3.Row, ...]]:
            if not requested_keys:
                return MappingProxyType({})
            requested_scopes = {scope for _, scope in requested_keys}
            selected: dict[tuple[str, str], list[sqlite3.Row]] = {
                key: [] for key in requested_keys
            }
            for row in rows:
                persisted_scope = row[scope_column]
                if not isinstance(persisted_scope, str):
                    raise LabPersistenceIntegrityError(
                        f"Derived {record_label} scoped {scope_column} is not text"
                    )
                if persisted_scope not in requested_scopes:
                    continue
                persisted_uuid = row[uuid_column]
                if not isinstance(persisted_uuid, str):
                    raise LabPersistenceIntegrityError(
                        f"Derived {record_label} scoped UUID identity is not text"
                    )
                try:
                    canonical_uuid = str(UUID(persisted_uuid))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise LabPersistenceIntegrityError(
                        f"Derived {record_label} scoped UUID identity is not a UUID"
                    ) from exc
                key = (canonical_uuid, persisted_scope)
                if key not in requested_keys:
                    continue
                if persisted_uuid != canonical_uuid:
                    raise LabPersistenceIntegrityError(
                        f"Derived {record_label} scoped index uses a noncanonical UUID alias"
                    )
                selected[key].append(row)
            return MappingProxyType(
                {key: tuple(exact_rows) for key, exact_rows in selected.items()}
            )

        def experiment_owner_rows(
            rows: list[sqlite3.Row],
            *,
            requested_ids: set[str],
            record_label: str,
        ) -> Mapping[str, tuple[sqlite3.Row, ...]]:
            selected: dict[str, list[sqlite3.Row]] = {
                experiment_id: [] for experiment_id in requested_ids
            }
            for row in rows:
                persisted_id = row["experiment_id"]
                if not isinstance(persisted_id, str):
                    raise LabPersistenceIntegrityError(
                        f"Persisted {record_label} experiment identity is not text"
                    )
                try:
                    canonical_id = str(UUID(persisted_id))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise LabPersistenceIntegrityError(
                        f"Persisted {record_label} experiment identity is not a UUID"
                    ) from exc
                if canonical_id not in requested_ids:
                    continue
                if persisted_id != canonical_id:
                    raise LabPersistenceIntegrityError(
                        f"Persisted {record_label} experiment identity uses a noncanonical UUID alias"
                    )
                selected[canonical_id].append(row)
            return MappingProxyType(
                {
                    experiment_id: tuple(owner_rows)
                    for experiment_id, owner_rows in selected.items()
                }
            )

        hypothesis_roots = exact_uuid_rows(
            experiment_rows,
            id_column="hypothesis_id",
            record_id=hypothesis_id,
            record_label="hypothesis root",
        )
        hypothesis_matches = exact_uuid_rows(
            hypothesis_rows,
            id_column="hypothesis_id",
            record_id=hypothesis_id,
            record_label="hypothesis index",
        )
        if len(hypothesis_matches) > 1:
            raise LabPersistenceIntegrityError(
                "Persisted hypothesis index identity contains duplicate exact rows"
            )

        requested_experiment_ids = {item.experiment_id for item in states}
        requested_run_ids: set[str] = set()
        requested_metric_ids: set[str] = set()
        requested_artifact_ids: set[str] = set()
        requested_metric_scopes: set[tuple[str, str]] = set()
        requested_artifact_scopes: set[tuple[str, str]] = set()
        for replay_state in states:
            requested_run_ids.update(replay_state.runs)
            for run_state in replay_state.runs.values():
                for metric_id, metric in run_state.metrics.items():
                    requested_metric_ids.add(metric_id)
                    requested_metric_scopes.add((metric.run_id, metric.name))
                for artifact_id, artifact in run_state.artifacts.items():
                    requested_artifact_ids.add(artifact_id)
                    requested_artifact_scopes.add(
                        (artifact.run_id, artifact.logical_path)
                    )

        return _DerivedIndexSnapshot(
            hypothesis_roots=hypothesis_roots,
            hypothesis_row=hypothesis_matches[0] if hypothesis_matches else None,
            run_rows=primary_rows(
                all_run_rows,
                id_column="run_id",
                requested_ids=requested_run_ids,
                record_label="run",
            ),
            metric_rows=primary_rows(
                all_metric_rows,
                id_column="metric_id",
                requested_ids=requested_metric_ids,
                record_label="metric",
            ),
            artifact_rows=primary_rows(
                all_artifact_rows,
                id_column="artifact_id",
                requested_ids=requested_artifact_ids,
                record_label="artifact",
            ),
            metric_scoped_rows=scoped_rows(
                all_metric_rows,
                uuid_column="run_id",
                scope_column="name",
                requested_keys=requested_metric_scopes,
                record_label="Metric",
            ),
            artifact_scoped_rows=scoped_rows(
                all_artifact_rows,
                uuid_column="run_id",
                scope_column="logical_path",
                requested_keys=requested_artifact_scopes,
                record_label="Artifact",
            ),
            experiment_run_rows=experiment_owner_rows(
                all_run_rows,
                requested_ids=requested_experiment_ids,
                record_label="run",
            ),
            experiment_metric_rows=experiment_owner_rows(
                all_metric_rows,
                requested_ids=requested_experiment_ids,
                record_label="metric",
            ),
            experiment_artifact_rows=experiment_owner_rows(
                all_artifact_rows,
                requested_ids=requested_experiment_ids,
                record_label="artifact",
            ),
        )

    @classmethod
    def _verify_derived_indexes(
        cls,
        connection: sqlite3.Connection,
        state: _ExperimentReplayState,
        *,
        discovery: _RegistrationDiscoverySnapshot | None = None,
        derived: _DerivedIndexSnapshot | None = None,
    ) -> None:
        snapshot = discovery or cls._registration_discovery_snapshot(connection)
        authoritative_owners = cls._authoritative_hypothesis_roots(
            connection,
            state.hypothesis.hypothesis_id,
            discovery=snapshot,
        )
        authoritative_roots = set(authoritative_owners)
        derived = derived or cls._derived_index_snapshot(connection, state)
        roots = derived.hypothesis_roots
        derived_root_ids = {str(root["experiment_id"]) for root in roots}
        if derived_root_ids != authoritative_roots:
            raise LabPersistenceIntegrityError(
                "Derived hypothesis ownership disagrees with authoritative chronology"
            )
        for owner_digest in authoritative_owners.values():
            if not hmac.compare_digest(
                owner_digest,
                state.hypothesis.hypothesis_digest,
            ):
                raise LabPersistenceIntegrityError(
                    "Authoritative hypothesis owner digest disagrees with recovered hypothesis"
                )
        for root in roots:
            root_hypothesis_digest = _validate_persisted_digest(
                root["hypothesis_digest"],
                "derived hypothesis owner digest",
            )
            if not hmac.compare_digest(
                root_hypothesis_digest,
                state.hypothesis.hypothesis_digest,
            ):
                raise LabPersistenceIntegrityError(
                    "Derived hypothesis owner digest disagrees with authoritative hypothesis"
                )

        hypothesis_row = derived.hypothesis_row
        if hypothesis_row is None:
            raise LabPersistenceIntegrityError(
                "Derived hypothesis identity index disagrees with chronology"
            )
        hypothesis_index_digest = _validate_persisted_digest(
            hypothesis_row["hypothesis_digest"],
            "derived hypothesis_digest",
        )
        if not hmac.compare_digest(
            hypothesis_index_digest,
            state.hypothesis.hypothesis_digest,
        ):
            raise LabPersistenceIntegrityError(
                "Derived hypothesis identity index disagrees with chronology"
            )

        for run_id, run_state in state.runs.items():
            chronology_root = cls._chronology_root_for_record(
                connection,
                record_type="run",
                record_id=run_id,
                discovery=snapshot,
            )
            if chronology_root != state.experiment_id:
                raise LabPersistenceIntegrityError(
                    "Authoritative run identity disagrees with recovered experiment"
                )
            if len(derived.run_rows.get(run_id, ())) != 1:
                raise LabPersistenceIntegrityError(
                    "Derived run identity disagrees with durable chronology"
                )
            for metric_id, metric in run_state.metrics.items():
                chronology_root = cls._chronology_root_for_record(
                    connection,
                    record_type="metric",
                    record_id=metric_id,
                    discovery=snapshot,
                )
                if chronology_root != state.experiment_id:
                    raise LabPersistenceIntegrityError(
                        "Authoritative metric identity disagrees with recovered experiment"
                    )
                if len(derived.metric_rows.get(metric_id, ())) != 1:
                    raise LabPersistenceIntegrityError(
                        "Derived metric identity disagrees with durable chronology"
                    )
                scoped_rows = derived.metric_scoped_rows.get(
                    (metric.run_id, metric.name),
                    (),
                )
                if (
                    len(scoped_rows) != 1
                    or scoped_rows[0]["metric_id"] != metric_id
                ):
                    raise LabPersistenceIntegrityError(
                        "Derived Metric scoped identity disagrees with durable chronology"
                    )
            for artifact_id, artifact in run_state.artifacts.items():
                chronology_root = cls._chronology_root_for_record(
                    connection,
                    record_type="artifact",
                    record_id=artifact_id,
                    discovery=snapshot,
                )
                if chronology_root != state.experiment_id:
                    raise LabPersistenceIntegrityError(
                        "Authoritative artifact identity disagrees with recovered experiment"
                    )
                if len(derived.artifact_rows.get(artifact_id, ())) != 1:
                    raise LabPersistenceIntegrityError(
                        "Derived artifact identity disagrees with durable chronology"
                    )
                scoped_rows = derived.artifact_scoped_rows.get(
                    (artifact.run_id, artifact.logical_path),
                    (),
                )
                if (
                    len(scoped_rows) != 1
                    or scoped_rows[0]["artifact_id"] != artifact_id
                ):
                    raise LabPersistenceIntegrityError(
                        "Derived Artifact scoped identity disagrees with durable chronology"
                    )

        actual_runs = {
            row["run_id"]: (
                row["run_digest"],
                row["manifest_digest"],
                row["ordinal"],
            )
            for row in derived.experiment_run_rows.get(state.experiment_id, ())
        }
        expected_runs = {
            run_id: (
                run_state.run.run_digest,
                run_state.run.manifest_digest,
                run_state.run.ordinal,
            )
            for run_id, run_state in state.runs.items()
        }
        if actual_runs != expected_runs:
            raise LabPersistenceIntegrityError(
                "Derived run index disagrees with durable chronology"
            )

        actual_metrics = {
            row["metric_id"]: (
                row["run_id"],
                row["run_digest"],
                row["manifest_digest"],
                row["metric_digest"],
                row["name"],
            )
            for row in derived.experiment_metric_rows.get(state.experiment_id, ())
        }
        expected_metrics: dict[str, tuple[Any, ...]] = {}
        for run_state in state.runs.values():
            for metric_id, metric in run_state.metrics.items():
                expected_metrics[metric_id] = (
                    metric.run_id,
                    metric.run_digest,
                    metric.manifest_digest,
                    metric.metric_digest,
                    metric.name,
                )
        if actual_metrics != expected_metrics:
            raise LabPersistenceIntegrityError(
                "Derived metric index disagrees with durable chronology"
            )

        actual_artifacts = {
            row["artifact_id"]: (
                row["run_id"],
                row["run_digest"],
                row["manifest_digest"],
                row["artifact_digest"],
                row["logical_path"],
            )
            for row in derived.experiment_artifact_rows.get(state.experiment_id, ())
        }
        expected_artifacts: dict[str, tuple[Any, ...]] = {}
        for run_state in state.runs.values():
            for artifact_id, artifact in run_state.artifacts.items():
                expected_artifacts[artifact_id] = (
                    artifact.run_id,
                    artifact.run_digest,
                    artifact.manifest_digest,
                    artifact.artifact_digest,
                    artifact.logical_path,
                )
        if actual_artifacts != expected_artifacts:
            raise LabPersistenceIntegrityError(
                "Derived artifact index disagrees with durable chronology"
            )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        receipt: LabRegistryEventReceipt,
        *,
        old_sequence: int,
        old_digest: str | None,
    ) -> None:
        try:
            connection.execute(
                """
                INSERT INTO lab_registry_events(
                    experiment_id, sequence, event_id, created_at, kind,
                    payload_json, previous_event_digest, event_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.experiment_id,
                    receipt.sequence,
                    receipt.event_id,
                    receipt.created_at,
                    receipt.kind.value,
                    canonical_registry_json(receipt.payload),
                    receipt.previous_event_digest,
                    receipt.event_digest,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LabPersistenceError("Could not append durable lab registry event") from exc

        if old_digest is None:
            cursor = connection.execute(
                """
                UPDATE lab_registry_experiments
                SET head_sequence = ?, head_event_digest = ?
                WHERE experiment_id = ?
                  AND head_sequence = ?
                  AND head_event_digest IS NULL
                """,
                (
                    receipt.sequence,
                    receipt.event_digest,
                    receipt.experiment_id,
                    old_sequence,
                ),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE lab_registry_experiments
                SET head_sequence = ?, head_event_digest = ?
                WHERE experiment_id = ?
                  AND head_sequence = ?
                  AND head_event_digest = ?
                """,
                (
                    receipt.sequence,
                    receipt.event_digest,
                    receipt.experiment_id,
                    old_sequence,
                    old_digest,
                ),
            )
        if cursor.rowcount != 1:
            raise LabPersistenceIntegrityError(
                "Durable lab experiment head changed during append"
            )

    def _audit_authoritative_record_identity(
        self,
        connection: sqlite3.Connection,
        receipt: LabRegistryEventReceipt,
        *,
        discovery: _RegistrationDiscoverySnapshot | None = None,
    ) -> None:
        if receipt.kind is LabRegistryEventKind.RUN_REGISTERED:
            run = experiment_run_from_dict(receipt.payload["run"])
            record_type = "run"
            record_id = run.run_id
            record_label = "Run"
        elif receipt.kind is LabRegistryEventKind.METRIC_REGISTERED:
            metric = metric_record_from_dict(receipt.payload["metric"])
            record_type = "metric"
            record_id = metric.metric_id
            record_label = "Metric"
        elif receipt.kind is LabRegistryEventKind.ARTIFACT_REGISTERED:
            artifact = artifact_record_from_dict(receipt.payload["artifact"])
            record_type = "artifact"
            record_id = artifact.artifact_id
            record_label = "Artifact"
        else:
            return

        snapshot = discovery or self._registration_discovery_snapshot(connection)
        chronology_root = self._chronology_root_for_record(
            connection,
            record_type=record_type,
            record_id=record_id,
            discovery=snapshot,
        )
        if chronology_root is None:
            return
        try:
            self._load_experiment(
                connection,
                chronology_root,
                discovery=snapshot,
            )
        except LabPersistenceIntegrityError:
            raise
        except InvalidLabRecordError as exc:
            raise LabPersistenceIntegrityError(
                f"Authoritative {record_label} registration references an unknown experiment root"
            ) from exc
        raise LabIdentityConflictError(f"{record_label} id is already registered")

    def _audit_global_identity_owner(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        record_id: str,
        record_label: str,
    ) -> None:
        row = self._uuid_identity_row(
            connection,
            table=table,
            id_column=id_column,
            record_id=record_id,
            record_label=record_label,
        )
        if row is None:
            return
        owner_experiment_id = str(row["experiment_id"])
        try:
            self._load_experiment(connection, owner_experiment_id)
        except LabPersistenceIntegrityError:
            raise
        except InvalidLabRecordError as exc:
            raise LabPersistenceIntegrityError(
                f"Derived {record_label} identity index references an unknown experiment root"
            ) from exc
        raise LabIdentityConflictError(f"{record_label} id is already registered")

    @staticmethod
    def _scoped_identity_rows(
        connection: sqlite3.Connection,
        *,
        table: str,
        uuid_column: str,
        uuid_value: str,
        scope_column: str,
        scope_value: str,
        record_label: str,
    ) -> tuple[sqlite3.Row, ...]:
        try:
            requested_uuid = UUID(uuid_value)
        except (TypeError, ValueError, AttributeError) as exc:
            raise LabPersistenceIntegrityError(
                f"Persisted {record_label} scoped lookup UUID identity is not a UUID"
            ) from exc
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        exact_rows: list[sqlite3.Row] = []
        for row in rows:
            persisted_scope = row[scope_column]
            if not isinstance(persisted_scope, str):
                raise LabPersistenceIntegrityError(
                    f"Derived {record_label} scoped {scope_column} is not text"
                )
            if persisted_scope != scope_value:
                continue
            persisted_uuid = row[uuid_column]
            if persisted_uuid == uuid_value:
                exact_rows.append(row)
                continue
            if not isinstance(persisted_uuid, str):
                raise LabPersistenceIntegrityError(
                    f"Derived {record_label} scoped UUID identity is not text"
                )
            try:
                semantically_equal = UUID(persisted_uuid) == requested_uuid
            except (TypeError, ValueError, AttributeError) as exc:
                raise LabPersistenceIntegrityError(
                    f"Derived {record_label} scoped UUID identity is not a UUID"
                ) from exc
            if semantically_equal:
                raise LabPersistenceIntegrityError(
                    f"Derived {record_label} scoped index uses a noncanonical UUID alias"
                )
        return tuple(exact_rows)

    def _audit_scoped_identity_owner(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        uuid_column: str,
        uuid_value: str,
        scope_column: str,
        scope_value: str,
        record_label: str,
    ) -> None:
        exact_rows = self._scoped_identity_rows(
            connection,
            table=table,
            uuid_column=uuid_column,
            uuid_value=uuid_value,
            scope_column=scope_column,
            scope_value=scope_value,
            record_label=record_label,
        )
        if len(exact_rows) > 1:
            raise LabPersistenceIntegrityError(
                f"Derived {record_label} scoped index contains duplicate exact identities"
            )
        if not exact_rows:
            return
        exact_row = exact_rows[0]
        owner_experiment_id = str(exact_row["experiment_id"])
        try:
            self._load_experiment(connection, owner_experiment_id)
        except LabPersistenceIntegrityError:
            raise
        except InvalidLabRecordError as exc:
            raise LabPersistenceIntegrityError(
                f"Derived {record_label} scoped index references an unknown experiment root"
            ) from exc
        raise LabIdentityConflictError(
            f"{record_label} scoped identity is already registered"
        )

    def _insert_derived_index(
        self,
        connection: sqlite3.Connection,
        receipt: LabRegistryEventReceipt,
    ) -> None:
        try:
            if receipt.kind is LabRegistryEventKind.RUN_REGISTERED:
                run = experiment_run_from_dict(receipt.payload["run"])
                self._audit_global_identity_owner(
                    connection,
                    table="lab_registry_runs",
                    id_column="run_id",
                    record_id=run.run_id,
                    record_label="Run",
                )
                connection.execute(
                    """
                    INSERT INTO lab_registry_runs(
                        run_id, experiment_id, run_digest, manifest_digest, ordinal
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id,
                        receipt.experiment_id,
                        run.run_digest,
                        run.manifest_digest,
                        run.ordinal,
                    ),
                )
            elif receipt.kind is LabRegistryEventKind.METRIC_REGISTERED:
                metric = metric_record_from_dict(receipt.payload["metric"])
                self._audit_global_identity_owner(
                    connection,
                    table="lab_registry_metrics",
                    id_column="metric_id",
                    record_id=metric.metric_id,
                    record_label="Metric",
                )
                self._audit_scoped_identity_owner(
                    connection,
                    table="lab_registry_metrics",
                    uuid_column="run_id",
                    uuid_value=metric.run_id,
                    scope_column="name",
                    scope_value=metric.name,
                    record_label="Metric",
                )
                connection.execute(
                    """
                    INSERT INTO lab_registry_metrics(
                        metric_id, experiment_id, run_id, run_digest,
                        manifest_digest, metric_digest, name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        metric.metric_id,
                        receipt.experiment_id,
                        metric.run_id,
                        metric.run_digest,
                        metric.manifest_digest,
                        metric.metric_digest,
                        metric.name,
                    ),
                )
            elif receipt.kind is LabRegistryEventKind.ARTIFACT_REGISTERED:
                artifact = artifact_record_from_dict(receipt.payload["artifact"])
                self._audit_global_identity_owner(
                    connection,
                    table="lab_registry_artifacts",
                    id_column="artifact_id",
                    record_id=artifact.artifact_id,
                    record_label="Artifact",
                )
                self._audit_scoped_identity_owner(
                    connection,
                    table="lab_registry_artifacts",
                    uuid_column="run_id",
                    uuid_value=artifact.run_id,
                    scope_column="logical_path",
                    scope_value=artifact.logical_path,
                    record_label="Artifact",
                )
                connection.execute(
                    """
                    INSERT INTO lab_registry_artifacts(
                        artifact_id, experiment_id, run_id, run_digest,
                        manifest_digest, artifact_digest, logical_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_id,
                        receipt.experiment_id,
                        artifact.run_id,
                        artifact.run_digest,
                        artifact.manifest_digest,
                        artifact.artifact_digest,
                        artifact.logical_path,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise LabPersistenceError(
                "Could not publish durable lab derived index"
            ) from exc
