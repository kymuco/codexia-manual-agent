from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
import types
from pathlib import Path

from codexia_manual_agent.capability_core import (
    CapabilityHostBridge,
    CapabilityHostRequest,
    CapabilityOutcome,
)
from codexia_manual_agent.standalone_host import (
    STANDALONE_PROCESS_HOST_ID,
    StandaloneProcessWorkRecoveryService,
    StandaloneProcessWorkRecoveryState,
)
from codexia_manual_agent.work_core import SqliteWorkStore


PROVIDER_REF = "codexia:process-pack-provider@8.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _PluginService:
    def __init__(self, plugin) -> None:
        self._plugin = plugin
        self.calls = 0

    def get(self, plugin_id: str):
        self.calls += 1
        if plugin_id != PROVIDER_REF:
            raise KeyError(plugin_id)
        return self._plugin


class _UnavailablePluginService:
    def get(self, _plugin_id: str):
        raise AssertionError("pure recovery must not resolve provider code")


class _AsyncStandaloneHost:
    host_id = STANDALONE_PROCESS_HOST_ID

    def __init__(self) -> None:
        self.calls = 0
        self.request: CapabilityHostRequest | None = None

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
        "sv2_process_pack_v2",
        plugin_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ProcessPackProviderV2()


def _service(path: Path, plugin, workspace: Path):
    return StandaloneProcessWorkRecoveryService(
        store=SqliteWorkStore(path),
        plugin_service=_PluginService(plugin),
        provider_ref=PROVIDER_REF,
        workspace=workspace,
    )


def _advance(path: Path, plugin, workspace: Path):
    return _service(path, plugin, workspace).start_or_resume_once(
        objective="Run the restart-safe standalone process vertical.",
        source_namespace="standalone.api",
        source_id="sv2-restart-safe",
        payload_digest=_sha("sv2-restart-safe-payload"),
        approved=True,
        actor="sv2-test-human",
        reason="prove restart-safe host composition",
    )


def test_sv2_restarts_after_every_exposed_durable_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    path = tmp_path / "sv2.sqlite"

    expected = [
        StandaloneProcessWorkRecoveryState.START_WORKFLOW,
        StandaloneProcessWorkRecoveryState.PIN_PACK,
        StandaloneProcessWorkRecoveryState.PROGRESS_CAPABILITY_NEED,
        StandaloneProcessWorkRecoveryState.DISPATCH_CAPABILITY,
        StandaloneProcessWorkRecoveryState.RECORD_OUTCOME_EVIDENCE,
        StandaloneProcessWorkRecoveryState.PROGRESS_COMPLETION_CLAIM,
        StandaloneProcessWorkRecoveryState.FINALIZE_WORK,
        StandaloneProcessWorkRecoveryState.COMPLETED,
    ]

    checkpoints = []
    for state in expected:
        checkpoint = _advance(path, plugin, tmp_path)
        checkpoints.append(checkpoint)
        assert checkpoint.state is state

    work_ids = {checkpoint.work_id for checkpoint in checkpoints}
    assert len(work_ids) == 1

    completed = checkpoints[-1]
    result = _service(
        path,
        plugin,
        tmp_path,
    ).completed_result(completed)
    assert result.snapshot.terminal_event_id == result.completion.completion_id

    store = SqliteWorkStore(path)
    before = store.events(completed.work_id)
    recovered_again = _advance(path, plugin, tmp_path)
    after = store.events(completed.work_id)

    assert recovered_again.state is StandaloneProcessWorkRecoveryState.COMPLETED
    assert recovered_again.work_id == completed.work_id
    assert after == before

    kinds = tuple(event.kind for event in after)
    assert kinds.count("workflow.started") == 1
    assert kinds.count("pack.workflow-bound") == 1
    assert kinds.count("capability.need-declared") == 1
    assert kinds.count("capability.handoff-admitted") == 1
    assert kinds.count("capability.outcome-recorded") == 1
    assert kinds.count("evidence.ref-recorded") == 1
    assert kinds.count("completion.claim-admitted") == 1
    assert kinds.count("work.completed") == 1


