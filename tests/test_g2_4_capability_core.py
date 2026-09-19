from __future__ import annotations

import hashlib

import pytest

from codexia_manual_agent.capability_core import (
    CAPABILITY_NEED_DECLARED_EVENT,
    CAPABILITY_OUTCOME_RECORDED_EVENT,
    CapabilityAdmission,
    CapabilityBinding,
    CapabilityNeed,
    CapabilityNeedSnapshot,
    CapabilityNeedState,
    CapabilityNeedStateError,
    CapabilityOutcome,
    CapabilityProjectionError,
    InvalidCapabilityRecord,
    project_capability_need,
    project_capability_needs,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkEvent,
    WorkIngressBinding,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WORKFLOW_COMPLETED_EVENT,
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
    WorkflowRunState,
    project_workflow_run,
)

PARAMETERS = {
    "argv": ["python", "-V"],
    "cwd_ref": "workspace",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work() -> Work:
    return Work.create(
        objective="Exercise capability need/outcome semantics",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="g2.4-test",
            payload_digest=_sha("payload"),
        ),
    )


def _workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:g2.4-test",
        version="1.0.0",
        definition_digest=_sha("workflow-definition"),
    )


def _capability_binding(
    *,
    capability_id: str = "process",
    version: str = "1.0.0",
    contract: str = "process-contract-v1",
) -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id=capability_id,
        version=version,
        contract_digest=_sha(contract),
    )


def _started_workflow(store: SqliteWorkStore):
    initial = store.create(_work())
    run = WorkflowRun.create(
        snapshot=initial,
        binding=_workflow_binding(),
    )
    projected = WorkflowAdmission(store).admit_start(run)
    return run, projected


def _declared_need(store: SqliteWorkStore):
    workflow_run, workflow = _started_workflow(store)
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow_run.work_id),
        binding=_capability_binding(),
        operation="run",
        parameters=PARAMETERS,
    )
    pending = CapabilityAdmission(store).admit_need(need)
    return workflow_run, need, pending


def test_capability_binding_is_exact_to_contract_semantics() -> None:
    first = _capability_binding(contract="v1")
    second = _capability_binding(contract="v2")

    assert first.capability_id == second.capability_id
    assert first.version == second.version
    assert first.contract_digest != second.contract_digest
    assert first.binding_digest != second.binding_digest


def test_prepared_need_is_not_truth_before_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, workflow = _started_workflow(store)
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow_run.work_id),
        binding=_capability_binding(),
        operation="run",
        parameters=PARAMETERS,
    )

    assert project_capability_needs(store.events(workflow_run.work_id)) == ()
    assert need.need_id not in {
        event.event_id for event in store.events(workflow_run.work_id)
    }


def test_need_admission_is_pending_and_does_not_change_workflow_or_work(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, need, pending = _declared_need(store)

    assert pending.need == need
    assert pending.state is CapabilityNeedState.PENDING
    assert pending.outcome is None
    assert project_workflow_run(
        store.events(need.work_id),
        workflow_run.workflow_run_id,
    ).state is WorkflowRunState.ACTIVE
    assert store.snapshot(need.work_id).state is WorkState.ACTIVE


def test_capability_parameters_are_content_bound_and_immutable(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, workflow = _started_workflow(store)
    parameters = {"items": ["a"], "nested": {"value": 1}}
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow_run.work_id),
        binding=_capability_binding(),
        operation="inspect",
        parameters=parameters,
    )

    parameters["items"].append("mutated")
    parameters["nested"]["value"] = 2

    assert need.to_dict()["parameters"] == {
        "items": ["a"],
        "nested": {"value": 1},
    }


def test_stale_need_declaration_is_rejected(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, workflow = _started_workflow(store)
    exact = store.snapshot(workflow_run.work_id)
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=exact,
        binding=_capability_binding(),
        operation="run",
        parameters=PARAMETERS,
    )

    unrelated = exact.next_event(kind="work.progress", payload={"n": 1})
    store.append(
        workflow_run.work_id,
        expected_revision=exact.revision,
        event=unrelated,
    )

    with pytest.raises(WorkConcurrencyError):
        CapabilityAdmission(store).admit_need(need)


def test_exact_need_retry_is_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, need, first = _declared_need(store)

    retried = CapabilityAdmission(store).admit_need(need)

    assert retried == first
    assert len(store.events(need.work_id)) == 2


