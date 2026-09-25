from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

from codexia_manual_agent.authority import ApprovalMode, LocalApprovalAuthority
from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityHostBridge,
    CapabilityHostRequest,
    CapabilityNeed,
    CapabilityNeedState,
    CapabilityOutcomeStatus,
)
from codexia_manual_agent.execution import ProcessLimits, prepare_process_proposal
from codexia_manual_agent.standalone_host import (
    STANDALONE_PROCESS_HOST_ID,
    DurableStandaloneProcessCapabilityPort,
    SqliteStandaloneProcessAttemptStore,
    StandaloneProcessAttemptState,
    StandaloneProcessRunnerOwnership,
    launch_process_attempt_runner,
    process_attempt_runner_is_active,
    translate_standalone_process_request,
)
from codexia_manual_agent.work_core import SqliteWorkStore, Work, WorkIngressBinding
from codexia_manual_agent.workflow_core import WorkflowAdmission, WorkflowRun
from codexia_manual_agent.workflow_runtime import (
    standalone_process_v2_capability_binding,
    standalone_process_v2_workflow_binding,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _CaptureHost:
    host_id = STANDALONE_PROCESS_HOST_ID

    def __init__(self) -> None:
        self.request: CapabilityHostRequest | None = None

    def submit(self, request: CapabilityHostRequest) -> None:
        self.request = request
        return None


def _parameters(
    *,
    argv: list[str] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    return {
        "argv": argv or [sys.executable, "-V"],
        "cwd_ref": "workspace",
        "cwd": ".",
        "limits": ProcessLimits(
            timeout_seconds=timeout_seconds,
        ).to_dict(),
    }


def _pending_request(
    tmp_path: Path,
    *,
    source_id: str,
    parameters: dict[str, object] | None = None,
):
    store = SqliteWorkStore(tmp_path / f"{source_id}-work.sqlite")
    work = Work.create(
        objective="Exercise SV4 runner ownership reconciliation.",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=initial,
            binding=standalone_process_v2_workflow_binding(),
        )
    )
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(work.work_id),
        binding=standalone_process_v2_capability_binding(),
        operation="run",
        parameters=parameters or _parameters(),
    )
    pending = CapabilityAdmission(store).admit_need(need)

    host = _CaptureHost()
    unresolved = CapabilityHostBridge(store).dispatch_once(
        pending,
        host,
    )
    assert unresolved.state is CapabilityNeedState.PENDING
    assert host.request is not None
    return store, host.request


def _prepare_attempt(
    *,
    attempts: SqliteStandaloneProcessAttemptStore,
    request: CapabilityHostRequest,
    workspace: Path,
):
    binding = standalone_process_v2_capability_binding()
    argv, cwd, limits = translate_standalone_process_request(
        request,
        binding,
    )
    proposal = prepare_process_proposal(
        workspace=workspace,
        argv=argv,
        cwd=cwd,
        limits=limits,
        summary=f"SV4 CapabilityNeed {request.need.need.need_id}",
    )
    receipt = LocalApprovalAuthority().decide(
        proposal,
        mode=ApprovalMode.RISKY,
        approved=True,
        actor="sv4-test-human",
        reason="exercise runner ownership recovery",
    )
    return attempts.prepare(
        request,
        proposal=proposal,
        receipt=receipt,
    )


def _port(
    tmp_path: Path,
    attempts: SqliteStandaloneProcessAttemptStore,
) -> DurableStandaloneProcessCapabilityPort:
    return DurableStandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=standalone_process_v2_capability_binding(),
        approved=True,
        attempt_store=attempts,
        actor="sv4-test-human",
        reason="exercise runner ownership recovery",
    )


