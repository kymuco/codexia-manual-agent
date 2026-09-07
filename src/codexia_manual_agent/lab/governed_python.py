from __future__ import annotations

import base64
import hmac
import json
import math
import os
import re
import sqlite3
import stat
import sys
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from uuid import UUID, uuid5

from codexia_manual_agent.authority import (
    ActionLifecycle,
    AuthorizationReceipt,
    LocalApprovalAuthority,
)
from codexia_manual_agent.execution import (
    ProcessExecutor,
    ProcessLimits,
    ProcessTerminationReason,
    prepare_process_proposal,
)
from codexia_manual_agent.lab.errors import (
    EvidenceBindingError,
    InvalidLabRecordError,
    LabIdentityConflictError,
    LabPersistenceError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
)
from codexia_manual_agent.lab.execution_evidence import (
    RunExecutionAuthorization,
    RunExecutionBinding,
    RunExecutionEvidence,
)
from codexia_manual_agent.lab.execution_registry import (
    RunExecutionPhase,
    RunExecutionRecovery,
    SqliteRunExecutionRegistry,
)
from codexia_manual_agent.lab.models import (
    ArtifactRecord,
    ExperimentManifest,
    ExperimentRun,
    MetricRecord,
)
from codexia_manual_agent.lab.registry import RegisteredRunSnapshot, SqliteLabRegistry
from codexia_manual_agent.session_events import (
    DurableAuthorizationConsumptionRegistry,
    SqliteSessionEventStore,
)


