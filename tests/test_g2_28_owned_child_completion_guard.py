from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from codexia_manual_agent.delegation_core import (
    Delegation,
    DelegationAdmission,
    DelegationChildrenLiveError,
    DelegationCompletionGuard,
)
from codexia_manual_agent.work_core import (
    WORK_CANCELLED_EVENT,
    WORK_COMPLETED_EVENT,
    SqliteWorkStore,
    Work,
    WorkIngressBinding,
    WorkNotFoundError,
    WorkState,
    WorkStateError,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work(
    store: SqliteWorkStore,
    *,
    source_id: str,
    objective: str,
) -> Work:
    work = Work.create(
        objective=objective,
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    store.create(work)
    return work


def _delegate(
    store: SqliteWorkStore,
    parent: Work,
    *,
    objective: str,
) -> Delegation:
    return DelegationAdmission(store).admit(
        Delegation.create(
            parent=store.snapshot(parent.work_id),
            child_objective=objective,
        )
    )


def _completion_event(store: SqliteWorkStore, work_id: str):
    snapshot = store.snapshot(work_id)
    return snapshot.next_event(
        kind=WORK_COMPLETED_EVENT,
        payload={"summary": "semantic work complete"},
    )


def _complete(store: SqliteWorkStore, work_id: str):
    return DelegationCompletionGuard(store).admit(
        _completion_event(store, work_id)
    )


def _cancel(store: SqliteWorkStore, work_id: str) -> None:
    snapshot = store.snapshot(work_id)
    store.append(
        work_id,
        expected_revision=snapshot.revision,
        event=snapshot.next_event(
            kind=WORK_CANCELLED_EVENT,
            payload={"reason": "no longer needed"},
        ),
    )


def test_plain_append_cannot_publish_work_completed(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "raw-bypass.sqlite")
    work = _work(
        store,
        source_id="raw-bypass",
        objective="Prove completion has a specialized boundary",
    )
    event = _completion_event(store, work.work_id)

    with pytest.raises(
        WorkStateError,
        match="guarded append_completion boundary",
    ):
        store.append(
            work.work_id,
            expected_revision=0,
            event=event,
        )

    assert store.snapshot(work.work_id).state is WorkState.ACTIVE


def test_child_create_append_cannot_publish_work_completed(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "child-create-bypass.sqlite")
    parent = _work(
        store,
        source_id="child-create-bypass",
        objective="Parent must not complete while creating a live child",
    )
    child = Work.create(
        objective="New child must not be committed through completion",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id="child-create-bypass-child",
            payload_digest=_sha("payload:child-create-bypass-child"),
        ),
    )
    completion = _completion_event(store, parent.work_id)
    before = store.events(parent.work_id)

    with pytest.raises(
        WorkStateError,
        match="guarded append_completion boundary",
    ):
        store.append_with_child_create(
            parent.work_id,
            expected_revision=0,
            event=completion,
            child_work=child,
        )

    assert store.events(parent.work_id) == before
    assert store.snapshot(parent.work_id).state is WorkState.ACTIVE
    with pytest.raises(WorkNotFoundError):
        store.snapshot(child.work_id)


def test_parent_completion_fails_while_owned_child_is_active(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "active-child.sqlite")
    parent = _work(
        store,
        source_id="active-child",
        objective="Parent objective",
    )
    delegation = _delegate(
        store,
        parent,
        objective="Child must finish independently",
    )
    completion = _completion_event(store, parent.work_id)
    before = store.events(parent.work_id)

    with pytest.raises(DelegationChildrenLiveError) as exc_info:
        DelegationCompletionGuard(store).admit(completion)

    assert exc_info.value.child_work_ids == (
        delegation.child_work.work_id,
    )
    assert store.events(parent.work_id) == before
    assert store.snapshot(parent.work_id).state is WorkState.ACTIVE


def test_same_parent_completion_can_succeed_after_child_becomes_terminal(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "retry-after-child.sqlite")
    parent = _work(
        store,
        source_id="retry-after-child",
        objective="Parent waits for child",
    )
    delegation = _delegate(
        store,
        parent,
        objective="Child completes later",
    )
    completion = _completion_event(store, parent.work_id)
    guard = DelegationCompletionGuard(store)

    with pytest.raises(DelegationChildrenLiveError):
        guard.admit(completion)

    _complete(store, delegation.child_work.work_id)
    admitted = guard.admit(completion)

    assert admitted.state is WorkState.COMPLETED
    assert admitted.terminal_event_id == completion.event_id


def test_completed_and_cancelled_children_both_satisfy_parent_guard(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "terminal-children.sqlite")
    parent = _work(
        store,
        source_id="terminal-children",
        objective="Parent owns two children",
    )
    completed_child = _delegate(
        store,
        parent,
        objective="Finish normally",
    )
    cancelled_child = _delegate(
        store,
        parent,
        objective="May be cancelled",
    )

    _complete(store, completed_child.child_work.work_id)
    _cancel(store, cancelled_child.child_work.work_id)

    admitted = _complete(store, parent.work_id)

    assert admitted.state is WorkState.COMPLETED
    assert (
        store.snapshot(completed_child.child_work.work_id).state
        is WorkState.COMPLETED
    )
    assert (
        store.snapshot(cancelled_child.child_work.work_id).state
        is WorkState.CANCELLED
    )


def test_one_live_child_blocks_parent_even_if_other_children_are_terminal(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "mixed.sqlite")
    parent = _work(
        store,
        source_id="mixed",
        objective="Parent has mixed child states",
    )
    completed_child = _delegate(
        store,
        parent,
        objective="Terminal child",
    )
    live_child = _delegate(
        store,
        parent,
        objective="Still live",
    )
    _complete(store, completed_child.child_work.work_id)
    completion = _completion_event(store, parent.work_id)

    with pytest.raises(DelegationChildrenLiveError) as exc_info:
        DelegationCompletionGuard(store).admit(completion)

    assert exc_info.value.child_work_ids == (live_child.child_work.work_id,)
    assert store.snapshot(parent.work_id).state is WorkState.ACTIVE


def test_nested_delegation_blocks_child_then_parent_without_work_graph(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "nested.sqlite")
    parent = _work(
        store,
        source_id="nested",
        objective="Root Work",
    )
    child_relation = _delegate(
        store,
        parent,
        objective="Child Work",
    )
    child = child_relation.child_work
    grandchild_relation = _delegate(
        store,
        child,
        objective="Grandchild Work",
    )
    child_completion = _completion_event(store, child.work_id)

    with pytest.raises(DelegationChildrenLiveError) as exc_info:
        DelegationCompletionGuard(store).admit(child_completion)

    assert exc_info.value.child_work_ids == (
        grandchild_relation.child_work.work_id,
    )

    _cancel(store, grandchild_relation.child_work.work_id)
    child_done = DelegationCompletionGuard(store).admit(child_completion)
    assert child_done.state is WorkState.COMPLETED

    parent_done = _complete(store, parent.work_id)
    assert parent_done.state is WorkState.COMPLETED


def test_exact_parent_completion_retry_is_idempotent(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    parent = _work(
        store,
        source_id="retry",
        objective="Retry terminal admission",
    )
    child = _delegate(
        store,
        parent,
        objective="Complete before parent",
    )
    _complete(store, child.child_work.work_id)
    completion = _completion_event(store, parent.work_id)
    guard = DelegationCompletionGuard(store)

    first = guard.admit(completion)
    events_after_first = store.events(parent.work_id)
    second = guard.admit(completion)

    assert second == first
    assert store.events(parent.work_id) == events_after_first


def test_g2_28_does_not_introduce_completion_claim_artifact_or_cleanup_policy() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "delegation_core" / "completion.py",
        root / "work_core" / "store.py",
    ]
    forbidden_names = {
        "CompletionClaim",
        "WorkCompletion",
        "ArtifactRef",
        "EvidenceRef",
        "Scheduler",
        "CleanupProof",
        "ProcessExecutor",
        "AuthorizationReceipt",
    }
    forbidden_modules = {
        "artifact_core",
        "evidence_core",
        "authority",
        "execution",
    }

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_names: set[str] = set()
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    imported_modules.add(node.module)
                imported_names.update(alias.name for alias in node.names)

        assert forbidden_names.isdisjoint(imported_names)
        assert not any(
            any(part in module for part in forbidden_modules)
            for module in imported_modules
        )
        assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
