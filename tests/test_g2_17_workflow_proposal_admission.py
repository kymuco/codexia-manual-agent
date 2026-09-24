from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from codexia_manual_agent.capability_core import (
    CapabilityBinding,
    CapabilityNeed,
    CapabilityNeedState,
)
from codexia_manual_agent.invariant_bridge import ResolvedWorkflowImplementation
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.role_core import (
    ContextProjection,
    RoleBinding,
    RoleRun,
    RoleRunState,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkIngressBinding,
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
from codexia_manual_agent.workflow_orchestration import (
    WorkflowProposalAdmissionBindingError,
    WorkflowProposalAdmissionService,
    WorkflowStepResult,
    WorkflowStepService,
)
from codexia_manual_agent.workflow_runtime import (
    WorkflowImplementationOwnershipError,
)

PROVIDER_REF = "codexia:g2.17-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:g2.17-workflow",
        version="1.0.0",
        definition_digest=_sha("g2.17-workflow"),
    )


def _role_binding() -> RoleBinding:
    return RoleBinding.create(
        role_id="codexia:g2.17-reviewer",
        version="1.0.0",
        instructions_digest=_sha("review exact evidence"),
    )


def _capability_binding() -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id="codexia:g2.17-read",
        version="1.0.0",
        contract_digest=_sha("g2.17-read-contract"),
    )


def _member(kind, semantic_id, version, digest):
    return PackMemberBinding.create(
        kind=kind,
        semantic_id=semantic_id,
        version=version,
        binding_digest=digest,
    )


def _started(store: SqliteWorkStore, *, source_id: str):
    workflow_binding = _workflow_binding()
    role_binding = _role_binding()
    capability_binding = _capability_binding()
    pack = PackBinding.create(
        pack_id="codexia:g2.17-pack",
        version="1.0.0",
        definition_digest=_sha("g2.17-pack"),
        members=(
            _member(
                PackMemberKind.WORKFLOW,
                workflow_binding.workflow_id,
                workflow_binding.version,
                workflow_binding.binding_digest,
            ),
            _member(
                PackMemberKind.ROLE,
                role_binding.role_id,
                role_binding.version,
                role_binding.binding_digest,
            ),
            _member(
                PackMemberKind.CAPABILITY,
                capability_binding.capability_id,
                capability_binding.version,
                capability_binding.binding_digest,
            ),
        ),
    )
    work = Work.create(
        objective="Route one typed Workflow proposal",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    run = WorkflowRun.create(snapshot=initial, binding=workflow_binding)
    workflow = WorkflowAdmission(store).admit_start(run)
    pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work_id),
            pack=pack,
        )
    )
    return work, workflow, pin, role_binding, capability_binding


class _Resolver:
    def __init__(self, implementation) -> None:
        self.implementation = implementation

    def resolve(self, *, provider_ref, workflow, pack_binding):
        return ResolvedWorkflowImplementation(
            provider_ref=provider_ref,
            workflow_binding=workflow.run.binding,
            pack_binding_digest=pack_binding.pack.binding_digest,
            implementation=self.implementation,
        )


class _Implementation:
    def __init__(self, binding: WorkflowBinding, kind: str, extra) -> None:
        self.binding = binding
        self.kind = kind
        self.extra = extra

    def propose(self, context):
        if self.kind == "workflow":
            return WorkflowCandidate.create(
                run_snapshot=context.workflow,
                work_snapshot=context.work,
                event_kind=WORKFLOW_COMPLETED_EVENT,
                payload={"reason": "g2.17 proof"},
            )
        if self.kind == "role":
            return RoleRun.create(
                workflow=context.workflow,
                snapshot=context.work,
                binding=self.extra,
                context=ContextProjection.create(
                    content_digest=_sha("g2.17-role-context"),
                ),
            )
        if self.kind == "capability":
            return CapabilityNeed.create(
                workflow=context.workflow,
                snapshot=context.work,
                binding=self.extra,
                operation="read",
                parameters={"artifact_ref": "evidence:A"},
            )
        if self.kind == "none":
            return None
        raise AssertionError(f"unknown test kind: {self.kind}")


def _step(
    store: SqliteWorkStore,
    workflow,
    *,
    kind: str,
    extra=None,
) -> WorkflowStepResult:
    implementation = _Implementation(workflow.run.binding, kind, extra)
    return WorkflowStepService(
        store=store,
        resolver=_Resolver(implementation),
    ).step(
        work_id=workflow.run.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        provider_ref=PROVIDER_REF,
    )


def test_routes_workflow_candidate_to_workflow_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "workflow.sqlite")
    work, workflow, _, _, _ = _started(store, source_id="workflow")
    result = _step(store, workflow, kind="workflow")

    admitted = WorkflowProposalAdmissionService(store).admit(result)

    assert admitted is not None
    assert admitted.state is WorkflowRunState.COMPLETED
    assert (
        project_workflow_run(
            store.events(work.work_id),
            workflow.run.workflow_run_id,
        ).state
        is WorkflowRunState.COMPLETED
    )


def test_routes_role_run_to_role_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "role.sqlite")
    _, workflow, _, role_binding, _ = _started(store, source_id="role")
    result = _step(
        store,
        workflow,
        kind="role",
        extra=role_binding,
    )

    admitted = WorkflowProposalAdmissionService(store).admit(result)

    assert admitted is not None
    assert admitted.state is RoleRunState.ACTIVE
    assert admitted.run == result.proposal


