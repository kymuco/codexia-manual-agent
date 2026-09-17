from __future__ import annotations

import json
from contextlib import closing
from typing import Any, Mapping

from codexia_manual_agent.domain.models import ProviderConversation, ProviderRequest, ProviderResponse
from codexia_manual_agent.work.admission import ContinuationAdmission, ContinuationDecision, ContinuationProposal
from codexia_manual_agent.work.attention import AttentionDisposition, DynamicAttentionContext, DynamicAttentionDecision
from codexia_manual_agent.work.chat_peer import ChatGPTPeerLoop, ChatPeerMessageOrigin, ChatPeerObservation, ChatPeerTurn
from codexia_manual_agent.work.contracts import InvalidWorkRecordError, WorkActorKind, WorkStatement, _exact_keys
from codexia_manual_agent.work.pilot_checkpoint import (
    MAX_PILOT_COGNITION_PROMPT_CHARS,
    MAX_PILOT_COGNITION_RESPONSE_CHARS,
    PilotCheckpointSource as _BasePilotCheckpointSource,
)
from codexia_manual_agent.work.supervisor import (
    SupervisorEventKind,
    SupervisorIntegrityError,
    SupervisorStateError,
    SupervisorWorkSnapshot,
    _observation_from_dict,
    _turn_from_dict,
)
from codexia_manual_agent.work.supervisor_driver import SupervisorCheckpointResult, SupervisorCompletion


_SEMANTIC_KEYS = {
    "proposal_text", "objective_fit", "constraint_fit", "scope_fit", "depth_fit",
    "evidence_fit", "material_human_choice", "admission_reason", "revision_request",
    "requested_human_response", "reversibility", "trajectory_impact", "alternatives",
    "attention_constraint_checks", "cognitive_disposition", "confidence_basis_points",
    "urgency", "attention_reason", "requested_response",
}


