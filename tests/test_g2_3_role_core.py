from __future__ import annotations

import hashlib
import json

import pytest

from codexia_manual_agent.role_core import (
    ROLE_COMPLETED_EVENT,
    CognitionOutcome,
    CognitionRequest,
    ContextProjection,
    InvalidRoleRecord,
    RoleAdmission,
    RoleBinding,
    RoleProjectionError,
    RoleRun,
    RoleRunState,
    RoleRunStateError,
    project_role_run,
    project_role_runs,
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

INSTRUCTIONS = "Act as a bounded reviewer. Return only evidence-bounded analysis."
CONTEXT = "exact bounded context for this one cognition operation"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work() -> Work:
    return Work.create(
        objective="Exercise RoleRun cognition semantics",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="g2.3-test",
            payload_digest=_sha("payload"),
        ),
    )


def _workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:g2.3-test",
        version="1.0.0",
        definition_digest=_sha("workflow-definition"),
    )


def _role_binding(*, instructions: str = INSTRUCTIONS) -> RoleBinding:
    return RoleBinding.create(
        role_id="codexia:reviewer",
        version="1.0.0",
        instructions_digest=_sha(instructions),
    )


def _context_projection(*, context: str = CONTEXT) -> ContextProjection:
    return ContextProjection.create(content_digest=_sha(context))


def _started_workflow(store: SqliteWorkStore):
    initial = store.create(_work())
    workflow_run = WorkflowRun.create(
        snapshot=initial,
        binding=_workflow_binding(),
    )
    workflow = WorkflowAdmission(store).admit_start(workflow_run)
    return workflow_run, workflow


def _started_role(store: SqliteWorkStore):
    workflow_run, workflow = _started_workflow(store)
    role_run = RoleRun.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow_run.work_id),
        binding=_role_binding(),
        context=_context_projection(),
    )
    role = RoleAdmission(store).admit_start(role_run)
    return workflow_run, role_run, role


def _admitted_request(store: SqliteWorkStore):
    workflow_run, role_run, role = _started_role(store)
    request = CognitionRequest.create(
        role=role,
        snapshot=store.snapshot(role_run.work_id),
        instructions=INSTRUCTIONS,
        context=CONTEXT,
    )
    requested = RoleAdmission(store).admit_request(request)
    return workflow_run, role_run, request, requested


def test_role_binding_is_exact_to_instruction_semantics() -> None:
    first = _role_binding(instructions="review carefully")
    second = _role_binding(instructions="review aggressively")

    assert first.role_id == second.role_id
    assert first.version == second.version
    assert first.instructions_digest != second.instructions_digest
    assert first.binding_digest != second.binding_digest


def test_context_projection_binds_content_without_storing_bytes() -> None:
    projection = _context_projection(context="sensitive bounded context")

    assert projection.content_digest == _sha("sensitive bounded context")
    assert "sensitive bounded context" not in json.dumps(projection.to_dict())


def test_prepared_role_run_is_not_truth_before_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, workflow = _started_workflow(store)
    role_run = RoleRun.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow_run.work_id),
        binding=_role_binding(),
        context=_context_projection(),
    )
    before = store.events(workflow_run.work_id)

    assert role_run.role_run_id not in {event.event_id for event in before}
    assert project_role_runs(before) == ()


