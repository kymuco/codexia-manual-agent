from __future__ import annotations

import hmac

from codexia_manual_agent.attention_core.models import AttentionNeed, AttentionResponse
from codexia_manual_agent.attention_core.projection import (
    project_attention_need,
    project_attention_response,
    project_attention_responses,
)
from codexia_manual_agent.work_core import WorkStore
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowRunState,
    project_workflow_run,
)


class AttentionAdmissionError(RuntimeError):
    """Base failure for durable Gen2 AttentionNeed admission."""


class AttentionBindingError(AttentionAdmissionError):
    """AttentionNeed changed exact Work/Workflow binding."""


class AttentionStateError(AttentionAdmissionError):
    """Attention record cannot be admitted from current semantic state."""


class AttentionResponseIngressConflictError(AttentionAdmissionError):
    """One capture-source identity was reused for different response evidence."""


class AttentionAdmission:
    """Admit exact Attention records into canonical Work truth."""

    def __init__(self, store: WorkStore) -> None:
        self._store = store
        self._workflow = WorkflowAdmission(store)

    def admit_need(self, need: AttentionNeed) -> AttentionNeed:
        if not isinstance(need, AttentionNeed):
            raise TypeError("need must be AttentionNeed")

        candidate = need.to_workflow_candidate()
        events = self._store.events(need.work_id)

        if any(event.event_id == candidate.event.event_id for event in events):
            self._workflow.admit_candidate(candidate)
            recovered = project_attention_need(
                self._store.events(need.work_id),
                need.attention_id,
            )
            if recovered != need:
                raise AttentionBindingError(
                    "attention identity reused for different exact AttentionNeed"
                )
            return recovered

        workflow = project_workflow_run(events, need.workflow_run_id)
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise AttentionStateError(
                "AttentionNeed cannot be declared by terminal WorkflowRun"
            )
        if workflow.run.work_id != need.work_id:
            raise AttentionBindingError("AttentionNeed crossed Work identity")
        if not hmac.compare_digest(
            workflow.run.work_digest,
            need.work_digest,
        ):
            raise AttentionBindingError("AttentionNeed changed Work binding")
        if not hmac.compare_digest(
            workflow.run.run_digest,
            need.workflow_run_digest,
        ):
            raise AttentionBindingError(
                "AttentionNeed changed WorkflowRun binding"
            )

        self._workflow.admit_candidate(candidate)
        return project_attention_need(
            self._store.events(need.work_id),
            need.attention_id,
        )

    def admit_response(
        self,
        response: AttentionResponse,
    ) -> AttentionResponse:
        if not isinstance(response, AttentionResponse):
            raise TypeError("response must be AttentionResponse")

        events = self._store.events(response.work_id)
        existing_responses = project_attention_responses(events)
        for existing in existing_responses:
            if (
                existing.source_namespace == response.source_namespace
                and existing.source_id == response.source_id
            ):
                if (
                    existing.attention_id == response.attention_id
                    and hmac.compare_digest(
                        existing.attention_need_digest,
                        response.attention_need_digest,
                    )
                    and hmac.compare_digest(
                        existing.source_payload_digest,
                        response.source_payload_digest,
                    )
                    and existing.response_text == response.response_text
                ):
                    return existing
                raise AttentionResponseIngressConflictError(
                    "AttentionResponse source identity is already bound "
                    "to different exact evidence"
                )

        need = project_attention_need(events, response.attention_id)
        if not hmac.compare_digest(
            need.need_digest,
            response.attention_need_digest,
        ):
            raise AttentionBindingError(
                "AttentionResponse changed AttentionNeed binding"
            )
        if response.work_id != need.work_id:
            raise AttentionBindingError(
                "AttentionResponse crossed Work identity"
            )
        if not hmac.compare_digest(response.work_digest, need.work_digest):
            raise AttentionBindingError(
                "AttentionResponse changed Work binding"
            )
        if response.workflow_run_id != need.workflow_run_id:
            raise AttentionBindingError(
                "AttentionResponse changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            response.workflow_run_digest,
            need.workflow_run_digest,
        ):
            raise AttentionBindingError(
                "AttentionResponse changed WorkflowRun binding"
            )

        event = response.to_work_event()
        self._store.append(
            response.work_id,
            expected_revision=response.start_revision,
            event=event,
        )
        return project_attention_response(
            self._store.events(response.work_id),
            response.response_id,
        )

