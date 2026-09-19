from __future__ import annotations

import hashlib

import pytest

from codexia_manual_agent.capability_core import (
    CAPABILITY_HANDOFF_ADMITTED_EVENT,
    CapabilityAdmission,
    CapabilityBinding,
    CapabilityHandoff,
    CapabilityHandoffProjectionError,
    CapabilityHostBindingError,
    CapabilityHostBridge,
    CapabilityHostBridgeError,
    CapabilityHostPortError,
    CapabilityHostRequest,
    CapabilityHostRoutingConflictError,
    CapabilityNeed,
    CapabilityNeedSnapshot,
    CapabilityNeedState,
    CapabilityOutcome,
    project_capability_handoff,
    project_capability_handoffs,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkEvent,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)

PARAMETERS = {
    "argv": ["python", "-V"],
    "cwd_ref": "workspace",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work(*, source_id: str = "g2.5-test") -> Work:
    return Work.create(
        objective="Exercise dual-host capability bridge",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha("payload"),
        ),
    )


def _workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:g2.5-test",
        version="1.0.0",
        definition_digest=_sha("workflow-definition"),
    )


def _capability_binding() -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id="process",
        version="1.0.0",
        contract_digest=_sha("process-contract-v1"),
    )


def _pending_need(store: SqliteWorkStore, *, source_id: str = "g2.5-test"):
    initial = store.create(_work(source_id=source_id))
    workflow_run = WorkflowRun.create(
        snapshot=initial,
        binding=_workflow_binding(),
    )
    workflow = WorkflowAdmission(store).admit_start(workflow_run)
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow_run.work_id),
        binding=_capability_binding(),
        operation="run",
        parameters=PARAMETERS,
    )
    pending = CapabilityAdmission(store).admit_need(need)
    return workflow_run, need, pending


class RecordingSuccessHost:
    def __init__(self, host_id: str, *, before_return=None) -> None:
        self._host_id = host_id
        self.calls = 0
        self.requests: list[CapabilityHostRequest] = []
        self._before_return = before_return

    @property
    def host_id(self) -> str:
        return self._host_id

    def submit(self, request: CapabilityHostRequest) -> CapabilityOutcome:
        self.calls += 1
        self.requests.append(request)
        if self._before_return is not None:
            self._before_return(request)
        return CapabilityOutcome.succeeded(
            request.need,
            attempt_id=f"{self.host_id}:attempt-1",
            attempt_digest=_sha(f"{self.host_id}:attempt-1"),
            observation={"host": self.host_id},
        )


class AsyncHost:
    def __init__(self, host_id: str) -> None:
        self._host_id = host_id
        self.calls = 0
        self.request: CapabilityHostRequest | None = None

    @property
    def host_id(self) -> str:
        return self._host_id

    def submit(self, request: CapabilityHostRequest) -> None:
        self.calls += 1
        self.request = request
        return None


class RaisingHost:
    host_id = "standalone.raise"

    def __init__(self) -> None:
        self.calls = 0

    def submit(self, request: CapabilityHostRequest):
        self.calls += 1
        raise RuntimeError("host transport failed")


class InvalidReturnHost:
    host_id = "standalone.invalid-return"

    def submit(self, request: CapabilityHostRequest):
        return "not-an-outcome"


@pytest.mark.parametrize("host_id", ["standalone.local", "hde.irr"])
def test_same_core_contract_supports_standalone_and_hde_hosts(
    tmp_path,
    host_id: str,
) -> None:
    store = SqliteWorkStore(tmp_path / f"{host_id}.sqlite")
    _, _, pending = _pending_need(store, source_id=host_id)
    host = RecordingSuccessHost(host_id)

    resolved = CapabilityHostBridge(store).dispatch_once(pending, host)

    assert host.calls == 1
    assert resolved.state is CapabilityNeedState.SUCCEEDED
    assert resolved.outcome is not None
    assert resolved.outcome.observation["host"] == host_id
    handoff = project_capability_handoff(
        store.events(pending.need.work_id),
        pending.need.need_id,
    )
    assert handoff.host_id == host_id


def test_host_request_exposes_only_handoff_and_pending_need() -> None:
    assert set(CapabilityHostRequest.__dataclass_fields__) == {"handoff", "need"}


