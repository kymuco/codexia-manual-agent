from __future__ import annotations

import base64
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping
from uuid import UUID, uuid4

from codexia_manual_agent.authority.models import (
    ActionProposal,
    ApprovalMode,
    AuthorizationDecision,
    AuthorizationReceipt,
    AuthorizationSource,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.execution.models import (
    ProcessExecutionObservation,
    ProcessTerminationReason,
    StreamObservation,
)
from codexia_manual_agent.execution.process import PROCESS_ACTION
from codexia_manual_agent.lab.errors import EvidenceBindingError, InvalidLabRecordError
from codexia_manual_agent.lab.models import ExperimentRun


RUN_EXECUTION_SCHEMA_VERSION = 1
MAX_RUN_EXECUTION_TIMESTAMP_CHARS = 64
MAX_RUN_EXECUTION_ERROR_CHARS = 8_192
MAX_RUN_EXECUTION_ARG_COUNT = 256
MAX_RUN_EXECUTION_ARG_CHARS = 32_768
MAX_RUN_EXECUTION_TOTAL_ARG_CHARS = 131_072

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_PARAMETER_KEYS = {
    "argv",
    "resolved_executable",
    "executable_size",
    "executable_sha256",
    "cwd",
    "environment_profile",
    "environment",
    "limits",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidLabRecordError(
            f"{field_name} must use canonical lowercase hyphenated UUID form"
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
        or len(value) > MAX_RUN_EXECUTION_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidLabRecordError(
            f"{field_name} must be bounded canonical timezone-aware ISO-8601"
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


def _new_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_argv(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise InvalidLabRecordError(f"{field_name} must be a sequence of strings")
    argv = tuple(value)
    if not argv or len(argv) > MAX_RUN_EXECUTION_ARG_COUNT:
        raise InvalidLabRecordError(f"{field_name} is empty or exceeds the entry budget")
    total = 0
    for index, item in enumerate(argv):
        if type(item) is not str or not item or "\x00" in item:
            raise InvalidLabRecordError(f"{field_name}[{index}] is invalid")
        if len(item) > MAX_RUN_EXECUTION_ARG_CHARS:
            raise InvalidLabRecordError(f"{field_name}[{index}] exceeds the character budget")
        total += len(item)
    if total > MAX_RUN_EXECUTION_TOTAL_ARG_CHARS:
        raise InvalidLabRecordError(f"{field_name} exceeds the total character budget")
    return argv


def _process_identity(proposal: ActionProposal) -> tuple[str, str, tuple[str, ...]]:
    if not isinstance(proposal, ActionProposal):
        raise TypeError("proposal must be an ActionProposal")
    if proposal.capability is not Capability.EXECUTE_PROCESS:
        raise EvidenceBindingError(
            "M4.3 run execution binding requires execute_process authority"
        )
    if proposal.action != PROCESS_ACTION:
        raise EvidenceBindingError("M4.3 run execution binding requires the M2.1 process action")
    parameters = proposal.to_dict()["parameters"]
    if not isinstance(parameters, Mapping) or set(parameters) != _PROCESS_PARAMETER_KEYS:
        raise EvidenceBindingError("Bound process proposal does not match the M2.1 schema")
    argv = _validate_argv(parameters["argv"], "proposal argv")
    resolved_executable = parameters["resolved_executable"]
    cwd = parameters["cwd"]
    if (
        type(resolved_executable) is not str
        or not resolved_executable
        or "\x00" in resolved_executable
    ):
        raise EvidenceBindingError("Bound process proposal executable identity is invalid")
    if type(cwd) is not str or not cwd or "\x00" in cwd:
        raise EvidenceBindingError("Bound process proposal cwd is invalid")
    expected_argv = (resolved_executable, *argv[1:])
    return cwd, resolved_executable, expected_argv


def _proposal_from_dict(value: Any) -> ActionProposal:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "proposal_id",
            "created_at",
            "capability",
            "action",
            "workspace_root",
            "parameters",
            "summary",
            "proposal_digest",
        },
        "proposal",
    )
    try:
        return ActionProposal(**dict(data))
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError("Invalid bound M2 action proposal") from exc


def _receipt_from_dict(value: Any) -> AuthorizationReceipt:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "receipt_id",
            "created_at",
            "proposal_id",
            "proposal_digest",
            "decision",
            "mode",
            "source",
            "actor",
            "reason",
            "single_use",
            "receipt_digest",
        },
        "authorization receipt",
    )
    try:
        return AuthorizationReceipt(**dict(data))
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError("Invalid bound M2 authorization receipt") from exc


def _stream_from_dict(value: Any, label: str) -> StreamObservation:
    data = _exact_keys(
        value,
        {"byte_count", "sha256", "data_base64", "truncated", "text_utf8"},
        label,
    )
    byte_count = data["byte_count"]
    if type(byte_count) is not int or byte_count < 0:
        raise InvalidLabRecordError(f"{label}.byte_count must be a non-negative integer")
    digest = _validate_digest(data["sha256"], f"{label}.sha256")
    encoded = data["data_base64"]
    if not isinstance(encoded, str):
        raise InvalidLabRecordError(f"{label}.data_base64 must be text")
    try:
        stored = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise InvalidLabRecordError(f"{label}.data_base64 is invalid") from exc
    truncated = data["truncated"]
    if type(truncated) is not bool or truncated != (byte_count > len(stored)):
        raise InvalidLabRecordError(f"{label}.truncated is inconsistent")
    text_utf8 = data["text_utf8"]
    if text_utf8 is not None and not isinstance(text_utf8, str):
        raise InvalidLabRecordError(f"{label}.text_utf8 must be text or null")
    if text_utf8 is not None:
        try:
            if stored.decode("utf-8") != text_utf8:
                raise InvalidLabRecordError(f"{label}.text_utf8 is inconsistent")
        except UnicodeDecodeError as exc:
            raise InvalidLabRecordError(f"{label}.text_utf8 is inconsistent") from exc
    elif stored:
        try:
            stored.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            raise InvalidLabRecordError(f"{label}.text_utf8 is missing for UTF-8 data")
    return StreamObservation(
        byte_count=byte_count,
        sha256=digest,
        data_base64=encoded,
        truncated=truncated,
        text_utf8=text_utf8,
    )


def _observation_from_dict(value: Any) -> ProcessExecutionObservation:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "observation_id",
            "created_at",
            "proposal_id",
            "proposal_digest",
            "receipt_id",
            "receipt_digest",
            "execution_id",
            "started",
            "pid",
            "cwd",
            "resolved_executable",
            "argv",
            "exit_code",
            "termination_reason",
            "duration_ms",
            "stdout",
            "stderr",
            "error",
            "observation_digest",
        },
        "process observation",
    )
    try:
        return ProcessExecutionObservation(
            schema_version=data["schema_version"],
            observation_id=data["observation_id"],
            created_at=data["created_at"],
            proposal_id=data["proposal_id"],
            proposal_digest=data["proposal_digest"],
            receipt_id=data["receipt_id"],
            receipt_digest=data["receipt_digest"],
            execution_id=data["execution_id"],
            started=data["started"],
            pid=data["pid"],
            cwd=data["cwd"],
            resolved_executable=data["resolved_executable"],
            argv=_validate_argv(data["argv"], "observation argv"),
            exit_code=data["exit_code"],
            termination_reason=ProcessTerminationReason(data["termination_reason"]),
            duration_ms=data["duration_ms"],
            stdout=_stream_from_dict(data["stdout"], "stdout"),
            stderr=_stream_from_dict(data["stderr"], "stderr"),
            error=data["error"],
            observation_digest=data["observation_digest"],
        )
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError("Invalid bound M2 process observation") from exc