class PilotCheckpointSource(_BasePilotCheckpointSource):
    """Vertical-A hardening without changing durable M6.3/M6.5 schemas."""

    def __call__(self, snapshot: SupervisorWorkSnapshot, peer_loop: ChatGPTPeerLoop, latest_turn: ChatPeerTurn | None) -> SupervisorCheckpointResult:
        if not isinstance(snapshot, SupervisorWorkSnapshot):
            raise SupervisorStateError("snapshot must be a SupervisorWorkSnapshot")
        if not isinstance(peer_loop, ChatGPTPeerLoop):
            raise SupervisorStateError("peer_loop must be a ChatGPTPeerLoop")
        if latest_turn is not None and not isinstance(latest_turn, ChatPeerTurn):
            raise SupervisorStateError("latest_turn must be a ChatPeerTurn or None")
        current = self.supervisor.recover(snapshot.work_id)
        if (current.last_sequence != snapshot.last_sequence or current.last_event_digest != snapshot.last_event_digest or current.cursor.cursor_digest != snapshot.cursor.cursor_digest):
            raise SupervisorStateError("Delegated work advanced before pilot checkpoint cognition")

        worker = self._latest_exact_worker_evidence(snapshot, latest_turn)
        external = self._latest_exact_external_observation(snapshot)
        if worker is not None and external is not None and external.messages and all(item.origin is ChatPeerMessageOrigin.ASSISTANT for item in external.messages):
            external = None
        human_answer = self._latest_exact_pilot_human_answer(snapshot)
        response = self.provider.send(ProviderRequest(
            prompt=self._render_hardened_prompt(snapshot, latest_turn, worker, external, human_answer),
            system=self._system_prompt(),
            conversation=self._cognition_conversation,
        ))
        if not isinstance(response, ProviderResponse):
            raise SupervisorStateError("Pilot cognition provider must return ProviderResponse")
        if len(response.text) > MAX_PILOT_COGNITION_RESPONSE_CHARS:
            raise InvalidWorkRecordError("Pilot cognition response exceeds its bounded response budget")
        if response.conversation is not None and response.conversation.conversation_id:
            self._cognition_conversation = ProviderConversation(conversation_id=response.conversation.conversation_id)

        payload = self._decode_response(response.text)
        if payload["mode"] == "complete":
            if worker is None:
                raise InvalidWorkRecordError("Pilot completion requires terminal exact worker evidence")
            payload = self._normalize_legacy_completion(snapshot, payload)
        proposal = self._proposal_from_evidence(snapshot, worker, payload["proposal_text"])
        admission = ContinuationAdmission.evaluate(
            handoff=snapshot.handoff, interpretation=snapshot.interpretation, proposal=proposal,
            assessor_kind=WorkActorKind.CODEXIA, assessor=self.actor,
            objective_fit=payload["objective_fit"], constraint_fit=payload["constraint_fit"],
            scope_fit=payload["scope_fit"], depth_fit=payload["depth_fit"], evidence_fit=payload["evidence_fit"],
            material_human_choice=payload["material_human_choice"], reason=payload["admission_reason"],
            revision_request=payload["revision_request"], requested_human_response=payload["requested_human_response"],
        )
        checks = self._constraint_checks(snapshot, payload)
        context = DynamicAttentionContext.create(
            handoff=snapshot.handoff, interpretation=snapshot.interpretation, proposal=proposal, admission=admission,
            context_builder_kind=WorkActorKind.CODEXIA, context_builder=self.actor,
            basis_statements=self._attention_basis(snapshot=snapshot, proposal=proposal, external_observation=external, human_answer=human_answer),
            reversibility=payload["reversibility"], trajectory_impact=payload["trajectory_impact"], alternatives=payload["alternatives"],
            attention_constraint_checks=checks,
        )
        attention = DynamicAttentionDecision.evaluate(
            handoff=snapshot.handoff, interpretation=snapshot.interpretation, proposal=proposal, admission=admission,
            context=context, assessor_kind=WorkActorKind.CODEXIA, assessor=self.actor,
            cognitive_disposition=payload["cognitive_disposition"], confidence_basis_points=payload["confidence_basis_points"],
            urgency=payload["urgency"], reason=payload["attention_reason"], requested_response=payload["requested_response"],
        )
        if payload["mode"] == "complete" and admission.decision is ContinuationDecision.ADMIT and attention.disposition is AttentionDisposition.KEEP_MOVING:
            return SupervisorCompletion(completion=WorkStatement.create(author_kind=WorkActorKind.CODEXIA, actor=self.actor, text=payload["completion_summary"]))
        return proposal, admission, attention

    @staticmethod
    def _decode_response(text: str) -> Mapping[str, Any]:
        if not isinstance(text, str):
            raise InvalidWorkRecordError("Pilot cognition response must be text")
        try:
            raw = json.loads(text.strip())
        except (json.JSONDecodeError, AttributeError) as exc:
            raise InvalidWorkRecordError("Pilot cognition response is not valid JSON") from exc
        if not isinstance(raw, Mapping):
            raise InvalidWorkRecordError("Pilot cognition response must be an object")
        mode = raw.get("mode")
        if mode == "complete" and set(raw) == {"mode", "completion_summary"}:
            value = _exact_keys(raw, {"mode", "completion_summary"}, "M6.6 pilot legacy completion judgment")
            if not isinstance(value["completion_summary"], str) or not value["completion_summary"].strip():
                raise InvalidWorkRecordError("Pilot completion_summary must be non-empty text")
            return value
        if mode not in {"checkpoint", "complete"}:
            raise InvalidWorkRecordError("Pilot cognition mode must be 'checkpoint' or 'complete'")
        keys = {"mode", *_SEMANTIC_KEYS}
        if mode == "complete":
            keys.add("completion_summary")
        value = _exact_keys(raw, keys, f"M6.6 pilot {mode} judgment")
        PilotCheckpointSource._validate_checkpoint_shape(value)
        if mode == "complete" and (not isinstance(value["completion_summary"], str) or not value["completion_summary"].strip()):
            raise InvalidWorkRecordError("Pilot completion_summary must be non-empty text")
        return value

    @staticmethod
    def _normalize_legacy_completion(snapshot: SupervisorWorkSnapshot, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if "proposal_text" in payload:
            return payload
        if snapshot.handoff.attention_constraints:
            raise InvalidWorkRecordError("Pilot completion must evaluate every exact HUMAN attention constraint once")
        return {
            **payload, "proposal_text": None, "objective_fit": "aligned", "constraint_fit": "aligned", "scope_fit": "aligned",
            "depth_fit": "aligned", "evidence_fit": "supported", "material_human_choice": False,
            "admission_reason": "Terminal exact worker evidence satisfies the delegated objective.",
            "revision_request": None, "requested_human_response": None, "reversibility": "reversible", "trajectory_impact": "routine",
            "alternatives": "none", "attention_constraint_checks": [], "cognitive_disposition": "keep_moving",
            "confidence_basis_points": 9900, "urgency": "none", "attention_reason": "No explicit HUMAN attention constraint is present.",
            "requested_response": None,
        }

    def _proposal_from_evidence(self, snapshot: SupervisorWorkSnapshot, worker: WorkStatement | None, proposal_text: str | None) -> ContinuationProposal:
        if worker is not None:
            if proposal_text is not None:
                raise InvalidWorkRecordError("Pilot cognition cannot replace exact worker follow-up text")
            if worker.author_kind is not WorkActorKind.WORKER:
                raise SupervisorIntegrityError("Exact logical worker evidence lost WORKER authorship")
            return ContinuationProposal.create(handoff=snapshot.handoff, interpretation=snapshot.interpretation, checkpoint_digest=snapshot.cursor.cursor_digest, statement=worker)
        if not isinstance(proposal_text, str) or not proposal_text.strip():
            raise InvalidWorkRecordError("Pilot cognition must propose bounded Codexia text when no worker evidence is current")
        statement = WorkStatement.create(author_kind=WorkActorKind.CODEXIA, actor=self.actor, text=proposal_text)
        return ContinuationProposal.create(handoff=snapshot.handoff, interpretation=snapshot.interpretation, checkpoint_digest=snapshot.cursor.cursor_digest, statement=statement)

    def _latest_exact_worker_evidence(self, snapshot: SupervisorWorkSnapshot, latest_turn: ChatPeerTurn | None) -> WorkStatement | None:
        if latest_turn is not None:
            return latest_turn.worker_message.statement
        with closing(self.supervisor._connect()) as connection:
            rows = connection.execute("SELECT kind, payload_json FROM work_supervisor_events WHERE work_id = ? AND sequence <= ? ORDER BY sequence DESC", (snapshot.work_id, snapshot.last_sequence)).fetchall()
        expected = snapshot.cursor.cursor_digest
        newest: WorkStatement | None = None
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SupervisorIntegrityError("Supervisor event payload is invalid while deriving worker evidence") from exc
            if row["kind"] == SupervisorEventKind.EXTERNAL_OBSERVED.value:
                if isinstance(payload, Mapping) and "pilot_human_answer" in payload:
                    return None
                observation = _observation_from_dict(_exact_keys(payload, {"observation"}, "Supervisor EXTERNAL_OBSERVED payload")["observation"])
                if observation.after_cursor.cursor_digest != expected:
                    raise SupervisorIntegrityError("Assistant-tail observation does not bind the recovered cursor")
                if not observation.messages or any(item.origin is not ChatPeerMessageOrigin.ASSISTANT for item in observation.messages):
                    return None
                if newest is None:
                    newest = observation.messages[-1].statement
                expected = observation.before_cursor_digest
                continue
            if row["kind"] == SupervisorEventKind.PEER_TURN_RECORDED.value:
                turn = _turn_from_dict(_exact_keys(payload, {"dispatch_digest", "turn"}, "Supervisor PEER_TURN_RECORDED payload")["turn"])
                if turn.after_cursor.cursor_digest != expected:
                    raise SupervisorIntegrityError("Logical worker tail does not chain to its exact peer turn")
                if turn.handoff_id != snapshot.handoff.handoff_id or turn.interpretation_id != snapshot.interpretation.interpretation_id:
                    raise SupervisorIntegrityError("Logical worker evidence changed delegated-work identity")
                self._confirm_terminal_snapshot(snapshot)
                return newest or turn.worker_message.statement
            return None
        return None

    def _latest_exact_pilot_human_answer(self, snapshot: SupervisorWorkSnapshot) -> WorkStatement | None:
        with closing(self.supervisor._connect()) as connection:
            rows = connection.execute("SELECT kind, payload_json FROM work_supervisor_events WHERE work_id = ? AND sequence <= ? ORDER BY sequence DESC", (snapshot.work_id, snapshot.last_sequence)).fetchall()
        for row in rows:
            if row["kind"] == SupervisorEventKind.CHECKPOINT_DECIDED.value:
                return None
            if row["kind"] != SupervisorEventKind.EXTERNAL_OBSERVED.value:
                continue
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SupervisorIntegrityError("External event payload is invalid while deriving HUMAN answer") from exc
            if not isinstance(payload, Mapping) or "pilot_human_answer" not in payload:
                continue
            value = _exact_keys(payload, {"pilot_human_answer", "waiting_sequence", "waiting_event_digest", "attention_decision_digest"}, "Supervisor pilot human-answer payload")
            answer = WorkStatement.from_dict(value["pilot_human_answer"])
            if answer.author_kind is not WorkActorKind.HUMAN:
                raise SupervisorIntegrityError("Pilot human answer lost HUMAN authorship")
            self._confirm_terminal_snapshot(snapshot)
            return answer
        return None

    @classmethod
    def _render_hardened_prompt(cls, snapshot: SupervisorWorkSnapshot, latest_turn: ChatPeerTurn | None, worker: WorkStatement | None, external: ChatPeerObservation | None, human_answer: WorkStatement | None) -> str:
        prompt = super()._render_prompt(snapshot=snapshot, latest_turn=latest_turn, external_observation=external, human_answer=human_answer)
        if worker is not None and latest_turn is None:
            prompt = prompt.replace("proposal_text MUST contain the next bounded Codexia-authored worker step.", "proposal_text MUST be null because current exact logical worker evidence exists and cannot be replaced.")
            prompt = prompt.replace("mode=complete is forbidden because the terminal exact event is not a worker turn.", "mode=complete is allowed only if current exact logical worker evidence satisfies the objective/completion expectation and this same response evaluates every HUMAN attention constraint exactly once through M6.2/M6.4.")
        old = '{"mode":"complete","completion_summary":"bounded Codexia-authored explanation of why the objective is complete"}'
        checks = [{"statement_digest": item.statement_digest, "status": "clear|triggered|uncertain", "reason": "reason"} for item in snapshot.handoff.attention_constraints]
        governed = json.dumps({
            "mode": "complete", "proposal_text": None, "objective_fit": "aligned", "constraint_fit": "aligned", "scope_fit": "aligned",
            "depth_fit": "aligned", "evidence_fit": "supported", "material_human_choice": False, "admission_reason": "reason",
            "revision_request": None, "requested_human_response": None, "reversibility": "reversible", "trajectory_impact": "routine",
            "alternatives": "none", "attention_constraint_checks": checks, "cognitive_disposition": "keep_moving",
            "confidence_basis_points": 9000, "urgency": "none", "attention_reason": "reason", "requested_response": None,
            "completion_summary": "bounded Codexia-authored explanation of why the objective is complete",
        }, ensure_ascii=False, separators=(",", ":"))
        prompt = prompt.replace(old, governed)
        prompt += "\n\nCurrent exact logical worker evidence:\n" + ("null" if worker is None else json.dumps(worker.to_dict(), ensure_ascii=False, sort_keys=True))
        if len(prompt) > MAX_PILOT_COGNITION_PROMPT_CHARS:
            raise InvalidWorkRecordError("Pilot cognition semantic projection exceeds its prompt budget")
        return prompt
