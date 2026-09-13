from __future__ import annotations

import json
from contextlib import closing
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
    ChatPeerTurn,
    PeerConversationChangedError,
)
from codexia_manual_agent.work.contracts import InvalidWorkRecordError, _exact_keys
from codexia_manual_agent.work.supervisor import (
    BackgroundWorkSupervisor,
    SupervisorEventKind,
    SupervisorIntegrityError,
    SupervisorStateError,
    SupervisorStatus,
    SupervisorWorkSnapshot,
    _turn_from_dict,
)

MAX_SUPERVISOR_DRIVE_STEPS = 128

SupervisorCheckpoint = tuple[
    ContinuationProposal,
    ContinuationAdmission,
    DynamicAttentionDecision,
]
SupervisorCheckpointSource = Callable[
    [SupervisorWorkSnapshot, ChatGPTPeerLoop, ChatPeerTurn | None],
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

    The driver owns no semantic admission, attention, or execution authority. It
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
        if (
            type(max_steps) is not int
            or not 1 <= max_steps <= MAX_SUPERVISOR_DRIVE_STEPS
        ):
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

            observation = self.supervisor.capture_external_chat(
                work_id,
                peer_loop=peer_loop,
            )
            if observation.messages:
                if snapshot.status is SupervisorStatus.WAITING_HUMAN and not any(
                    item.origin is ChatPeerMessageOrigin.EXTERNAL_USER
                    for item in observation.messages
                ):
                    self.supervisor.discard_external_observation(
                        work_id,
                        observation=observation,
                    )
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

            self.supervisor.discard_external_observation(
                work_id,
                observation=observation,
            )

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
                    # The durable claim already prevents replay. A conversation
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

            latest_turn = self._latest_exact_peer_turn(snapshot)
            checkpoint = checkpoint_source(snapshot, peer_loop, latest_turn)
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

    def _latest_exact_peer_turn(
        self,
        snapshot: SupervisorWorkSnapshot,
    ) -> ChatPeerTurn | None:
        """Return only the peer turn that is the exact terminal durable event."""

        with closing(self.supervisor._connect()) as connection:
            row = connection.execute(
                """
                SELECT kind, payload_json
                FROM work_supervisor_events
                WHERE work_id = ? AND sequence = ?
                """,
                (snapshot.work_id, snapshot.last_sequence),
            ).fetchone()
        if row is None:
            raise SupervisorIntegrityError(
                "Supervisor terminal event disappeared while driving work"
            )
        if row["kind"] != SupervisorEventKind.PEER_TURN_RECORDED.value:
            return None
        try:
            payload = json.loads(row["payload_json"])
            value = _exact_keys(
                payload,
                {"dispatch_digest", "turn"},
                "Supervisor PEER_TURN_RECORDED payload",
            )
            turn = _turn_from_dict(value["turn"])
        except (InvalidWorkRecordError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SupervisorIntegrityError(
                "Terminal peer-turn event failed exact decoding"
            ) from exc
        if (
            turn.after_cursor.conversation_id != snapshot.cursor.conversation_id
            or turn.after_cursor.cursor_digest != snapshot.cursor.cursor_digest
            or turn.handoff_id != snapshot.handoff.handoff_id
            or turn.interpretation_id != snapshot.interpretation.interpretation_id
        ):
            raise SupervisorIntegrityError(
                "Terminal peer turn does not bind the recovered READY cursor"
            )
        confirmed = self.supervisor.recover(snapshot.work_id)
        if (
            confirmed.last_sequence != snapshot.last_sequence
            or confirmed.last_event_digest != snapshot.last_event_digest
        ):
            raise SupervisorStateError(
                "Delegated work advanced while deriving exact worker evidence"
            )
        return turn

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