def test_sv2_existing_handoff_is_recovered_without_redispatch(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    path = tmp_path / "sv2-handoff.sqlite"

    checkpoint = None
    for _ in range(4):
        checkpoint = _advance(path, plugin, tmp_path)
    assert checkpoint is not None
    assert checkpoint.state is StandaloneProcessWorkRecoveryState.DISPATCH_CAPABILITY
    assert checkpoint.capability is not None

    store = SqliteWorkStore(path)
    async_host = _AsyncStandaloneHost()
    pending = checkpoint.capability
    unresolved = CapabilityHostBridge(store).dispatch_once(
        pending,
        async_host,
    )
    assert async_host.calls == 1
    assert async_host.request is not None
    assert unresolved.state.value == "pending"

    before = store.events(checkpoint.work_id)
    restarted = _advance(path, plugin, tmp_path)
    after = store.events(checkpoint.work_id)

    assert (
        restarted.state
        is StandaloneProcessWorkRecoveryState.AWAITING_OUTCOME_RECONCILIATION
    )
    assert after == before

    request = async_host.request
    outcome = CapabilityOutcome.succeeded(
        request.need,
        attempt_id="sv2-reconciled-attempt",
        attempt_digest=_sha("sv2-reconciled-attempt"),
        observation={"source": "reconciled-without-redispatch"},
    )
    CapabilityHostBridge(store).record_outcome(
        outcome,
        host_id=STANDALONE_PROCESS_HOST_ID,
    )

    resumed = _advance(path, plugin, tmp_path)
    assert (
        resumed.state
        is StandaloneProcessWorkRecoveryState.PROGRESS_COMPLETION_CLAIM
    )

    resumed = _advance(path, plugin, tmp_path)
    assert resumed.state is StandaloneProcessWorkRecoveryState.FINALIZE_WORK
    resumed = _advance(path, plugin, tmp_path)
    assert resumed.state is StandaloneProcessWorkRecoveryState.COMPLETED

    kinds = tuple(event.kind for event in store.events(checkpoint.work_id))
    assert kinds.count("capability.handoff-admitted") == 1
    assert kinds.count("capability.outcome-recorded") == 1


def test_sv2_pure_recovery_does_not_resolve_provider(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    path = tmp_path / "sv2-offline-recovery.sqlite"

    checkpoint = None
    for _ in range(8):
        checkpoint = _advance(path, plugin, tmp_path)
    assert checkpoint is not None
    assert checkpoint.state is StandaloneProcessWorkRecoveryState.COMPLETED

    recovered = StandaloneProcessWorkRecoveryService(
        store=SqliteWorkStore(path),
        plugin_service=_UnavailablePluginService(),
        provider_ref=PROVIDER_REF,
        workspace=tmp_path,
    ).recover(checkpoint.work_id)

    assert recovered.state is StandaloneProcessWorkRecoveryState.COMPLETED
    assert recovered.completion == checkpoint.completion


def test_sv2_failed_capability_is_stable_and_does_not_retry(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    path = tmp_path / "sv2-denied.sqlite"

    kwargs = {
        "objective": "Keep denied process Work durably incomplete.",
        "source_namespace": "standalone.api",
        "source_id": "sv2-denied",
        "payload_digest": _sha("sv2-denied-payload"),
        "approved": False,
        "actor": "sv2-test-human",
        "reason": "deny execution",
    }

    checkpoint = None
    for _ in range(5):
        checkpoint = _service(
            path,
            plugin,
            tmp_path,
        ).advance_once(**kwargs)
    assert checkpoint is not None
    assert checkpoint.state is StandaloneProcessWorkRecoveryState.CAPABILITY_FAILED

    store = SqliteWorkStore(path)
    before = store.events(checkpoint.work_id)
    again = _service(path, plugin, tmp_path).advance_once(**kwargs)
    after = store.events(checkpoint.work_id)

    assert again.state is StandaloneProcessWorkRecoveryState.CAPABILITY_FAILED
    assert after == before
    assert "work.completed" not in tuple(event.kind for event in after)


def test_sv2_unknown_outcome_is_stable_and_never_redispatched(
    tmp_path,
    monkeypatch,
) -> None:
    plugin = _load_plugin(monkeypatch)
    path = tmp_path / "sv2-unknown.sqlite"

    checkpoint = None
    for _ in range(4):
        checkpoint = _advance(path, plugin, tmp_path)
    assert checkpoint is not None
    assert checkpoint.state is StandaloneProcessWorkRecoveryState.DISPATCH_CAPABILITY
    assert checkpoint.capability is not None

    store = SqliteWorkStore(path)
    async_host = _AsyncStandaloneHost()
    CapabilityHostBridge(store).dispatch_once(
        checkpoint.capability,
        async_host,
    )
    assert async_host.calls == 1
    assert async_host.request is not None

    request = async_host.request
    outcome = CapabilityOutcome.unknown(
        request.need,
        attempt_id="sv2-unknown-attempt",
        attempt_digest=_sha("sv2-unknown-attempt"),
        detail="external effect state cannot be proven",
        observation={"source": "ambiguous-test-host"},
    )
    CapabilityHostBridge(store).record_outcome(
        outcome,
        host_id=STANDALONE_PROCESS_HOST_ID,
    )

    before = store.events(checkpoint.work_id)
    recovered = _advance(path, plugin, tmp_path)
    after = store.events(checkpoint.work_id)

    assert (
        recovered.state
        is StandaloneProcessWorkRecoveryState.CAPABILITY_OUTCOME_UNKNOWN
    )
    assert after == before
    assert async_host.calls == 1
    assert "work.completed" not in tuple(event.kind for event in after)


def test_sv2_recovery_source_has_no_generic_scheduler_loop() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "standalone_host" / "process_work_recovery.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)

    assert "Scheduler" not in imported_names
    assert "Queue" not in imported_names
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
