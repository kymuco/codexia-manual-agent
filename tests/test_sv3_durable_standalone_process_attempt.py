from __future__ import annotations

import ast
import hashlib
import importlib.util
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from codexia_manual_agent.authority import (
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.capability_core import (
    CapabilityHostBridge,
    CapabilityHostRequest,
)
from codexia_manual_agent.domain.errors import AuthorizationConsumedError
from codexia_manual_agent.execution import prepare_process_proposal
from codexia_manual_agent.standalone_host import (
    STANDALONE_PROCESS_HOST_ID,
    DurableStandaloneProcessCapabilityPort,
    SqliteStandaloneProcessAttemptStore,
    StandaloneProcessAttemptState,
    StandaloneProcessWorkRecoveryService,
    StandaloneProcessWorkRecoveryState,
    translate_standalone_process_request,
)
from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_runtime import (
    standalone_process_v2_capability_binding,
)


PROVIDER_REF = "codexia:process-pack-provider@8.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _PluginService:
    def __init__(self, plugin) -> None:
        self._plugin = plugin

    def get(self, plugin_id: str):
        if plugin_id != PROVIDER_REF:
            raise KeyError(plugin_id)
        return self._plugin


class _AsyncStandaloneHost:
    host_id = STANDALONE_PROCESS_HOST_ID

    def __init__(self) -> None:
        self.request: CapabilityHostRequest | None = None
        self.calls = 0

    def submit(self, request: CapabilityHostRequest) -> None:
        self.calls += 1
        self.request = request
        return None


def _load_plugin(monkeypatch):
    class StubBasePlugin:
        pass

    invariant_module = types.ModuleType("invariant")
    spec_module = types.ModuleType("invariant.spec")
    spec_module.BasePlugin = StubBasePlugin
    invariant_module.spec = spec_module
    monkeypatch.setitem(sys.modules, "invariant", invariant_module)
    monkeypatch.setitem(sys.modules, "invariant.spec", spec_module)

    root = Path(__file__).resolve().parents[1]
    plugin_path = root / "examples" / "invariant_process_pack_v2" / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "sv3_process_pack_v2",
        plugin_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ProcessPackProviderV2()


def _service(
    work_path: Path,
    plugin,
    workspace: Path,
    *,
    attempts: SqliteStandaloneProcessAttemptStore | None = None,
):
    return StandaloneProcessWorkRecoveryService(
        store=SqliteWorkStore(work_path),
        plugin_service=_PluginService(plugin),
        provider_ref=PROVIDER_REF,
        workspace=workspace,
        process_attempt_store=attempts,
    )


def _kwargs(*, source_id: str) -> dict[str, object]:
    return {
        "objective": "Run the durable standalone process attempt vertical.",
        "source_namespace": "standalone.api",
        "source_id": source_id,
        "payload_digest": _sha(f"payload:{source_id}"),
        "approved": True,
        "actor": "sv3-test-human",
        "reason": "prove durable process-attempt recovery",
    }


def _pending_handoff(
    *,
    work_path: Path,
    plugin,
    workspace: Path,
    source_id: str,
):
    service = _service(work_path, plugin, workspace)
    checkpoint = None
    for _ in range(4):
        checkpoint = service.advance_once(**_kwargs(source_id=source_id))
    assert checkpoint is not None
    assert (
        checkpoint.state
        is StandaloneProcessWorkRecoveryState.DISPATCH_CAPABILITY
    )
    assert checkpoint.capability is not None

    store = SqliteWorkStore(work_path)
    host = _AsyncStandaloneHost()
    CapabilityHostBridge(store).dispatch_once(
        checkpoint.capability,
        host,
    )
    assert host.calls == 1
    assert host.request is not None

    recovered = service.recover(checkpoint.work_id)
    assert (
        recovered.state
        is StandaloneProcessWorkRecoveryState.AWAITING_OUTCOME_RECONCILIATION
    )
    assert recovered.handoff is not None
    assert recovered.capability is not None
    return recovered, host.request


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
        summary=f"Gen2 CapabilityNeed {request.need.need.need_id}",
    )
    receipt = LocalApprovalAuthority().decide(
        proposal,
        mode=ApprovalMode.RISKY,
        approved=True,
        actor="sv3-test-human",
        reason="prepare exact durable attempt without launching runner",
    )
    return attempts.prepare(
        request,
        proposal=proposal,
        receipt=receipt,
    )