def _wait_until(
    predicate,
    *,
    timeout: float = 15.0,
    interval: float = 0.05,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        time.sleep(interval)


def test_sv4_runner_ownership_has_one_live_owner(tmp_path) -> None:
    journal = tmp_path / "attempts.sqlite"
    attempt_id = "standalone-process:test-attempt"

    first = StandaloneProcessRunnerOwnership(
        journal_path=journal,
        attempt_id=attempt_id,
    )
    second = StandaloneProcessRunnerOwnership(
        journal_path=journal,
        attempt_id=attempt_id,
    )

    assert first.try_acquire() is True
    assert process_attempt_runner_is_active(
        journal_path=journal,
        attempt_id=attempt_id,
    ) is True
    assert second.try_acquire() is False

    first.release()

    assert process_attempt_runner_is_active(
        journal_path=journal,
        attempt_id=attempt_id,
    ) is False
    assert second.try_acquire() is True
    second.release()


def test_sv4_live_consumed_runner_remains_ambiguous(tmp_path) -> None:
    store, request = _pending_request(
        tmp_path,
        source_id="sv4-live-consumed",
    )
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "live-consumed-attempts.sqlite"
    )
    prepared = _prepare_attempt(
        attempts=attempts,
        request=request,
        workspace=tmp_path,
    )

    ownership = StandaloneProcessRunnerOwnership(
        journal_path=attempts.path,
        attempt_id=prepared.attempt_id,
    )
    assert ownership.try_acquire() is True
    try:
        attempts.consume(
            prepared.receipt.receipt_id,
            receipt_digest=prepared.receipt.receipt_digest,
            proposal_id=prepared.proposal.proposal_id,
            proposal_digest=prepared.proposal.proposal_digest,
        )
        assert (
            attempts.recover(prepared.attempt_id).state
            is StandaloneProcessAttemptState.AUTHORITY_CONSUMED
        )

        outcome = _port(tmp_path, attempts).reconcile(request)

        assert outcome is None
        assert (
            attempts.recover(prepared.attempt_id).state
            is StandaloneProcessAttemptState.AUTHORITY_CONSUMED
        )
        assert (
            store.snapshot(request.need.need.work_id).state.value
            == "active"
        )
    finally:
        ownership.release()

    outcome = _port(tmp_path, attempts).reconcile(request)

    assert outcome is not None
    assert outcome.status is CapabilityOutcomeStatus.UNKNOWN
    assert (
        attempts.recover(prepared.attempt_id).state
        is StandaloneProcessAttemptState.ERROR_AFTER_CONSUME
    )
    assert (
        attempts.recover(prepared.attempt_id).runner_error["error_type"]
        == "RunnerOwnershipLostAfterConsumption"
    )


def test_sv4_active_unconsumed_owner_suppresses_relaunch(
    tmp_path,
    monkeypatch,
) -> None:
    _, request = _pending_request(
        tmp_path,
        source_id="sv4-live-unconsumed",
    )
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "live-unconsumed-attempts.sqlite"
    )
    prepared = _prepare_attempt(
        attempts=attempts,
        request=request,
        workspace=tmp_path,
    )
    ownership = StandaloneProcessRunnerOwnership(
        journal_path=attempts.path,
        attempt_id=prepared.attempt_id,
    )
    assert ownership.try_acquire() is True

    import codexia_manual_agent.standalone_host.process_attempt_capability as module

    def _forbidden_launch(**_kwargs):
        raise AssertionError("live exact runner ownership must suppress relaunch")

    monkeypatch.setattr(
        module,
        "launch_process_attempt_runner",
        _forbidden_launch,
    )
    try:
        outcome = _port(tmp_path, attempts).reconcile(
            request,
            resume_unconsumed=True,
        )
    finally:
        ownership.release()

    assert outcome is None
    assert (
        attempts.recover(prepared.attempt_id).state
        is StandaloneProcessAttemptState.AUTHORIZED_UNCONSUMED
    )


def test_sv4_hard_killed_runner_reconciles_to_unknown(
    tmp_path,
) -> None:
    store, request = _pending_request(
        tmp_path,
        source_id="sv4-hard-kill",
        parameters=_parameters(
            argv=[
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ],
            timeout_seconds=30.0,
        ),
    )
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "hard-kill-attempts.sqlite"
    )
    prepared = _prepare_attempt(
        attempts=attempts,
        request=request,
        workspace=tmp_path,
    )

    runner = launch_process_attempt_runner(
        journal_path=attempts.path,
        attempt_id=prepared.attempt_id,
    )
    try:
        _wait_until(
            lambda: (
                attempts.recover(prepared.attempt_id).state
                is StandaloneProcessAttemptState.AUTHORITY_CONSUMED
                and process_attempt_runner_is_active(
                    journal_path=attempts.path,
                    attempt_id=prepared.attempt_id,
                )
            ),
            timeout=20.0,
        )

        runner.kill()
        runner.wait(timeout=10.0)

        _wait_until(
            lambda: not process_attempt_runner_is_active(
                journal_path=attempts.path,
                attempt_id=prepared.attempt_id,
            ),
            timeout=10.0,
        )

        outcome = _port(tmp_path, attempts).reconcile(request)

        assert outcome is not None
        assert outcome.status is CapabilityOutcomeStatus.UNKNOWN
        assert (
            outcome.observation["stage"]
            == "runner_error_after_authority_consumption"
        )

        resolved = CapabilityHostBridge(store).record_outcome(
            outcome,
            host_id=STANDALONE_PROCESS_HOST_ID,
        )
        assert resolved.state is CapabilityNeedState.OUTCOME_UNKNOWN
        assert (
            attempts.recover(prepared.attempt_id).state
            is StandaloneProcessAttemptState.ERROR_AFTER_CONSUME
        )
        assert attempts.recover(prepared.attempt_id).observation is None
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=10.0)