@dataclass(frozen=True, slots=True)
class RunExecutionBinding:
    schema_version: int
    binding_id: str
    created_at: str
    run: ExperimentRun
    proposal: ActionProposal
    binding_digest: str

    @classmethod
    def create(
        cls,
        *,
        run: ExperimentRun,
        proposal: ActionProposal,
        binding_id: str | None = None,
        created_at: str | None = None,
    ) -> "RunExecutionBinding":
        if not isinstance(run, ExperimentRun):
            raise TypeError("run must be an ExperimentRun")
        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be an ActionProposal")
        binding_id = binding_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        base = {
            "schema_version": RUN_EXECUTION_SCHEMA_VERSION,
            "binding_id": binding_id,
            "created_at": created_at,
            "run": run.to_dict(),
            "proposal": proposal.to_dict(),
        }
        return cls(
            schema_version=RUN_EXECUTION_SCHEMA_VERSION,
            binding_id=binding_id,
            created_at=created_at,
            run=run,
            proposal=proposal,
            binding_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != RUN_EXECUTION_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M4.3 run execution binding schema")
        _validate_uuid(self.binding_id, "binding_id")
        _validate_timestamp(self.created_at, "created_at")
        if not isinstance(self.run, ExperimentRun):
            raise InvalidLabRecordError("binding run must be an ExperimentRun")
        _validate_uuid(self.run.run_id, "run.run_id")
        _validate_uuid(self.run.experiment_id, "run.experiment_id")
        if not isinstance(self.proposal, ActionProposal):
            raise InvalidLabRecordError("binding proposal must be an ActionProposal")
        _validate_uuid(self.proposal.proposal_id, "proposal.proposal_id")
        _process_identity(self.proposal)
        _validate_digest(self.binding_digest, "binding_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.binding_digest):
            raise InvalidLabRecordError("Run execution binding digest does not match payload")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "binding_id": self.binding_id,
            "created_at": self.created_at,
            "run": self.run.to_dict(),
            "proposal": self.proposal.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "binding_digest": self.binding_digest}


@dataclass(frozen=True, slots=True)
class RunExecutionAuthorization:
    schema_version: int
    authorization_id: str
    created_at: str
    binding_id: str
    binding_digest: str
    proposal_id: str
    proposal_digest: str
    receipt: AuthorizationReceipt
    authorization_digest: str

    @classmethod
    def create(
        cls,
        *,
        binding: RunExecutionBinding,
        receipt: AuthorizationReceipt,
        authorization_id: str | None = None,
        created_at: str | None = None,
    ) -> "RunExecutionAuthorization":
        if not isinstance(binding, RunExecutionBinding):
            raise TypeError("binding must be a RunExecutionBinding")
        if not isinstance(receipt, AuthorizationReceipt):
            raise TypeError("receipt must be an AuthorizationReceipt")
        authorization_id = authorization_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        base = {
            "schema_version": RUN_EXECUTION_SCHEMA_VERSION,
            "authorization_id": authorization_id,
            "created_at": created_at,
            "binding_id": binding.binding_id,
            "binding_digest": binding.binding_digest,
            "proposal_id": binding.proposal.proposal_id,
            "proposal_digest": binding.proposal.proposal_digest,
            "receipt": receipt.to_dict(),
        }
        return cls(
            schema_version=RUN_EXECUTION_SCHEMA_VERSION,
            authorization_id=authorization_id,
            created_at=created_at,
            binding_id=binding.binding_id,
            binding_digest=binding.binding_digest,
            proposal_id=binding.proposal.proposal_id,
            proposal_digest=binding.proposal.proposal_digest,
            receipt=receipt,
            authorization_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != RUN_EXECUTION_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M4.3 run execution authorization schema")
        _validate_uuid(self.authorization_id, "authorization_id")
        _validate_timestamp(self.created_at, "created_at")
        _validate_uuid(self.binding_id, "binding_id")
        _validate_digest(self.binding_digest, "binding_digest")
        _validate_uuid(self.proposal_id, "proposal_id")
        _validate_digest(self.proposal_digest, "proposal_digest")
        if not isinstance(self.receipt, AuthorizationReceipt):
            raise InvalidLabRecordError("authorization receipt must be an AuthorizationReceipt")
        if self.receipt.decision is not AuthorizationDecision.ALLOW:
            raise EvidenceBindingError("Only an ALLOW receipt can authorize a run execution")
        if (
            self.receipt.proposal_id != self.proposal_id
            or not hmac.compare_digest(self.receipt.proposal_digest, self.proposal_digest)
        ):
            raise EvidenceBindingError(
                "Run execution authorization does not bind the exact execution proposal"
            )
        _validate_digest(self.authorization_digest, "authorization_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.authorization_digest):
            raise InvalidLabRecordError(
                "Run execution authorization digest does not match payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "authorization_id": self.authorization_id,
            "created_at": self.created_at,
            "binding_id": self.binding_id,
            "binding_digest": self.binding_digest,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "receipt": self.receipt.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "authorization_digest": self.authorization_digest}


@dataclass(frozen=True, slots=True)
class RunExecutionEvidence:
    schema_version: int
    evidence_id: str
    created_at: str
    binding_id: str
    binding_digest: str
    authorization_id: str
    authorization_digest: str
    experiment_id: str
    run_id: str
    run_digest: str
    manifest_digest: str
    proposal_id: str
    proposal_digest: str
    receipt_id: str
    receipt_digest: str
    expected_cwd: str
    expected_resolved_executable: str
    expected_argv: tuple[str, ...]
    observation: ProcessExecutionObservation
    evidence_digest: str

    @classmethod
    def create(
        cls,
        *,
        binding: RunExecutionBinding,
        authorization: RunExecutionAuthorization,
        observation: ProcessExecutionObservation,
        evidence_id: str | None = None,
        created_at: str | None = None,
    ) -> "RunExecutionEvidence":
        if not isinstance(binding, RunExecutionBinding):
            raise TypeError("binding must be a RunExecutionBinding")
        if not isinstance(authorization, RunExecutionAuthorization):
            raise TypeError("authorization must be a RunExecutionAuthorization")
        if not isinstance(observation, ProcessExecutionObservation):
            raise TypeError("observation must be a ProcessExecutionObservation")
        expected_cwd, expected_executable, expected_argv = _process_identity(binding.proposal)
        evidence_id = evidence_id or str(uuid4())
        created_at = created_at or _new_timestamp()
        base = {
            "schema_version": RUN_EXECUTION_SCHEMA_VERSION,
            "evidence_id": evidence_id,
            "created_at": created_at,
            "binding_id": binding.binding_id,
            "binding_digest": binding.binding_digest,
            "authorization_id": authorization.authorization_id,
            "authorization_digest": authorization.authorization_digest,
            "experiment_id": binding.run.experiment_id,
            "run_id": binding.run.run_id,
            "run_digest": binding.run.run_digest,
            "manifest_digest": binding.run.manifest_digest,
            "proposal_id": binding.proposal.proposal_id,
            "proposal_digest": binding.proposal.proposal_digest,
            "receipt_id": authorization.receipt.receipt_id,
            "receipt_digest": authorization.receipt.receipt_digest,
            "expected_cwd": expected_cwd,
            "expected_resolved_executable": expected_executable,
            "expected_argv": list(expected_argv),
            "observation": observation.to_dict(),
        }
        return cls(
            schema_version=RUN_EXECUTION_SCHEMA_VERSION,
            evidence_id=evidence_id,
            created_at=created_at,
            binding_id=binding.binding_id,
            binding_digest=binding.binding_digest,
            authorization_id=authorization.authorization_id,
            authorization_digest=authorization.authorization_digest,
            experiment_id=binding.run.experiment_id,
            run_id=binding.run.run_id,
            run_digest=binding.run.run_digest,
            manifest_digest=binding.run.manifest_digest,
            proposal_id=binding.proposal.proposal_id,
            proposal_digest=binding.proposal.proposal_digest,
            receipt_id=authorization.receipt.receipt_id,
            receipt_digest=authorization.receipt.receipt_digest,
            expected_cwd=expected_cwd,
            expected_resolved_executable=expected_executable,
            expected_argv=expected_argv,
            observation=observation,
            evidence_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != RUN_EXECUTION_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M4.3 run execution evidence schema")
        for name, value in (
            ("evidence_id", self.evidence_id),
            ("binding_id", self.binding_id),
            ("authorization_id", self.authorization_id),
            ("experiment_id", self.experiment_id),
            ("run_id", self.run_id),
            ("proposal_id", self.proposal_id),
            ("receipt_id", self.receipt_id),
        ):
            _validate_uuid(value, name)
        _validate_timestamp(self.created_at, "created_at")
        for name, value in (
            ("binding_digest", self.binding_digest),
            ("authorization_digest", self.authorization_digest),
            ("run_digest", self.run_digest),
            ("manifest_digest", self.manifest_digest),
            ("proposal_digest", self.proposal_digest),
            ("receipt_digest", self.receipt_digest),
        ):
            _validate_digest(value, name)
        if type(self.expected_cwd) is not str or not self.expected_cwd or "\x00" in self.expected_cwd:
            raise InvalidLabRecordError("expected_cwd is invalid")
        if (
            type(self.expected_resolved_executable) is not str
            or not self.expected_resolved_executable
            or "\x00" in self.expected_resolved_executable
        ):
            raise InvalidLabRecordError("expected_resolved_executable is invalid")
        expected_argv = _validate_argv(self.expected_argv, "expected_argv")
        object.__setattr__(self, "expected_argv", expected_argv)
        if not isinstance(self.observation, ProcessExecutionObservation):
            raise InvalidLabRecordError("observation must be a ProcessExecutionObservation")
        if (
            self.observation.proposal_id != self.proposal_id
            or not hmac.compare_digest(self.observation.proposal_digest, self.proposal_digest)
            or self.observation.receipt_id != self.receipt_id
            or not hmac.compare_digest(self.observation.receipt_digest, self.receipt_digest)
        ):
            raise EvidenceBindingError(
                "Process observation does not bind the exact proposal/receipt lineage"
            )
        if (
            self.observation.cwd != self.expected_cwd
            or self.observation.resolved_executable != self.expected_resolved_executable
            or self.observation.argv != self.expected_argv
        ):
            raise EvidenceBindingError(
                "Process observation does not match the exact pre-execution process identity"
            )
        if self.observation.error is not None and (
            not isinstance(self.observation.error, str)
            or len(self.observation.error) > MAX_RUN_EXECUTION_ERROR_CHARS
        ):
            raise InvalidLabRecordError("Process observation error exceeds the M4.3 budget")
        _validate_digest(self.evidence_digest, "evidence_digest")
        if not hmac.compare_digest(_digest(self._payload()), self.evidence_digest):
            raise InvalidLabRecordError("Run execution evidence digest does not match payload")

    @property
    def execution_succeeded(self) -> bool:
        return (
            self.observation.started
            and self.observation.termination_reason is ProcessTerminationReason.EXITED
            and self.observation.exit_code == 0
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "created_at": self.created_at,
            "binding_id": self.binding_id,
            "binding_digest": self.binding_digest,
            "authorization_id": self.authorization_id,
            "authorization_digest": self.authorization_digest,
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "run_digest": self.run_digest,
            "manifest_digest": self.manifest_digest,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "receipt_id": self.receipt_id,
            "receipt_digest": self.receipt_digest,
            "expected_cwd": self.expected_cwd,
            "expected_resolved_executable": self.expected_resolved_executable,
            "expected_argv": list(self.expected_argv),
            "observation": self.observation.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "evidence_digest": self.evidence_digest}


def run_execution_binding_from_dict(value: Any) -> RunExecutionBinding:
    data = _exact_keys(
        value,
        {"schema_version", "binding_id", "created_at", "run", "proposal", "binding_digest"},
        "run execution binding",
    )
    from codexia_manual_agent.lab.serialization import experiment_run_from_dict

    return RunExecutionBinding(
        schema_version=data["schema_version"],
        binding_id=data["binding_id"],
        created_at=data["created_at"],
        run=experiment_run_from_dict(data["run"]),
        proposal=_proposal_from_dict(data["proposal"]),
        binding_digest=data["binding_digest"],
    )


def run_execution_authorization_from_dict(value: Any) -> RunExecutionAuthorization:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "authorization_id",
            "created_at",
            "binding_id",
            "binding_digest",
            "proposal_id",
            "proposal_digest",
            "receipt",
            "authorization_digest",
        },
        "run execution authorization",
    )
    return RunExecutionAuthorization(
        schema_version=data["schema_version"],
        authorization_id=data["authorization_id"],
        created_at=data["created_at"],
        binding_id=data["binding_id"],
        binding_digest=data["binding_digest"],
        proposal_id=data["proposal_id"],
        proposal_digest=data["proposal_digest"],
        receipt=_receipt_from_dict(data["receipt"]),
        authorization_digest=data["authorization_digest"],
    )


def run_execution_evidence_from_dict(value: Any) -> RunExecutionEvidence:
    data = _exact_keys(
        value,
        {
            "schema_version",
            "evidence_id",
            "created_at",
            "binding_id",
            "binding_digest",
            "authorization_id",
            "authorization_digest",
            "experiment_id",
            "run_id",
            "run_digest",
            "manifest_digest",
            "proposal_id",
            "proposal_digest",
            "receipt_id",
            "receipt_digest",
            "expected_cwd",
            "expected_resolved_executable",
            "expected_argv",
            "observation",
            "evidence_digest",
        },
        "run execution evidence",
    )
    return RunExecutionEvidence(
        schema_version=data["schema_version"],
        evidence_id=data["evidence_id"],
        created_at=data["created_at"],
        binding_id=data["binding_id"],
        binding_digest=data["binding_digest"],
        authorization_id=data["authorization_id"],
        authorization_digest=data["authorization_digest"],
        experiment_id=data["experiment_id"],
        run_id=data["run_id"],
        run_digest=data["run_digest"],
        manifest_digest=data["manifest_digest"],
        proposal_id=data["proposal_id"],
        proposal_digest=data["proposal_digest"],
        receipt_id=data["receipt_id"],
        receipt_digest=data["receipt_digest"],
        expected_cwd=data["expected_cwd"],
        expected_resolved_executable=data["expected_resolved_executable"],
        expected_argv=_validate_argv(data["expected_argv"], "expected_argv"),
        observation=_observation_from_dict(data["observation"]),
        evidence_digest=data["evidence_digest"],
    )
