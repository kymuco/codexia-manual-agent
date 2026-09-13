from __future__ import annotations

import hmac
from contextvars import ContextVar
from threading import Lock
from typing import Any

from codexia_manual_agent.work.chat_peer import (
    ChatGPTPeerLoop,
    ChatPeerCursor,
    ChatPeerObservation,
    ChatPeerTurn,
)
from codexia_manual_agent.work.supervisor import (
    BackgroundWorkSupervisor,
    SupervisorStateError,
)

_MAX_LIVE_EVIDENCE = 4096
_registry_lock = Lock()
_verified_observations: dict[str, str] = {}
_verified_turns: dict[str, str] = {}
_reconciliation_commit: ContextVar[bool] = ContextVar(
    "m6_5_reconciliation_commit",
    default=False,
)


def _remember(registry: dict[str, str], digest: str, before_cursor_digest: str) -> None:
    with _registry_lock:
        if len(registry) >= _MAX_LIVE_EVIDENCE:
            registry.pop(next(iter(registry)))
        registry[digest] = before_cursor_digest


def _consume(registry: dict[str, str], digest: str) -> str | None:
    with _registry_lock:
        return registry.pop(digest, None)


def _install_peer_capture() -> None:
    if getattr(ChatGPTPeerLoop, "_m6_5_provenance_hardened", False):
        return

    original_observe = ChatGPTPeerLoop.observe
    original_continue = ChatGPTPeerLoop.continue_admitted

    def observe(
        self: ChatGPTPeerLoop,
        cursor: ChatPeerCursor,
    ) -> ChatPeerObservation:
        observation = original_observe(self, cursor)
        _remember(
            _verified_observations,
            observation.observation_digest,
            observation.before_cursor_digest,
        )
        return observation

    def continue_admitted(self: ChatGPTPeerLoop, **kwargs: Any) -> ChatPeerTurn:
        turn = original_continue(self, **kwargs)
        _remember(
            _verified_turns,
            turn.turn_digest,
            turn.before_cursor_digest,
        )
        return turn

    ChatGPTPeerLoop.observe = observe  # type: ignore[method-assign]
    ChatGPTPeerLoop.continue_admitted = continue_admitted  # type: ignore[method-assign]
    ChatGPTPeerLoop._m6_5_provenance_hardened = True  # type: ignore[attr-defined]


def _install_supervisor_guards() -> None:
    if getattr(BackgroundWorkSupervisor, "_m6_5_provenance_hardened", False):
        return

    original_record_external = BackgroundWorkSupervisor.record_external_observation
    original_record_peer_turn = BackgroundWorkSupervisor.record_peer_turn
    original_reconcile = BackgroundWorkSupervisor.reconcile_in_flight_chat

    def record_external_observation(
        self: BackgroundWorkSupervisor,
        work_id: str,
        *,
        observation: ChatPeerObservation,
    ):
        snapshot = self.recover(work_id)
        captured_before = _consume(
            _verified_observations,
            observation.observation_digest,
        )
        if captured_before is None or not hmac.compare_digest(
            captured_before,
            snapshot.cursor.cursor_digest,
        ):
            raise SupervisorStateError(
                "External observation must come from one fresh live M6.3 observe() "
                "call at the exact supervisor cursor"
            )
        return original_record_external(
            self,
            work_id,
            observation=observation,
        )

    def observe_external_chat(
        self: BackgroundWorkSupervisor,
        work_id: str,
        *,
        peer_loop: ChatGPTPeerLoop,
    ):
        if not isinstance(peer_loop, ChatGPTPeerLoop):
            raise SupervisorStateError("peer_loop must be a ChatGPTPeerLoop")
        snapshot = self.recover(work_id)
        observation = peer_loop.observe(snapshot.cursor)
        return record_external_observation(
            self,
            work_id,
            observation=observation,
        )

    def record_peer_turn(
        self: BackgroundWorkSupervisor,
        work_id: str,
        *,
        turn: ChatPeerTurn,
    ):
        snapshot = self.recover(work_id)
        if not _reconciliation_commit.get():
            captured_before = _consume(_verified_turns, turn.turn_digest)
            if captured_before is None or not hmac.compare_digest(
                captured_before,
                snapshot.cursor.cursor_digest,
            ):
                raise SupervisorStateError(
                    "Peer turn must come from one fresh live M6.3 continuation "
                    "at the exact supervisor cursor"
                )
        return original_record_peer_turn(self, work_id, turn=turn)

    def reconcile_in_flight_chat(
        self: BackgroundWorkSupervisor,
        work_id: str,
        *,
        peer_loop: ChatGPTPeerLoop,
    ):
        token = _reconciliation_commit.set(True)
        try:
            return original_reconcile(
                self,
                work_id,
                peer_loop=peer_loop,
            )
        finally:
            _reconciliation_commit.reset(token)

    BackgroundWorkSupervisor.record_external_observation = (  # type: ignore[method-assign]
        record_external_observation
    )
    BackgroundWorkSupervisor.observe_external_chat = observe_external_chat  # type: ignore[attr-defined]
    BackgroundWorkSupervisor.record_peer_turn = record_peer_turn  # type: ignore[method-assign]
    BackgroundWorkSupervisor.reconcile_in_flight_chat = (  # type: ignore[method-assign]
        reconcile_in_flight_chat
    )
    BackgroundWorkSupervisor._m6_5_provenance_hardened = True  # type: ignore[attr-defined]


_install_peer_capture()
_install_supervisor_guards()