def test_routes_capability_need_to_capability_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "capability.sqlite")
    _, workflow, _, _, capability = _started(store, source_id="capability")
    result = _step(
        store,
        workflow,
        kind="capability",
        extra=capability,
    )

    admitted = WorkflowProposalAdmissionService(store).admit(result)

    assert admitted is not None
    assert admitted.state is CapabilityNeedState.PENDING
    assert admitted.need == result.proposal


def test_none_proposal_is_exact_noop(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "none.sqlite")
    work, workflow, _, _, _ = _started(store, source_id="none")
    result = _step(store, workflow, kind="none")
    before = store.events(work.work_id)

    assert WorkflowProposalAdmissionService(store).admit(result) is None
    assert store.events(work.work_id) == before


def test_result_proposal_read_view_mismatch_fails_before_mutation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "mismatch.sqlite")
    work, workflow, _, _, capability = _started(store, source_id="mismatch")
    result = _step(
        store,
        workflow,
        kind="capability",
        extra=capability,
    )
    forged = replace(result, work_revision=result.work_revision + 1)
    before = store.events(work.work_id)

    with pytest.raises(WorkflowProposalAdmissionBindingError):
        WorkflowProposalAdmissionService(store).admit(forged)

    assert store.events(work.work_id) == before


def test_pack_binding_mismatch_fails_before_mutation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "pack.sqlite")
    work, workflow, _, role_binding, _ = _started(store, source_id="pack")
    result = _step(store, workflow, kind="role", extra=role_binding)
    forged = replace(result, pack_binding_digest="0" * 64)
    before = store.events(work.work_id)

    with pytest.raises(WorkflowProposalAdmissionBindingError):
        WorkflowProposalAdmissionService(store).admit(forged)

    assert store.events(work.work_id) == before


def test_post_step_concurrency_remains_existing_admission_cas(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    work, workflow, _, _, capability = _started(store, source_id="stale")
    result = _step(
        store,
        workflow,
        kind="capability",
        extra=capability,
    )
    current = store.snapshot(work.work_id)
    store.append(
        work.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.external-observation",
            payload={"source": "concurrent"},
        ),
    )
    before = store.events(work.work_id)

    with pytest.raises(WorkConcurrencyError):
        WorkflowProposalAdmissionService(store).admit(result)

    assert store.events(work.work_id) == before


def test_exact_retry_after_later_work_event_remains_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    work, workflow, _, role_binding, _ = _started(store, source_id="retry")
    result = _step(store, workflow, kind="role", extra=role_binding)
    service = WorkflowProposalAdmissionService(store)
    first = service.admit(result)
    assert first is not None

    current_workflow = project_workflow_run(
        store.events(work.work_id),
        workflow.run.workflow_run_id,
    )
    note = WorkflowCandidate.create(
        run_snapshot=current_workflow,
        work_snapshot=store.snapshot(work.work_id),
        event_kind="workflow.note",
        payload={"after": "role admission"},
    )
    WorkflowAdmission(store).admit_candidate(note)
    before_retry = store.events(work.work_id)

    retried = service.admit(result)

    assert retried == first
    assert store.events(work.work_id) == before_retry


def test_provider_ref_is_not_admission_authority(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "provider-ref.sqlite")
    _, workflow, _, _, capability = _started(store, source_id="provider-ref")
    result = _step(
        store,
        workflow,
        kind="capability",
        extra=capability,
    )
    rebound_provenance = replace(
        result,
        provider_ref="codexia:different-provenance@9.9.9",
    )

    admitted = WorkflowProposalAdmissionService(store).admit(rebound_provenance)

    assert admitted is not None
    assert admitted.state is CapabilityNeedState.PENDING


def test_forged_reserved_raw_candidate_still_fails_g2_16(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "reserved.sqlite")
    work, workflow, pin, _, _ = _started(store, source_id="reserved")
    snapshot = store.snapshot(work.work_id)
    raw = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=snapshot,
        event_kind="role.future-core-event",
        payload={"probe": True},
    )
    forged = WorkflowStepResult(
        work_id=work.work_id,
        workflow_run_id=workflow.run.workflow_run_id,
        work_revision=snapshot.revision,
        work_event_digest=snapshot.last_event_digest,
        pack_binding_digest=pin.pack.binding_digest,
        workflow_binding_digest=workflow.run.binding.binding_digest,
        provider_ref=PROVIDER_REF,
        proposal=raw,
    )
    before = store.events(work.work_id)

    with pytest.raises(WorkflowImplementationOwnershipError):
        WorkflowProposalAdmissionService(store).admit(forged)

    assert store.events(work.work_id) == before


def test_admission_source_has_no_scheduler_host_provider_or_loop() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "workflow_orchestration" / "proposal_admission.py"
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
        "CapabilityHostBridge",
        "CognitionPort",
        "ModelProvider",
        "ProcessExecutor",
        "Scheduler",
    }
    assert forbidden.isdisjoint(imported_names)
    assert not any(
        module.endswith(
            (".providers", ".authority", ".execution", ".standalone_host")
        )
        for module in imported_modules
    )
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
