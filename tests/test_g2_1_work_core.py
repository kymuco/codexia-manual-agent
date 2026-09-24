from __future__ import annotations

import hashlib
import sqlite3

import pytest

from codexia_manual_agent.work_core import (
    WORK_CANCELLED_EVENT,
    WORK_COMPLETED_EVENT,
    InvalidWorkCoreRecord,
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkIngressBinding,
    WorkIngressConflictError,
    WorkPersistenceIntegrityError,
    WorkState,
    WorkStateError,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work(
    *,
    source_id: str = "request-1",
    payload: str = "payload",
    objective: str = "Do the work",
) -> Work:
    ingress = WorkIngressBinding.create(
        source_namespace="standalone.api",
        source_id=source_id,
        payload_digest=_sha(payload),
    )
    return Work.create(objective=objective, ingress=ingress)


def test_create_is_idempotent_for_same_exact_ingress(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    first = _work()
    second = _work()

    first_snapshot = store.create(first)
    second_snapshot = store.create(second)

    assert first_snapshot.work.work_id == first.work_id
    assert second_snapshot.work.work_id == first.work_id
    assert second_snapshot.revision == 0
    assert second_snapshot.state is WorkState.ACTIVE


def test_same_ingress_identity_with_changed_payload_fails_closed(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    store.create(_work(payload="v1"))

    with pytest.raises(WorkIngressConflictError):
        store.create(_work(payload="v2"))


def test_same_exact_ingress_with_changed_objective_fails_closed(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    store.create(_work(objective="Original objective"))

    with pytest.raises(WorkIngressConflictError):
        store.create(_work(objective="Changed objective"))


def test_compare_and_append_binds_exact_snapshot(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    initial = store.create(_work())

    completed = initial.next_event(
        kind=WORK_COMPLETED_EVENT,
        payload={"summary": "done"},
    )
    result = store.append_completion(
        initial.work.work_id,
        expected_revision=initial.revision,
        event=completed,
    )

    assert result.revision == 1
    assert result.state is WorkState.COMPLETED
    assert result.last_event_digest == completed.event_digest
    assert result.terminal_event_id == completed.event_id


def test_stale_parallel_candidate_is_rejected(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    initial = store.create(_work())

    first = initial.next_event(kind="work.progress", payload={"step": 1})
    stale = initial.next_event(kind=WORK_COMPLETED_EVENT)

    after_first = store.append(
        initial.work.work_id,
        expected_revision=0,
        event=first,
    )
    assert after_first.revision == 1

    with pytest.raises(WorkConcurrencyError):
        store.append_completion(
            initial.work.work_id,
            expected_revision=0,
            event=stale,
        )


def test_exact_event_retry_is_idempotent_after_ambiguous_ack(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    initial = store.create(_work())

    event = initial.next_event(kind="work.progress", payload={"step": "inspect"})
    first = store.append(
        initial.work.work_id,
        expected_revision=0,
        event=event,
    )
    retried = store.append(
        initial.work.work_id,
        expected_revision=0,
        event=event,
    )

    assert first.revision == 1
    assert retried.revision == 1
    assert store.events(initial.work.work_id) == (event,)


def test_event_chain_survives_store_restart(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    initial = store.create(_work())
    first = initial.next_event(kind="work.progress", payload={"n": 1})
    after_first = store.append(initial.work.work_id, expected_revision=0, event=first)
    second = after_first.next_event(kind="work.progress", payload={"n": 2})
    expected = store.append(initial.work.work_id, expected_revision=1, event=second)

    recovered = SqliteWorkStore(path).snapshot(initial.work.work_id)

    assert recovered == expected
    assert recovered.revision == 2
    assert recovered.last_event_digest == second.event_digest


def test_terminal_snapshot_cannot_prepare_another_event(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    initial = store.create(_work())
    cancelled = initial.next_event(kind=WORK_CANCELLED_EVENT)
    terminal = store.append(initial.work.work_id, expected_revision=0, event=cancelled)

    with pytest.raises(InvalidWorkCoreRecord):
        terminal.next_event(kind="work.progress")


def test_store_rejects_event_after_terminal_work(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    initial = store.create(_work())
    terminal_event = initial.next_event(kind=WORK_COMPLETED_EVENT)
    store.append_completion(
        initial.work.work_id,
        expected_revision=0,
        event=terminal_event,
    )

    forged = terminal_event.create(
        work_id=initial.work.work_id,
        sequence=2,
        kind="work.progress",
        previous_event_digest=terminal_event.event_digest,
    )
    with pytest.raises(WorkStateError):
        store.append(initial.work.work_id, expected_revision=1, event=forged)


def test_tampered_event_payload_fails_recovery(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    initial = store.create(_work())
    event = initial.next_event(kind="work.progress", payload={"trusted": True})
    store.append(initial.work.work_id, expected_revision=0, event=event)

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE g2_work_event_v1
            SET payload_json = ?
            WHERE event_id = ?
            """,
            ('{"trusted":false}', event.event_id),
        )

    with pytest.raises(WorkPersistenceIntegrityError):
        SqliteWorkStore(path).snapshot(initial.work.work_id)


def test_cancelled_work_remains_terminal_after_restart(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    initial = store.create(_work())
    cancelled = initial.next_event(
        kind=WORK_CANCELLED_EVENT,
        payload={"reason": "operator cancellation"},
    )
    store.append(initial.work.work_id, expected_revision=0, event=cancelled)

    recovered = SqliteWorkStore(path).snapshot(initial.work.work_id)

    assert recovered.state is WorkState.CANCELLED
    assert recovered.terminal_event_id == cancelled.event_id
