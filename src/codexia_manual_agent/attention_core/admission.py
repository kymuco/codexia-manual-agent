from __future__ import annotations

import hmac

from codexia_manual_agent.attention_core.models import AttentionNeed
from codexia_manual_agent.attention_core.projection import project_attention_need
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
    """AttentionNeed cannot be declared from current semantic state."""


class AttentionAdmission:
    """Admit exact AttentionNeed declarations into canonical Work truth."""

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