def _wait_for_attempt(
    attempts: SqliteStandaloneProcessAttemptStore,
    attempt_id: str,
    *,
    states: set[StandaloneProcessAttemptState],
    timeout: float = 20.0,
):
    deadline = time.monotonic() + timeout
    while True:
        snapshot = attempts.recover(attempt_id)
        if snapshot.state in states:
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"attempt stayed {snapshot.state.value} beyond timeout"
            )
        time.sleep(0.05)


def test_sv3_full_vertical_uses_durable_observed_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    work_path = tmp_path / "work.sqlite"
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "attempts.sqlite"
    )
    service = _service(
        work_path,
        plugin,
        tmp_path,
        attempts=attempts,
    )
    kwargs = _kwargs(source_id="sv3-full")

    checkpoint = None
    for _ in range(8):
        checkpoint = service.advance_once(**kwargs)
    assert checkpoint is not None
    assert checkpoint.state is StandaloneProcessWorkRecoveryState.COMPLETED
    assert checkpoint.handoff is not None
    assert checkpoint.capability is not None
    assert checkpoint.capability.outcome is not None

    attempt = attempts.recover_for_handoff(checkpoint.handoff.handoff_id)
    assert attempt is not None
    assert attempt.state is StandaloneProcessAttemptState.OBSERVED
    assert attempt.authority_consumed is True
    assert attempt.observation is not None
    assert (
        checkpoint.capability.outcome.attempt_id
        == attempt.attempt_id
    )
    assert (
        checkpoint.capability.outcome.attempt_digest
        == attempt.attempt_digest
    )

    kinds = tuple(
        event.kind
        for event in SqliteWorkStore(work_path).events(checkpoint.work_id)
    )
    assert kinds.count("capability.handoff-admitted") == 1
    assert kinds.count("capability.outcome-recorded") == 1
    assert kinds.count("work.completed") == 1


def test_sv3_missing_attempt_after_handoff_can_resume_same_handoff(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    work_path = tmp_path / "missing-attempt-work.sqlite"
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "missing-attempts.sqlite"
    )
    checkpoint, _ = _pending_handoff(
        work_path=work_path,
        plugin=plugin,
        workspace=tmp_path,
        source_id="sv3-missing-attempt",
    )

    before = SqliteWorkStore(work_path).events(checkpoint.work_id)
    resumed = _service(
        work_path,
        plugin,
        tmp_path,
        attempts=attempts,
    ).advance_once(**_kwargs(source_id="sv3-missing-attempt"))

    assert resumed.handoff is not None
    assert resumed.handoff.handoff_id == checkpoint.handoff.handoff_id
    attempt = attempts.recover_for_handoff(checkpoint.handoff.handoff_id)
    assert attempt is not None

    after = SqliteWorkStore(work_path).events(checkpoint.work_id)
    assert sum(
        event.kind == "capability.handoff-admitted"
        for event in after
    ) == 1
    assert len(after) >= len(before)


