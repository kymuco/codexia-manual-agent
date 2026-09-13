from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from codexia_manual_agent.work.admission import (
    ContinuationAdmission,
    ContinuationProposal,
)
from codexia_manual_agent.work.attention import DynamicAttentionDecision
from codexia_manual_agent.work.chat_peer import (
    ChatGPTPeerLoop,
    ChatPeerMessageOrigin,
    PeerConversationChangedError,
)
from codexia_manual_agent.work.supervisor import (
    BackgroundWorkSupervisor,
    SupervisorStateError,
    SupervisorStatus,
    SupervisorWorkSnapshot,
)

MAX_SUPERVISOR_DRIVE_STEPS = 128

SupervisorCheckpoint = tuple[
    ContinuationProposal,
    ContinuationAdmission,
    DynamicAttentionDecision,
]
SupervisorCheckpointSource = Callable[
    [SupervisorWorkSnapshot, ChatGPTPeerLoop],
    SupervisorCheckpoint | None,
]


class SupervisorDriveStop(StrEnum):
    NO_CHECKPOINT = "no_checkpoint"
    WAITING_HUMAN = "waiting_human"
    IN_FLIGHT_AMBIGUOUS = "in_flight_ambiguous"
    COMPLETED = "completed"
    STEP_BUDGET = "step_budget"


@dataclass(frozen=True, slots=True)
class SupervisorDriveResult:
    work_id: str
    stop: SupervisorDriveStop
    snapshot: SupervisorWorkSnapshot
    steps: int
    provider_turns: int


class BackgroundWorkDriver:
    """Bounded event-driven pump over one durable delegated work.

    The driver owns no semantic admission, attention, or execution authority.  It
    only connects exact supervisor readiness state to an injected Codexia-side
    checkpoint source and the already-governed M6.3 peer transport.
    """

    def __init__(self, supervisor: BackgroundWorkSupervisor) -> None:
        if not isinstance(supervisor, BackgroundWorkSupervisor):
            raise SupervisorStateError(
                "supervisor must be a BackgroundWorkSupervisor"
            )
        self.supervisor = supervisor

    def drive_chat_until_blocked(
        self,
        work_id: str,
        *,
        peer_loop: ChatGPTPeerLoop,
        checkpoint_source: SupervisorCheckpointSource,
        max_steps: int = 32,
    ) -> SupervisorDriveResult:
        if not isinstance(peer_loop, ChatGPTPeerLoop):
            raise SupervisorStateError("peer_loop must be a ChatGPTPeerLoop")
        if not callable(checkpoint_source):
            raise SupervisorStateError("checkpoint_source must be callable")
        if type(max_steps) is not int or not 1 <= max_steps <= MAX_SUPERVISOR_DRIVE_STEPS:
            raise SupervisorStateError(
                f"max_steps must be an integer from 1 to {MAX_SUPERVISOR_DRIVE_STEPS}"
            )

        provider_turns = 0
        for step in range(max_steps):
            snapshot = self.supervisor.recover(work_id)

            if snapshot.status is SupervisorStatus.COMPLETED:
                return self._result(
                    snapshot,
                    SupervisorDriveStop.COMPLETED,
                    step,
                    provider_turns,
                )

            if snapshot.status is SupervisorStatus.IN_FLIGHT:
                reconciled = self.supervisor.reconcile_in_flight_chat(
                    work_id,
                    peer_loop=peer_loop,
                )
                if reconciled.last_sequence == snapshot.last_sequence:
                    return self._result(
                        reconciled,
                        SupervisorDriveStop.IN_FLIGHT_AMBIGUOUS,
                        step + 1,
                        provider_turns,
                    )
                continue

            observation = peer_loop.observe(snapshot.cursor)
            if observation.messages:
                if snapshot.status is SupervisorStatus.WAITING_HUMAN and not any(
                    item.origin is ChatPeerMessageOrigin.EXTERNAL_USER
                    for item in observation.messages
                ):
                    return self._result(
                        snapshot,
                        SupervisorDriveStop.WAITING_HUMAN,
                        step + 1,
                        provider_turns,
                    )
                self.supervisor.record_external_observation(
                    work_id,
                    observation=observation,
                )
                continue

            if snapshot.status is SupervisorStatus.WAITING_HUMAN:
                return self._result(
                    snapshot,
                    SupervisorDriveStop.WAITING_HUMAN,
                    step + 1,
                    provider_turns,
                )

            if snapshot.status is SupervisorStatus.PREPARED:
                lease = self.supervisor.claim_dispatch(work_id)
                try:
                    self.supervisor.execute_claimed_chat(lease, peer_loop)
                except PeerConversationChangedError:
                    # The durable claim already prevents replay.  A conversation
                    # race here is therefore a reconciliation boundary, not retry
                    # permission.
                    return self._result(
                        self.supervisor.recover(work_id),
                        SupervisorDriveStop.IN_FLIGHT_AMBIGUOUS,
                        step + 1,
                        provider_turns,
                    )
                provider_turns += 1
                continue

            if snapshot.status is not SupervisorStatus.READY:
                raise SupervisorStateError(
                    f"Unsupported supervisor drive state: {snapshot.status.value}"
                )

            checkpoint = checkpoint_source(snapshot, peer_loop)
            if checkpoint is None:
                return self._result(
                    snapshot,
                    SupervisorDriveStop.NO_CHECKPOINT,
                    step + 1,
                    provider_turns,
                )
            if not isinstance(checkpoint, tuple) or len(checkpoint) != 3:
                raise SupervisorStateError(
                    "checkpoint_source must return (proposal, admission, attention) or None"
                )
            proposal, admission, attention = checkpoint
            self.supervisor.record_checkpoint(
                work_id,
                proposal=proposal,
                admission=admission,
                attention=attention,
            )

        snapshot = self.supervisor.recover(work_id)
        return self._result(
            snapshot,
            SupervisorDriveStop.STEP_BUDGET,
            max_steps,
            provider_turns,
        )

    @staticmethod
    def _result(
        snapshot: SupervisorWorkSnapshot,
        stop: SupervisorDriveStop,
        steps: int,
        provider_turns: int,
    ) -> SupervisorDriveResult:
        return SupervisorDriveResult(
            work_id=snapshot.work_id,
            stop=stop,
            snapshot=snapshot,
            steps=steps,
            provider_turns=provider_turns,
        )
