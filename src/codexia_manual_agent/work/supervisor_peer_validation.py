from __future__ import annotations

import hmac

from codexia_manual_agent.work.admission import ContinuationDecision
from codexia_manual_agent.work.chat_peer import ChatPeerTurn
from codexia_manual_agent.work.contracts import InvalidWorkRecordError
from codexia_manual_agent.work.supervisor import (
    BackgroundWorkSupervisor,
    SupervisorWorkSnapshot,
)
from codexia_manual_agent.work.supervisor_revision import _render_codexia_revision


def _install_peer_turn_validation() -> None:
    if getattr(BackgroundWorkSupervisor, "_m6_5_peer_validation_enabled", False):
        return

    original_validate_peer_turn = BackgroundWorkSupervisor._validate_peer_turn

    def validate_peer_turn(
        snapshot: SupervisorWorkSnapshot,
        turn: ChatPeerTurn,
    ) -> None:
        if not isinstance(turn, ChatPeerTurn):
            raise InvalidWorkRecordError("turn must be a ChatPeerTurn")
        dispatch = snapshot.pending_dispatch
        if dispatch is None:
            raise InvalidWorkRecordError("No exact pending dispatch exists")

        if dispatch.admission.decision is ContinuationDecision.ADMIT:
            original_validate_peer_turn(snapshot, turn)
            return
        if dispatch.admission.decision is not ContinuationDecision.REVISE:
            raise InvalidWorkRecordError(
                "Peer turn cannot close a non-ADMIT/non-REVISE supervisor dispatch"
            )

        if (
            turn.admission_id != dispatch.admission.admission_id
            or not hmac.compare_digest(
                turn.admission_digest,
                dispatch.admission.admission_digest,
            )
            or turn.handoff_id != snapshot.handoff.handoff_id
            or not hmac.compare_digest(
                turn.handoff_digest,
                snapshot.handoff.handoff_digest,
            )
            or turn.interpretation_id != snapshot.interpretation.interpretation_id
            or not hmac.compare_digest(
                turn.interpretation_digest,
                snapshot.interpretation.interpretation_digest,
            )
            or not hmac.compare_digest(
                turn.before_cursor_digest,
                snapshot.cursor.cursor_digest,
            )
            or turn.after_cursor.conversation_id != snapshot.cursor.conversation_id
        ):
            raise InvalidWorkRecordError(
                "Peer turn does not close the exact pending supervisor dispatch"
            )

        expected_prompt = _render_codexia_revision(
            handoff=snapshot.handoff,
            interpretation=snapshot.interpretation,
            proposal=dispatch.proposal,
            admission=dispatch.admission,
        )
        if turn.codexia_message.statement.text != expected_prompt:
            raise InvalidWorkRecordError(
                "Peer turn Codexia message differs from exact pending revision envelope"
            )

    BackgroundWorkSupervisor._validate_peer_turn = staticmethod(  # type: ignore[method-assign]
        validate_peer_turn
    )
    BackgroundWorkSupervisor._m6_5_peer_validation_enabled = True  # type: ignore[attr-defined]


_install_peer_turn_validation()
