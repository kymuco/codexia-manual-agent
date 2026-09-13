from __future__ import annotations

import hmac
from dataclasses import replace

from codexia_manual_agent.domain.models import ProviderConversation, ProviderRequest
from codexia_manual_agent.work.admission import (
    ContinuationAdmission,
    ContinuationDecision,
    ContinuationProposal,
)
from codexia_manual_agent.work.attention import (
    AttentionDisposition,
    DynamicAttentionDecision,
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
    _digest,
    _exact_keys,
    _validate_digest,
    _validate_timestamp,
    _validate_uuid,
)
from codexia_manual_agent.work.supervisor import (
    SUPERVISOR_DISPATCH_SCHEMA_VERSION,
    BackgroundWorkSupervisor,
    SupervisorDispatch,
    SupervisorDispatchLease,
    SupervisorEventKind,
    SupervisorIntegrityError,
    SupervisorStateError,
    SupervisorStatus,
    _bounded_peer_id,
)


def _render_codexia_revision(
    *,
    handoff: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    proposal: ContinuationProposal,
    admission: ContinuationAdmission,
) -> str:
    if not isinstance(admission, ContinuationAdmission):
        raise InvalidWorkRecordError("admission must be a ContinuationAdmission")
    if admission.decision is not ContinuationDecision.REVISE:
        raise InvalidWorkRecordError(
            "Only a REVISE admission can render a worker revision request"
        )
    if admission.revision_request is None:
        raise InvalidWorkRecordError("REVISE admission lost its revision request")
    return (
        "[Codexia delegated-work revision]\n"
        "This message is authored by Codexia, not a new human instruction.\n"
        f"handoff_id={handoff.handoff_id}\n"
        f"interpretation_id={interpretation.interpretation_id}\n"
        f"admission_id={admission.admission_id}\n"
        f"checkpoint_digest={proposal.checkpoint_digest}\n\n"
        "The proposed next step is still inside the delegated trajectory, but it "
        "is not yet admissible. Revise it without asking the human unless a new "
        "material choice appears.\n\n"
        "Original worker candidate:\n"
        f"{proposal.statement.text}\n\n"
        "Codexia revision request:\n"
        f"{admission.revision_request}"
    )


