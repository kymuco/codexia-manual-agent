from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from codexia_manual_agent.delegation_core import (
    DELEGATION_CHILD_OWNED_EVENT,
    Delegation,
    DelegationAdmission,
    DelegationCompletionGuard,
    project_delegation,
    project_delegations,
)
from codexia_manual_agent.work_core import (
    WORK_COMPLETED_EVENT,
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkIngressBinding,
    WorkIngressConflictError,
    WorkNotFoundError,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
)
from codexia_manual_agent.workflow_runtime import (
    WorkflowImplementationOwnershipError,
    validate_generic_workflow_candidate_ownership,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parent(store: SqliteWorkStore, *, source_id: str) -> Work:
    work = Work.create(
        objective="Parent durable work",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    store.create(work)
    return work


def _delegation(
    store: SqliteWorkStore,
    parent: Work,
    *,
    objective: str = "Independent durable child objective",
) -> Delegation:
    return Delegation.create(
        parent=store.snapshot(parent.work_id),
        child_objective=objective,
    )


def test_owned_child_work_is_atomic_durable_and_restart_safe(tmp_path) -> None:
    path = tmp_path / "delegation.sqlite"
    store = SqliteWorkStore(path)
    parent = _parent(store, source_id="durable")
    delegation = _delegation(store, parent)

    admitted = DelegationAdmission(store).admit(delegation)

    assert admitted == delegation
    assert store.events(parent.work_id)[-1].kind == DELEGATION_CHILD_OWNED_EVENT
    child = store.snapshot(delegation.child_work.work_id)
    assert child.work == delegation.child_work
    assert child.state is WorkState.ACTIVE

    restarted = SqliteWorkStore(path)
    assert project_delegation(
        restarted.events(parent.work_id),
        delegation.delegation_id,
    ) == delegation
    assert restarted.snapshot(delegation.child_work.work_id).work == delegation.child_work


def test_exact_retry_after_commit_is_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    parent = _parent(store, source_id="retry")
    delegation = _delegation(store, parent)
    service = DelegationAdmission(store)

    first = service.admit(delegation)
    before_parent = store.events(parent.work_id)
    before_child = store.events(delegation.child_work.work_id)

    second = service.admit(delegation)

    assert second == first
    assert store.events(parent.work_id) == before_parent
    assert store.events(delegation.child_work.work_id) == before_child
    assert project_delegations(store.events(parent.work_id)) == (delegation,)


def test_stale_parent_revision_rolls_back_child_creation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    parent = _parent(store, source_id="stale")
    delegation = _delegation(store, parent)
    current = store.snapshot(parent.work_id)
    store.append(
        parent.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.external-observation",
            payload={"source": "concurrent"},
        ),
    )

    with pytest.raises(WorkConcurrencyError):
        DelegationAdmission(store).admit(delegation)

    with pytest.raises(WorkNotFoundError):
        store.snapshot(delegation.child_work.work_id)
    assert project_delegations(store.events(parent.work_id)) == ()


def test_existing_child_without_parent_event_is_not_adopted(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "no-adoption.sqlite")
    parent = _parent(store, source_id="no-adoption")
    delegation = _delegation(store, parent)
    store.create(delegation.child_work)
    before_parent = store.events(parent.work_id)

    with pytest.raises(WorkIngressConflictError):
        DelegationAdmission(store).admit(delegation)

    assert store.events(parent.work_id) == before_parent
    assert project_delegations(store.events(parent.work_id)) == ()


def test_child_work_has_independent_lifecycle_and_does_not_complete_parent(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "lifecycle.sqlite")
    parent = _parent(store, source_id="lifecycle")
    delegation = DelegationAdmission(store).admit(_delegation(store, parent))
    child = store.snapshot(delegation.child_work.work_id)
    completed = child.next_event(
        kind=WORK_COMPLETED_EVENT,
        payload={"summary": "child complete"},
    )
    DelegationCompletionGuard(store).admit(completed)

    assert store.snapshot(child.work.work_id).state is WorkState.COMPLETED
    assert store.snapshot(parent.work_id).state is WorkState.ACTIVE
    assert project_delegations(store.events(parent.work_id)) == (delegation,)


def test_child_ingress_is_exactly_bound_to_parent_and_delegation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "binding.sqlite")
    parent = _parent(store, source_id="binding")
    delegation = _delegation(store, parent)

    assert delegation.child_work.ingress.source_namespace == "codexia.delegation"
    assert delegation.child_work.ingress.source_id == delegation.delegation_id
    assert delegation.parent_work_id == parent.work_id
    assert delegation.parent_work_digest == parent.work_digest


def test_generic_workflow_candidate_cannot_manufacture_delegation_event(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "ownership.sqlite")
    parent = _parent(store, source_id="ownership")
    binding = WorkflowBinding.create(
        workflow_id="codexia:g2.25-workflow",
        version="1.0.0",
        definition_digest=_sha("g2.25-workflow"),
    )
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=store.snapshot(parent.work_id),
            binding=binding,
        )
    )
    candidate = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=store.snapshot(parent.work_id),
        event_kind="delegation.child-owned",
        payload={"bypass": True},
    )

    with pytest.raises(WorkflowImplementationOwnershipError):
        validate_generic_workflow_candidate_ownership(candidate)


def test_delegation_core_does_not_import_legacy_delegation_authority_or_scheduler() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    delegation = root / "delegation_core"
    imported_names: set[str] = set()
    imported_modules: set[str] = set()

    for path in delegation.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    imported_modules.add(node.module)
                imported_names.update(alias.name for alias in node.names)

    forbidden_names = {
        "Scheduler",
        "AuthorizationReceipt",
        "DelegationCoordinator",
        "DelegationBudget",
        "Capability",
    }
    assert forbidden_names.isdisjoint(imported_names)
    assert "codexia_manual_agent.delegation" not in imported_modules
    assert not any(
        module.endswith((".authority", ".execution", ".standalone_host"))
        for module in imported_modules
    )