def test_sv3_prepared_unconsumed_attempt_relaunches_same_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    work_path = tmp_path / "prepared-work.sqlite"
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "prepared-attempts.sqlite"
    )
    checkpoint, request = _pending_handoff(
        work_path=work_path,
        plugin=plugin,
        workspace=tmp_path,
        source_id="sv3-prepared",
    )
    prepared = _prepare_attempt(
        attempts=attempts,
        request=request,
        workspace=tmp_path,
    )
    assert (
        prepared.state
        is StandaloneProcessAttemptState.AUTHORIZED_UNCONSUMED
    )
    receipt_id = prepared.receipt.receipt_id

    service = _service(
        work_path,
        plugin,
        tmp_path,
        attempts=attempts,
    )
    first = service.advance_once(**_kwargs(source_id="sv3-prepared"))
    assert first.work_id == checkpoint.work_id

    terminal = _wait_for_attempt(
        attempts,
        prepared.attempt_id,
        states={StandaloneProcessAttemptState.OBSERVED},
    )
    assert terminal.receipt.receipt_id == receipt_id
    assert terminal.authority_consumed is True

    resumed = service.advance_once(**_kwargs(source_id="sv3-prepared"))
    assert (
        resumed.state
        is StandaloneProcessWorkRecoveryState.RECORD_OUTCOME_EVIDENCE
    )
    assert resumed.capability is not None
    assert resumed.capability.outcome is not None
    assert resumed.capability.outcome.attempt_id == prepared.attempt_id


def test_sv4_consumed_without_live_runner_closes_unknown_without_relaunch(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    work_path = tmp_path / "consumed-work.sqlite"
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "consumed-attempts.sqlite"
    )
    checkpoint, request = _pending_handoff(
        work_path=work_path,
        plugin=plugin,
        workspace=tmp_path,
        source_id="sv3-consumed",
    )
    prepared = _prepare_attempt(
        attempts=attempts,
        request=request,
        workspace=tmp_path,
    )
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

    import codexia_manual_agent.standalone_host.process_attempt_capability as module

    def _forbidden_launch(**_kwargs):
        raise AssertionError(
            "consumed attempt must never launch another runner"
        )

    monkeypatch.setattr(
        module,
        "launch_process_attempt_runner",
        _forbidden_launch,
    )

    before = SqliteWorkStore(work_path).events(checkpoint.work_id)
    resumed = _service(
        work_path,
        plugin,
        tmp_path,
        attempts=attempts,
    ).advance_once(**_kwargs(source_id="sv3-consumed"))
    after = SqliteWorkStore(work_path).events(checkpoint.work_id)

    assert (
        resumed.state
        is StandaloneProcessWorkRecoveryState.CAPABILITY_OUTCOME_UNKNOWN
    )
    assert len(after) == len(before) + 1
    assert after[-1].kind == "capability.outcome-recorded"
    assert sum(
        event.kind == "capability.handoff-admitted"
        for event in after
    ) == 1
    assert "work.completed" not in tuple(event.kind for event in after)
    recovered_attempt = attempts.recover(prepared.attempt_id)
    assert (
        recovered_attempt.state
        is StandaloneProcessAttemptState.ERROR_AFTER_CONSUME
    )
    assert (
        recovered_attempt.runner_error["error_type"]
        == "RunnerOwnershipLostAfterConsumption"
    )


def test_sv3_durable_consumption_has_one_winner(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    work_path = tmp_path / "consume-work.sqlite"
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "consume-attempts.sqlite"
    )
    _, request = _pending_handoff(
        work_path=work_path,
        plugin=plugin,
        workspace=tmp_path,
        source_id="sv3-consume-once",
    )
    prepared = _prepare_attempt(
        attempts=attempts,
        request=request,
        workspace=tmp_path,
    )

    kwargs = {
        "receipt_digest": prepared.receipt.receipt_digest,
        "proposal_id": prepared.proposal.proposal_id,
        "proposal_digest": prepared.proposal.proposal_digest,
    }
    attempts.consume(prepared.receipt.receipt_id, **kwargs)
    with pytest.raises(AuthorizationConsumedError):
        attempts.consume(prepared.receipt.receipt_id, **kwargs)


