from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from codexia_manual_agent.domain.models import ProviderConversation, ProviderRequest
from codexia_manual_agent.providers.chatgpt_web import (
    ChatGPTConversationMessage,
    ChatGPTWebProvider,
)
from codexia_manual_agent.work.admission import (
    ContinuationAdmission,
    ContinuationDecision,
    ContinuationProposal,
)
from codexia_manual_agent.work.contracts import (
    InvalidWorkRecordError,
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
    _digest,
    _validate_digest,
)

CHAT_PEER_CURSOR_SCHEMA_VERSION = 1
CHAT_PEER_MESSAGE_SCHEMA_VERSION = 1
CHAT_PEER_OBSERVATION_SCHEMA_VERSION = 1
CHAT_PEER_TURN_SCHEMA_VERSION = 1
MAX_PEER_MESSAGES = 4_096


class PeerConversationChangedError(InvalidWorkRecordError):
    """Raised when live chat state no longer matches the exact peer cursor."""


class ChatPeerMessageOrigin(StrEnum):
    CODEXIA_SEND = "codexia_send"
    EXTERNAL_USER = "external_user"
    ASSISTANT = "assistant"


def _bounded_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidWorkRecordError(f"{field_name} must be text")
    value = value.strip()
    if not value or len(value) > 512 or "\x00" in value:
        raise InvalidWorkRecordError(f"{field_name} is invalid")
    return value


def _message_fingerprint(message: ChatGPTConversationMessage) -> str:
    return _digest(
        {
            "node_id": _bounded_id(message.node_id, "node_id"),
            "message_id": _bounded_id(message.message_id, "message_id"),
            "role": _bounded_id(message.role, "role"),
            "text": message.text,
        }
    )


def _captured_message_fingerprint(message: CapturedChatPeerMessage) -> str:
    return _digest(
        {
            "node_id": message.node_id,
            "message_id": message.provider_message_id,
            "role": message.transport_role,
            "text": message.statement.text,
        }
    )


def _validate_cursor_transition(
    before: ChatPeerCursor,
    after: ChatPeerCursor,
    messages: tuple[CapturedChatPeerMessage, ...],
) -> None:
    if before.conversation_id != after.conversation_id:
        raise InvalidWorkRecordError("Chat peer transition changed conversation identity")
    for message in messages:
        if message.conversation_id != before.conversation_id:
            raise InvalidWorkRecordError(
                "Captured chat message changed conversation identity"
            )
    expected = before.message_fingerprints + tuple(
        _captured_message_fingerprint(message) for message in messages
    )
    if after.message_fingerprints != expected:
        raise InvalidWorkRecordError(
            "Chat peer transition does not match its exact captured message delta"
        )