def test_handoff_is_durable_before_host_submit(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)

    def assert_handoff_exists(request: CapabilityHostRequest) -> None:
        projected = project_capability_handoff(
            store.events(request.need.need.work_id),
            request.need.need.need_id,
        )
        assert projected == request.handoff

    host = RecordingSuccessHost(
        "standalone.local",
        before_return=assert_handoff_exists,
    )

    CapabilityHostBridge(store).dispatch_once(pending, host)

    assert host.calls == 1


def test_handoff_is_not_authority_or_attempt_proof(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    host = AsyncHost("standalone.local")

    still_pending = CapabilityHostBridge(store).dispatch_once(pending, host)

    assert still_pending.state is CapabilityNeedState.PENDING
    assert still_pending.outcome is None
    handoff = project_capability_handoff(
        store.events(pending.need.work_id),
        pending.need.need_id,
    )
    assert handoff.host_id == "standalone.local"
    assert "attempt" not in handoff.to_dict()
    assert "authorization" not in handoff.to_dict()


def test_exact_redispatch_same_process_does_not_call_host_twice(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    host = AsyncHost("standalone.local")
    bridge = CapabilityHostBridge(store)

    first = bridge.dispatch_once(pending, host)
    second = bridge.dispatch_once(first, host)

    assert first.state is CapabilityNeedState.PENDING
    assert second == first
    assert host.calls == 1


def test_restart_does_not_redispatch_existing_handoff(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    _, _, pending = _pending_need(store)
    first_host = AsyncHost("standalone.local")
    CapabilityHostBridge(store).dispatch_once(pending, first_host)
    assert first_host.calls == 1

    restarted = SqliteWorkStore(path)
    recovered = CapabilityAdmission(restarted)
    pending_after_restart = recovered.admit_need(pending.need)
    second_host = AsyncHost("standalone.local")

    result = CapabilityHostBridge(restarted).dispatch_once(
        pending_after_restart,
        second_host,
    )

    assert result.state is CapabilityNeedState.PENDING
    assert second_host.calls == 0


def test_same_need_cannot_be_rerouted_to_another_host(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    bridge = CapabilityHostBridge(store)
    bridge.dispatch_once(pending, AsyncHost("standalone.local"))

    with pytest.raises(CapabilityHostRoutingConflictError):
        bridge.dispatch_once(pending, AsyncHost("hde.irr"))


def test_host_exception_leaves_handoff_and_suppresses_retry(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    host = RaisingHost()
    bridge = CapabilityHostBridge(store)

    with pytest.raises(CapabilityHostPortError):
        bridge.dispatch_once(pending, host)

    assert host.calls == 1
    handoff = project_capability_handoff(
        store.events(pending.need.work_id),
        pending.need.need_id,
    )
    assert handoff.host_id == host.host_id

    retry_host = RaisingHost()
    result = bridge.dispatch_once(pending, retry_host)

    assert result.state is CapabilityNeedState.PENDING
    assert retry_host.calls == 0


def test_invalid_host_return_does_not_erase_handoff(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    bridge = CapabilityHostBridge(store)

    with pytest.raises(CapabilityHostPortError):
        bridge.dispatch_once(pending, InvalidReturnHost())

    assert project_capability_handoff(
        store.events(pending.need.work_id),
        pending.need.need_id,
    ).host_id == "standalone.invalid-return"


def test_async_host_can_record_outcome_later(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    host = AsyncHost("hde.irr")
    bridge = CapabilityHostBridge(store)

    bridge.dispatch_once(pending, host)
    assert host.request is not None
    outcome = CapabilityOutcome.succeeded(
        host.request.need,
        attempt_id="hde-attempt-1",
        attempt_digest=_sha("hde-attempt-1"),
        observation={"executor": "runplane"},
    )

    resolved = bridge.record_outcome(outcome, host_id=host.host_id)

    assert resolved.state is CapabilityNeedState.SUCCEEDED
    assert resolved.outcome == outcome
    assert host.calls == 1


def test_async_outcome_requires_existing_handoff(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    outcome = CapabilityOutcome.succeeded(
        pending,
        attempt_id="orphan-attempt",
        attempt_digest=_sha("orphan-attempt"),
    )

    with pytest.raises(CapabilityHostBindingError):
        CapabilityHostBridge(store).record_outcome(
            outcome,
            host_id="standalone.local",
        )


def test_async_outcome_must_match_handoff_host(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    host = AsyncHost("standalone.local")
    bridge = CapabilityHostBridge(store)
    bridge.dispatch_once(pending, host)
    assert host.request is not None
    outcome = CapabilityOutcome.succeeded(
        host.request.need,
        attempt_id="attempt-1",
        attempt_digest=_sha("attempt-1"),
    )

    with pytest.raises(CapabilityHostRoutingConflictError):
        bridge.record_outcome(outcome, host_id="hde.irr")


def test_record_outcome_never_calls_port(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    host = AsyncHost("standalone.local")
    bridge = CapabilityHostBridge(store)
    bridge.dispatch_once(pending, host)
    assert host.request is not None
    before_calls = host.calls

    outcome = CapabilityOutcome.failed(
        host.request.need,
        attempt_id="attempt-failed",
        attempt_digest=_sha("attempt-failed"),
        error="host execution failed",
    )
    result = bridge.record_outcome(outcome, host_id=host.host_id)

    assert result.state is CapabilityNeedState.FAILED
    assert host.calls == before_calls


def test_synchronous_host_response_must_match_dispatched_need(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, first = _pending_need(store, source_id="first")
    _, _, second = _pending_need(store, source_id="second")

    class WrongNeedHost:
        host_id = "standalone.wrong-need"

        def submit(self, request: CapabilityHostRequest) -> CapabilityOutcome:
            return CapabilityOutcome.succeeded(
                second,
                attempt_id="wrong-attempt",
                attempt_digest=_sha("wrong-attempt"),
            )

    with pytest.raises(CapabilityHostBindingError):
        CapabilityHostBridge(store).dispatch_once(first, WrongNeedHost())


def test_terminal_need_is_not_dispatched(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    outcome = CapabilityOutcome.failed(
        pending,
        attempt_id="preexisting-attempt",
        attempt_digest=_sha("preexisting-attempt"),
        error="failed before G2.5 bridge",
    )
    terminal = CapabilityAdmission(store).admit_outcome(outcome)
    host = AsyncHost("standalone.local")

    with pytest.raises(CapabilityHostBridgeError):
        CapabilityHostBridge(store).dispatch_once(terminal, host)

    assert host.calls == 0


def test_projection_rejects_handoff_for_unknown_need(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, first = _pending_need(store, source_id="first")
    _, _, second = _pending_need(store, source_id="second")

    first_snapshot = store.snapshot(first.need.work_id)
    valid_foreign_handoff = CapabilityHandoff.create(
        need=first,
        snapshot=first_snapshot,
        host_id="standalone.local",
    )

    second_snapshot = store.snapshot(second.need.work_id)
    forged_outer_event = WorkEvent.create(
        work_id=second.need.work_id,
        sequence=second_snapshot.revision + 1,
        kind=CAPABILITY_HANDOFF_ADMITTED_EVENT,
        payload={"capability_handoff": valid_foreign_handoff.to_dict()},
        previous_event_digest=second_snapshot.last_event_digest,
        event_id=valid_foreign_handoff.handoff_id,
        created_at=valid_foreign_handoff.created_at,
    )
    store.append(
        second.need.work_id,
        expected_revision=second_snapshot.revision,
        event=forged_outer_event,
    )

    with pytest.raises(CapabilityHandoffProjectionError):
        project_capability_handoffs(store.events(second.need.work_id))


def test_only_one_handoff_event_is_recorded(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    host = AsyncHost("standalone.local")
    bridge = CapabilityHostBridge(store)

    bridge.dispatch_once(pending, host)
    bridge.dispatch_once(pending, host)

    handoff_events = [
        event
        for event in store.events(pending.need.work_id)
        if event.kind == CAPABILITY_HANDOFF_ADMITTED_EVENT
    ]
    assert len(handoff_events) == 1


def test_projection_rejects_handoff_after_terminal_outcome(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _pending_need(store)
    outcome = CapabilityOutcome.failed(
        pending,
        attempt_id="terminal-attempt",
        attempt_digest=_sha("terminal-attempt"),
        error="terminal failure",
    )
    CapabilityAdmission(store).admit_outcome(outcome)

    synthetic_pending = CapabilityNeedSnapshot(
        need=pending.need,
        state=CapabilityNeedState.PENDING,
        outcome=None,
    )
    current = store.snapshot(pending.need.work_id)
    handoff = CapabilityHandoff.create(
        need=synthetic_pending,
        snapshot=current,
        host_id="standalone.local",
    )
    store.append(
        pending.need.work_id,
        expected_revision=current.revision,
        event=handoff.to_event(),
    )

    with pytest.raises(CapabilityHandoffProjectionError):
        project_capability_handoffs(store.events(pending.need.work_id))