def test_successful_outcome_resolves_need_only(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, need, pending = _declared_need(store)
    outcome = CapabilityOutcome.succeeded(
        pending,
        attempt_id="host-attempt-1",
        attempt_digest=_sha("attempt-1"),
        observation={"exit_code": 0},
    )

    resolved = CapabilityAdmission(store).admit_outcome(outcome)

    assert resolved.state is CapabilityNeedState.SUCCEEDED
    assert resolved.outcome == outcome
    assert store.snapshot(need.work_id).state is WorkState.ACTIVE
    assert project_workflow_run(
        store.events(need.work_id),
        workflow_run.workflow_run_id,
    ).state is WorkflowRunState.ACTIVE


def test_failed_outcome_is_terminal_for_need(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _declared_need(store)
    outcome = CapabilityOutcome.failed(
        pending,
        attempt_id="host-attempt-failed",
        attempt_digest=_sha("attempt-failed"),
        error="process exited nonzero",
        observation={"exit_code": 1},
    )

    resolved = CapabilityAdmission(store).admit_outcome(outcome)

    assert resolved.state is CapabilityNeedState.FAILED
    assert resolved.outcome == outcome


def test_unknown_outcome_is_terminal_and_does_not_grant_retry(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _declared_need(store)
    unknown = CapabilityOutcome.unknown(
        pending,
        attempt_id="host-attempt-unknown",
        attempt_digest=_sha("attempt-unknown"),
        detail="effect may have occurred; host could not reconcile",
    )
    admission = CapabilityAdmission(store)
    terminal = admission.admit_outcome(unknown)

    assert terminal.state is CapabilityNeedState.OUTCOME_UNKNOWN
    with pytest.raises(InvalidCapabilityRecord):
        CapabilityOutcome.succeeded(
            terminal,
            attempt_id="retry-attempt",
            attempt_digest=_sha("retry-attempt"),
        )


def test_exact_outcome_retry_is_idempotent_after_work_advances(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, need, pending = _declared_need(store)
    outcome = CapabilityOutcome.succeeded(
        pending,
        attempt_id="host-attempt-1",
        attempt_digest=_sha("attempt-1"),
        observation={"ok": True},
    )
    admission = CapabilityAdmission(store)
    first = admission.admit_outcome(outcome)

    current = store.snapshot(need.work_id)
    unrelated = current.next_event(kind="work.after-outcome", payload={"n": 1})
    store.append(
        need.work_id,
        expected_revision=current.revision,
        event=unrelated,
    )

    retried = admission.admit_outcome(outcome)

    assert retried == first
    assert len(
        [
            event
            for event in store.events(need.work_id)
            if event.kind == CAPABILITY_OUTCOME_RECORDED_EVENT
        ]
    ) == 1


def test_second_distinct_outcome_for_same_need_is_rejected(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, pending = _declared_need(store)
    first = CapabilityOutcome.failed(
        pending,
        attempt_id="attempt-one",
        attempt_digest=_sha("attempt-one"),
        error="failed",
    )
    second = CapabilityOutcome.succeeded(
        pending,
        attempt_id="attempt-two",
        attempt_digest=_sha("attempt-two"),
    )
    admission = CapabilityAdmission(store)
    admission.admit_outcome(first)

    with pytest.raises(CapabilityNeedStateError):
        admission.admit_outcome(second)


def test_unrelated_work_progress_does_not_stale_host_outcome(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, need, pending = _declared_need(store)
    outcome = CapabilityOutcome.succeeded(
        pending,
        attempt_id="attempt-concurrent",
        attempt_digest=_sha("attempt-concurrent"),
        observation={"result": "external fact"},
    )

    current = store.snapshot(need.work_id)
    unrelated = current.next_event(
        kind="work.concurrent-progress",
        payload={"changed": True},
    )
    store.append(
        need.work_id,
        expected_revision=current.revision,
        event=unrelated,
    )

    resolved = CapabilityAdmission(store).admit_outcome(outcome)

    assert resolved.state is CapabilityNeedState.SUCCEEDED
    assert resolved.outcome == outcome


def test_outcome_can_be_recorded_after_requesting_workflow_completed(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, need, pending = _declared_need(store)
    outcome = CapabilityOutcome.succeeded(
        pending,
        attempt_id="late-attempt",
        attempt_digest=_sha("late-attempt"),
    )

    workflow = project_workflow_run(
        store.events(need.work_id),
        workflow_run.workflow_run_id,
    )
    completion = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=store.snapshot(need.work_id),
        event_kind=WORKFLOW_COMPLETED_EVENT,
        payload={"reason": "semantic workflow closed"},
    )
    WorkflowAdmission(store).admit_candidate(completion)

    resolved = CapabilityAdmission(store).admit_outcome(outcome)

    assert resolved.state is CapabilityNeedState.SUCCEEDED
    assert project_workflow_run(
        store.events(need.work_id),
        workflow_run.workflow_run_id,
    ).state is WorkflowRunState.COMPLETED
    assert store.snapshot(need.work_id).state is WorkState.ACTIVE


def test_restart_projects_pending_need_without_attempt_assumption(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    _, need, pending = _declared_need(store)

    restarted = SqliteWorkStore(path)
    recovered = project_capability_need(
        restarted.events(need.work_id),
        need.need_id,
    )

    assert recovered == pending
    assert recovered.state is CapabilityNeedState.PENDING
    assert recovered.outcome is None


def test_restart_projects_terminal_outcome(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    _, need, pending = _declared_need(store)
    outcome = CapabilityOutcome.unknown(
        pending,
        attempt_id="restart-attempt",
        attempt_digest=_sha("restart-attempt"),
        detail="host reconciliation remained ambiguous",
    )
    expected = CapabilityAdmission(store).admit_outcome(outcome)

    restarted = SqliteWorkStore(path)
    recovered = project_capability_need(
        restarted.events(need.work_id),
        need.need_id,
    )

    assert recovered == expected
    assert recovered.state is CapabilityNeedState.OUTCOME_UNKNOWN


def test_projection_rejects_need_without_workflow_wrapper(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, workflow = _started_workflow(store)
    exact = store.snapshot(workflow_run.work_id)
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=exact,
        binding=_capability_binding(),
        operation="run",
        parameters=PARAMETERS,
    )
    forged = WorkEvent.create(
        work_id=need.work_id,
        sequence=exact.revision + 1,
        kind=CAPABILITY_NEED_DECLARED_EVENT,
        payload={"capability_need": need.to_dict()},
        previous_event_digest=exact.last_event_digest,
        event_id=need.need_id,
        created_at=need.created_at,
    )
    store.append(
        need.work_id,
        expected_revision=exact.revision,
        event=forged,
    )

    with pytest.raises(CapabilityProjectionError):
        project_capability_needs(store.events(need.work_id))


def test_projection_rejects_tampered_need_parameters(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, workflow = _started_workflow(store)
    exact = store.snapshot(workflow_run.work_id)
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=exact,
        binding=_capability_binding(),
        operation="run",
        parameters=PARAMETERS,
    )
    candidate = need.to_workflow_candidate()
    payload = candidate.event.to_dict()["payload"]
    payload["payload"]["capability_need"]["parameters"]["argv"] = ["python", "-c", "x"]
    forged = WorkEvent.create(
        work_id=need.work_id,
        sequence=candidate.event.sequence,
        kind=candidate.event.kind,
        payload=payload,
        previous_event_digest=candidate.event.previous_event_digest,
        event_id=candidate.event.event_id,
        created_at=candidate.event.created_at,
    )
    store.append(
        need.work_id,
        expected_revision=candidate.expected_revision,
        event=forged,
    )

    with pytest.raises(CapabilityProjectionError):
        project_capability_needs(store.events(need.work_id))


def test_projection_rejects_outcome_for_unknown_need(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, workflow = _started_workflow(store)
    exact = store.snapshot(workflow_run.work_id)
    unadmitted_need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=exact,
        binding=_capability_binding(),
        operation="run",
        parameters=PARAMETERS,
    )
    pending = CapabilityNeedSnapshot(
        need=unadmitted_need,
        state=CapabilityNeedState.PENDING,
        outcome=None,
    )
    outcome = CapabilityOutcome.succeeded(
        pending,
        attempt_id="orphan-attempt",
        attempt_digest=_sha("orphan-attempt"),
    )
    event = outcome.to_event(exact)
    store.append(
        exact.work.work_id,
        expected_revision=exact.revision,
        event=event,
    )

    with pytest.raises(CapabilityProjectionError):
        project_capability_needs(store.events(exact.work.work_id))


def test_outcome_record_is_not_workflow_candidate_progression(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, need, pending = _declared_need(store)
    outcome = CapabilityOutcome.succeeded(
        pending,
        attempt_id="observation-attempt",
        attempt_digest=_sha("observation-attempt"),
    )
    CapabilityAdmission(store).admit_outcome(outcome)

    events = store.events(need.work_id)
    outcome_events = [
        event for event in events if event.kind == CAPABILITY_OUTCOME_RECORDED_EVENT
    ]
    assert len(outcome_events) == 1
    assert set(outcome_events[0].to_dict()["payload"]) == {"capability_outcome"}
    assert project_workflow_run(
        events,
        workflow_run.workflow_run_id,
    ).state is WorkflowRunState.ACTIVE
