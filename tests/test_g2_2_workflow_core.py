from __future__ import annotations

import hashlib

import pytest

from codexia_manual_agent.work_core import (
    WORK_COMPLETED_EVENT,
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkEvent,
    WorkIngressBinding,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WORKFLOW_COMPLETED_EVENT,
    InvalidWorkflowRecord,
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowProjectionError,
    WorkflowRun,
    WorkflowRunState,
    project_workflow_run,
    project_workflow_runs,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work() -> Work:
    return Work.create(
        objective="Exercise workflow semantics",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="g2.2-test",
            payload_digest=_sha("payload"),
        ),
    )


def _binding(
    *,
    workflow_id: str = "codexia:test-workflow",
    version: str = "1.0.0",
    definition: str = "definition-v1",
) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=workflow_id,
        version=version,
        definition_digest=_sha(definition),
    )


def _started_run(
    store: SqliteWorkStore,
    *,
    binding: WorkflowBinding | None = None,
):
    initial = store.create(_work())
    run = WorkflowRun.create(
        snapshot=initial,
        binding=binding or _binding(),
    )
    projected = WorkflowAdmission(store).admit_start(run)
    return run, projected


def test_workflow_binding_is_exact_to_definition_digest() -> None:
    first = _binding(definition="v1")
    second = _binding(definition="v2")

    assert first.workflow_id == second.workflow_id
    assert first.version == second.version
    assert first.definition_digest != second.definition_digest
    assert first.binding_digest != second.binding_digest


