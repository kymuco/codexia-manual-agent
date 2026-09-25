from __future__ import annotations

from typing import Protocol

from codexia_manual_agent.delegation_core.projection import project_delegations
from codexia_manual_agent.work_core import (
    WORK_COMPLETED_EVENT,
    WorkConcurrencyError,
    WorkEvent,
    WorkSnapshot,
    WorkState,
    WorkStore,
)


class _CompletionStorePort(WorkStore, Protocol):
    """Private WorkStore extension owned by semantic completion admission."""

    def _append_completion(
        self,
        work_id: str,
        *,
        expected_revision: int,
        event: WorkEvent,
        read_preconditions: tuple[WorkSnapshot, ...] = (),
    ) -> WorkSnapshot: ...


class DelegationCompletionError(RuntimeError):
    """Base failure for the owned-child parent completion guard."""


class DelegationCompletionBindingError(DelegationCompletionError):
    """Completion input no longer binds exact parent/child semantics."""


class DelegationChildrenLiveError(DelegationCompletionError):
    """Parent Work still owns at least one non-terminal child Work."""

    def __init__(self, child_work_ids: tuple[str, ...]) -> None:
        self.child_work_ids = child_work_ids
        joined = ", ".join(child_work_ids)
        super().__init__(
            f"parent Work cannot complete while owned children remain live: {joined}"
        )


class _DelegationCompletionGuard:
    """Private low-level child guard for one prepared work.completed event.

    The public semantic terminal surface is completion_core.WorkCompletionAdmissionService.
    This private boundary owns no completion-quality judgment, evidence
    interpretation, artifact policy, scheduler, execution, or cleanup semantics.
    It only enforces
    the frozen Delegation invariant:

        parent terminal completion
        requires every owned child Work to be COMPLETED or CANCELLED.
    """

    def __init__(self, store: _CompletionStorePort) -> None:
        self._store = store

    def admit(self, event: WorkEvent) -> WorkSnapshot:
        if not isinstance(event, WorkEvent):
            raise TypeError("event must be WorkEvent")
        if event.kind != WORK_COMPLETED_EVENT:
            raise DelegationCompletionBindingError(
                "DelegationCompletionGuard accepts only work.completed"
            )

        events = self._store.events(event.work_id)
        expected_revision = event.sequence - 1
        if expected_revision > len(events):
            raise WorkConcurrencyError(
                "completion event is derived from an unavailable future revision"
            )

        observed_events = events[:expected_revision]
        observed_digest = (
            None if not observed_events else observed_events[-1].event_digest
        )
        if observed_digest != event.previous_event_digest:
            raise DelegationCompletionBindingError(
                "completion event does not bind exact parent chronology prefix"
            )

        terminal_children: list[WorkSnapshot] = []
        live_children: list[str] = []
        for delegation in project_delegations(observed_events):
            child = self._store.snapshot(delegation.child_work.work_id)
            if child.work.to_dict() != delegation.child_work.to_dict():
                raise DelegationCompletionBindingError(
                    "owned child Work changed exact Delegation binding"
                )
            if child.state not in {
                WorkState.COMPLETED,
                WorkState.CANCELLED,
            }:
                live_children.append(child.work.work_id)
                continue
            terminal_children.append(child)

        if live_children:
            raise DelegationChildrenLiveError(tuple(live_children))

        admitted = self._store._append_completion(
            event.work_id,
            expected_revision=expected_revision,
            event=event,
            read_preconditions=tuple(terminal_children),
        )
        if admitted.state is not WorkState.COMPLETED:
            raise DelegationCompletionBindingError(
                "guarded completion did not produce COMPLETED Work"
            )
        if admitted.terminal_event_id != event.event_id:
            raise DelegationCompletionBindingError(
                "guarded completion recovered different terminal identity"
            )
        return admitted
