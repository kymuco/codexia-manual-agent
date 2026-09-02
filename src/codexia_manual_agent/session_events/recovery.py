from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from codexia_manual_agent.authority.models import (
    ActionProposal,
    AuthorizationDecision,
    AuthorizationReceipt,
)
from codexia_manual_agent.domain.models import ProviderConversation
from codexia_manual_agent.session_events.models import (
    ActionRecoveryState,
    EventKind,
    RecoveryDisposition,
    SessionEventIntegrityError,
    SessionEventReceipt,
    SessionEventStateError,
    proposal_from_event_payload,
    receipt_from_event_payload,
)


@dataclass(frozen=True, slots=True)
class RecoveredAction:
    proposal: ActionProposal
    receipt: AuthorizationReceipt | None
    state: ActionRecoveryState
    execution_id: str | None
    observation_id: str | None


@dataclass(frozen=True, slots=True)
class SessionRecovery:
    session_id: str
    disposition: RecoveryDisposition
    events: tuple[SessionEventReceipt, ...]
    latest_conversation: ProviderConversation | None
    turns: int
    tool_calls: int
    model_chars: int
    tool_observation_json: tuple[str, ...]
    open_provider_request_ids: tuple[str, ...]
    actions: tuple[RecoveredAction, ...]
    interruption_reason: str | None

    @property
    def can_resume_provider(self) -> bool:
        return self.disposition is RecoveryDisposition.RESUMABLE


@dataclass(slots=True)
class _ActionState:
    proposal: ActionProposal
    receipt: AuthorizationReceipt | None = None
    state: ActionRecoveryState = ActionRecoveryState.PROPOSED
    execution_id: str | None = None
    observation_id: str | None = None


@dataclass(slots=True)
class _RunCounters:
    turns: int = 0
    tool_calls: int = 0
    model_chars: int = 0


