from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from codexia_manual_agent.authority import (
    ApprovalMode,
    AuthorizationDecision,
    LocalApprovalAuthority,
)
from codexia_manual_agent.capability_core import (
    CapabilityBinding,
    CapabilityHostRequest,
    CapabilityOutcome,
)
from codexia_manual_agent.execution import (
    ProcessExecutionObservation,
    ProcessTerminationReason,
    prepare_process_proposal,
)
from codexia_manual_agent.standalone_host.process_attempt import (
    PROCESS_ATTEMPT_ADAPTER,
    SqliteStandaloneProcessAttemptStore,
    StandaloneProcessAttemptSnapshot,
    StandaloneProcessAttemptState,
)
from codexia_manual_agent.standalone_host.process_attempt_runner import (
    launch_process_attempt_runner,
)
from codexia_manual_agent.standalone_host.process_capability import (
    PROCESS_CAPABILITY_ID,
    PROCESS_CAPABILITY_VERSION,
    STANDALONE_PROCESS_HOST_ID,
    StandaloneProcessHostError,
    translate_standalone_process_request,
)

_MAX_OUTCOME_STREAM_TEXT_CHARS = 32_768
_MAX_OUTCOME_ERROR_CHARS = 16_000


def _bounded_text(value: str, limit: int) -> str:
    return value[:limit] if len(value) > limit else value


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


