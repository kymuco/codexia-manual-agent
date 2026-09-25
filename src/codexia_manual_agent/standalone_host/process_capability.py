from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from codexia_manual_agent.application.execute_process import (
    ExecuteProcessService,
    ProcessExecutionResult,
)
from codexia_manual_agent.authority import ApprovalMode
from codexia_manual_agent.capability_core import (
    CapabilityBinding,
    CapabilityHostRequest,
    CapabilityOutcome,
)
from codexia_manual_agent.domain.errors import (
    ApprovalRequiredError,
    AuthorizationDeniedError,
    InvalidProcessSpecError,
    ProcessExecutableChangedError,
    ProcessExecutableNotFoundError,
    ProcessExecutionError,
    ProcessWorkspaceBoundaryError,
)
from codexia_manual_agent.execution import ProcessLimits, ProcessTerminationReason

STANDALONE_PROCESS_HOST_ID = "standalone.local"
PROCESS_CAPABILITY_ID = "process"
PROCESS_CAPABILITY_VERSION = "1.0.0"
PROCESS_OPERATION = "run"

_REQUIRED_PARAMETER_KEYS = {"argv", "cwd_ref", "cwd", "limits"}
_MAX_OUTCOME_STREAM_TEXT_CHARS = 32_768
_MAX_OUTCOME_ERROR_CHARS = 16_000


