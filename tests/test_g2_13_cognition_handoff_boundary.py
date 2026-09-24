from __future__ import annotations

import hashlib

import pytest

from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.role_core import (
    COGNITION_HANDOFF_ADMITTED_EVENT,
    CognitionHandoff,
    CognitionHandoffProjectionError,
    CognitionOutcome,
    CognitionPortRequest,
    CognitionTransportBindingError,
    CognitionTransportBridge,
    CognitionTransportBridgeError,
    CognitionTransportPortError,
    CognitionTransportRoutingConflictError,
    ContextProjection,
    RoleAdmission,
    RoleBinding,
    RoleRun,
    RoleRunState,
    project_cognition_handoff,
    project_cognition_handoffs,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkEvent,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WORKFLOW_COMPLETED_EVENT,
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    RoleCognitionMaterializationService,
)

INSTRUCTIONS = "Review the bounded evidence and return one exact conclusion."
CONTEXT = "artifact=A; evidence=B"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:g2.13-cognition",
        version="1.0.0",
        definition_digest=_sha("g2.13-workflow"),
    )


def _role_binding() -> RoleBinding:
    return RoleBinding.create(
        role_id="codexia:reviewer",
        version="1.0.0",
        instructions_digest=_sha(INSTRUCTIONS),
    )


def _pack(
    workflow: WorkflowBinding,
    role: RoleBinding,
) -> PackBinding:
    return PackBinding.create(
        pack_id="codexia:g2.13-pack",
        version="1.0.0",
        definition_digest=_sha("g2.13-pack"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow.workflow_id,
                version=workflow.version,
                binding_digest=workflow.binding_digest,
            ),
            PackMemberBinding.create(
                kind=PackMemberKind.ROLE,
                semantic_id=role.role_id,
                version=role.version,
                binding_digest=role.binding_digest,
            ),
        ),
    )


class _Instructions:
    def resolve(self, _binding: RoleBinding) -> str:
        return INSTRUCTIONS


class _Context:
    def resolve(self, _projection: ContextProjection) -> str:
        return CONTEXT


def _materializer(store) -> RoleCognitionMaterializationService:
    return RoleCognitionMaterializationService(
        store=store,
        instructions=_Instructions(),
        context=_Context(),
    )


def _requested(
    store: SqliteWorkStore,
    *,
    source_id: str = "g2.13",
):
    workflow_binding = _workflow_binding()
    role_binding = _role_binding()
    work = Work.create(
        objective="Exercise durable cognition handoff",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    workflow_run = WorkflowRun.create(
        snapshot=initial,
        binding=workflow_binding,
    )
    workflow = WorkflowAdmission(store).admit_start(workflow_run)
    PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work_id),
            pack=_pack(workflow_binding, role_binding),
        )
    )
    role_run = RoleRun.create(
        workflow=workflow,
        snapshot=store.snapshot(work.work_id),
        binding=role_binding,
        context=ContextProjection.create(
            content_digest=_sha(CONTEXT),
        ),
    )
    role = RoleAdmission(store).admit_start(role_run)
    materialized = _materializer(store).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )
    requested = RoleAdmission(store).admit_request(materialized.request)
    return work, workflow, requested, materialized.request


class AsyncPort:
    def __init__(self, port_id: str) -> None:
        self._port_id = port_id
        self.calls = 0
        self.request: CognitionPortRequest | None = None

    @property
    def port_id(self) -> str:
        return self._port_id

    def complete(self, request: CognitionPortRequest):
        self.calls += 1
        self.request = request


class SuccessPort:
    def __init__(self, port_id: str, *, before_return=None) -> None:
        self._port_id = port_id
        self.calls = 0
        self.requests: list[CognitionPortRequest] = []
        self._before_return = before_return

    @property
    def port_id(self) -> str:
        return self._port_id

    def complete(self, request: CognitionPortRequest):
        self.calls += 1
        self.requests.append(request)
        if self._before_return is not None:
            self._before_return(request)
        return CognitionOutcome.succeeded(
            request.request,
            output_text="bounded cognition result",
        )


class RaisingPort:
    port_id = "cognition.raise"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, _request: CognitionPortRequest):
        self.calls += 1
        raise RuntimeError("transport became ambiguous")


class InvalidReturnPort:
    port_id = "cognition.invalid"

    def complete(self, _request: CognitionPortRequest):
        return "not-an-outcome"


