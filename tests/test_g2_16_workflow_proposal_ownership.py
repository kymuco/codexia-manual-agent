from __future__ import annotations

import hashlib

import pytest

from codexia_manual_agent.capability_core import CapabilityNeed
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.role_core import ContextProjection, RoleBinding, RoleRun
from codexia_manual_agent.work_core import SqliteWorkStore, Work, WorkIngressBinding
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowCandidate,
    WorkflowRun,
)
from codexia_manual_agent.workflow_runtime import (
    WorkflowImplementationBoundary,
    WorkflowImplementationOwnershipError,
    WorkflowStepContext,
    standalone_process_capability_binding,
    standalone_process_workflow_binding,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _role_binding() -> RoleBinding:
    return RoleBinding.create(
        role_id="codexia:g2.16-reviewer",
        version="1.0.0",
        instructions_digest=_sha("review exact evidence"),
    )


def _context(store: SqliteWorkStore) -> tuple[WorkflowStepContext, RoleBinding]:
    workflow_binding = standalone_process_workflow_binding()
    capability_binding = standalone_process_capability_binding()
    role_binding = _role_binding()
    pack = PackBinding.create(
        pack_id="codexia:g2.16-pack",
        version="1.0.0",
        definition_digest=_sha("g2.16-pack"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow_binding.workflow_id,
                version=workflow_binding.version,
                binding_digest=workflow_binding.binding_digest,
            ),
            PackMemberBinding.create(
                kind=PackMemberKind.CAPABILITY,
                semantic_id=capability_binding.capability_id,
                version=capability_binding.version,
                binding_digest=capability_binding.binding_digest,
            ),
            PackMemberBinding.create(
                kind=PackMemberKind.ROLE,
                semantic_id=role_binding.role_id,
                version=role_binding.version,
                binding_digest=role_binding.binding_digest,
            ),
        ),
    )
    work = Work.create(
        objective="Prove Workflow proposal ownership",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="g2.16",
            payload_digest=_sha("g2.16-payload"),
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
    return (
        WorkflowStepContext(
            work=store.snapshot(work.work_id),
            workflow=workflow,
            pack_binding=pin,
        ),
        role_binding,
    )


def test_typed_role_proposal_is_allowed_but_raw_role_candidate_is_rejected(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    context, role_binding = _context(store)
    role = RoleRun.create(
        workflow=context.workflow,
        snapshot=context.work,
        binding=role_binding,
        context=ContextProjection.create(content_digest=_sha("role-context")),
    )

    class TypedRoleImplementation:
        binding = standalone_process_workflow_binding()

        def propose(self, _context):
            return role

    typed = WorkflowImplementationBoundary().prepare(
        TypedRoleImplementation(),
        context,
    )
    assert typed == role

    class RawRoleCandidateImplementation:
        binding = standalone_process_workflow_binding()

        def propose(self, _context):
            return role.to_start_candidate()

    before = store.events(context.work.work.work_id)
    with pytest.raises(WorkflowImplementationOwnershipError):
        WorkflowImplementationBoundary().prepare(
            RawRoleCandidateImplementation(),
            context,
        )
    assert store.events(context.work.work.work_id) == before


def test_typed_capability_is_allowed_but_raw_need_candidate_is_rejected(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    context, _ = _context(store)
    need = CapabilityNeed.create(
        workflow=context.workflow,
        snapshot=context.work,
        binding=standalone_process_capability_binding(),
        operation="run",
        parameters={
            "argv": ["python", "-V"],
            "cwd_ref": "workspace",
            "cwd": ".",
            "limits": {
                "timeout_seconds": 30.0,
                "max_stdout_bytes": 65_536,
                "max_stderr_bytes": 65_536,
            },
        },
    )

    class TypedNeedImplementation:
        binding = standalone_process_workflow_binding()

        def propose(self, _context):
            return need

    typed = WorkflowImplementationBoundary().prepare(
        TypedNeedImplementation(),
        context,
    )
    assert typed == need

    class RawNeedCandidateImplementation:
        binding = standalone_process_workflow_binding()

        def propose(self, _context):
            return need.to_workflow_candidate()

    before = store.events(context.work.work.work_id)
    with pytest.raises(WorkflowImplementationOwnershipError):
        WorkflowImplementationBoundary().prepare(
            RawNeedCandidateImplementation(),
            context,
        )
    assert store.events(context.work.work.work_id) == before


@pytest.mark.parametrize(
    "kind",
    [
        "role.future-core-event",
        "capability.future-core-event",
        "pack.future-core-event",
    ],
)
def test_reserved_core_namespace_is_future_closed(kind: str, tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / f"{kind.split('.')[0]}.sqlite")
    context, _ = _context(store)
    candidate = WorkflowCandidate.create(
        run_snapshot=context.workflow,
        work_snapshot=context.work,
        event_kind=kind,
        payload={"probe": True},
    )

    class Implementation:
        binding = standalone_process_workflow_binding()

        def propose(self, _context):
            return candidate

    with pytest.raises(WorkflowImplementationOwnershipError):
        WorkflowImplementationBoundary().prepare(Implementation(), context)


@pytest.mark.parametrize("kind", ["workflow.note", "domain.reviewed"])
def test_workflow_and_domain_candidate_namespaces_remain_available(
    kind: str,
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / f"{kind.replace('.', '-')}.sqlite")
    context, _ = _context(store)
    candidate = WorkflowCandidate.create(
        run_snapshot=context.workflow,
        work_snapshot=context.work,
        event_kind=kind,
        payload={"allowed": True},
    )

    class Implementation:
        binding = standalone_process_workflow_binding()

        def propose(self, _context):
            return candidate

    assert (
        WorkflowImplementationBoundary().prepare(Implementation(), context)
        == candidate
    )