def test_role_start_is_admitted_under_exact_workflow(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, role_run, role = _started_role(store)

    assert role.run == role_run
    assert role.state is RoleRunState.ACTIVE
    assert project_workflow_run(
        store.events(workflow_run.work_id),
        workflow_run.workflow_run_id,
    ).state is WorkflowRunState.ACTIVE
    assert store.snapshot(workflow_run.work_id).state is WorkState.ACTIVE


def test_stale_role_start_is_rejected(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, workflow = _started_workflow(store)
    exact = store.snapshot(workflow_run.work_id)
    role_run = RoleRun.create(
        workflow=workflow,
        snapshot=exact,
        binding=_role_binding(),
        context=_context_projection(),
    )
    unrelated = exact.next_event(kind="work.progress", payload={"n": 1})
    store.append(
        workflow_run.work_id,
        expected_revision=exact.revision,
        event=unrelated,
    )

    with pytest.raises(WorkConcurrencyError):
        RoleAdmission(store).admit_start(role_run)


def test_cognition_request_requires_exact_role_instructions(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, role = _started_role(store)

    with pytest.raises(InvalidRoleRecord):
        CognitionRequest.create(
            role=role,
            snapshot=store.snapshot(role_run.work_id),
            instructions="different instructions",
            context=CONTEXT,
        )


def test_cognition_request_requires_exact_context_projection(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, role = _started_role(store)

    with pytest.raises(InvalidRoleRecord):
        CognitionRequest.create(
            role=role,
            snapshot=store.snapshot(role_run.work_id),
            instructions=INSTRUCTIONS,
            context="different context",
        )


def test_request_is_durable_before_cognition_outcome(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, request, requested = _admitted_request(store)

    assert requested.state is RoleRunState.REQUESTED
    assert requested.request_id == request.request_id
    assert requested.request_digest == request.request_digest

    chronology = json.dumps(
        [event.to_dict() for event in store.events(role_run.work_id)],
        ensure_ascii=False,
    )
    assert INSTRUCTIONS not in chronology
    assert CONTEXT not in chronology
    assert request.instructions_digest in chronology
    assert request.context_digest in chronology


def test_exact_request_retry_is_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, request, first = _admitted_request(store)

    retried = RoleAdmission(store).admit_request(request)

    assert retried == first
    assert len(store.events(role_run.work_id)) == 3


def test_outcome_cannot_be_admitted_before_request(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, role = _started_role(store)
    request = CognitionRequest.create(
        role=role,
        snapshot=store.snapshot(role_run.work_id),
        instructions=INSTRUCTIONS,
        context=CONTEXT,
    )
    outcome = CognitionOutcome.succeeded(request, output_text="candidate analysis")

    with pytest.raises(RoleRunStateError):
        RoleAdmission(store).admit_outcome(outcome)


def test_successful_outcome_completes_role_only(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, role_run, request, _ = _admitted_request(store)
    outcome = CognitionOutcome.succeeded(
        request,
        output_text="evidence-bounded candidate analysis",
    )

    completed = RoleAdmission(store).admit_outcome(outcome)

    assert completed.state is RoleRunState.COMPLETED
    assert completed.output_text == "evidence-bounded candidate analysis"
    assert store.snapshot(role_run.work_id).state is WorkState.ACTIVE
    assert project_workflow_run(
        store.events(role_run.work_id),
        workflow_run.workflow_run_id,
    ).state is WorkflowRunState.ACTIVE


def test_failed_outcome_is_terminal_for_role(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, request, _ = _admitted_request(store)
    outcome = CognitionOutcome.failed(request, error="provider rejected request")

    failed = RoleAdmission(store).admit_outcome(outcome)

    assert failed.state is RoleRunState.FAILED
    assert failed.error == "provider rejected request"


def test_unknown_outcome_is_terminal_and_does_not_grant_resend(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, request, _ = _admitted_request(store)
    outcome = CognitionOutcome.unknown(
        request,
        detail="provider may have accepted request; response was not observed",
    )

    unknown = RoleAdmission(store).admit_outcome(outcome)

    assert unknown.state is RoleRunState.OUTCOME_UNKNOWN
    with pytest.raises(InvalidRoleRecord):
        CognitionRequest.create(
            role=unknown,
            snapshot=store.snapshot(role_run.work_id),
            instructions=INSTRUCTIONS,
            context=CONTEXT,
        )


def test_exact_outcome_retry_is_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, request, _ = _admitted_request(store)
    outcome = CognitionOutcome.succeeded(request, output_text="result")
    admission = RoleAdmission(store)

    first = admission.admit_outcome(outcome)
    retried = admission.admit_outcome(outcome)

    assert retried == first
    assert len(store.events(role_run.work_id)) == 4


def test_stale_cognition_outcome_is_rejected(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, request, _ = _admitted_request(store)
    outcome = CognitionOutcome.succeeded(request, output_text="stale result")

    current = store.snapshot(role_run.work_id)
    unrelated = current.next_event(
        kind="work.external-observation",
        payload={"changed": True},
    )
    store.append(
        role_run.work_id,
        expected_revision=current.revision,
        event=unrelated,
    )

    with pytest.raises(WorkConcurrencyError):
        RoleAdmission(store).admit_outcome(outcome)


def test_two_role_runs_can_coexist_under_one_workflow(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, workflow = _started_workflow(store)
    admission = RoleAdmission(store)

    first = RoleRun.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow_run.work_id),
        binding=_role_binding(),
        context=_context_projection(context="first context"),
    )
    admission.admit_start(first)

    workflow = project_workflow_run(
        store.events(workflow_run.work_id),
        workflow_run.workflow_run_id,
    )
    second = RoleRun.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow_run.work_id),
        binding=RoleBinding.create(
            role_id="codexia:critic",
            version="1.0.0",
            instructions_digest=_sha("critic instructions"),
        ),
        context=_context_projection(context="second context"),
    )
    admission.admit_start(second)

    projected = project_role_runs(store.events(workflow_run.work_id))

    assert [item.run.role_run_id for item in projected] == [
        first.role_run_id,
        second.role_run_id,
    ]
    assert all(item.state is RoleRunState.ACTIVE for item in projected)


def test_restart_projects_requested_and_terminal_role_state(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    _, role_run, request, requested = _admitted_request(store)

    restarted = SqliteWorkStore(path)
    recovered_requested = project_role_run(
        restarted.events(role_run.work_id),
        role_run.role_run_id,
    )
    assert recovered_requested == requested

    outcome = CognitionOutcome.succeeded(request, output_text="after restart")
    completed = RoleAdmission(restarted).admit_outcome(outcome)

    restarted_again = SqliteWorkStore(path)
    recovered_completed = project_role_run(
        restarted_again.events(role_run.work_id),
        role_run.role_run_id,
    )
    assert recovered_completed == completed


def test_projection_rejects_raw_role_event_without_workflow_wrapper(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, _, _ = _admitted_request(store)
    current = store.snapshot(role_run.work_id)
    forged = WorkEvent.create(
        work_id=role_run.work_id,
        sequence=current.revision + 1,
        kind=ROLE_COMPLETED_EVENT,
        payload={"not": "workflow wrapped"},
        previous_event_digest=current.last_event_digest,
    )
    store.append(
        role_run.work_id,
        expected_revision=current.revision,
        event=forged,
    )

    with pytest.raises(RoleProjectionError):
        project_role_runs(store.events(role_run.work_id))


def test_role_output_does_not_finish_workflow_when_role_completes(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    workflow_run, role_run, request, _ = _admitted_request(store)
    RoleAdmission(store).admit_outcome(
        CognitionOutcome.succeeded(request, output_text="model proposal"),
    )

    workflow = project_workflow_run(
        store.events(role_run.work_id),
        workflow_run.workflow_run_id,
    )
    assert workflow.state is WorkflowRunState.ACTIVE

    completion = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=store.snapshot(role_run.work_id),
        event_kind=WORKFLOW_COMPLETED_EVENT,
        payload={"reason": "workflow explicitly decided to finish"},
    )
    WorkflowAdmission(store).admit_candidate(completion)

    assert project_workflow_run(
        store.events(role_run.work_id),
        workflow_run.workflow_run_id,
    ).state is WorkflowRunState.COMPLETED
    assert store.snapshot(role_run.work_id).state is WorkState.ACTIVE


def test_exact_role_start_retry_is_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, first = _started_role(store)

    retried = RoleAdmission(store).admit_start(role_run)

    assert retried == first
    assert len(store.events(role_run.work_id)) == 2


def test_second_distinct_request_for_requested_role_is_rejected(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, _, requested = _admitted_request(store)

    with pytest.raises(InvalidRoleRecord):
        CognitionRequest.create(
            role=requested,
            snapshot=store.snapshot(role_run.work_id),
            instructions=INSTRUCTIONS,
            context=CONTEXT,
        )


def test_projection_rejects_tampered_durable_request_digest(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, role_run, role = _started_role(store)
    request = CognitionRequest.create(
        role=role,
        snapshot=store.snapshot(role_run.work_id),
        instructions=INSTRUCTIONS,
        context=CONTEXT,
    )
    candidate = request.to_workflow_candidate()
    payload = candidate.event.to_dict()["payload"]
    payload["payload"]["cognition_request"]["request_digest"] = "0" * 64
    forged = WorkEvent.create(
        work_id=candidate.event.work_id,
        sequence=candidate.event.sequence,
        kind=candidate.event.kind,
        payload=payload,
        previous_event_digest=candidate.event.previous_event_digest,
        event_id=candidate.event.event_id,
        created_at=candidate.event.created_at,
    )
    store.append(
        role_run.work_id,
        expected_revision=candidate.expected_revision,
        event=forged,
    )

    with pytest.raises(RoleProjectionError):
        project_role_runs(store.events(role_run.work_id))
