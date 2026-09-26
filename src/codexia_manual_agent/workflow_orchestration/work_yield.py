from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codexia_manual_agent.attention_core import (
    ATTENTION_NEED_DECLARED_EVENT,
    AttentionNeed,
    project_attention_need,
)
from codexia_manual_agent.completion_core import (
    WorkCompletion,
    project_work_completion,
)
from codexia_manual_agent.work_core import (
    WORK_COMPLETED_EVENT,
    WorkEvent,
    WorkSnapshot,
    WorkState,
    WorkStore,
)


class DurableWorkYieldProjectionError(RuntimeError):
    """Durable Work chronology cannot be projected to one exact yield frontier."""


class DurableWorkYieldKind(StrEnum):
    NONE = "none"
    COMPLETION = "completion"
    ATTENTION = "attention"


@dataclass(frozen=True, slots=True)
class DurableWorkYield:
    """Ephemeral projection of one exact durable Work return frontier.

    NONE is not a durable Work state. It only means that the observed exact
    chronology does not currently expose a WorkCompletion or current-head
    AttentionNeed suitable for a host return boundary.
    """

    kind: DurableWorkYieldKind
    snapshot: WorkSnapshot
    completion: WorkCompletion | None = None
    attention: AttentionNeed | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not DurableWorkYieldKind:
            raise TypeError("kind must be an exact DurableWorkYieldKind")
        if not isinstance(self.snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")

        if self.kind is DurableWorkYieldKind.NONE:
            if self.completion is not None or self.attention is not None:
                raise ValueError("NONE yield cannot carry durable return material")
            return

        if self.kind is DurableWorkYieldKind.COMPLETION:
            if not isinstance(self.completion, WorkCompletion):
                raise ValueError("COMPLETION yield requires exact WorkCompletion")
            if self.attention is not None:
                raise ValueError("COMPLETION yield cannot carry AttentionNeed")
            return

        if not isinstance(self.attention, AttentionNeed):
            raise ValueError("ATTENTION yield requires exact AttentionNeed")
        if self.completion is not None:
            raise ValueError("ATTENTION yield cannot carry WorkCompletion")


def project_durable_work_yield(
    work_id: str,
    *,
    store: WorkStore,
) -> DurableWorkYield:
    """Project the current durable host-return frontier without progressing Work."""

    if type(work_id) is not str or not work_id:
        raise ValueError("work_id must be non-empty text")

    snapshot = store.snapshot(work_id)
    events = store.events(work_id)
    _validate_exact_observation(work_id, snapshot, events)

    if snapshot.state is WorkState.COMPLETED:
        completion = project_work_completion(events)
        if completion is None:
            raise DurableWorkYieldProjectionError(
                "COMPLETED Work has no exact WorkCompletion"
            )
        head = events[-1]
        if (
            head.kind != WORK_COMPLETED_EVENT
            or head.event_id != completion.completion_id
            or head.sequence != snapshot.revision
            or head.event_digest != snapshot.last_event_digest
            or snapshot.terminal_event_id != completion.completion_id
        ):
            raise DurableWorkYieldProjectionError(
                "WorkCompletion is not the exact terminal Work chronology head"
            )
        return DurableWorkYield(
            kind=DurableWorkYieldKind.COMPLETION,
            snapshot=snapshot,
            completion=completion,
        )

    if (
        snapshot.state is WorkState.ACTIVE
        and events
        and events[-1].kind == ATTENTION_NEED_DECLARED_EVENT
    ):
        head = events[-1]
        need = project_attention_need(events, head.event_id)
        if (
            need.work_id != snapshot.work.work_id
            or need.work_digest != snapshot.work.work_digest
            or head.event_id != need.attention_id
            or head.sequence != snapshot.revision
            or head.event_digest != snapshot.last_event_digest
        ):
            raise DurableWorkYieldProjectionError(
                "AttentionNeed is not the exact current Work chronology head"
            )
        return DurableWorkYield(
            kind=DurableWorkYieldKind.ATTENTION,
            snapshot=snapshot,
            attention=need,
        )

    return DurableWorkYield(
        kind=DurableWorkYieldKind.NONE,
        snapshot=snapshot,
    )


def _validate_exact_observation(
    expected_work_id: str,
    snapshot: WorkSnapshot,
    events: tuple[WorkEvent, ...],
) -> None:
    if snapshot.work.work_id != expected_work_id:
        raise DurableWorkYieldProjectionError(
            "Work snapshot crossed requested Work identity"
        )
    if any(event.work_id != expected_work_id for event in events):
        raise DurableWorkYieldProjectionError(
            "durable chronology crossed requested Work identity"
        )
    if snapshot.revision != len(events):
        raise DurableWorkYieldProjectionError(
            "Work snapshot revision does not match durable chronology length"
        )
    if snapshot.revision == 0:
        if events or snapshot.last_event_digest is not None:
            raise DurableWorkYieldProjectionError(
                "revision-zero Work has inconsistent durable chronology"
            )
        return

    if not events:
        raise DurableWorkYieldProjectionError(
            "nonzero Work revision has no durable chronology"
        )
    head = events[-1]
    if (
        head.work_id != snapshot.work.work_id
        or head.sequence != snapshot.revision
        or head.event_digest != snapshot.last_event_digest
    ):
        raise DurableWorkYieldProjectionError(
            "Work snapshot does not bind exact durable chronology head"
        )
