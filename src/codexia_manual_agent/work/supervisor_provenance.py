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
_verified_observations: dict[str, tuple[str, str | None]] = {}
_verified_turns: dict[str, tuple[str, str | None]] = {}
_observation_work_id: ContextVar[str | None] = ContextVar(
    "m6_5_observation_work_id",
    default=None,
)
_turn_work_id: ContextVar[str | None] = ContextVar(
    "m6_5_turn_work_id",
    default=None,
)
_reconciliation_commit: ContextVar[bool] = ContextVar(
    "m6_5_reconciliation_commit",
    default=False,
)


def _remember(
    registry: dict[str, tuple[str, str | None]],
    digest: str,
    before_cursor_digest: str,
    work_id: str | None,
) -> None:
    with _registry_lock:
        if len(registry) >= _MAX_LIVE_EVIDENCE:
            registry.pop(next(iter(registry)))
        registry[digest] = (before_cursor_digest, work_id)


def _consume(
    registry: dict[str, tuple[str, str | None]],
    digest: str,
) -> tuple[str, str | None] | None:
    with _registry_lock:
        return registry.pop(digest, None)


def _same_cursor_work_count(
    supervisor: BackgroundWorkSupervisor,
    *,
    conversation_id: str,
    cursor_digest: str,
) -> int:
    return sum(
        1
        for item in supervisor.list_active()
        if item.cursor.conversation_id == conversation_id
        and hmac.compare_digest(item.cursor.cursor_digest, cursor_digest)
    )


def _install_peer_capture() -> None:
    if getattr(ChatGPTPeerLoop, "_m6_5_provenance_hardened", False):
        return

    original_observe = ChatGPTPeerLoop.observe
    original_continue = ChatGPTPeerLoop.continue_admitted
    original_revision = ChatGPTPeerLoop.revise_requested  # type: ignore[attr-defined]

    def observe(
        self: ChatGPTPeerLoop,
        cursor: ChatPeerCursor,
    ) -> ChatPeerObservation:
        observation = original_observe(self, cursor)
        _remember(
            _verified_observations,
            observation.observation_digest,
            observation.before_cursor_digest,
            _observation_work_id.get(),
        )
        return observation

    def continue_admitted(self: ChatGPTPeerLoop, **kwargs: Any) -> ChatPeerTurn:
        turn = original_continue(self, **kwargs)
        _remember(
            _verified_turns,
            turn.turn_digest,
            turn.before_cursor_digest,
            _turn_work_id.get(),
        )
        return turn

    def revise_requested(self: ChatGPTPeerLoop, **kwargs: Any) -> ChatPeerTurn:
        turn = original_revision(self, **kwargs)
        _remember(
            _verified_turns,
            turn.turn_digest,
            turn.before_cursor_digest,
            _turn_work_id.get(),
        )
        return turn

    ChatGPTPeerLoop.observe = observe  # type: ignore[method-assign]
    ChatGPTPeerLoop.continue_admitted = continue_admitted  # type: ignore[method-assign]
    ChatGPTPeerLoop.revise_requested = revise_requested  # type: ignore[attr-defined]
    ChatGPTPeerLoop._m6_5_provenance_hardened = True  # type: ignore[attr-defined]


