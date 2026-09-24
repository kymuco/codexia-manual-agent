from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

import pytest

from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityBinding,
    CapabilityHostBridge,
    CapabilityHostPortError,
    CapabilityHostRequest,
    CapabilityHostRoutingConflictError,
    CapabilityNeed,
    CapabilityNeedState,
    CapabilityOutcome,
    project_capability_handoff,
)
from codexia_manual_agent.execution import ProcessLimits
from codexia_manual_agent.standalone_host import (
    StandaloneProcessCapabilityPort,
)
from codexia_manual_agent.work_core import SqliteWorkStore, Work, WorkIngressBinding
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    CapabilityProgressionService,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding() -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id="process",
        version="1.0.0",
        contract_digest=_sha("g2.20-process-contract"),
    )


def _pending_need(
    store: SqliteWorkStore,
    *,
    source_id: str,
    parameters: dict[str, object] | None = None,
):
    work = Work.create(
        objective="Exercise bounded Capability progression",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    workflow_run = WorkflowRun.create(
        snapshot=initial,
        binding=WorkflowBinding.create(
            workflow_id="codexia:g2.20-workflow",
            version="1.0.0",
            definition_digest=_sha("g2.20-workflow"),
        ),
    )
    workflow = WorkflowAdmission(store).admit_start(workflow_run)
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(work.work_id),
        binding=_binding(),
        operation="run",
        parameters=parameters or {"probe": source_id},
    )
    pending = CapabilityAdmission(store).admit_need(need)
    return work, pending


class SuccessHost:
    def __init__(self, host_id: str = "g2.20.success") -> None:
        self._host_id = host_id
        self.calls = 0

    @property
    def host_id(self) -> str:
        return self._host_id

    def submit(self, request: CapabilityHostRequest) -> CapabilityOutcome:
        self.calls += 1
        return CapabilityOutcome.succeeded(
            request.need,
            attempt_id=f"{self.host_id}:attempt",
            attempt_digest=_sha(f"{self.host_id}:attempt"),
            observation={"host": self.host_id},
        )


class AsyncHost:
    def __init__(self, host_id: str = "g2.20.async") -> None:
        self._host_id = host_id
        self.calls = 0

    @property
    def host_id(self) -> str:
        return self._host_id

    def submit(self, _request: CapabilityHostRequest) -> None:
        self.calls += 1
        return None


class RaisingHost:
    host_id = "g2.20.raise"

    def __init__(self) -> None:
        self.calls = 0

    def submit(self, _request: CapabilityHostRequest):
        self.calls += 1
        raise RuntimeError("ambiguous host transport")


def test_pending_need_progresses_through_selected_host(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "success.sqlite")
    work, pending = _pending_need(store, source_id="success")
    host = SuccessHost()

    resolved = CapabilityProgressionService(store).progress_once(
        work_id=work.work_id,
        need_id=pending.need.need_id,
        port=host,
    )

    assert resolved.state is CapabilityNeedState.SUCCEEDED
    assert host.calls == 1
    assert resolved.outcome is not None
    assert resolved.outcome.observation["host"] == host.host_id


def test_terminal_need_is_exact_noop_and_never_calls_host(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "terminal.sqlite")
    work, pending = _pending_need(store, source_id="terminal")
    outcome = CapabilityOutcome.failed(
        pending,
        attempt_id="preexisting",
        attempt_digest=_sha("preexisting"),
        error="already terminal",
    )
    terminal = CapabilityAdmission(store).admit_outcome(outcome)
    host = SuccessHost()
    before = store.events(work.work_id)

    recovered = CapabilityProgressionService(store).progress_once(
        work_id=work.work_id,
        need_id=pending.need.need_id,
        port=host,
    )

    assert recovered == terminal
    assert host.calls == 0
    assert store.events(work.work_id) == before