def test_sv3_runner_survives_launcher_process_exit(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    work_path = tmp_path / "survive-work.sqlite"
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "survive-attempts.sqlite"
    )
    _, request = _pending_handoff(
        work_path=work_path,
        plugin=plugin,
        workspace=tmp_path,
        source_id="sv3-surviving-runner",
    )
    prepared = _prepare_attempt(
        attempts=attempts,
        request=request,
        workspace=tmp_path,
    )

    code = (
        "from codexia_manual_agent.standalone_host "
        "import launch_process_attempt_runner; "
        f"launch_process_attempt_runner("
        f"journal_path={str(attempts.path)!r}, "
        f"attempt_id={prepared.attempt_id!r})"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    observed = _wait_for_attempt(
        attempts,
        prepared.attempt_id,
        states={StandaloneProcessAttemptState.OBSERVED},
    )
    assert observed.observation is not None
    assert observed.observation.started is True
    assert observed.observation.exit_code == 0
    assert observed.authority_consumed is True


def test_sv3_observed_attempt_reconciles_without_runner_replay(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    work_path = tmp_path / "observed-work.sqlite"
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "observed-attempts.sqlite"
    )
    checkpoint, request = _pending_handoff(
        work_path=work_path,
        plugin=plugin,
        workspace=tmp_path,
        source_id="sv3-observed",
    )
    prepared = _prepare_attempt(
        attempts=attempts,
        request=request,
        workspace=tmp_path,
    )

    from codexia_manual_agent.standalone_host import run_process_attempt

    assert (
        run_process_attempt(
            journal_path=attempts.path,
            attempt_id=prepared.attempt_id,
        )
        == 0
    )
    assert (
        attempts.recover(prepared.attempt_id).state
        is StandaloneProcessAttemptState.OBSERVED
    )

    import codexia_manual_agent.standalone_host.process_attempt_capability as module

    def _forbidden_launch(**_kwargs):
        raise AssertionError(
            "observed attempt must reconcile without another runner"
        )

    monkeypatch.setattr(
        module,
        "launch_process_attempt_runner",
        _forbidden_launch,
    )

    resumed = _service(
        work_path,
        plugin,
        tmp_path,
        attempts=attempts,
    ).advance_once(**_kwargs(source_id="sv3-observed"))

    assert (
        resumed.state
        is StandaloneProcessWorkRecoveryState.RECORD_OUTCOME_EVIDENCE
    )
    assert resumed.handoff is not None
    assert resumed.handoff.handoff_id == checkpoint.handoff.handoff_id
    assert resumed.capability is not None
    assert resumed.capability.outcome is not None
    assert resumed.capability.outcome.attempt_id == prepared.attempt_id


def test_sv3_first_durable_authority_identity_wins(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    work_path = tmp_path / "first-authority-work.sqlite"
    attempts = SqliteStandaloneProcessAttemptStore(
        tmp_path / "first-authority-attempts.sqlite"
    )
    _, request = _pending_handoff(
        work_path=work_path,
        plugin=plugin,
        workspace=tmp_path,
        source_id="sv3-first-authority",
    )
    prepared = _prepare_attempt(
        attempts=attempts,
        request=request,
        workspace=tmp_path,
    )

    replacement = LocalApprovalAuthority().decide(
        prepared.proposal,
        mode=ApprovalMode.RISKY,
        approved=False,
        actor="sv3-other-human",
        reason="late caller cannot replace durable allow receipt",
    )
    recovered = attempts.prepare(
        request,
        proposal=prepared.proposal,
        receipt=replacement,
    )

    assert recovered.receipt == prepared.receipt
    assert recovered.receipt != replacement
    assert (
        recovered.state
        is StandaloneProcessAttemptState.AUTHORIZED_UNCONSUMED
    )


def test_sv3_production_code_does_not_import_session_or_lab_semantics() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "standalone_host" / "process_attempt.py",
        root / "standalone_host" / "process_attempt_capability.py",
        root / "standalone_host" / "process_attempt_runner.py",
    ]

    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any("session_events" in module for module in modules)
        assert not any(
            module == "codexia_manual_agent.lab"
            or module.startswith("codexia_manual_agent.lab.")
            for module in modules
        )
        assert "ExperimentRun" not in source