@dataclass(frozen=True, slots=True)
class ChatPeerCursor:
    """Exact current-branch checkpoint; it attributes no pre-existing message."""

    schema_version: int
    conversation_id: str
    message_fingerprints: tuple[str, ...]
    cursor_digest: str

    @classmethod
    def from_messages(
        cls,
        conversation_id: str,
        messages: Iterable[ChatGPTConversationMessage],
    ) -> ChatPeerCursor:
        conversation_id = _bounded_id(conversation_id, "conversation_id")
        fingerprints = tuple(_message_fingerprint(message) for message in messages)
        if len(fingerprints) > MAX_PEER_MESSAGES:
            raise InvalidWorkRecordError("Chat peer cursor exceeds its message budget")
        base = {
            "schema_version": CHAT_PEER_CURSOR_SCHEMA_VERSION,
            "conversation_id": conversation_id,
            "message_fingerprints": list(fingerprints),
        }
        return cls(
            schema_version=CHAT_PEER_CURSOR_SCHEMA_VERSION,
            conversation_id=conversation_id,
            message_fingerprints=fingerprints,
            cursor_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != CHAT_PEER_CURSOR_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported chat peer cursor schema")
        object.__setattr__(
            self,
            "conversation_id",
            _bounded_id(self.conversation_id, "conversation_id"),
        )
        fingerprints = tuple(self.message_fingerprints)
        if len(fingerprints) > MAX_PEER_MESSAGES:
            raise InvalidWorkRecordError("Chat peer cursor exceeds its message budget")
        for fingerprint in fingerprints:
            _validate_digest(fingerprint, "message_fingerprint")
        object.__setattr__(self, "message_fingerprints", fingerprints)
        _validate_digest(self.cursor_digest, "cursor_digest")
        if not hmac.compare_digest(self.cursor_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError("Chat peer cursor digest does not match")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "conversation_id": self.conversation_id,
            "message_fingerprints": list(self.message_fingerprints),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "cursor_digest": self.cursor_digest}


@dataclass(frozen=True, slots=True)
class CapturedChatPeerMessage:
    """One newly observed provider message with locally derived semantic provenance."""

    schema_version: int
    conversation_id: str
    node_id: str
    provider_message_id: str
    transport_role: str
    origin: ChatPeerMessageOrigin
    statement: WorkStatement
    capture_digest: str

    @classmethod
    def capture(
        cls,
        *,
        conversation_id: str,
        message: ChatGPTConversationMessage,
        origin: ChatPeerMessageOrigin,
        actor: str,
    ) -> CapturedChatPeerMessage:
        conversation_id = _bounded_id(conversation_id, "conversation_id")
        if message.role == "user":
            expected_kind = (
                WorkActorKind.CODEXIA
                if origin is ChatPeerMessageOrigin.CODEXIA_SEND
                else WorkActorKind.HUMAN
                if origin is ChatPeerMessageOrigin.EXTERNAL_USER
                else None
            )
        elif message.role == "assistant":
            expected_kind = (
                WorkActorKind.WORKER
                if origin is ChatPeerMessageOrigin.ASSISTANT
                else None
            )
        else:
            expected_kind = None
        if expected_kind is None:
            raise InvalidWorkRecordError(
                "Chat transport role is incompatible with claimed peer origin"
            )
        statement = WorkStatement.create(
            author_kind=expected_kind,
            actor=actor,
            text=message.text,
        )
        base = {
            "schema_version": CHAT_PEER_MESSAGE_SCHEMA_VERSION,
            "conversation_id": conversation_id,
            "node_id": _bounded_id(message.node_id, "node_id"),
            "provider_message_id": _bounded_id(message.message_id, "message_id"),
            "transport_role": message.role,
            "origin": origin.value,
            "statement": statement.to_dict(),
        }
        return cls(
            schema_version=CHAT_PEER_MESSAGE_SCHEMA_VERSION,
            conversation_id=conversation_id,
            node_id=base["node_id"],
            provider_message_id=base["provider_message_id"],
            transport_role=message.role,
            origin=origin,
            statement=statement,
            capture_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != CHAT_PEER_MESSAGE_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported captured chat peer message schema")
        object.__setattr__(
            self,
            "conversation_id",
            _bounded_id(self.conversation_id, "conversation_id"),
        )
        object.__setattr__(self, "node_id", _bounded_id(self.node_id, "node_id"))
        object.__setattr__(
            self,
            "provider_message_id",
            _bounded_id(self.provider_message_id, "provider_message_id"),
        )
        if self.transport_role not in {"user", "assistant"}:
            raise InvalidWorkRecordError("Unsupported chat peer transport role")
        try:
            origin = ChatPeerMessageOrigin(self.origin)
        except (TypeError, ValueError) as exc:
            raise InvalidWorkRecordError("Unsupported chat peer message origin") from exc
        object.__setattr__(self, "origin", origin)
        if not isinstance(self.statement, WorkStatement):
            raise InvalidWorkRecordError("statement must be a WorkStatement")
        expected = {
            ChatPeerMessageOrigin.CODEXIA_SEND: ("user", WorkActorKind.CODEXIA),
            ChatPeerMessageOrigin.EXTERNAL_USER: ("user", WorkActorKind.HUMAN),
            ChatPeerMessageOrigin.ASSISTANT: ("assistant", WorkActorKind.WORKER),
        }[origin]
        if (
            self.transport_role != expected[0]
            or self.statement.author_kind is not expected[1]
        ):
            raise InvalidWorkRecordError(
                "Captured chat message provenance does not match transport origin"
            )
        _validate_digest(self.capture_digest, "capture_digest")
        if not hmac.compare_digest(self.capture_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError("Captured chat peer message digest does not match")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "conversation_id": self.conversation_id,
            "node_id": self.node_id,
            "provider_message_id": self.provider_message_id,
            "transport_role": self.transport_role,
            "origin": self.origin.value,
            "statement": self.statement.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "capture_digest": self.capture_digest}


@dataclass(frozen=True, slots=True)
class ChatPeerObservation:
    schema_version: int
    before_cursor_digest: str
    after_cursor: ChatPeerCursor
    messages: tuple[CapturedChatPeerMessage, ...]
    observation_digest: str

    @classmethod
    def create(
        cls,
        *,
        before: ChatPeerCursor,
        after: ChatPeerCursor,
        messages: Iterable[CapturedChatPeerMessage],
    ) -> ChatPeerObservation:
        messages = tuple(messages)
        _validate_cursor_transition(before, after, messages)
        base = {
            "schema_version": CHAT_PEER_OBSERVATION_SCHEMA_VERSION,
            "before_cursor_digest": before.cursor_digest,
            "after_cursor": after.to_dict(),
            "messages": [message.to_dict() for message in messages],
        }
        return cls(
            schema_version=CHAT_PEER_OBSERVATION_SCHEMA_VERSION,
            before_cursor_digest=before.cursor_digest,
            after_cursor=after,
            messages=messages,
            observation_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != CHAT_PEER_OBSERVATION_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported chat peer observation schema")
        _validate_digest(self.before_cursor_digest, "before_cursor_digest")
        if not isinstance(self.after_cursor, ChatPeerCursor):
            raise InvalidWorkRecordError("after_cursor must be a ChatPeerCursor")
        messages = tuple(self.messages)
        if len(messages) > MAX_PEER_MESSAGES:
            raise InvalidWorkRecordError("Chat peer observation exceeds its message budget")
        for message in messages:
            if not isinstance(message, CapturedChatPeerMessage):
                raise InvalidWorkRecordError(
                    "Chat peer observation messages must be captured messages"
                )
            if message.conversation_id != self.after_cursor.conversation_id:
                raise InvalidWorkRecordError(
                    "Chat peer observation changed conversation identity"
                )
        object.__setattr__(self, "messages", messages)
        _validate_digest(self.observation_digest, "observation_digest")
        if not hmac.compare_digest(
            self.observation_digest,
            _digest(self._base_dict()),
        ):
            raise InvalidWorkRecordError("Chat peer observation digest does not match")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "before_cursor_digest": self.before_cursor_digest,
            "after_cursor": self.after_cursor.to_dict(),
            "messages": [message.to_dict() for message in self.messages],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "observation_digest": self.observation_digest}


@dataclass(frozen=True, slots=True)
class ChatPeerTurn:
    schema_version: int
    admission_id: str
    admission_digest: str
    before_cursor_digest: str
    codexia_message: CapturedChatPeerMessage
    worker_message: CapturedChatPeerMessage
    after_cursor: ChatPeerCursor
    turn_digest: str

    @classmethod
    def create(
        cls,
        *,
        admission: ContinuationAdmission,
        before: ChatPeerCursor,
        codexia_message: CapturedChatPeerMessage,
        worker_message: CapturedChatPeerMessage,
        after: ChatPeerCursor,
    ) -> ChatPeerTurn:
        _validate_cursor_transition(
            before,
            after,
            (codexia_message, worker_message),
        )
        base = {
            "schema_version": CHAT_PEER_TURN_SCHEMA_VERSION,
            "admission_id": admission.admission_id,
            "admission_digest": admission.admission_digest,
            "before_cursor_digest": before.cursor_digest,
            "codexia_message": codexia_message.to_dict(),
            "worker_message": worker_message.to_dict(),
            "after_cursor": after.to_dict(),
        }
        return cls(
            schema_version=CHAT_PEER_TURN_SCHEMA_VERSION,
            admission_id=admission.admission_id,
            admission_digest=admission.admission_digest,
            before_cursor_digest=before.cursor_digest,
            codexia_message=codexia_message,
            worker_message=worker_message,
            after_cursor=after,
            turn_digest=_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != CHAT_PEER_TURN_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported chat peer turn schema")
        object.__setattr__(
            self,
            "admission_id",
            _bounded_id(self.admission_id, "admission_id"),
        )
        _validate_digest(self.admission_digest, "admission_digest")
        _validate_digest(self.before_cursor_digest, "before_cursor_digest")
        if not isinstance(self.codexia_message, CapturedChatPeerMessage):
            raise InvalidWorkRecordError(
                "codexia_message must be a CapturedChatPeerMessage"
            )
        if not isinstance(self.worker_message, CapturedChatPeerMessage):
            raise InvalidWorkRecordError(
                "worker_message must be a CapturedChatPeerMessage"
            )
        if not isinstance(self.after_cursor, ChatPeerCursor):
            raise InvalidWorkRecordError("after_cursor must be a ChatPeerCursor")
        if self.codexia_message.origin is not ChatPeerMessageOrigin.CODEXIA_SEND:
            raise InvalidWorkRecordError("Peer turn Codexia message provenance is invalid")
        if self.worker_message.origin is not ChatPeerMessageOrigin.ASSISTANT:
            raise InvalidWorkRecordError("Peer turn worker message provenance is invalid")
        conversation_id = self.after_cursor.conversation_id
        if (
            self.codexia_message.conversation_id != conversation_id
            or self.worker_message.conversation_id != conversation_id
        ):
            raise InvalidWorkRecordError("Peer turn changed conversation identity")
        _validate_digest(self.turn_digest, "turn_digest")
        if not hmac.compare_digest(self.turn_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError("Chat peer turn digest does not match")

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "admission_id": self.admission_id,
            "admission_digest": self.admission_digest,
            "before_cursor_digest": self.before_cursor_digest,
            "codexia_message": self.codexia_message.to_dict(),
            "worker_message": self.worker_message.to_dict(),
            "after_cursor": self.after_cursor.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_dict(), "turn_digest": self.turn_digest}

    def followup_proposal(
        self,
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
    ) -> ContinuationProposal:
        """Bind the exact captured worker response as a candidate next-work proposal.

        This is only provenance wiring. Whether the worker text actually contains a
        useful next action remains semantic cognition and M6.2 admission work.
        """

        return ContinuationProposal.create(
            handoff=handoff,
            interpretation=interpretation,
            checkpoint_digest=self.after_cursor.cursor_digest,
            statement=self.worker_message.statement,
        )


class ChatGPTPeerLoop:
    """One bounded ChatGPT/Codexia peer-loop surface over the current branch."""

    def __init__(
        self,
        provider: ChatGPTWebProvider,
        *,
        codexia_actor: str = "codexia",
        human_actor: str = "human",
        worker_actor: str = "chatgpt",
    ) -> None:
        self.provider = provider
        self.codexia_actor = codexia_actor
        self.human_actor = human_actor
        self.worker_actor = worker_actor

    def attach(self, conversation_id: str) -> ChatPeerCursor:
        messages = self.provider.read_messages(conversation_id)
        return ChatPeerCursor.from_messages(conversation_id, messages)

    def observe(self, cursor: ChatPeerCursor) -> ChatPeerObservation:
        current = self.provider.read_messages(cursor.conversation_id)
        delta = self._require_prefix(cursor, current)
        captures: list[CapturedChatPeerMessage] = []
        for message in delta:
            if message.role == "user":
                origin = ChatPeerMessageOrigin.EXTERNAL_USER
                actor = self.human_actor
            elif message.role == "assistant":
                origin = ChatPeerMessageOrigin.ASSISTANT
                actor = self.worker_actor
            else:  # provider already rejects this; keep the boundary explicit
                raise InvalidWorkRecordError("Unsupported peer observation role")
            captures.append(
                CapturedChatPeerMessage.capture(
                    conversation_id=cursor.conversation_id,
                    message=message,
                    origin=origin,
                    actor=actor,
                )
            )
        after = ChatPeerCursor.from_messages(cursor.conversation_id, current)
        return ChatPeerObservation.create(before=cursor, after=after, messages=captures)

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
        if len(delta) != 2 or delta[0].role != "user" or delta[1].role != "assistant":
            raise PeerConversationChangedError(
                "Codexia continuation did not produce one exact user/assistant branch delta"
            )
        sent_message, worker_message = delta
        if sent_message.text != prompt:
            raise PeerConversationChangedError(
                "Observed Codexia transport message differs from the exact sent envelope"
            )
        if worker_message.text != response.text:
            raise PeerConversationChangedError(
                "Observed worker message differs from the provider response"
            )
        if (
            response.conversation.message_id is not None
            and worker_message.message_id != response.conversation.message_id
        ):
            raise PeerConversationChangedError(
                "Observed worker message identity differs from the provider response"
            )

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
            after_messages,
        )
        return ChatPeerTurn.create(
            admission=admission,
            before=cursor,
            codexia_message=captured_codexia,
            worker_message=captured_worker,
            after=after_cursor,
        )

    @staticmethod
    def _require_prefix(
        cursor: ChatPeerCursor,
        current: tuple[ChatGPTConversationMessage, ...],
    ) -> tuple[ChatGPTConversationMessage, ...]:
        current_fingerprints = tuple(_message_fingerprint(message) for message in current)
        prefix = current_fingerprints[: len(cursor.message_fingerprints)]
        if prefix != cursor.message_fingerprints:
            raise PeerConversationChangedError(
                "ChatGPT current branch no longer preserves the exact peer cursor prefix"
            )
        return current[len(cursor.message_fingerprints) :]

    @staticmethod
    def _render_codexia_continuation(
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        proposal: ContinuationProposal,
        admission: ContinuationAdmission,
    ) -> str:
        # The visible label helps the human/worker understand provenance, but it is
        # not the security mechanism. Provenance comes from before/after provider
        # reconciliation in continue_admitted().
        return (
            "[Codexia delegated-work continuation]\n"
            "This message is authored by Codexia, not a new human instruction.\n"
            f"handoff_id={handoff.handoff_id}\n"
            f"interpretation_id={interpretation.interpretation_id}\n"
            f"admission_id={admission.admission_id}\n"
            f"checkpoint_digest={proposal.checkpoint_digest}\n\n"
            "Continue the admitted delegated work:\n"
            f"{proposal.statement.text}"
        )