def test_host_exception_persists_handoff_and_retry_never_redispatches(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "raise.sqlite")
    work, pending = _pending_need(store, source_id="raise")
    first = RaisingHost()
    service = CapabilityProgressionService(store)

    with pytest.raises(CapabilityHostPortError):
        service.progress_once(
            work_id=work.work_id,
            need_id=pending.need.need_id,
            port=first,
        )

    assert first.calls == 1
    assert project_capability_handoff(
        store.events(work.work_id),
        pending.need.need_id,
    ).host_id == first.host_id

    retry = RaisingHost()
    recovered = service.progress_once(
        work_id=work.work_id,
        need_id=pending.need.need_id,
        port=retry,
    )

    assert recovered.state is CapabilityNeedState.PENDING
    assert retry.calls == 0


def test_restart_after_async_handoff_never_redispatches(tmp_path) -> None:
    path = tmp_path / "restart.sqlite"
    store = SqliteWorkStore(path)
    work, pending = _pending_need(store, source_id="restart")
    first = AsyncHost()

    unresolved = CapabilityProgressionService(store).progress_once(
        work_id=work.work_id,
        need_id=pending.need.need_id,
        port=first,
    )
    assert unresolved.state is CapabilityNeedState.PENDING
    assert first.calls == 1

    restarted = SqliteWorkStore(path)
    second = AsyncHost()
    recovered = CapabilityProgressionService(restarted).progress_once(
        work_id=work.work_id,
        need_id=pending.need.need_id,
        port=second,
    )

    assert recovered.state is CapabilityNeedState.PENDING
    assert second.calls == 0


def test_existing_handoff_to_other_host_preserves_routing_conflict(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "routing.sqlite")
    work, pending = _pending_need(store, source_id="routing")
    first = AsyncHost("g2.20.host-a")
    CapabilityProgressionService(store).progress_once(
        work_id=work.work_id,
        need_id=pending.need.need_id,
        port=first,
    )
    other = AsyncHost("g2.20.host-b")

    with pytest.raises(CapabilityHostRoutingConflictError):
        CapabilityProgressionService(store).progress_once(
            work_id=work.work_id,
            need_id=pending.need.need_id,
            port=other,
        )

    assert other.calls == 0


def test_real_standalone_process_port_uses_existing_authority_execution(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "process.sqlite")
    parameters = {
        "argv": [sys.executable, "-V"],
        "cwd_ref": "workspace",
        "cwd": ".",
        "limits": ProcessLimits().to_dict(),
    }
    work, pending = _pending_need(
        store,
        source_id="process",
        parameters=parameters,
    )
    port = StandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=pending.need.binding,
        approved=True,
        actor="g2.20-test-human",
    )

    resolved = CapabilityProgressionService(store).progress_once(
        work_id=work.work_id,
        need_id=pending.need.need_id,
        port=port,
    )

    assert resolved.state is CapabilityNeedState.SUCCEEDED
    assert resolved.outcome is not None
    assert resolved.outcome.observation["authorization"]["source"] == "human"
    assert resolved.outcome.observation["execution"]["started"] is True
    assert resolved.outcome.observation["execution"]["exit_code"] == 0


def test_async_outcome_remains_owned_by_capability_host_bridge(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "async-outcome.sqlite")
    work, pending = _pending_need(store, source_id="async-outcome")
    host = AsyncHost()
    service = CapabilityProgressionService(store)

    service.progress_once(
        work_id=work.work_id,
        need_id=pending.need.need_id,
        port=host,
    )
    handoff = project_capability_handoff(
        store.events(work.work_id),
        pending.need.need_id,
    )
    outcome = CapabilityOutcome.succeeded(
        pending,
        attempt_id="async-attempt",
        attempt_digest=_sha("async-attempt"),
        observation={"handoff_id": handoff.handoff_id},
    )

    resolved = CapabilityHostBridge(store).record_outcome(
        outcome,
        host_id=host.host_id,
    )

    assert resolved.state is CapabilityNeedState.SUCCEEDED
    assert host.calls == 1


def test_progression_source_has_no_loop_standalone_host_or_executor() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "workflow_orchestration" / "capability_progression.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)

    forbidden = {
        "StandaloneProcessCapabilityPort",
        "ProcessExecutor",
        "Scheduler",
        "CognitionPort",
    }
    assert forbidden.isdisjoint(imported_names)
    assert not any(
        module.endswith(
            (".standalone_host", ".authority", ".execution", ".providers")
        )
        for module in imported_modules
    )
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
