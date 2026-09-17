from __future__ import annotations

import hmac
from typing import Any

from codexia_manual_agent.domain.models import ProviderConversation, ProviderRequest
from codexia_manual_agent.work.admission import (
    ContinuationAdmission,
    ContinuationDecision,
    ContinuationProposal,
)
from codexia_manual_agent.work.chat_peer import (
    CapturedChatPeerMessage,
    ChatGPTPeerLoop,
    ChatPeerCursor,
    ChatPeerMessageOrigin,
    ChatPeerTurn,
    PeerConversationChangedError,
)
from codexia_manual_agent.work.contracts import (
    InvalidWorkRecordError,
    WorkHandoff,
    WorkIntentInterpretation,
)


def _record_logical_assistant_turn(
    self: ChatGPTPeerLoop,
    *,
    cursor: ChatPeerCursor,
    prompt: str,
    admission: ContinuationAdmission,
    response: Any,
    operation: str,
) -> ChatPeerTurn:
    """Preserve ChatPeerTurn v1 while tolerating assistant-only product tail."""

    if (
        response.conversation is None
        or response.conversation.conversation_id != cursor.conversation_id
    ):
        raise PeerConversationChangedError(
            f"ChatGPT {operation} response changed conversation identity"
        )

    after_messages = self.provider.read_messages(cursor.conversation_id)
    delta = self._require_prefix(cursor, after_messages)
    if len(delta) < 2 or delta[0].role != "user":
        raise PeerConversationChangedError(
            f"Codexia {operation} did not produce an exact user/assistant branch delta"
        )

    sent_message = delta[0]
    assistant_tail = delta[1:]
    if sent_message.text != prompt:
        raise PeerConversationChangedError(
            f"Observed Codexia {operation} differs from the exact sent envelope"
        )
    if any(message.role != "assistant" for message in assistant_tail):
        raise PeerConversationChangedError(
            f"Codexia {operation} contains intervening non-worker activity"
        )

    response_message_id = response.conversation.message_id
    if response_message_id is not None:
        matches = [
            message
            for message in assistant_tail
            if message.message_id == response_message_id
        ]
        if len(matches) != 1 or matches[0].text != response.text:
            raise PeerConversationChangedError(
                "Observed assistant tail does not contain the exact provider response"
            )
    else:
        matches = [message for message in assistant_tail if message.text == response.text]
        if len(matches) != 1:
            raise PeerConversationChangedError(
                "Observed assistant tail cannot uniquely correlate the provider response"
            )

    # Keep the frozen ChatPeerTurn v1 cardinality. The first assistant artifact
    # closes IN_FLIGHT; later assistant-only artifacts remain beyond this cursor
    # and are captured by the next normal external observation. M6.6 checkpoint
    # hardening then reconstructs the current logical worker evidence from the
    # exact peer turn plus that exact assistant-only tail.
    worker_message = assistant_tail[0]
    prefix_length = len(cursor.message_fingerprints)
    recorded_messages = after_messages[: prefix_length + 2]

    captured_codexia = CapturedChatPeerMessage.capture(
        conversation_id=cursor.conversation_id,
        message=sent_message,
        origin=ChatPeerMessageOrigin.CODEXIA_SEND,
        actor=self.codexia_actor,
    )
    captured_worker = CapturedChatPeerMessage.capture(
        conversation_id=cursor.conversation_id,
        message=worker_message,
        origin=ChatPeerMessageOrigin.ASSISTANT,
        actor=self.worker_actor,
    )
    after_cursor = ChatPeerCursor.from_messages(
        cursor.conversation_id,
        recorded_messages,
    )
    return ChatPeerTurn.create(
        admission=admission,
        before=cursor,
        codexia_message=captured_codexia,
        worker_message=captured_worker,
        after=after_cursor,
    )