def test_handoff_is_durable_before_cognition_port_call(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, requested, request = _requested(store)

    def assert_handoff_exists(port_request: CognitionPortRequest) -> None:
        projected = project_cognition_handoff(
            store.events(request.work_id),
            request.request_id,
        )
        assert projected == port_request.handoff

    port = SuccessPort(
        "cognition.local",
        before_return=assert_handoff_exists,
    )

    completed = CognitionTransportBridge(store).dispatch_once(request, port)

    assert completed.state is RoleRunState.COMPLETED
    assert port.calls == 1
    assert requested.state is RoleRunState.REQUESTED


def test_handoff_is_routing_not_attempt_or_response_proof(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    port = AsyncPort("cognition.local")

    pending = CognitionTransportBridge(store).dispatch_once(request, port)

    assert pending.state is RoleRunState.REQUESTED
    handoff = project_cognition_handoff(
        store.events(request.work_id),
        request.request_id,
    )
    record = handoff.to_dict()
    assert handoff.port_id == "cognition.local"
    assert "attempt" not in record
    assert "response" not in record
    assert "retry" not in record


def test_cognition_port_request_exposes_only_handoff_and_request() -> None:
    assert set(CognitionPortRequest.__dataclass_fields__) == {
        "handoff",
        "request",
    }


def test_success_outcome_is_rebound_after_handoff_and_admitted(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    port = SuccessPort("cognition.local")

    completed = CognitionTransportBridge(store).dispatch_once(request, port)

    assert completed.state is RoleRunState.COMPLETED
    assert completed.output_text == "bounded cognition result"
    handoff = project_cognition_handoff(
        store.events(request.work_id),
        request.request_id,
    )
    events = store.events(request.work_id)
    handoff_event = next(
        event
        for event in events
        if event.event_id == handoff.handoff_id
    )
    outcome_event = next(
        event
        for event in events
        if event.event_id == completed.terminal_event_id
    )
    assert outcome_event.previous_event_digest == handoff_event.event_digest
    assert outcome_event.sequence == handoff_event.sequence + 1


def test_exact_retry_after_synchronous_completion_does_not_call_port_again(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    port = SuccessPort("cognition.local")
    bridge = CognitionTransportBridge(store)

    first = bridge.dispatch_once(request, port)
    second = bridge.dispatch_once(request, port)

    assert first.state is RoleRunState.COMPLETED
    assert second == first
    assert port.calls == 1


def test_exact_redispatch_does_not_call_same_port_twice(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    port = AsyncPort("cognition.local")
    bridge = CognitionTransportBridge(store)

    first = bridge.dispatch_once(request, port)
    second = bridge.dispatch_once(request, port)

    assert first.state is RoleRunState.REQUESTED
    assert second == first
    assert port.calls == 1


def test_restart_does_not_redispatch_existing_handoff(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    work, _, requested, request = _requested(store)
    first_port = AsyncPort("cognition.local")
    CognitionTransportBridge(store).dispatch_once(request, first_port)
    assert first_port.calls == 1

    restarted = SqliteWorkStore(path)
    rematerialized = _materializer(restarted).rematerialize_request(
        work_id=work.work_id,
        role_run_id=requested.run.role_run_id,
    )
    second_port = AsyncPort("cognition.local")

    recovered = CognitionTransportBridge(restarted).dispatch_once(
        rematerialized.request,
        second_port,
    )

    assert recovered.state is RoleRunState.REQUESTED
    assert second_port.calls == 0


def test_same_request_cannot_be_rerouted_to_another_port(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    bridge = CognitionTransportBridge(store)
    bridge.dispatch_once(request, AsyncPort("cognition.first"))

    with pytest.raises(CognitionTransportRoutingConflictError):
        bridge.dispatch_once(request, AsyncPort("cognition.second"))


def test_port_exception_leaves_handoff_and_suppresses_retry(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    bridge = CognitionTransportBridge(store)
    first = RaisingPort()

    with pytest.raises(CognitionTransportPortError):
        bridge.dispatch_once(request, first)

    assert first.calls == 1
    assert project_cognition_handoff(
        store.events(request.work_id),
        request.request_id,
    ).port_id == first.port_id

    retry = RaisingPort()
    recovered = bridge.dispatch_once(request, retry)

    assert recovered.state is RoleRunState.REQUESTED
    assert retry.calls == 0


def test_invalid_port_return_does_not_erase_handoff(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)

    with pytest.raises(CognitionTransportPortError):
        CognitionTransportBridge(store).dispatch_once(
            request,
            InvalidReturnPort(),
        )

    assert project_cognition_handoff(
        store.events(request.work_id),
        request.request_id,
    ).port_id == InvalidReturnPort.port_id


def test_async_outcome_can_be_recorded_later_without_redispatch(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    port = AsyncPort("cognition.async")
    bridge = CognitionTransportBridge(store)

    pending = bridge.dispatch_once(request, port)
    assert pending.state is RoleRunState.REQUESTED
    outcome = CognitionOutcome.succeeded(
        request,
        output_text="later result",
    )
    before_calls = port.calls

    completed = bridge.record_outcome(
        outcome,
        port_id=port.port_id,
    )

    assert completed.state is RoleRunState.COMPLETED
    assert completed.output_text == "later result"
    assert port.calls == before_calls


def test_async_outcome_requires_existing_handoff(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    outcome = CognitionOutcome.succeeded(
        request,
        output_text="orphan",
    )

    with pytest.raises(CognitionTransportBindingError):
        CognitionTransportBridge(store).record_outcome(
            outcome,
            port_id="cognition.local",
        )


def test_async_outcome_must_match_handoff_port(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    bridge = CognitionTransportBridge(store)
    bridge.dispatch_once(request, AsyncPort("cognition.first"))
    outcome = CognitionOutcome.succeeded(
        request,
        output_text="result",
    )

    with pytest.raises(CognitionTransportRoutingConflictError):
        bridge.record_outcome(
            outcome,
            port_id="cognition.second",
        )


def test_terminal_role_is_not_dispatched(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    direct = CognitionOutcome.failed(
        request,
        error="failed before transport bridge",
    )
    failed = RoleAdmission(store).admit_outcome(direct)
    port = AsyncPort("cognition.local")

    with pytest.raises(CognitionTransportBridgeError):
        CognitionTransportBridge(store).dispatch_once(request, port)

    assert failed.state is RoleRunState.FAILED
    assert port.calls == 0


def test_new_handoff_requires_active_requesting_workflow(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, workflow, _, request = _requested(store)
    completion = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=store.snapshot(work.work_id),
        event_kind=WORKFLOW_COMPLETED_EVENT,
    )
    WorkflowAdmission(store).admit_candidate(completion)
    port = AsyncPort("cognition.local")

    with pytest.raises(CognitionTransportBridgeError):
        CognitionTransportBridge(store).dispatch_once(request, port)

    assert port.calls == 0
    assert project_cognition_handoffs(store.events(work.work_id)) == ()


def test_projection_rejects_handoff_for_unknown_request(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, requested, request = _requested(store, source_id="first")

    other = SqliteWorkStore(tmp_path / "other.sqlite")
    _, _, _, other_request = _requested(other, source_id="other")
    current = store.snapshot(request.work_id)

    synthetic_role = requested
    synthetic_role = type(synthetic_role)(
        run=synthetic_role.run,
        state=RoleRunState.REQUESTED,
        request_id=other_request.request_id,
        request_digest=other_request.request_digest,
        terminal_event_id=None,
        output_text=None,
        error=None,
    )
    handoff = CognitionHandoff.create(
        role=synthetic_role,
        snapshot=current,
        port_id="cognition.local",
    )
    forged = WorkEvent.create(
        work_id=request.work_id,
        sequence=current.revision + 1,
        kind=COGNITION_HANDOFF_ADMITTED_EVENT,
        payload={"cognition_handoff": handoff.to_dict()},
        previous_event_digest=current.last_event_digest,
        event_id=handoff.handoff_id,
        created_at=handoff.created_at,
    )
    store.append(
        request.work_id,
        expected_revision=current.revision,
        event=forged,
    )

    with pytest.raises(CognitionHandoffProjectionError):
        project_cognition_handoffs(store.events(request.work_id))


def test_projection_rejects_handoff_after_terminal_role_outcome(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, requested, request = _requested(store)
    terminal = RoleAdmission(store).admit_outcome(
        CognitionOutcome.failed(
            request,
            error="terminal before handoff",
        )
    )
    synthetic_requested = type(requested)(
        run=terminal.run,
        state=RoleRunState.REQUESTED,
        request_id=request.request_id,
        request_digest=request.request_digest,
        terminal_event_id=None,
        output_text=None,
        error=None,
    )
    current = store.snapshot(request.work_id)
    handoff = CognitionHandoff.create(
        role=synthetic_requested,
        snapshot=current,
        port_id="cognition.local",
    )
    store.append(
        request.work_id,
        expected_revision=current.revision,
        event=handoff.to_event(),
    )

    with pytest.raises(CognitionHandoffProjectionError):
        project_cognition_handoffs(store.events(request.work_id))


def test_only_one_handoff_event_is_recorded(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, _, request = _requested(store)
    port = AsyncPort("cognition.local")
    bridge = CognitionTransportBridge(store)

    bridge.dispatch_once(request, port)
    bridge.dispatch_once(request, port)

    handoff_events = [
        event
        for event in store.events(request.work_id)
        if event.kind == COGNITION_HANDOFF_ADMITTED_EVENT
    ]
    assert len(handoff_events) == 1
