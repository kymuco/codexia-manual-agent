from __future__ import annotations

import hmac

from codexia_manual_agent.completion_core.projection import (
    project_admitted_completion_claim,
)
from codexia_manual_agent.completion_core.work_completion import WorkCompletion
from codexia_manual_agent.completion_core.work_completion_projection import (
    project_work_completion,
)
from codexia_manual_agent.delegation_core.completion import (
    DelegationCompletionGuard,
)
from codexia_manual_agent.work_core import (
    WorkConcurrencyError,
    WorkSnapshot,
    WorkState,
    WorkStore,
)


class WorkCompletionAdmissionError(RuntimeError):
    """Base failure for Gen2 terminal WorkCompletion admission."""


class WorkCompletionBindingError(WorkCompletionAdmissionError):
    """WorkCompletion changed exact Work or admitted-claim semantics."""


class WorkCompletionStateError(WorkCompletionAdmissionError):
    """WorkCompletion cannot be admitted from recovered Work state."""


class WorkCompletionIdentityConflictError(WorkCompletionAdmissionError):
    """Terminal WorkCompletion differs from already canonical completion."""


class WorkCompletionAdmissionService:
    """Admit one exact WorkCompletion through the owned-child terminal guard.

    The service requires the selected completion.claim-admitted event to be the
    exact current Work head. Therefore any intervening Work event invalidates
    terminal completion. The existing DelegationCompletionGuard remains the
    only component that can publish work.completed through the private store
    primitive.

    This boundary owns no Pack completion criterion, artifact/evidence meaning,
    cleanup semantics, execution authority, scheduling, or parent-HDE state.
    """

    def __init__(self, store: WorkStore) -> None:
        self._store = store
        self._guard = DelegationCompletionGuard(store)  # type: ignore[arg-type]

    def admit(self, completion: WorkCompletion) -> WorkSnapshot:
        if not isinstance(completion, WorkCompletion):
            raise TypeError("completion must be WorkCompletion")

        events = self._store.events(completion.work_id)
        existing = project_work_completion(events)
        if existing is not None:
            if existing != completion:
                raise WorkCompletionIdentityConflictError(
                    "Work is already completed by different exact WorkCompletion"
                )
            snapshot = self._store.snapshot(completion.work_id)
            if (
                snapshot.state is not WorkState.COMPLETED
                or snapshot.terminal_event_id != completion.completion_id
            ):
                raise WorkCompletionStateError(
                    "Recovered WorkCompletion does not match terminal Work state"
                )
            return snapshot

        current = self._store.snapshot(completion.work_id)
        if current.state is not WorkState.ACTIVE:
            raise WorkCompletionStateError(
                "WorkCompletion requires active Work"
            )
        if current.work.work_id != completion.work_id:
            raise WorkCompletionBindingError(
                "WorkCompletion crossed Work identity"
            )
        if not hmac.compare_digest(
            current.work.work_digest,
            completion.work_digest,
        ):
            raise WorkCompletionBindingError(
                "WorkCompletion changed Work binding"
            )
        if current.revision != completion.claim_admission_sequence:
            raise WorkConcurrencyError(
                "WorkCompletion is not based on exact current claim-admission revision"
            )
        if (
            current.last_event_digest
            != completion.claim_admission_event_digest
        ):
            raise WorkConcurrencyError(
                "WorkCompletion is not based on exact current claim-admission chronology"
            )
        if not events:
            raise WorkCompletionBindingError(
                "WorkCompletion requires durable CompletionClaim admission"
            )

        head = events[-1]
        if head.sequence != completion.claim_admission_sequence:
            raise WorkCompletionBindingError(
                "WorkCompletion claim admission is not current Work head"
            )
        if head.event_id != completion.claim_id:
            raise WorkCompletionBindingError(
                "WorkCompletion changed latest admitted CompletionClaim identity"
            )
        if head.event_digest != completion.claim_admission_event_digest:
            raise WorkCompletionBindingError(
                "WorkCompletion changed latest claim-admission event digest"
            )

        try:
            claim = project_admitted_completion_claim(
                events,
                completion.claim_id,
            )
        except RuntimeError as exc:
            raise WorkCompletionBindingError(
                "WorkCompletion references unknown admitted CompletionClaim"
            ) from exc

        if not hmac.compare_digest(
            claim.claim_digest,
            completion.claim_digest,
        ):
            raise WorkCompletionBindingError(
                "WorkCompletion changed admitted CompletionClaim digest"
            )
        if claim.work_id != completion.work_id:
            raise WorkCompletionBindingError(
                "WorkCompletion CompletionClaim belongs to another Work"
            )
        if not hmac.compare_digest(
            claim.work_digest,
            completion.work_digest,
        ):
            raise WorkCompletionBindingError(
                "WorkCompletion CompletionClaim changed Work binding"
            )

        admitted = self._guard.admit(completion.to_event())
        recovered = project_work_completion(
            self._store.events(completion.work_id)
        )
        if recovered != completion:
            raise WorkCompletionBindingError(
                "guarded terminal admission recovered different WorkCompletion"
            )
        return admitted