def _revise_requested(
    self: ChatGPTPeerLoop,
    *,
    cursor: ChatPeerCursor,
    handoff: WorkHandoff,
    interpretation: WorkIntentInterpretation,
    proposal: ContinuationProposal,
    admission: ContinuationAdmission,
) -> ChatPeerTurn:
    if not isinstance(cursor, ChatPeerCursor):
        raise InvalidWorkRecordError("cursor must be a ChatPeerCursor")
    if not isinstance(handoff, WorkHandoff):
        raise InvalidWorkRecordError("handoff must be a WorkHandoff")
    if not isinstance(interpretation, WorkIntentInterpretation):
        raise InvalidWorkRecordError(
            "interpretation must be a WorkIntentInterpretation"
        )
    if not isinstance(proposal, ContinuationProposal):
        raise InvalidWorkRecordError("proposal must be a ContinuationProposal")
    if not isinstance(admission, ContinuationAdmission):
        raise InvalidWorkRecordError("admission must be a ContinuationAdmission")
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

    prompt = _render_codexia_revision(
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
            "ChatGPT revision response changed conversation identity"
        )

    after_messages = self.provider.read_messages(cursor.conversation_id)
    delta = self._require_prefix(cursor, after_messages)
    if len(delta) != 2 or delta[0].role != "user" or delta[1].role != "assistant":
        raise PeerConversationChangedError(
            "Codexia revision did not produce one exact user/assistant branch delta"
        )
    sent_message, worker_message = delta
    if sent_message.text != prompt:
        raise PeerConversationChangedError(
            "Observed Codexia revision differs from the exact sent envelope"
        )
    if worker_message.text != response.text:
        raise PeerConversationChangedError(
            "Observed revised worker message differs from the provider response"
        )
    if (
        response.conversation.message_id is not None
        and worker_message.message_id != response.conversation.message_id
    ):
        raise PeerConversationChangedError(
            "Observed revised worker identity differs from the provider response"
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


def _install_peer_revision() -> None:
    if getattr(ChatGPTPeerLoop, "_m6_5_revision_enabled", False):
        return
    ChatGPTPeerLoop.revise_requested = _revise_requested  # type: ignore[attr-defined]
    ChatGPTPeerLoop._render_codexia_revision = staticmethod(  # type: ignore[attr-defined]
        _render_codexia_revision
    )
    ChatGPTPeerLoop._m6_5_revision_enabled = True  # type: ignore[attr-defined]


def _install_supervisor_revision() -> None:
    if getattr(BackgroundWorkSupervisor, "_m6_5_revision_enabled", False):
        return

    original_dispatch_post_init = SupervisorDispatch.__post_init__
    original_dispatch_validate_exact = SupervisorDispatch._validate_exact
    original_record_checkpoint = BackgroundWorkSupervisor.record_checkpoint
    original_apply_event = BackgroundWorkSupervisor._apply_event
    original_execute = BackgroundWorkSupervisor.execute_claimed_chat
    original_reconcile = BackgroundWorkSupervisor.reconcile_in_flight_chat

    def dispatch_validate_exact(
        *,
        handoff: WorkHandoff,
        interpretation: WorkIntentInterpretation,
        cursor: ChatPeerCursor,
        proposal: ContinuationProposal,
        admission: ContinuationAdmission,
        attention: DynamicAttentionDecision,
    ) -> None:
        if not isinstance(handoff, WorkHandoff):
            raise InvalidWorkRecordError("handoff must be a WorkHandoff")
        if not isinstance(interpretation, WorkIntentInterpretation):
            raise InvalidWorkRecordError(
                "interpretation must be a WorkIntentInterpretation"
            )
        if not isinstance(cursor, ChatPeerCursor):
            raise InvalidWorkRecordError("cursor must be a ChatPeerCursor")
        if not isinstance(proposal, ContinuationProposal):
            raise InvalidWorkRecordError("proposal must be a ContinuationProposal")
        if not isinstance(admission, ContinuationAdmission):
            raise InvalidWorkRecordError("admission must be a ContinuationAdmission")
        if not isinstance(attention, DynamicAttentionDecision):
            raise InvalidWorkRecordError("attention must be a DynamicAttentionDecision")
        if admission.decision is ContinuationDecision.ADMIT:
            original_dispatch_validate_exact(
                handoff=handoff,
                interpretation=interpretation,
                cursor=cursor,
                proposal=proposal,
                admission=admission,
                attention=attention,
            )
            return
        if admission.decision is not ContinuationDecision.REVISE:
            raise InvalidWorkRecordError(
                "Only ADMIT or REVISE can become a supervisor dispatch"
            )
        admission.assert_binds(handoff, interpretation, proposal)
        attention.context.assert_binds(handoff, interpretation, proposal, admission)
        attention.assert_binds(attention.context)
        if not hmac.compare_digest(proposal.checkpoint_digest, cursor.cursor_digest):
            raise InvalidWorkRecordError(
                "Supervisor dispatch proposal is stale for the exact peer cursor"
            )
        if attention.disposition is not AttentionDisposition.KEEP_MOVING:
            raise InvalidWorkRecordError(
                "Human-attention work cannot become a supervisor dispatch"
            )

    def dispatch_post_init(self: SupervisorDispatch) -> None:
        if not isinstance(self.proposal, ContinuationProposal):
            raise InvalidWorkRecordError("proposal must be a ContinuationProposal")
        if not isinstance(self.admission, ContinuationAdmission):
            raise InvalidWorkRecordError("admission must be a ContinuationAdmission")
        if not isinstance(self.attention, DynamicAttentionDecision):
            raise InvalidWorkRecordError("attention must be a DynamicAttentionDecision")
        if self.admission.decision is ContinuationDecision.ADMIT:
            original_dispatch_post_init(self)
            return
        if self.schema_version != SUPERVISOR_DISPATCH_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported supervisor dispatch schema")
        _validate_uuid(self.dispatch_id, "dispatch_id")
        _validate_timestamp(self.created_at, "created_at")
        object.__setattr__(
            self,
            "conversation_id",
            _bounded_peer_id(self.conversation_id, "conversation_id"),
        )
        _validate_digest(self.cursor_digest, "cursor_digest")
        if self.admission.decision is not ContinuationDecision.REVISE:
            raise InvalidWorkRecordError(
                "Supervisor dispatch cannot bind this admission decision"
            )
        if self.attention.disposition is not AttentionDisposition.KEEP_MOVING:
            raise InvalidWorkRecordError(
                "Supervisor dispatch cannot suppress required human attention"
            )
        if (
            self.admission.proposal_id != self.proposal.proposal_id
            or not hmac.compare_digest(
                self.admission.proposal_digest,
                self.proposal.proposal_digest,
            )
            or self.attention.context.proposal_id != self.proposal.proposal_id
            or not hmac.compare_digest(
                self.attention.context.proposal_digest,
                self.proposal.proposal_digest,
            )
            or self.attention.context.admission_id != self.admission.admission_id
            or not hmac.compare_digest(
                self.attention.context.admission_digest,
                self.admission.admission_digest,
            )
            or not hmac.compare_digest(
                self.proposal.checkpoint_digest,
                self.cursor_digest,
            )
            or not hmac.compare_digest(
                self.attention.context.checkpoint_digest,
                self.cursor_digest,
            )
        ):
            raise InvalidWorkRecordError(
                "Supervisor dispatch lost its exact proposal/admission/checkpoint binding"
            )
        _validate_digest(self.dispatch_digest, "dispatch_digest")
        if not hmac.compare_digest(self.dispatch_digest, _digest(self._base_dict())):
            raise InvalidWorkRecordError(
                "Supervisor dispatch digest does not match its exact intent"
            )

    def record_checkpoint(
        self: BackgroundWorkSupervisor,
        work_id: str,
        *,
        proposal: ContinuationProposal,
        admission: ContinuationAdmission,
        attention: DynamicAttentionDecision,
    ):
        if not isinstance(proposal, ContinuationProposal):
            raise InvalidWorkRecordError("proposal must be a ContinuationProposal")
        if not isinstance(admission, ContinuationAdmission):
            raise InvalidWorkRecordError("admission must be a ContinuationAdmission")
        if not isinstance(attention, DynamicAttentionDecision):
            raise InvalidWorkRecordError("attention must be a DynamicAttentionDecision")
        if admission.decision is not ContinuationDecision.REVISE:
            return original_record_checkpoint(
                self,
                work_id,
                proposal=proposal,
                admission=admission,
                attention=attention,
            )
        snapshot = self.recover(work_id)
        if snapshot.status is not SupervisorStatus.READY:
            raise SupervisorStateError(
                "A new checkpoint decision requires READY supervisor state"
            )
        self._validate_checkpoint(snapshot, proposal, admission, attention)
        dispatch = None
        if attention.disposition is AttentionDisposition.KEEP_MOVING:
            dispatch = SupervisorDispatch.create(
                handoff=snapshot.handoff,
                interpretation=snapshot.interpretation,
                cursor=snapshot.cursor,
                proposal=proposal,
                admission=admission,
                attention=attention,
            )
        payload = {
            "proposal": proposal.to_dict(),
            "admission": admission.to_dict(),
            "attention": attention.to_dict(),
            "dispatch": None if dispatch is None else dispatch.to_dict(),
        }
        return self._append(
            snapshot,
            SupervisorEventKind.CHECKPOINT_DECIDED,
            payload,
        )

    def apply_event(self: BackgroundWorkSupervisor, snapshot, event):
        if (
            snapshot is None
            or event.kind is not SupervisorEventKind.CHECKPOINT_DECIDED
        ):
            return original_apply_event(self, snapshot, event)
        try:
            value = _exact_keys(
                event.payload,
                {"proposal", "admission", "attention", "dispatch"},
                "Supervisor CHECKPOINT_DECIDED payload",
            )
            admission = ContinuationAdmission.from_dict(value["admission"])
            attention = DynamicAttentionDecision.from_dict(value["attention"])
            if (
                admission.decision is not ContinuationDecision.REVISE
                or attention.disposition is not AttentionDisposition.KEEP_MOVING
            ):
                return original_apply_event(self, snapshot, event)
            if snapshot.status is not SupervisorStatus.READY:
                raise SupervisorIntegrityError(
                    "CHECKPOINT_DECIDED requires READY state"
                )
            proposal = ContinuationProposal.from_dict(value["proposal"])
            self._validate_checkpoint(snapshot, proposal, admission, attention)
            dispatch_value = value["dispatch"]
            if dispatch_value is None:
                raise SupervisorIntegrityError(
                    "REVISE + KEEP_MOVING checkpoint lost its worker revision dispatch"
                )
            dispatch = SupervisorDispatch.from_dict(dispatch_value)
            dispatch.assert_binds(
                snapshot.handoff,
                snapshot.interpretation,
                snapshot.cursor,
            )
            if (
                dispatch.proposal.proposal_digest != proposal.proposal_digest
                or dispatch.admission.admission_digest != admission.admission_digest
                or dispatch.attention.decision_digest != attention.decision_digest
            ):
                raise SupervisorIntegrityError(
                    "Supervisor revision dispatch differs from checkpoint decision"
                )
            return replace(
                snapshot,
                status=SupervisorStatus.PREPARED,
                last_proposal=proposal,
                last_admission=admission,
                last_attention=attention,
                pending_dispatch=dispatch,
                in_flight_claim_id=None,
                last_sequence=event.sequence,
                last_event_digest=event.event_digest,
            )
        except InvalidWorkRecordError as exc:
            raise SupervisorIntegrityError(
                f"Supervisor event {event.sequence} failed exact REVISE validation"
            ) from exc

    def execute_claimed_chat(
        self: BackgroundWorkSupervisor,
        lease: SupervisorDispatchLease,
        peer_loop: ChatGPTPeerLoop,
    ):
        if not isinstance(lease, SupervisorDispatchLease):
            raise SupervisorStateError("lease must be a SupervisorDispatchLease")
        if not isinstance(peer_loop, ChatGPTPeerLoop):
            raise SupervisorStateError("peer_loop must be a ChatGPTPeerLoop")
        snapshot = self.recover(lease.work_id)
        dispatch = snapshot.pending_dispatch
        if dispatch is None or dispatch.admission.decision is not ContinuationDecision.REVISE:
            return original_execute(self, lease, peer_loop)
        live = self._live_claims.pop(lease.claim_id, None)
        if live != (lease.work_id, lease.dispatch_digest):
            raise SupervisorStateError(
                "Dispatch lease is not live in this supervisor process"
            )
        snapshot = self.recover(lease.work_id)
        dispatch = snapshot.pending_dispatch
        if (
            snapshot.status is not SupervisorStatus.IN_FLIGHT
            or dispatch is None
            or dispatch.admission.decision is not ContinuationDecision.REVISE
            or snapshot.in_flight_claim_id != lease.claim_id
            or not hmac.compare_digest(
                dispatch.dispatch_digest,
                lease.dispatch_digest,
            )
        ):
            raise SupervisorStateError(
                "Live revision lease no longer matches exact durable supervisor state"
            )
        dispatch.assert_binds(
            snapshot.handoff,
            snapshot.interpretation,
            snapshot.cursor,
        )
        turn = peer_loop.revise_requested(
            cursor=snapshot.cursor,
            handoff=snapshot.handoff,
            interpretation=snapshot.interpretation,
            proposal=dispatch.proposal,
            admission=dispatch.admission,
        )
        return self.record_peer_turn(snapshot.work_id, turn=turn)

    def reconcile_in_flight_chat(
        self: BackgroundWorkSupervisor,
        work_id: str,
        *,
        peer_loop: ChatGPTPeerLoop,
    ):
        if not isinstance(peer_loop, ChatGPTPeerLoop):
            raise SupervisorStateError("peer_loop must be a ChatGPTPeerLoop")
        snapshot = self.recover(work_id)
        dispatch = snapshot.pending_dispatch
        if dispatch is None or dispatch.admission.decision is not ContinuationDecision.REVISE:
            return original_reconcile(self, work_id, peer_loop=peer_loop)
        if snapshot.status is not SupervisorStatus.IN_FLIGHT:
            raise SupervisorStateError(
                "Only an IN_FLIGHT dispatch can be reconciled without replay"
            )
        dispatch.assert_binds(
            snapshot.handoff,
            snapshot.interpretation,
            snapshot.cursor,
        )
        current = peer_loop.provider.read_messages(snapshot.cursor.conversation_id)
        delta = ChatGPTPeerLoop._require_prefix(snapshot.cursor, current)
        if not delta:
            return snapshot
        expected_prompt = _render_codexia_revision(
            handoff=snapshot.handoff,
            interpretation=snapshot.interpretation,
            proposal=dispatch.proposal,
            admission=dispatch.admission,
        )
        first = delta[0]
        if first.role != "user" or first.text != expected_prompt:
            raise SupervisorStateError(
                "In-flight reconciliation found activity other than the exact "
                "Codexia revision envelope"
            )
        if len(delta) == 1:
            return snapshot
        second = delta[1]
        if second.role != "assistant":
            raise SupervisorStateError(
                "In-flight revision reconciliation found an intervening message"
            )
        captured_codexia = CapturedChatPeerMessage.capture(
            conversation_id=snapshot.cursor.conversation_id,
            message=first,
            origin=ChatPeerMessageOrigin.CODEXIA_SEND,
            actor=peer_loop.codexia_actor,
        )
        captured_worker = CapturedChatPeerMessage.capture(
            conversation_id=snapshot.cursor.conversation_id,
            message=second,
            origin=ChatPeerMessageOrigin.ASSISTANT,
            actor=peer_loop.worker_actor,
        )
        prefix_length = len(snapshot.cursor.message_fingerprints)
        after = ChatPeerCursor.from_messages(
            snapshot.cursor.conversation_id,
            current[: prefix_length + 2],
        )
        turn = ChatPeerTurn.create(
            admission=dispatch.admission,
            before=snapshot.cursor,
            codexia_message=captured_codexia,
            worker_message=captured_worker,
            after=after,
        )
        return self.record_peer_turn(snapshot.work_id, turn=turn)

    SupervisorDispatch._validate_exact = staticmethod(  # type: ignore[method-assign]
        dispatch_validate_exact
    )
    SupervisorDispatch.__post_init__ = dispatch_post_init  # type: ignore[method-assign]
    BackgroundWorkSupervisor.record_checkpoint = record_checkpoint  # type: ignore[method-assign]
    BackgroundWorkSupervisor._apply_event = apply_event  # type: ignore[method-assign]
    BackgroundWorkSupervisor.execute_claimed_chat = execute_claimed_chat  # type: ignore[method-assign]
    BackgroundWorkSupervisor.reconcile_in_flight_chat = (  # type: ignore[method-assign]
        reconcile_in_flight_chat
    )
    BackgroundWorkSupervisor._m6_5_revision_enabled = True  # type: ignore[attr-defined]


_install_peer_revision()
_install_supervisor_revision()