def _observation_payload(
    snapshot: StandaloneProcessAttemptSnapshot,
    process: ProcessExecutionObservation,
) -> dict[str, Any]:
    receipt = snapshot.receipt
    proposal = snapshot.proposal
    return {
        "adapter": PROCESS_ATTEMPT_ADAPTER,
        "attempt": {
            "attempt_id": snapshot.attempt_id,
            "attempt_digest": snapshot.attempt_digest,
            "handoff_id": snapshot.handoff_id,
            "handoff_digest": snapshot.handoff_digest,
        },
        "proposal": {
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
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


class DurableStandaloneProcessCapabilityPort:
    """Durable process host bound one-to-one to CapabilityHandoff.

    Preparation persists the exact proposal and authorization receipt before an
    independent runner is launched. The runner owns one-shot receipt consumption
    and terminal process observation.

    Reconciliation never creates a new attempt. An unconsumed exact attempt may
    relaunch the same runner identity; durable receipt consumption ensures at
    most one runner can cross the external-effect boundary.
    """

    host_id = STANDALONE_PROCESS_HOST_ID

    def __init__(
        self,
        *,
        workspace: str | Path,
        binding: CapabilityBinding,
        approved: bool,
        attempt_store: SqliteStandaloneProcessAttemptStore,
        actor: str = "local-human",
        reason: str | None = None,
    ) -> None:
        if not isinstance(binding, CapabilityBinding):
            raise TypeError("binding must be CapabilityBinding")
        if (
            binding.capability_id != PROCESS_CAPABILITY_ID
            or binding.version != PROCESS_CAPABILITY_VERSION
        ):
            raise StandaloneProcessHostError(
                "Durable process host requires process capability 1.0.0"
            )
        if type(approved) is not bool:
            raise TypeError("approved must be an explicit boolean")
        if not isinstance(attempt_store, SqliteStandaloneProcessAttemptStore):
            raise TypeError(
                "attempt_store must be SqliteStandaloneProcessAttemptStore"
            )
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("actor must be non-empty text")
        if reason is not None and not isinstance(reason, str):
            raise TypeError("reason must be text or None")

        self._workspace = Path(workspace)
        self._binding = binding
        self._approved = approved
        self._attempt_store = attempt_store
        self._actor = actor
        self._reason = reason

    def submit(
        self,
        request: CapabilityHostRequest,
    ) -> CapabilityOutcome | None:
        if not isinstance(request, CapabilityHostRequest):
            raise TypeError("request must be CapabilityHostRequest")
        if request.handoff.host_id != self.host_id:
            raise StandaloneProcessHostError(
                "CapabilityHandoff is routed to another host"
            )

        argv, cwd, limits = translate_standalone_process_request(
            request,
            self._binding,
        )
        proposal = prepare_process_proposal(
            workspace=self._workspace,
            argv=argv,
            cwd=cwd,
            limits=limits,
            summary=f"Gen2 CapabilityNeed {request.need.need.need_id}",
        )
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            proposal,
            mode=ApprovalMode.RISKY,
            approved=self._approved,
            actor=self._actor,
            reason=self._reason,
        )
        snapshot = self._attempt_store.prepare(
            request,
            proposal=proposal,
            receipt=receipt,
        )
        if snapshot.state is not StandaloneProcessAttemptState.AUTHORIZED_UNCONSUMED:
            return self._outcome(request, snapshot)

        runner = launch_process_attempt_runner(
            journal_path=self._attempt_store.path,
            attempt_id=snapshot.attempt_id,
        )
        try:
            runner.wait(timeout=limits.timeout_seconds + 10.0)
        except subprocess.TimeoutExpired:
            return None
        return self.reconcile(request)

    def reconcile(
        self,
        request: CapabilityHostRequest,
        *,
        resume_unconsumed: bool = False,
    ) -> CapabilityOutcome | None:
        if not isinstance(request, CapabilityHostRequest):
            raise TypeError("request must be CapabilityHostRequest")
        snapshot = self._attempt_store.recover_for_handoff(
            request.handoff.handoff_id
        )
        if snapshot is None:
            return None
        self._validate_request_binding(request, snapshot)

        if (
            snapshot.state
            is StandaloneProcessAttemptState.AUTHORIZED_UNCONSUMED
        ):
            if resume_unconsumed:
                launch_process_attempt_runner(
                    journal_path=self._attempt_store.path,
                    attempt_id=snapshot.attempt_id,
                )
            return None

        if snapshot.state is StandaloneProcessAttemptState.AUTHORITY_CONSUMED:
            return None

        return self._outcome(request, snapshot)

    def _validate_request_binding(
        self,
        request: CapabilityHostRequest,
        snapshot: StandaloneProcessAttemptSnapshot,
    ) -> None:
        handoff = request.handoff
        need = request.need.need
        if (
            snapshot.handoff_id != handoff.handoff_id
            or snapshot.handoff_digest != handoff.handoff_digest
            or snapshot.need_id != need.need_id
            or snapshot.need_digest != need.need_digest
            or snapshot.work_id != need.work_id
            or snapshot.work_digest != need.work_digest
        ):
            raise StandaloneProcessHostError(
                "Durable process attempt changed CapabilityHandoff binding"
            )

    @staticmethod
    def _outcome(
        request: CapabilityHostRequest,
        snapshot: StandaloneProcessAttemptSnapshot,
    ) -> CapabilityOutcome | None:
        state = snapshot.state
        if state is StandaloneProcessAttemptState.DENIED:
            return CapabilityOutcome.failed(
                request.need,
                attempt_id=snapshot.attempt_id,
                attempt_digest=snapshot.attempt_digest,
                error=(
                    snapshot.receipt.reason
                    or "Local policy denied process execution"
                ),
                observation={
                    "adapter": PROCESS_ATTEMPT_ADAPTER,
                    "stage": "authorization_denied",
                    "receipt_id": snapshot.receipt.receipt_id,
                    "receipt_digest": snapshot.receipt.receipt_digest,
                },
            )

        if state is StandaloneProcessAttemptState.REJECTED_BEFORE_CONSUME:
            assert snapshot.runner_error is not None
            return CapabilityOutcome.failed(
                request.need,
                attempt_id=snapshot.attempt_id,
                attempt_digest=snapshot.attempt_digest,
                error=_bounded_text(
                    str(snapshot.runner_error["detail"]),
                    _MAX_OUTCOME_ERROR_CHARS,
                ),
                observation={
                    "adapter": PROCESS_ATTEMPT_ADAPTER,
                    "stage": "runner_rejected_before_effect",
                    "runner_error": dict(snapshot.runner_error),
                },
            )

        if state is StandaloneProcessAttemptState.ERROR_AFTER_CONSUME:
            assert snapshot.runner_error is not None
            return CapabilityOutcome.unknown(
                request.need,
                attempt_id=snapshot.attempt_id,
                attempt_digest=snapshot.attempt_digest,
                detail=_bounded_text(
                    str(snapshot.runner_error["detail"]),
                    _MAX_OUTCOME_ERROR_CHARS,
                ),
                observation={
                    "adapter": PROCESS_ATTEMPT_ADAPTER,
                    "stage": "runner_error_after_authority_consumption",
                    "runner_error": dict(snapshot.runner_error),
                },
            )

        if state is not StandaloneProcessAttemptState.OBSERVED:
            return None
        process = snapshot.observation
        assert process is not None
        observation = _observation_payload(snapshot, process)

        if (
            process.started
            and process.termination_reason is ProcessTerminationReason.EXITED
            and process.exit_code == 0
        ):
            return CapabilityOutcome.succeeded(
                request.need,
                attempt_id=snapshot.attempt_id,
                attempt_digest=snapshot.attempt_digest,
                observation=observation,
            )

        return CapabilityOutcome.failed(
            request.need,
            attempt_id=snapshot.attempt_id,
            attempt_digest=snapshot.attempt_digest,
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
