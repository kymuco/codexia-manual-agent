from __future__ import annotations

import hmac

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


class PilotChatGPTPeerLoop(ChatGPTPeerLoop):
    """M6.6 live peer loop that tolerates one logical tool-using assistant turn.

    A ChatGPT product turn may expose multiple visible assistant artifacts between
    the exact Codexia user envelope and the terminal provider response. The base
    M6.3 schema remains unchanged: this loop records the first assistant artifact
    as the exact peer turn and leaves any remaining assistant-only tail to normal
    external observation. Pilot checkpoint hardening then folds that exact tail
    back into current logical worker evidence. Any intervening user message still
    fails closed as a conversation race.
    """

    def continue_admitted(
        self,
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
        if (
            response.conversation is None
            or response.conversation.conversation_id != cursor.conversation_id
        ):
            raise PeerConversationChangedError(
                "ChatGPT continuation response changed conversation identity"
            )

        after_messages = self.provider.read_messages(cursor.conversation_id)
        delta = self._require_prefix(cursor, after_messages)
        if len(delta) < 2 or delta[0].role != "user":
            raise PeerConversationChangedError(
                "Codexia continuation did not produce an exact user/assistant branch delta"
            )
        sent_message = delta[0]
        assistant_tail = delta[1:]
        if sent_message.text != prompt:
            raise PeerConversationChangedError(
                "Observed Codexia transport message differs from the exact sent envelope"
            )
        if any(message.role != "assistant" for message in assistant_tail):
            raise PeerConversationChangedError(
                "Codexia continuation contains intervening non-worker activity"
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

        # Keep the frozen ChatPeerTurn v1 cardinality. Only the first assistant
        # artifact closes IN_FLIGHT; the remaining assistant-only tail stays beyond
        # this cursor and is captured by the next normal driver observation.
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