def test_workflow_start_is_admitted_into_work_chronology(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    initial = store.create(_work())
    run = WorkflowRun.create(snapshot=initial, binding=_binding())

    projected = WorkflowAdmission(store).admit_start(run)

    events = store.events(initial.work.work_id)
    assert len(events) == 1
    assert events[0].event_id == run.workflow_run_id
    assert projected.run == run
    assert projected.state is WorkflowRunState.ACTIVE
    assert store.snapshot(initial.work.work_id).state is WorkState.ACTIVE


def test_prepared_workflow_run_is_not_truth_before_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    initial = store.create(_work())
    WorkflowRun.create(snapshot=initial, binding=_binding())

    assert store.events(initial.work.work_id) == ()
    assert project_workflow_runs(store.events(initial.work.work_id)) == ()


def test_stale_workflow_start_is_rejected(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    initial = store.create(_work())
    run = WorkflowRun.create(snapshot=initial, binding=_binding())

    unrelated = initial.next_event(kind="work.progress", payload={"n": 1})
    store.append(initial.work.work_id, expected_revision=0, event=unrelated)

    with pytest.raises(WorkConcurrencyError):
        WorkflowAdmission(store).admit_start(run)


def test_exact_workflow_start_retry_is_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    run, first_projection = _started_run(store)
    admission = WorkflowAdmission(store)

    work_snapshot = store.snapshot(run.work_id)
    candidate = WorkflowCandidate.create(
        run_snapshot=first_projection,
        work_snapshot=work_snapshot,
        event_kind="workflow.progress",
        payload={"step": 1},
    )
    admission.admit_candidate(candidate)

    retried = admission.admit_start(run)

    assert retried.run == run
    assert retried.state is WorkflowRunState.ACTIVE
    assert len(store.events(run.work_id)) == 2


def test_candidate_is_not_work_truth_before_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    run, projected = _started_run(store)
    before = store.snapshot(run.work_id)
    before_events = store.events(run.work_id)

    candidate = WorkflowCandidate.create(
        run_snapshot=projected,
        work_snapshot=before,
        event_kind="workflow.progress",
        payload={"step": "inspect"},
    )

    assert store.snapshot(run.work_id) == before
    assert store.events(run.work_id) == before_events
    assert candidate.event not in before_events


def test_candidate_admission_binds_exact_work_revision(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    run, projected = _started_run(store)
    exact = store.snapshot(run.work_id)

    candidate = WorkflowCandidate.create(
        run_snapshot=projected,
        work_snapshot=exact,
        event_kind="workflow.progress",
        payload={"step": 1},
    )

    unrelated = exact.next_event(kind="work.external-observation", payload={"n": 1})
    store.append(run.work_id, expected_revision=exact.revision, event=unrelated)

    with pytest.raises(WorkConcurrencyError):
        WorkflowAdmission(store).admit_candidate(candidate)


def test_exact_candidate_retry_is_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    run, projected = _started_run(store)
    admission = WorkflowAdmission(store)
    candidate = WorkflowCandidate.create(
        run_snapshot=projected,
        work_snapshot=store.snapshot(run.work_id),
        event_kind="workflow.progress",
        payload={"step": 1},
    )

    first = admission.admit_candidate(candidate)
    retried = admission.admit_candidate(candidate)

    assert first == retried
    assert len(store.events(run.work_id)) == 2


def test_two_workflow_runs_can_coexist_on_one_work(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    initial = store.create(_work())
    admission = WorkflowAdmission(store)

    first = WorkflowRun.create(
        snapshot=initial,
        binding=_binding(workflow_id="codexia:first"),
    )
    admission.admit_start(first)

    second = WorkflowRun.create(
        snapshot=store.snapshot(initial.work.work_id),
        binding=_binding(
            workflow_id="codexia:second",
            version="2.0.0",
            definition="second-definition",
        ),
    )
    admission.admit_start(second)

    projections = project_workflow_runs(store.events(initial.work.work_id))

    assert [item.run.workflow_run_id for item in projections] == [
        first.workflow_run_id,
        second.workflow_run_id,
    ]
    assert all(item.state is WorkflowRunState.ACTIVE for item in projections)


def test_workflow_completion_does_not_complete_work(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    run, projected = _started_run(store)
    candidate = WorkflowCandidate.create(
        run_snapshot=projected,
        work_snapshot=store.snapshot(run.work_id),
        event_kind=WORKFLOW_COMPLETED_EVENT,
        payload={"summary": "workflow finished"},
    )

    terminal = WorkflowAdmission(store).admit_candidate(candidate)

    assert terminal.state is WorkflowRunState.COMPLETED
    assert store.snapshot(run.work_id).state is WorkState.ACTIVE


def test_terminal_workflow_run_cannot_propose_more_progress(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    run, projected = _started_run(store)
    admission = WorkflowAdmission(store)
    terminal_candidate = WorkflowCandidate.create(
        run_snapshot=projected,
        work_snapshot=store.snapshot(run.work_id),
        event_kind=WORKFLOW_COMPLETED_EVENT,
    )
    terminal = admission.admit_candidate(terminal_candidate)

    with pytest.raises(InvalidWorkflowRecord):
        WorkflowCandidate.create(
            run_snapshot=terminal,
            work_snapshot=store.snapshot(run.work_id),
            event_kind="workflow.progress",
        )


def test_workflow_candidate_cannot_directly_complete_work(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    run, projected = _started_run(store)

    with pytest.raises(InvalidWorkflowRecord):
        WorkflowCandidate.create(
            run_snapshot=projected,
            work_snapshot=store.snapshot(run.work_id),
            event_kind=WORK_COMPLETED_EVENT,
        )


def test_restart_projects_exact_workflow_lifecycle(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    run, projected = _started_run(store)
    candidate = WorkflowCandidate.create(
        run_snapshot=projected,
        work_snapshot=store.snapshot(run.work_id),
        event_kind="workflow.progress",
        payload={"step": "before restart"},
    )
    WorkflowAdmission(store).admit_candidate(candidate)

    restarted = SqliteWorkStore(path)
    recovered = project_workflow_run(
        restarted.events(run.work_id),
        run.workflow_run_id,
    )

    assert recovered.run == run
    assert recovered.state is WorkflowRunState.ACTIVE


def test_projection_rejects_unbound_workflow_terminal_event(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    run, _ = _started_run(store)
    current = store.snapshot(run.work_id)
    forged = WorkEvent.create(
        work_id=run.work_id,
        sequence=current.revision + 1,
        kind=WORKFLOW_COMPLETED_EVENT,
        payload={"summary": "missing workflow provenance"},
        previous_event_digest=current.last_event_digest,
    )
    store.append_completion(
        run.work_id,
        expected_revision=current.revision,
        event=forged,
    )

    with pytest.raises(WorkflowProjectionError):
        project_workflow_runs(store.events(run.work_id))


def test_projection_rejects_workflow_run_shape_drift(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    initial = store.create(_work())
    run = WorkflowRun.create(snapshot=initial, binding=_binding())
    start = run.to_start_event()
    payload = start.to_dict()["payload"]
    payload["workflow_run"]["unexpected"] = True
    forged = WorkEvent.create(
        work_id=start.work_id,
        sequence=start.sequence,
        kind=start.kind,
        payload=payload,
        previous_event_digest=start.previous_event_digest,
        event_id=start.event_id,
        created_at=start.created_at,
    )
    store.append(initial.work.work_id, expected_revision=0, event=forged)

    with pytest.raises(WorkflowProjectionError):
        project_workflow_runs(store.events(initial.work.work_id))


def test_projection_rejects_raw_workflow_direct_work_completion(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    run, _ = _started_run(store)
    current = store.snapshot(run.work_id)
    forged = WorkEvent.create(
        work_id=run.work_id,
        sequence=current.revision + 1,
        kind=WORK_COMPLETED_EVENT,
        payload={
            "_workflow": {
                "workflow_run_id": run.workflow_run_id,
                "workflow_run_digest": run.run_digest,
            },
            "payload": {"summary": "bypass"},
        },
        previous_event_digest=current.last_event_digest,
    )
    store.append(
        run.work_id,
        expected_revision=current.revision,
        event=forged,
    )

    with pytest.raises(WorkflowProjectionError):
        project_workflow_runs(store.events(run.work_id))


def test_projection_ignores_unrelated_non_object_payload(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    run, _ = _started_run(store)
    current = store.snapshot(run.work_id)
    unrelated = WorkEvent.create(
        work_id=run.work_id,
        sequence=current.revision + 1,
        kind="domain.list-observation",
        payload=["one", "two"],
        previous_event_digest=current.last_event_digest,
    )
    store.append(
        run.work_id,
        expected_revision=current.revision,
        event=unrelated,
    )

    projected = project_workflow_run(
        store.events(run.work_id),
        run.workflow_run_id,
    )

    assert projected.state is WorkflowRunState.ACTIVE