def _continue_admitted_with_logical_tail(
    self: ChatGPTPeerLoop,
    *,
    cursor: ChatPeerCursor,
    handoff: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    proposal: ContinuationProposal,
    admission: ContinuationAdmission,
) -> ChatPeerTurn:
    if admission.decision is not ContinuationDecision.ADMIT:
        raise InvalidWorkRecordError("Only an ADMIT continuation can enter the peer loop")
    admission.assert_binds(handoff, interpretation, proposal)
    if not hmac.compare_digest(proposal.checkpoint_digest, cursor.cursor_digest):
        raise PeerConversationChangedError(
            "Admitted proposal is stale for the current chat peer cursor"
        )

    before_messages = self.provider.read_messages(cursor.conversation_id)
    unseen = self._require_prefix(cursor, before_messages)
    if unseen:
        raise PeerConversationChangedError(
            "Conversation advanced after admission; observe human/worker turns first"
        )

    prompt = self._render_codexia_continuation(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
    )
    response = self.provider.send(
        ProviderRequest(
            prompt=prompt,
            conversation=ProviderConversation(
                conversation_id=cursor.conversation_id,
            ),
        )
    )
    return _record_logical_assistant_turn(
        self,
        cursor=cursor,
        prompt=prompt,
        admission=admission,
        response=response,
        operation="continuation",
    )


def _revise_requested_with_logical_tail(
    self: ChatGPTPeerLoop,
    *,
    cursor: ChatPeerCursor,
    handoff: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    proposal: ContinuationProposal,
    admission: ContinuationAdmission,
) -> ChatPeerTurn:
    if admission.decision is not ContinuationDecision.REVISE:
        raise InvalidWorkRecordError(
            "Only a REVISE admission can enter the peer revision loop"
        )
    admission.assert_binds(handoff, interpretation, proposal)
    if not hmac.compare_digest(proposal.checkpoint_digest, cursor.cursor_digest):
        raise PeerConversationChangedError(
            "Revision proposal is stale for the current chat peer cursor"
        )

    before_messages = self.provider.read_messages(cursor.conversation_id)
    unseen = self._require_prefix(cursor, before_messages)
    if unseen:
        raise PeerConversationChangedError(
            "Conversation advanced after revision decision; observe turns first"
        )

    render_revision = getattr(self, "_render_codexia_revision", None)
    if not callable(render_revision):
        raise InvalidWorkRecordError("M6.5 peer revision renderer is unavailable")
    prompt = render_revision(
        handoff=handoff,
        interpretation=interpretation,
        proposal=proposal,
        admission=admission,
    )
    response = self.provider.send(
        ProviderRequest(
            prompt=prompt,
            conversation=ProviderConversation(
                conversation_id=cursor.conversation_id,
            ),
        )
    )
    return _record_logical_assistant_turn(
        self,
        cursor=cursor,
        prompt=prompt,
        admission=admission,
        response=response,
        operation="revision",
    )


def _install_m6_6_logical_worker_tail() -> None:
    """Install before M6.5 provenance wraps the live peer-loop methods."""

    if getattr(ChatGPTPeerLoop, "_m6_6_logical_worker_tail_enabled", False):
        return
    if not callable(getattr(ChatGPTPeerLoop, "revise_requested", None)):
        raise InvalidWorkRecordError(
            "M6.5 peer revision surface must be installed before M6.6 hardening"
        )

    ChatGPTPeerLoop.continue_admitted = (  # type: ignore[method-assign]
        _continue_admitted_with_logical_tail
    )
    ChatGPTPeerLoop.revise_requested = (  # type: ignore[attr-defined]
        _revise_requested_with_logical_tail
    )
    ChatGPTPeerLoop._m6_6_logical_worker_tail_enabled = True  # type: ignore[attr-defined]


_install_m6_6_logical_worker_tail()

# Keep the pilot-facing name without creating a subclass that would bypass the
# M6.5 provenance wrapper installed later in package initialization.
PilotChatGPTPeerLoop = ChatGPTPeerLoop


__all__ = ["PilotChatGPTPeerLoop"]