class StandaloneProcessHostError(RuntimeError):
    """Standalone process adapter rejected an incompatible Gen2 Need."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_text(value: str, limit: int) -> str:
    return value[:limit] if len(value) > limit else value


def _error_text(exc: BaseException) -> str:
    return _bounded_text(
        f"{type(exc).__name__}: {exc}",
        _MAX_OUTCOME_ERROR_CHARS,
    )


def _stream_summary(stream) -> dict[str, Any]:
    text = stream.text_utf8
    preview = (
        None
        if text is None
        else _bounded_text(text, _MAX_OUTCOME_STREAM_TEXT_CHARS)
    )
    return {
        "byte_count": stream.byte_count,
        "sha256": stream.sha256,
        "truncated": stream.truncated,
        "text_utf8_preview": preview,
        "text_preview_truncated": (
            text is not None and len(text) > _MAX_OUTCOME_STREAM_TEXT_CHARS
        ),
    }


def translate_standalone_process_request(
    request: CapabilityHostRequest,
    binding: CapabilityBinding,
) -> tuple[list[str], str, ProcessLimits]:
    """Validate and translate the exact process/run v1 host request."""

    if not isinstance(request, CapabilityHostRequest):
        raise TypeError("request must be CapabilityHostRequest")
    if not isinstance(binding, CapabilityBinding):
        raise TypeError("binding must be CapabilityBinding")

    need = request.need.need
    if need.binding != binding:
        raise StandaloneProcessHostError(
            "CapabilityNeed binding is not supported by this host"
        )
    if need.operation != PROCESS_OPERATION:
        raise StandaloneProcessHostError(
            "Standalone process host supports only operation='run'"
        )

    parameters = need.to_dict()["parameters"]
    if (
        not isinstance(parameters, Mapping)
        or set(parameters) != _REQUIRED_PARAMETER_KEYS
    ):
        raise StandaloneProcessHostError(
            "process/run parameters do not match the exact v1 schema"
        )

    if parameters["cwd_ref"] != "workspace":
        raise StandaloneProcessHostError(
            "process/run cwd_ref must be exactly 'workspace'"
        )

    argv = parameters["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or any(type(item) is not str or not item for item in argv)
    ):
        raise StandaloneProcessHostError(
            "process/run argv must be a non-empty string list"
        )

    cwd = parameters["cwd"]
    if not isinstance(cwd, str) or not cwd:
        raise StandaloneProcessHostError(
            "process/run cwd must be non-empty workspace-relative text"
        )

    limits_data = parameters["limits"]
    if not isinstance(limits_data, dict):
        raise StandaloneProcessHostError(
            "process/run limits must be an object"
        )
    try:
        limits = ProcessLimits(**limits_data)
    except (TypeError, ValueError) as exc:
        raise StandaloneProcessHostError(
            "process/run limits are invalid"
        ) from exc
    if limits.to_dict() != limits_data:
        raise StandaloneProcessHostError(
            "process/run limits are not canonical"
        )

    return list(argv), cwd, limits


class StandaloneProcessCapabilityPort:
    """Concrete standalone host for one exact process/run capability binding.

    This adapter does not invent authority or execution semantics. It translates
    one exact Gen2 CapabilityNeed into the existing ExecuteProcessService, which
    already owns local proposal construction, human approval binding, single-use
    authorization, process containment, and execution observation.

    The caller must provide an explicit boolean approval decision when building
    the port. This adapter does not prompt, infer approval, or treat handoff as
    permission.
    """

    host_id = STANDALONE_PROCESS_HOST_ID

    def __init__(
        self,
        *,
        workspace: str | Path,
        binding: CapabilityBinding,
        approved: bool,
        actor: str = "local-human",
        reason: str | None = None,
        service: ExecuteProcessService | None = None,
    ) -> None:
        if not isinstance(binding, CapabilityBinding):
            raise TypeError("binding must be CapabilityBinding")
        if binding.capability_id != PROCESS_CAPABILITY_ID:
            raise StandaloneProcessHostError(
                "Standalone process host requires capability_id='process'"
            )
        if binding.version != PROCESS_CAPABILITY_VERSION:
            raise StandaloneProcessHostError(
                "Standalone process host requires process capability version 1.0.0"
            )
        if type(approved) is not bool:
            raise TypeError("approved must be an explicit boolean")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be non-empty text")
        if reason is not None and not isinstance(reason, str):
            raise TypeError("reason must be text or None")

        self._workspace = Path(workspace)
        self._binding = binding
        self._approved = approved
        self._actor = actor
        self._reason = reason
        self._service = service or ExecuteProcessService()

    @property
    def binding(self) -> CapabilityBinding:
        return self._binding

    def submit(self, request: CapabilityHostRequest) -> CapabilityOutcome:
        if not isinstance(request, CapabilityHostRequest):
            raise TypeError("request must be CapabilityHostRequest")
        if request.handoff.host_id != self.host_id:
            raise StandaloneProcessHostError(
                "CapabilityHandoff is routed to another host"
            )

        need = request.need.need
        attempt_id = f"standalone-process:{request.handoff.handoff_id}"
        attempt_digest = _sha256_json(
            {
                "adapter": "standalone-process-host-v1",
                "host_id": self.host_id,
                "handoff_id": request.handoff.handoff_id,
                "handoff_digest": request.handoff.handoff_digest,
                "need_id": need.need_id,
                "need_digest": need.need_digest,
            }
        )

        try:
            argv, cwd, limits = self._translate_request(request)
            result = self._service.run(
                workspace=self._workspace,
                argv=argv,
                cwd=cwd,
                mode=ApprovalMode.RISKY,
                approved=self._approved,
                actor=self._actor,
                reason=self._reason,
                limits=limits,
                summary=f"Gen2 CapabilityNeed {need.need_id}",
            )
        except (
            ApprovalRequiredError,
            AuthorizationDeniedError,
            InvalidProcessSpecError,
            ProcessWorkspaceBoundaryError,
            ProcessExecutableNotFoundError,
            ProcessExecutableChangedError,
            StandaloneProcessHostError,
        ) as exc:
            return CapabilityOutcome.failed(
                request.need,
                attempt_id=attempt_id,
                attempt_digest=attempt_digest,
                error=_error_text(exc),
                observation={
                    "adapter": "standalone-process-host-v1",
                    "stage": "rejected_before_observed_effect",
                    "error_type": type(exc).__name__,
                },
            )
        except ProcessExecutionError as exc:
            return CapabilityOutcome.unknown(
                request.need,
                attempt_id=attempt_id,
                attempt_digest=attempt_digest,
                detail=_error_text(exc),
                observation={
                    "adapter": "standalone-process-host-v1",
                    "stage": "execution_ambiguity",
                    "error_type": type(exc).__name__,
                },
            )

        observation = self._observation(result)
        process = result.observation
        if (
            process.started
            and process.termination_reason is ProcessTerminationReason.EXITED
            and process.exit_code == 0
        ):
            return CapabilityOutcome.succeeded(
                request.need,
                attempt_id=attempt_id,
                attempt_digest=attempt_digest,
                observation=observation,
            )

        return CapabilityOutcome.failed(
            request.need,
            attempt_id=attempt_id,
            attempt_digest=attempt_digest,
            error=_bounded_text(
                (
                    process.error
                    or (
                        "Process did not complete successfully: "
                        f"{process.termination_reason.value}, "
                        f"exit_code={process.exit_code}"
                    )
                ),
                _MAX_OUTCOME_ERROR_CHARS,
            ),
            observation=observation,
        )

    def _translate_request(
        self,
        request: CapabilityHostRequest,
    ) -> tuple[list[str], str, ProcessLimits]:
        return translate_standalone_process_request(
            request,
            self._binding,
        )

    @staticmethod
    def _observation(result: ProcessExecutionResult) -> dict[str, Any]:
        receipt = result.authorization
        process = result.observation
        return {
            "adapter": "standalone-process-host-v1",
            "proposal": {
                "proposal_id": result.proposal.proposal_id,
                "proposal_digest": result.proposal.proposal_digest,
            },
            "authorization": {
                "receipt_id": receipt.receipt_id,
                "receipt_digest": receipt.receipt_digest,
                "decision": receipt.decision.value,
                "source": receipt.source.value,
            },
            "execution": {
                "execution_id": process.execution_id,
                "observation_id": process.observation_id,
                "observation_digest": process.observation_digest,
                "started": process.started,
                "cwd": process.cwd,
                "exit_code": process.exit_code,
                "termination_reason": process.termination_reason.value,
                "duration_ms": process.duration_ms,
                "stdout": _stream_summary(process.stdout),
                "stderr": _stream_summary(process.stderr),
                "error": (
                    None
                    if process.error is None
                    else _bounded_text(
                        process.error,
                        _MAX_OUTCOME_ERROR_CHARS,
                    )
                ),
            },
        }