PYTHON_JSON_PROFILE = "python-inline-json-result.v1"
PYTHON_JSON_RESULT_SCHEMA = "codexia.python-json-result.v1"
PHYSICAL_EVIDENCE_SCHEMA_VERSION = 1
MAX_INLINE_SOURCE_CHARS = 12_000
MAX_INLINE_SOURCE_BYTES = 24_000
MAX_INPUT_JSON_CHARS = 8_000
MAX_RESULT_BYTES = 1_048_576
MAX_PHYSICAL_RECEIPT_JSON_CHARS = 2_097_152

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_REPARSE_POINT = 0x400


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            _thaw(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidLabRecordError(
            "M4.3.2 value is not canonical JSON-compatible data"
        ) from exc


def _digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidLabRecordError(f"{field_name} must use canonical UUID form")
    return value


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


def _logical_output_path(run_id: str) -> str:
    _validate_uuid(run_id, "run_id")
    return f".codexia/m4-runs/{run_id}/result.json"


def _validate_metric_name(value: Any) -> str:
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise InvalidLabRecordError("M4.3.2 metric name is invalid")
    return value


def _validate_unit(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 128
        or "\x00" in value
    ):
        raise InvalidLabRecordError("M4.3.2 metric unit is invalid")
    return value


def _validate_metric_value(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceBindingError("Result metric value must be a finite number")
    if isinstance(value, int) and not -(2**63) <= value <= 2**63 - 1:
        raise EvidenceBindingError("Result integer metric exceeds signed 64-bit range")
    if isinstance(value, float) and not math.isfinite(value):
        raise EvidenceBindingError("Result metric value must be finite")
    return value


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceBindingError(f"Result JSON contains duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise EvidenceBindingError(f"Result JSON contains non-finite constant: {value}")


@dataclass(frozen=True, slots=True)
class PythonJsonExperimentSpec:
    source: str
    input_json: str
    metric_name: str
    metric_unit: str | None
    source_sha256: str
    input_sha256: str

    @classmethod
    def from_manifest(cls, manifest: ExperimentManifest) -> "PythonJsonExperimentSpec":
        if not isinstance(manifest, ExperimentManifest):
            raise TypeError("manifest must be an ExperimentManifest")
        params = _exact_keys(
            manifest.parameters,
            {"profile", "source", "input", "metric"},
            "M4.3.2 manifest parameters",
        )
        if params["profile"] != PYTHON_JSON_PROFILE:
            raise InvalidLabRecordError(
                "Manifest does not use the admitted M4.3.2 Python profile"
            )
        source = params["source"]
        if (
            not isinstance(source, str)
            or not source
            or "\x00" in source
            or len(source) > MAX_INLINE_SOURCE_CHARS
            or len(source.encode("utf-8")) > MAX_INLINE_SOURCE_BYTES
        ):
            raise InvalidLabRecordError(
                "Inline Python source is empty or exceeds the M4.3.2 budget"
            )
        input_json = _canonical_json(params["input"])
        if len(input_json) > MAX_INPUT_JSON_CHARS:
            raise InvalidLabRecordError("Manifest input exceeds the M4.3.2 argv budget")
        metric = _exact_keys(
            params["metric"],
            {"name", "unit"},
            "M4.3.2 metric declaration",
        )
        name = _validate_metric_name(metric["name"])
        unit = _validate_unit(metric["unit"])
        return cls(
            source=source,
            input_json=input_json,
            metric_name=name,
            metric_unit=unit,
            source_sha256=sha256(source.encode("utf-8")).hexdigest(),
            input_sha256=sha256(input_json.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class PreparedPythonJsonRun:
    run: ExperimentRun
    manifest_digest: str
    m3_session_id: str
    output_logical_path: str
    spec: PythonJsonExperimentSpec
    binding: RunExecutionBinding

    @property
    def proposal(self):
        return self.binding.proposal


@dataclass(frozen=True, slots=True)
class PhysicalOutputSnapshot:
    logical_path: str
    size_bytes: int
    sha256: str
    data: bytes


@dataclass(frozen=True, slots=True)
class PhysicalEvidenceReceipt:
    schema_version: int
    receipt_id: str
    run_id: str
    run_digest: str
    manifest_digest: str
    execution_evidence_digest: str
    observation_digest: str
    stdout_sha256: str
    output_logical_path: str
    output_size_bytes: int
    output_sha256: str
    artifact: ArtifactRecord
    metric: MetricRecord
    receipt_digest: str

    @classmethod
    def create(
        cls,
        *,
        run: ExperimentRun,
        execution_evidence: RunExecutionEvidence,
        snapshot: PhysicalOutputSnapshot,
        artifact: ArtifactRecord,
        metric: MetricRecord,
    ) -> "PhysicalEvidenceReceipt":
        receipt_id = str(uuid5(UUID(run.run_id), "codexia:m4.3.2:physical-evidence"))
        base = {
            "schema_version": PHYSICAL_EVIDENCE_SCHEMA_VERSION,
            "receipt_id": receipt_id,
            "run_id": run.run_id,
            "run_digest": run.run_digest,
            "manifest_digest": run.manifest_digest,
            "execution_evidence_digest": execution_evidence.evidence_digest,
            "observation_digest": execution_evidence.observation.observation_digest,
            "stdout_sha256": execution_evidence.observation.stdout.sha256,
            "output_logical_path": snapshot.logical_path,
            "output_size_bytes": snapshot.size_bytes,
            "output_sha256": snapshot.sha256,
            "artifact": artifact.to_dict(),
            "metric": metric.to_dict(),
        }
        return cls(
            schema_version=PHYSICAL_EVIDENCE_SCHEMA_VERSION,
            receipt_id=receipt_id,
            run_id=run.run_id,
            run_digest=run.run_digest,
            manifest_digest=run.manifest_digest,
            execution_evidence_digest=execution_evidence.evidence_digest,
            observation_digest=execution_evidence.observation.observation_digest,
            stdout_sha256=execution_evidence.observation.stdout.sha256,
            output_logical_path=snapshot.logical_path,
            output_size_bytes=snapshot.size_bytes,
            output_sha256=snapshot.sha256,
            artifact=artifact,
            metric=metric,
            receipt_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != PHYSICAL_EVIDENCE_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M4.3.2 physical evidence schema")
        _validate_uuid(self.receipt_id, "receipt_id")
        _validate_uuid(self.run_id, "run_id")
        if self.receipt_id != str(
            uuid5(UUID(self.run_id), "codexia:m4.3.2:physical-evidence")
        ):
            raise EvidenceBindingError(
                "Physical evidence receipt id is not run-deterministic"
            )
        for name in (
            "run_digest",
            "manifest_digest",
            "execution_evidence_digest",
            "observation_digest",
            "stdout_sha256",
            "output_sha256",
            "receipt_digest",
        ):
            _validate_digest(getattr(self, name), name)
        if self.output_logical_path != _logical_output_path(self.run_id):
            raise EvidenceBindingError(
                "Physical evidence output path is not the deterministic run path"
            )
        if (
            type(self.output_size_bytes) is not int
            or not 0 <= self.output_size_bytes <= MAX_RESULT_BYTES
        ):
            raise InvalidLabRecordError("Physical evidence output size is invalid")
        if not isinstance(self.artifact, ArtifactRecord) or not isinstance(
            self.metric, MetricRecord
        ):
            raise InvalidLabRecordError("Physical evidence contains invalid M4 records")
        if (
            self.artifact.run_id != self.run_id
            or self.metric.run_id != self.run_id
            or not hmac.compare_digest(self.artifact.run_digest, self.run_digest)
            or not hmac.compare_digest(self.metric.run_digest, self.run_digest)
            or not hmac.compare_digest(
                self.artifact.manifest_digest,
                self.manifest_digest,
            )
            or not hmac.compare_digest(
                self.metric.manifest_digest,
                self.manifest_digest,
            )
            or self.artifact.logical_path != self.output_logical_path
            or self.artifact.size_bytes != self.output_size_bytes
            or not hmac.compare_digest(self.artifact.sha256, self.output_sha256)
            or not hmac.compare_digest(self.stdout_sha256, self.output_sha256)
        ):
            raise EvidenceBindingError(
                "Physical evidence records do not bind the exact observed output bytes"
            )
        if not hmac.compare_digest(_digest(self._payload()), self.receipt_digest):
            raise InvalidLabRecordError(
                "Physical evidence receipt digest does not match payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "run_digest": self.run_digest,
            "manifest_digest": self.manifest_digest,
            "execution_evidence_digest": self.execution_evidence_digest,
            "observation_digest": self.observation_digest,
            "stdout_sha256": self.stdout_sha256,
            "output_logical_path": self.output_logical_path,
            "output_size_bytes": self.output_size_bytes,
            "output_sha256": self.output_sha256,
            "artifact": self.artifact.to_dict(),
            "metric": self.metric.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_digest": self.receipt_digest}


def _artifact_from_dict(value: Any) -> ArtifactRecord:
    data = _exact_keys(
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
        "physical evidence artifact",
    )
    return ArtifactRecord(
        schema_version=data["schema_version"],
        artifact_id=data["artifact_id"],
        created_at=data["created_at"],
        run_id=data["run_id"],
        run_digest=data["run_digest"],
        manifest_digest=data["manifest_digest"],
        logical_path=data["logical_path"],
        size_bytes=data["size_bytes"],
        sha256=data["sha256"],
        media_type=data["media_type"],
        artifact_digest=data["artifact_digest"],
    )


def _metric_from_dict(value: Any) -> MetricRecord:
    data = _exact_keys(
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
        "physical evidence metric",
    )
    return MetricRecord(
        schema_version=data["schema_version"],
        metric_id=data["metric_id"],
        created_at=data["created_at"],
        run_id=data["run_id"],
        run_digest=data["run_digest"],
        manifest_digest=data["manifest_digest"],
        name=data["name"],
        value=data["value"],
        unit=data["unit"],
        metric_digest=data["metric_digest"],
    )


def physical_evidence_receipt_from_dict(value: Any) -> PhysicalEvidenceReceipt:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "receipt_id",
            "run_id",
            "run_digest",
            "manifest_digest",
            "execution_evidence_digest",
            "observation_digest",
            "stdout_sha256",
            "output_logical_path",
            "output_size_bytes",
            "output_sha256",
            "artifact",
            "metric",
            "receipt_digest",
        },
        "physical evidence receipt",
    )
    return PhysicalEvidenceReceipt(
        schema_version=data["schema_version"],
        receipt_id=data["receipt_id"],
        run_id=data["run_id"],
        run_digest=data["run_digest"],
        manifest_digest=data["manifest_digest"],
        execution_evidence_digest=data["execution_evidence_digest"],
        observation_digest=data["observation_digest"],
        stdout_sha256=data["stdout_sha256"],
        output_logical_path=data["output_logical_path"],
        output_size_bytes=data["output_size_bytes"],
        output_sha256=data["output_sha256"],
        artifact=_artifact_from_dict(data["artifact"]),
        metric=_metric_from_dict(data["metric"]),
        receipt_digest=data["receipt_digest"],
    )


def _has_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _read_physical_output(
    workspace: str | Path,
    logical_path: str,
) -> PhysicalOutputSnapshot:
    try:
        root = Path(workspace).expanduser().resolve(strict=True)
    except OSError as exc:
        raise EvidenceBindingError("M4.3.2 workspace root cannot be resolved") from exc
    if not root.is_dir():
        raise EvidenceBindingError("M4.3.2 workspace root is not a directory")
    if "\\" in logical_path:
        raise EvidenceBindingError("Physical output path must use POSIX separators")
    relative = PurePosixPath(logical_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise EvidenceBindingError("Physical output path is not canonical and relative")

    current = root
    before: os.stat_result | None = None
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise EvidenceBindingError(
                f"Required physical output is missing: {logical_path}"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or _has_reparse_point(info):
            raise EvidenceBindingError(
                "Physical output path contains a symlink/junction/reparse point"
            )
        final = index == len(relative.parts) - 1
        if final:
            if not stat.S_ISREG(info.st_mode):
                raise EvidenceBindingError("Physical output is not a regular file")
            before = info
        elif not stat.S_ISDIR(info.st_mode):
            raise EvidenceBindingError("Physical output parent is not a directory")

    assert before is not None
    if before.st_size > MAX_RESULT_BYTES:
        raise EvidenceBindingError("Physical output exceeds the M4.3.2 byte budget")
    try:
        with current.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not _same_file_identity(before, opened):
                raise EvidenceBindingError(
                    "Physical output changed between path validation and open"
                )
            data = handle.read(MAX_RESULT_BYTES + 1)
            after_open = os.fstat(handle.fileno())
    except EvidenceBindingError:
        raise
    except OSError as exc:
        raise EvidenceBindingError("Physical output could not be read") from exc
    if len(data) > MAX_RESULT_BYTES:
        raise EvidenceBindingError("Physical output exceeds the M4.3.2 byte budget")
    try:
        after_path = os.lstat(current)
    except OSError as exc:
        raise EvidenceBindingError("Physical output disappeared after verification") from exc
    if (
        stat.S_ISLNK(after_path.st_mode)
        or _has_reparse_point(after_path)
        or not _same_file_identity(before, after_open)
        or not _same_file_identity(after_open, after_path)
        or len(data) != after_path.st_size
    ):
        raise EvidenceBindingError(
            "Physical output mutated while evidence bytes were captured"
        )
    return PhysicalOutputSnapshot(
        logical_path=logical_path,
        size_bytes=len(data),
        sha256=sha256(data).hexdigest(),
        data=data,
    )


def _observed_stdout_bytes(evidence: RunExecutionEvidence) -> bytes:
    stream = evidence.observation.stdout
    if stream.truncated:
        raise EvidenceBindingError(
            "M4.3.2 requires complete stdout bytes; truncated stdout is not evidence"
        )
    try:
        data = base64.b64decode(stream.data_base64.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise EvidenceBindingError("Observed stdout bytes are not valid base64") from exc
    if len(data) != stream.byte_count:
        raise EvidenceBindingError("Observed stdout byte count is inconsistent")
    if not hmac.compare_digest(sha256(data).hexdigest(), stream.sha256):
        raise EvidenceBindingError(
            "Observed stdout digest does not match retained bytes"
        )
    return data


def _extract_metric(
    snapshot: PhysicalOutputSnapshot,
    *,
    run_id: str,
    spec: PythonJsonExperimentSpec,
) -> int | float:
    try:
        text = snapshot.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceBindingError("Result artifact is not UTF-8 JSON") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except EvidenceBindingError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceBindingError("Result artifact is not valid bounded JSON") from exc
    try:
        root = _exact_keys(
            value,
            {"schema", "run_id", "metric"},
            "result artifact",
        )
        metric = _exact_keys(
            root["metric"],
            {"name", "value", "unit"},
            "result metric",
        )
    except InvalidLabRecordError as exc:
        raise EvidenceBindingError(
            "Result artifact shape is not the declared schema"
        ) from exc
    if root["schema"] != PYTHON_JSON_RESULT_SCHEMA or root["run_id"] != run_id:
        raise EvidenceBindingError("Result artifact does not bind the exact M4.3.2 run")
    if metric["name"] != spec.metric_name or metric["unit"] != spec.metric_unit:
        raise EvidenceBindingError("Result metric does not match the manifest declaration")
    return _validate_metric_value(metric["value"])


@dataclass(frozen=True, slots=True)
class PhysicalEvidenceRecovery:
    receipt: PhysicalEvidenceReceipt
    execution: RunExecutionRecovery
    run: RegisteredRunSnapshot
    artifact: ArtifactRecord
    metric: MetricRecord


class SqlitePhysicalEvidenceRegistry:
    def __init__(
        self,
        lab_registry: SqliteLabRegistry,
        execution_registry: SqliteRunExecutionRegistry,
    ) -> None:
        if lab_registry.database_path.resolve() != execution_registry.database_path.resolve():
            raise ValueError("M4.3.2 registries must share one SQLite trust domain")
        self._lab = lab_registry
        self._executions = execution_registry
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
            raise LabPersistenceError("SQLite physical evidence operation failed") from exc

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lab_physical_evidence (
                    run_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES lab_registry_runs(run_id)
                )
                """
            )

    def finalize(self, run_id: str) -> PhysicalEvidenceRecovery:
        """Finalize already-observed execution without replaying or minting authority."""

        _validate_uuid(run_id, "run_id")
        execution = self._executions.recover(run_id)
        if (
            execution.phase is not RunExecutionPhase.OBSERVED
            or execution.evidence is None
            or not execution.execution_succeeded
        ):
            raise LabRegistryStateError(
                "Physical evidence finalization requires successful M4.3.1 observation"
            )
        lab_recovery = self._lab.recover_for_run(run_id)
        run_snapshot = lab_recovery.run(run_id)
        if run_snapshot.evidence_sealed:
            raise LabRegistryStateError(
                "Cannot finalize physical evidence for a sealed run"
            )
        if run_snapshot.run.to_dict() != execution.binding.run.to_dict():
            raise EvidenceBindingError(
                "M4.2 run disagrees with M4.3.1 execution binding"
            )
        spec = PythonJsonExperimentSpec.from_manifest(lab_recovery.manifest)
        logical_path = _logical_output_path(run_id)
        snapshot = _read_physical_output(
            execution.binding.proposal.workspace_root,
            logical_path,
        )
        stdout_bytes = _observed_stdout_bytes(execution.evidence)
        if snapshot.data != stdout_bytes or not hmac.compare_digest(
            snapshot.sha256,
            execution.evidence.observation.stdout.sha256,
        ):
            raise EvidenceBindingError(
                "Physical result bytes differ from exact stdout bytes bound to the execution observation"
            )
        metric_value = _extract_metric(snapshot, run_id=run_id, spec=spec)
        created_at = execution.evidence.created_at
        artifact = ArtifactRecord.create(
            run=run_snapshot.run,
            logical_path=logical_path,
            size_bytes=snapshot.size_bytes,
            sha256_digest=snapshot.sha256,
            media_type="application/json",
            artifact_id=str(
                uuid5(UUID(run_id), "codexia:m4.3.2:artifact:result.json")
            ),
            created_at=created_at,
        )
        metric = MetricRecord.create(
            run=run_snapshot.run,
            name=spec.metric_name,
            value=metric_value,
            unit=spec.metric_unit,
            metric_id=str(
                uuid5(UUID(run_id), f"codexia:m4.3.2:metric:{spec.metric_name}")
            ),
            created_at=created_at,
        )
        self._ensure_artifact(artifact)
        self._ensure_metric(metric)

        current = _read_physical_output(
            execution.binding.proposal.workspace_root,
            logical_path,
        )
        if current.data != stdout_bytes:
            raise EvidenceBindingError(
                "Physical result changed before terminal evidence publication"
            )
        receipt = PhysicalEvidenceReceipt.create(
            run=run_snapshot.run,
            execution_evidence=execution.evidence,
            snapshot=current,
            artifact=artifact,
            metric=metric,
        )
        return self.publish(receipt)

    def publish(self, receipt: PhysicalEvidenceReceipt) -> PhysicalEvidenceRecovery:
        if not isinstance(receipt, PhysicalEvidenceReceipt):
            raise TypeError("receipt must be a PhysicalEvidenceReceipt")
        self._validate_dependencies(receipt, verify_physical=True)
        raw = _canonical_json(receipt.to_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM lab_physical_evidence WHERE run_id = ?",
                (receipt.run_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["payload_json"] != raw
                    or row["receipt_digest"] != receipt.receipt_digest
                ):
                    connection.execute("ROLLBACK")
                    raise LabIdentityConflictError(
                        "Run already has different physical evidence"
                    )
                connection.execute("COMMIT")
            else:
                connection.execute(
                    """
                    INSERT INTO lab_physical_evidence(
                        run_id, receipt_id, payload_json, receipt_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        receipt.run_id,
                        receipt.receipt_id,
                        raw,
                        receipt.receipt_digest,
                    ),
                )
                connection.execute("COMMIT")
        return self.recover(receipt.run_id)

    def recover(self, run_id: str) -> PhysicalEvidenceRecovery:
        _validate_uuid(run_id, "run_id")
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM lab_physical_evidence WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise InvalidLabRecordError("Unknown physical evidence for run")
            raw = row["payload_json"]
            if not isinstance(raw, str) or len(raw) > MAX_PHYSICAL_RECEIPT_JSON_CHARS:
                raise LabPersistenceIntegrityError(
                    "Persisted physical evidence JSON is invalid"
                )
            try:
                value = json.loads(
                    raw,
                    object_pairs_hook=_reject_duplicate_pairs,
                    parse_constant=_reject_constant,
                )
                receipt = physical_evidence_receipt_from_dict(value)
            except (
                EvidenceBindingError,
                InvalidLabRecordError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise LabPersistenceIntegrityError(
                    "Persisted physical evidence failed validation"
                ) from exc
            if _canonical_json(receipt.to_dict()) != raw:
                raise LabPersistenceIntegrityError(
                    "Persisted physical evidence is not canonical JSON"
                )
            if (
                row["receipt_id"] != receipt.receipt_id
                or row["receipt_digest"] != receipt.receipt_digest
            ):
                raise LabPersistenceIntegrityError(
                    "Persisted physical evidence indexes disagree with receipt"
                )
            connection.execute("COMMIT")
        try:
            return self._validate_dependencies(receipt, verify_physical=True)
        except (
            EvidenceBindingError,
            InvalidLabRecordError,
            LabRegistryStateError,
        ) as exc:
            raise LabPersistenceIntegrityError(
                "Physical evidence no longer matches durable execution/evidence state"
            ) from exc

    def _ensure_artifact(self, artifact: ArtifactRecord) -> None:
        snapshot = self._lab.recover_for_run(artifact.run_id).run(artifact.run_id)
        same_path = [
            item
            for item in snapshot.artifacts.values()
            if item.logical_path == artifact.logical_path
        ]
        if same_path:
            if len(same_path) != 1 or same_path[0] != artifact:
                raise EvidenceBindingError(
                    "Durable artifact path is already bound to different bytes"
                )
            return
        self._lab.register_artifact(artifact)

    def _ensure_metric(self, metric: MetricRecord) -> None:
        snapshot = self._lab.recover_for_run(metric.run_id).run(metric.run_id)
        same_name = [item for item in snapshot.metrics.values() if item.name == metric.name]
        if same_name:
            if len(same_name) != 1 or same_name[0] != metric:
                raise EvidenceBindingError(
                    "Durable metric name is already bound to a different value"
                )
            return
        self._lab.register_metric(metric)

    def _validate_dependencies(
        self,
        receipt: PhysicalEvidenceReceipt,
        *,
        verify_physical: bool,
    ) -> PhysicalEvidenceRecovery:
        execution = self._executions.recover(receipt.run_id)
        if (
            execution.phase is not RunExecutionPhase.OBSERVED
            or execution.evidence is None
            or not execution.execution_succeeded
            or not hmac.compare_digest(
                execution.evidence.evidence_digest,
                receipt.execution_evidence_digest,
            )
            or not hmac.compare_digest(
                execution.evidence.observation.observation_digest,
                receipt.observation_digest,
            )
            or not hmac.compare_digest(
                execution.evidence.observation.stdout.sha256,
                receipt.stdout_sha256,
            )
        ):
            raise EvidenceBindingError(
                "Physical evidence lacks exact successful M4.3.1 execution provenance"
            )
        recovery = self._lab.recover_for_run(receipt.run_id)
        run = recovery.run(receipt.run_id)
        if run.run.to_dict() != execution.binding.run.to_dict():
            raise EvidenceBindingError(
                "Physical evidence run disagrees with execution binding"
            )
        artifact = run.artifacts.get(receipt.artifact.artifact_id)
        metric = run.metrics.get(receipt.metric.metric_id)
        if artifact != receipt.artifact or metric != receipt.metric:
            raise EvidenceBindingError(
                "Physical evidence records are not durably registered in M4.2"
            )
        if verify_physical:
            snapshot = _read_physical_output(
                execution.binding.proposal.workspace_root,
                receipt.output_logical_path,
            )
            stdout_bytes = _observed_stdout_bytes(execution.evidence)
            if (
                snapshot.data != stdout_bytes
                or snapshot.size_bytes != receipt.output_size_bytes
                or not hmac.compare_digest(snapshot.sha256, receipt.output_sha256)
                or not hmac.compare_digest(snapshot.sha256, receipt.stdout_sha256)
            ):
                raise EvidenceBindingError(
                    "Physical output bytes changed or no longer match exact observed stdout"
                )
            spec = PythonJsonExperimentSpec.from_manifest(recovery.manifest)
            value = _extract_metric(snapshot, run_id=receipt.run_id, spec=spec)
            if value != receipt.metric.value:
                raise EvidenceBindingError(
                    "Recovered physical bytes no longer yield the durable metric"
                )
        return PhysicalEvidenceRecovery(
            receipt=receipt,
            execution=execution,
            run=run,
            artifact=receipt.artifact,
            metric=receipt.metric,
        )


@dataclass(frozen=True, slots=True)
class GovernedPythonRunResult:
    execution: RunExecutionRecovery
    physical: PhysicalEvidenceRecovery | None


class GovernedPythonJsonRunner:
    """One narrow M4.3.2 profile over the existing M2/M3 authority spine."""

    def __init__(
        self,
        lab_registry: SqliteLabRegistry,
        session_store: SqliteSessionEventStore,
        execution_registry: SqliteRunExecutionRegistry,
        physical_registry: SqlitePhysicalEvidenceRegistry,
    ) -> None:
        paths = {
            lab_registry.database_path.resolve(),
            session_store.path.resolve(),
            execution_registry.database_path.resolve(),
            physical_registry.database_path.resolve(),
        }
        if len(paths) != 1:
            raise ValueError("M4.3.2 runner requires one SQLite trust domain")
        self._lab = lab_registry
        self._m3 = session_store
        self._executions = execution_registry
        self._physical = physical_registry

    def prepare(
        self,
        *,
        run_id: str,
        m3_session_id: str,
        workspace: str | Path,
        python_executable: str | Path | None = None,
    ) -> PreparedPythonJsonRun:
        recovery = self._lab.recover_for_run(run_id)
        run_snapshot = recovery.run(run_id)
        if run_snapshot.evidence_sealed:
            raise LabRegistryStateError("Cannot prepare execution for a sealed run")
        spec = PythonJsonExperimentSpec.from_manifest(recovery.manifest)
        output_path = _logical_output_path(run_id)
        if os.path.lexists(Path(workspace) / PurePosixPath(output_path)):
            raise EvidenceBindingError(
                "Deterministic M4.3.2 output already exists before execution"
            )
        executable = str(python_executable or sys.executable)
        proposal = prepare_process_proposal(
            workspace=workspace,
            argv=[
                executable,
                "-I",
                "-c",
                spec.source,
                run_id,
                output_path,
                spec.input_json,
            ],
            limits=ProcessLimits(
                timeout_seconds=30.0,
                max_stdout_bytes=MAX_RESULT_BYTES,
                max_stderr_bytes=65_536,
            ),
            summary="Execute one exact M4.3.2 inline Python JSON experiment.",
        )
        binding = RunExecutionBinding.create(run=run_snapshot.run, proposal=proposal)
        self._m3.record_proposal(m3_session_id, proposal)
        self._executions.register_binding(
            binding,
            m3_session_id=m3_session_id,
        )
        return PreparedPythonJsonRun(
            run=run_snapshot.run,
            manifest_digest=recovery.manifest.manifest_digest,
            m3_session_id=m3_session_id,
            output_logical_path=output_path,
            spec=spec,
            binding=binding,
        )

    def execute_authorized(
        self,
        prepared: PreparedPythonJsonRun,
        *,
        receipt: AuthorizationReceipt,
    ) -> GovernedPythonRunResult:
        if not isinstance(prepared, PreparedPythonJsonRun):
            raise TypeError("prepared must be a PreparedPythonJsonRun")
        if not isinstance(receipt, AuthorizationReceipt):
            raise TypeError("receipt must be an AuthorizationReceipt")
        authority = LocalApprovalAuthority(
            consumption_registry=DurableAuthorizationConsumptionRegistry(
                self._m3,
                session_id=prepared.m3_session_id,
            )
        )
        authority.verify_authorization(
            prepared.proposal,
            receipt,
            mode=receipt.mode,
        )
        self._m3.record_authorization(prepared.m3_session_id, receipt)
        authorization = RunExecutionAuthorization.create(
            binding=prepared.binding,
            receipt=receipt,
        )
        self._executions.register_authorization(authorization)

        output = Path(prepared.proposal.workspace_root) / PurePosixPath(
            prepared.output_logical_path
        )
        if os.path.lexists(output):
            raise EvidenceBindingError(
                "M4.3.2 output appeared before one-shot execution consumption"
            )

        lifecycle = ActionLifecycle(prepared.proposal, receipt.mode)
        lifecycle.apply_receipt(receipt, authority=authority)
        observation = ProcessExecutor().execute(lifecycle, authority=authority)
        self._m3.record_execution(
            prepared.m3_session_id,
            proposal=prepared.proposal,
            receipt=receipt,
            execution_id=observation.execution_id,
        )
        self._m3.record_observation(
            prepared.m3_session_id,
            proposal=prepared.proposal,
            execution_id=observation.execution_id,
            observation_id=observation.observation_id,
            observation_digest=observation.observation_digest,
        )
        execution_evidence = RunExecutionEvidence.create(
            binding=prepared.binding,
            authorization=authorization,
            observation=observation,
        )
        execution = self._executions.register_evidence(execution_evidence)
        if (
            observation.termination_reason is not ProcessTerminationReason.EXITED
            or observation.exit_code != 0
        ):
            return GovernedPythonRunResult(execution=execution, physical=None)
        physical = self._physical.finalize(prepared.run.run_id)
        return GovernedPythonRunResult(execution=execution, physical=physical)