def _install_supervisor_guards() -> None:
    if getattr(BackgroundWorkSupervisor, "_m6_5_provenance_hardened", False):
        return

    original_record_external = BackgroundWorkSupervisor.record_external_observation
    original_record_peer_turn = BackgroundWorkSupervisor.record_peer_turn
    original_execute = BackgroundWorkSupervisor.execute_claimed_chat
    original_reconcile = BackgroundWorkSupervisor.reconcile_in_flight_chat

    def _require_observation_ticket(
        self: BackgroundWorkSupervisor,
        work_id: str,
        observation: ChatPeerObservation,
    ) -> None:
        snapshot = self.recover(work_id)
        captured = _consume(
            _verified_observations,
            observation.observation_digest,
        )
        if captured is None:
            raise SupervisorStateError(
                "External observation must come from one fresh live M6.3 observe() call"
            )
        captured_before, captured_work_id = captured
        if not hmac.compare_digest(
            captured_before,
            snapshot.cursor.cursor_digest,
        ):
            raise SupervisorStateError(
                "Live external observation does not bind the exact supervisor cursor"
            )
        if captured_work_id is not None and captured_work_id != work_id:
            raise SupervisorStateError(
                "Live external observation is bound to different delegated work"
            )
        if captured_work_id is None and _same_cursor_work_count(
            self,
            conversation_id=snapshot.cursor.conversation_id,
            cursor_digest=snapshot.cursor.cursor_digest,
        ) != 1:
            raise SupervisorStateError(
                "Unbound live observation is ambiguous across multiple delegated works; "
                "use capture_external_chat() for exact work binding"
            )

    def record_external_observation(
        self: BackgroundWorkSupervisor,
        work_id: str,
        *,
        observation: ChatPeerObservation,
    ):
        _require_observation_ticket(self, work_id, observation)
        return original_record_external(
            self,
            work_id,
            observation=observation,
        )

    def capture_external_chat(
        self: BackgroundWorkSupervisor,
        work_id: str,
        *,
        peer_loop: ChatGPTPeerLoop,
    ) -> ChatPeerObservation:
        if not isinstance(peer_loop, ChatGPTPeerLoop):
            raise SupervisorStateError("peer_loop must be a ChatGPTPeerLoop")
        snapshot = self.recover(work_id)
        token = _observation_work_id.set(work_id)
        try:
            return peer_loop.observe(snapshot.cursor)
        finally:
            _observation_work_id.reset(token)

    def discard_external_observation(
        self: BackgroundWorkSupervisor,
        work_id: str,
        *,
        observation: ChatPeerObservation,
    ) -> None:
        """Consume one exact live evidence ticket without changing durable state."""

        _require_observation_ticket(self, work_id, observation)

    def observe_external_chat(
        self: BackgroundWorkSupervisor,
        work_id: str,
        *,
        peer_loop: ChatGPTPeerLoop,
    ):
        observation = capture_external_chat(
            self,
            work_id,
            peer_loop=peer_loop,
        )
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
            captured = _consume(_verified_turns, turn.turn_digest)
            if captured is None:
                raise SupervisorStateError(
                    "Peer turn must come from one fresh live M6.3 continuation or revision"
                )
            captured_before, captured_work_id = captured
            if not hmac.compare_digest(
                captured_before,
                snapshot.cursor.cursor_digest,
            ):
                raise SupervisorStateError(
                    "Live peer turn does not bind the exact supervisor cursor"
                )
            if captured_work_id is not None and captured_work_id != work_id:
                raise SupervisorStateError(
                    "Live peer turn is bound to different delegated work"
                )
        return original_record_peer_turn(self, work_id, turn=turn)

    def execute_claimed_chat(
        self: BackgroundWorkSupervisor,
        lease,
        peer_loop: ChatGPTPeerLoop,
    ):
        token = _turn_work_id.set(lease.work_id)
        try:
            return original_execute(self, lease, peer_loop)
        finally:
            _turn_work_id.reset(token)

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
    BackgroundWorkSupervisor.capture_external_chat = capture_external_chat  # type: ignore[attr-defined]
    BackgroundWorkSupervisor.discard_external_observation = (  # type: ignore[attr-defined]
        discard_external_observation
    )
    BackgroundWorkSupervisor.observe_external_chat = observe_external_chat  # type: ignore[attr-defined]
    BackgroundWorkSupervisor.record_peer_turn = record_peer_turn  # type: ignore[method-assign]
    BackgroundWorkSupervisor.execute_claimed_chat = execute_claimed_chat  # type: ignore[method-assign]
    BackgroundWorkSupervisor.reconcile_in_flight_chat = (  # type: ignore[method-assign]
        reconcile_in_flight_chat
    )
    BackgroundWorkSupervisor._m6_5_provenance_hardened = True  # type: ignore[attr-defined]


_install_peer_capture()
_install_supervisor_guards()