def recover_session(
    events: tuple[SessionEventReceipt, ...],
    *,
    consumed_authorizations: Mapping[str, Mapping[str, str]],
) -> SessionRecovery:
    if not events:
        raise SessionEventIntegrityError("Cannot recover an empty M3 event chronology")
    session_id = events[0].session_id
    previous: SessionEventReceipt | None = None
    for index, event in enumerate(events):
        if event.session_id != session_id:
            raise SessionEventIntegrityError("Recovery chronology crosses session identities")
        if event.sequence != index:
            raise SessionEventIntegrityError("Recovery chronology has a sequence gap/reorder")
        if index == 0:
            if event.previous_event_digest is not None:
                raise SessionEventIntegrityError("First M3 event must not have a previous digest")
        elif previous is None or event.previous_event_digest != previous.event_digest:
            raise SessionEventIntegrityError("Recovery chronology has a broken hash chain")
        previous = event

    actions: dict[str, _ActionState] = {}
    receipts: dict[str, AuthorizationReceipt] = {}
    open_runs: set[str] = set()
    known_runs: set[str] = set()
    run_counters: dict[str, _RunCounters] = {}
    open_provider_requests: dict[str, str] = {}
    known_provider_requests: set[str] = set()
    latest_conversation: ProviderConversation | None = None
    session_provider: str | None = None
    turns = 0
    tool_calls = 0
    model_chars = 0
    observations: list[str] = []
    interruption_reason: str | None = None
    session_completed = False

    for event in events:
        payload = event.payload

        if event.kind is EventKind.SESSION_STARTED:
            if event.sequence != 0:
                raise SessionEventStateError("session_started must be the first event")
            if session_provider is not None:
                raise SessionEventStateError("session_started was recorded twice")
            session_provider = str(payload["provider"])
            continue

        if session_provider is None:
            raise SessionEventStateError("Persistent chronology lacks session_started")

        if session_completed:
            raise SessionEventStateError("No event may follow session_completed")

        if event.kind is EventKind.RUN_STARTED:
            run_id = str(payload["run_id"])
            if open_runs:
                raise SessionEventStateError(
                    "Persistent recovery chronology contains overlapping runs"
                )
            if open_provider_requests:
                raise SessionEventStateError(
                    "A new run cannot start while a provider request outcome is unresolved"
                )
            if run_id in known_runs:
                raise SessionEventStateError("run_id was reused in one session")
            known_runs.add(run_id)
            open_runs.add(run_id)
            run_counters[run_id] = _RunCounters()
            interruption_reason = None
            continue

        if event.kind is EventKind.MODEL_REQUEST_STARTED:
            run_id = str(payload["run_id"])
            request_id = str(payload["request_id"])
            if run_id not in open_runs:
                raise SessionEventStateError("Model request belongs to a non-running run")
            if str(payload["provider"]) != session_provider:
                raise SessionEventStateError("Model request changed the persistent provider identity")
            if request_id in known_provider_requests:
                raise SessionEventStateError("Provider request id was reused")
            if open_provider_requests:
                raise SessionEventStateError(
                    "Persistent session cannot have two unresolved provider requests"
                )
            request_conversation = ProviderConversation.from_dict(payload["conversation"])
            if latest_conversation is not None:
                if request_conversation != latest_conversation:
                    raise SessionEventStateError(
                        "Model request conversation differs from recovered conversation identity"
                    )
            elif request_conversation is not None:
                latest_conversation = request_conversation
            known_provider_requests.add(request_id)
            open_provider_requests[request_id] = run_id
            continue

        if event.kind is EventKind.MODEL_RESPONSE_RECORDED:
            run_id = str(payload["run_id"])
            request_id = str(payload["request_id"])
            if open_provider_requests.get(request_id) != run_id:
                raise SessionEventStateError(
                    "Model response is not bound to the exact pending provider request"
                )
            if str(payload["provider"]) != session_provider:
                raise SessionEventStateError("Model response changed the persistent provider identity")
            del open_provider_requests[request_id]
            response_chars = (
                len(str(payload["response_text"]))
                if "response_text" in payload
                else int(payload["response_chars"])
            )
            counters = run_counters[run_id]
            counters.turns += 1
            counters.model_chars += response_chars
            turns += 1
            model_chars += response_chars
            response_conversation = ProviderConversation.from_dict(payload["conversation"])
            if response_conversation is not None:
                if (
                    latest_conversation is not None
                    and latest_conversation.conversation_id is not None
                    and response_conversation.conversation_id is not None
                    and latest_conversation.conversation_id
                    != response_conversation.conversation_id
                ):
                    raise SessionEventStateError(
                        "Model response changed the recovered conversation id"
                    )
                latest_conversation = response_conversation
            continue

        if event.kind is EventKind.TOOL_OBSERVATION_RECORDED:
            run_id = str(payload["run_id"])
            if run_id not in open_runs:
                raise SessionEventStateError("Tool observation belongs to a non-running run")
            if run_id in open_provider_requests.values():
                raise SessionEventStateError(
                    "Tool observation cannot precede completion of the pending model response"
                )
            run_counters[run_id].tool_calls += 1
            tool_calls += 1
            observations.append(str(payload["observation_json"]))
            continue

        if event.kind is EventKind.RUN_COMPLETED:
            run_id = str(payload["run_id"])
            if run_id not in open_runs:
                raise SessionEventStateError("run_completed does not match an open run")
            if run_id in open_provider_requests.values():
                raise SessionEventStateError(
                    "run_completed cannot hide an unresolved provider request"
                )
            counters = run_counters[run_id]
            if (
                int(payload["turns"]) != counters.turns
                or int(payload["tool_calls"]) != counters.tool_calls
                or int(payload["model_chars"]) != counters.model_chars
            ):
                raise SessionEventStateError(
                    "run_completed counters disagree with durable response/tool chronology"
                )
            terminal_conversation = ProviderConversation.from_dict(payload["conversation"])
            if terminal_conversation != latest_conversation:
                raise SessionEventStateError(
                    "run_completed conversation differs from durable provider chronology"
                )
            open_runs.remove(run_id)
            continue

        if event.kind is EventKind.RUN_INTERRUPTED:
            run_id = str(payload["run_id"])
            if run_id not in open_runs:
                raise SessionEventStateError("run_interrupted does not match an open run")
            request_id = payload["request_id"]
            unresolved_for_run = {
                item_id
                for item_id, item_run_id in open_provider_requests.items()
                if item_run_id == run_id
            }
            if unresolved_for_run:
                if request_id is None or str(request_id) not in unresolved_for_run:
                    raise SessionEventStateError(
                        "run_interrupted must name its exact unresolved provider request"
                    )
            elif request_id is not None:
                raise SessionEventStateError(
                    "run_interrupted names a provider request that is not unresolved"
                )
            open_runs.remove(run_id)
            interruption_reason = str(payload["reason"])
            continue

        if event.kind is EventKind.SESSION_COMPLETED:
            if open_runs:
                raise SessionEventStateError("session_completed cannot hide an open run")
            if open_provider_requests:
                raise SessionEventStateError(
                    "session_completed cannot hide an unresolved provider request"
                )
            session_completed = True
            continue

        if event.kind is EventKind.ACTION_PROPOSED:
            proposal = proposal_from_event_payload(payload)
            if proposal.proposal_id in actions:
                raise SessionEventStateError("Action proposal id was durably recorded twice")
            actions[proposal.proposal_id] = _ActionState(proposal=proposal)
            continue

        if event.kind is EventKind.AUTHORIZATION_RECORDED:
            receipt = receipt_from_event_payload(payload)
            action = actions.get(receipt.proposal_id)
            if action is None:
                raise SessionEventStateError("Authorization precedes its durable proposal")
            if receipt.proposal_digest != action.proposal.proposal_digest:
                raise SessionEventStateError("Authorization is bound to another proposal payload")
            if action.receipt is not None:
                raise SessionEventStateError("Action has more than one durable authorization")
            if receipt.receipt_id in receipts:
                raise SessionEventStateError("Authorization receipt id was reused")
            receipts[receipt.receipt_id] = receipt
            action.receipt = receipt
            action.state = (
                ActionRecoveryState.AUTHORIZED_UNCONSUMED
                if receipt.decision is AuthorizationDecision.ALLOW
                else ActionRecoveryState.DENIED
            )
            continue

        if event.kind is EventKind.AUTHORIZATION_CONSUMED:
            receipt_id = str(payload["receipt_id"])
            receipt = receipts.get(receipt_id)
            if receipt is None:
                raise SessionEventStateError("Consumption precedes durable authorization")
            action = actions.get(receipt.proposal_id)
            assert action is not None
            if action.state is not ActionRecoveryState.AUTHORIZED_UNCONSUMED:
                raise SessionEventStateError("Authorization consumption is duplicated/out of order")
            if receipt.decision is not AuthorizationDecision.ALLOW:
                raise SessionEventStateError("A denial receipt was marked consumed")
            if (
                str(payload["receipt_digest"]) != receipt.receipt_digest
                or str(payload["proposal_id"]) != action.proposal.proposal_id
                or str(payload["proposal_digest"]) != action.proposal.proposal_digest
            ):
                raise SessionEventStateError("Consumption event binding changed")
            durable = consumed_authorizations.get(receipt_id)
            if durable is None:
                raise SessionEventIntegrityError(
                    "Consumption event has no matching durable one-shot registry row"
                )
            if (
                durable.get("receipt_digest") != receipt.receipt_digest
                or durable.get("proposal_id") != action.proposal.proposal_id
                or durable.get("proposal_digest") != action.proposal.proposal_digest
                or durable.get("consumed_event_id") != event.event_id
            ):
                raise SessionEventIntegrityError(
                    "Durable one-shot registry disagrees with consumption event"
                )
            action.state = ActionRecoveryState.CONSUMED_NOT_EXECUTION_RECORDED
            continue

        if event.kind is EventKind.ACTION_EXECUTED:
            proposal_id = str(payload["proposal_id"])
            action = actions.get(proposal_id)
            if action is None or action.receipt is None:
                raise SessionEventStateError("Execution lacks durable proposal/authorization")
            receipt = action.receipt
            if action.state is not ActionRecoveryState.CONSUMED_NOT_EXECUTION_RECORDED:
                raise SessionEventStateError("Execution lacks a prior one-shot consumption event")
            if (
                str(payload["proposal_digest"]) != action.proposal.proposal_digest
                or str(payload["receipt_id"]) != receipt.receipt_id
                or str(payload["receipt_digest"]) != receipt.receipt_digest
            ):
                raise SessionEventStateError("Execution event binding changed")
            action.execution_id = str(payload["execution_id"])
            action.state = ActionRecoveryState.EXECUTED
            continue

        if event.kind is EventKind.ACTION_OBSERVED:
            proposal_id = str(payload["proposal_id"])
            action = actions.get(proposal_id)
            if action is None or action.state is not ActionRecoveryState.EXECUTED:
                raise SessionEventStateError("Observation lacks a durable execution")
            if (
                str(payload["proposal_digest"]) != action.proposal.proposal_digest
                or str(payload["execution_id"]) != action.execution_id
            ):
                raise SessionEventStateError("Observation event binding changed")
            action.observation_id = str(payload["observation_id"])
            action.state = ActionRecoveryState.OBSERVED
            continue

    consumed_event_ids = {
        event.event_id
        for event in events
        if event.kind is EventKind.AUTHORIZATION_CONSUMED
    }
    for receipt_id, durable in consumed_authorizations.items():
        if durable.get("consumed_event_id") not in consumed_event_ids:
            raise SessionEventIntegrityError(
                f"Durable consumed receipt has no chronology event: {receipt_id}"
            )

    recovered_actions = tuple(
        RecoveredAction(
            proposal=item.proposal,
            receipt=item.receipt,
            state=item.state,
            execution_id=item.execution_id,
            observation_id=item.observation_id,
        )
        for item in actions.values()
    )

    unresolved_terminal_states = {
        ActionRecoveryState.PROPOSED,
        ActionRecoveryState.AUTHORIZED_UNCONSUMED,
        ActionRecoveryState.CONSUMED_NOT_EXECUTION_RECORDED,
        ActionRecoveryState.EXECUTED,
    }
    if session_completed and any(
        item.state in unresolved_terminal_states for item in recovered_actions
    ):
        raise SessionEventStateError(
            "session_completed cannot hide an unresolved authority lifecycle"
        )

    if session_completed:
        disposition = RecoveryDisposition.COMPLETED
    elif open_provider_requests:
        disposition = RecoveryDisposition.UNKNOWN_PROVIDER_OUTCOME
        interruption_reason = "unknown_provider_outcome"
    elif any(
        item.state is ActionRecoveryState.CONSUMED_NOT_EXECUTION_RECORDED
        for item in recovered_actions
    ):
        disposition = RecoveryDisposition.BLOCKED_CONSUMED_AUTHORITY
        interruption_reason = "consumed_authority_without_execution_record"
    elif any(item.state is ActionRecoveryState.EXECUTED for item in recovered_actions):
        disposition = RecoveryDisposition.INTERRUPTED
        interruption_reason = "execution_without_terminal_observation"
    elif any(
        item.state is ActionRecoveryState.AUTHORIZED_UNCONSUMED
        for item in recovered_actions
    ):
        disposition = RecoveryDisposition.INTERRUPTED
        interruption_reason = "authorized_action_pending_execution"
    elif any(item.state is ActionRecoveryState.PROPOSED for item in recovered_actions):
        disposition = RecoveryDisposition.WAITING_HUMAN
        interruption_reason = "proposal_without_authorization"
    elif interruption_reason is not None or open_runs:
        disposition = RecoveryDisposition.INTERRUPTED
    else:
        disposition = RecoveryDisposition.RESUMABLE

    return SessionRecovery(
        session_id=session_id,
        disposition=disposition,
        events=events,
        latest_conversation=latest_conversation,
        turns=turns,
        tool_calls=tool_calls,
        model_chars=model_chars,
        tool_observation_json=tuple(observations),
        open_provider_request_ids=tuple(sorted(open_provider_requests)),
        actions=recovered_actions,
        interruption_reason=interruption_reason,
    )
