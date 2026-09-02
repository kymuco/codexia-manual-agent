from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4


class ProcessTerminationReason(StrEnum):
    EXITED = "exited"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    SPAWN_ERROR = "spawn_error"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: dict[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _validate_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest") from exc


@dataclass(frozen=True, slots=True)
class ProcessLimits:
    timeout_seconds: float = 30.0
    max_stdout_bytes: int = 65_536
    max_stderr_bytes: int = 65_536

    def __post_init__(self) -> None:
        timeout = float(self.timeout_seconds)
        if not 0.05 <= timeout <= 3600.0:
            raise ValueError("timeout_seconds must be between 0.05 and 3600")
        for name, value in (
            ("max_stdout_bytes", self.max_stdout_bytes),
            ("max_stderr_bytes", self.max_stderr_bytes),
        ):
            if type(value) is not int or not 1 <= value <= 16_777_216:
                raise ValueError(
                    f"{name} must be an integer between 1 and 16777216"
                )
        object.__setattr__(self, "timeout_seconds", timeout)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
        }


@dataclass(frozen=True, slots=True)
class StreamObservation:
    byte_count: int
    sha256: str
    data_base64: str
    truncated: bool
    text_utf8: str | None

    @classmethod
    def from_bytes(
        cls,
        *,
        byte_count: int,
        digest: str,
        stored: bytes,
    ) -> "StreamObservation":
        _validate_sha256(digest, "stream sha256")
        try:
            text = stored.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        return cls(
            byte_count=byte_count,
            sha256=digest,
            data_base64=base64.b64encode(stored).decode("ascii"),
            truncated=byte_count > len(stored),
            text_utf8=text,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "data_base64": self.data_base64,
            "truncated": self.truncated,
            "text_utf8": self.text_utf8,
        }


@dataclass(frozen=True, slots=True)
class ProcessExecutionObservation:
    schema_version: int
    observation_id: str
    created_at: str
    proposal_id: str
    proposal_digest: str
    receipt_id: str
    receipt_digest: str
    execution_id: str
    started: bool
    pid: int | None
    cwd: str
    resolved_executable: str
    argv: tuple[str, ...]
    exit_code: int | None
    termination_reason: ProcessTerminationReason
    duration_ms: int
    stdout: StreamObservation
    stderr: StreamObservation
    error: str | None
    observation_digest: str

    @classmethod
    def create(
        cls,
        *,
        proposal_id: str,
        proposal_digest: str,
        receipt_id: str,
        receipt_digest: str,
        execution_id: str,
        started: bool,
        pid: int | None,
        cwd: str,
        resolved_executable: str,
        argv: tuple[str, ...],
        exit_code: int | None,
        termination_reason: ProcessTerminationReason,
        duration_ms: int,
        stdout: StreamObservation,
        stderr: StreamObservation,
        error: str | None = None,
        observation_id: str | None = None,
        created_at: str | None = None,
    ) -> "ProcessExecutionObservation":
        observation_id = observation_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        payload = {
            "schema_version": 1,
            "observation_id": observation_id,
            "created_at": created_at,
            "proposal_id": proposal_id,
            "proposal_digest": proposal_digest,
            "receipt_id": receipt_id,
            "receipt_digest": receipt_digest,
            "execution_id": execution_id,
            "started": started,
            "pid": pid,
            "cwd": cwd,
            "resolved_executable": resolved_executable,
            "argv": list(argv),
            "exit_code": exit_code,
            "termination_reason": ProcessTerminationReason(termination_reason).value,
            "duration_ms": duration_ms,
            "stdout": stdout.to_dict(),
            "stderr": stderr.to_dict(),
            "error": error,
        }
        return cls(
            schema_version=1,
            observation_id=observation_id,
            created_at=created_at,
            proposal_id=proposal_id,
            proposal_digest=proposal_digest,
            receipt_id=receipt_id,
            receipt_digest=receipt_digest,
            execution_id=execution_id,
            started=started,
            pid=pid,
            cwd=cwd,
            resolved_executable=resolved_executable,
            argv=tuple(argv),
            exit_code=exit_code,
            termination_reason=ProcessTerminationReason(termination_reason),
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            error=error,
            observation_digest=_digest(payload),
        )

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Unsupported process observation schema version")
        _validate_uuid(self.observation_id, "observation_id")
        _validate_uuid(self.proposal_id, "proposal_id")
        _validate_uuid(self.receipt_id, "receipt_id")
        _validate_sha256(self.proposal_digest, "proposal_digest")
        _validate_sha256(self.receipt_digest, "receipt_digest")
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise ValueError("execution_id must be non-empty")
        expected = _digest(self._payload())
        if not hmac.compare_digest(expected, self.observation_digest):
            raise ValueError("Process observation digest does not match payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "created_at": self.created_at,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "receipt_id": self.receipt_id,
            "receipt_digest": self.receipt_digest,
            "execution_id": self.execution_id,
            "started": self.started,
            "pid": self.pid,
            "cwd": self.cwd,
            "resolved_executable": self.resolved_executable,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "termination_reason": self.termination_reason.value,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout.to_dict(),
            "stderr": self.stderr.to_dict(),
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["observation_digest"] = self.observation_digest
        return payload
