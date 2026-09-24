from __future__ import annotations

import hmac
from typing import TypeAlias

from codexia_manual_agent.attention_core import AttentionAdmission, AttentionNeed
from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityNeed,
    CapabilityNeedSnapshot,
)
from codexia_manual_agent.pack_core import project_workflow_pack_binding
from codexia_manual_agent.role_core import (
    RoleAdmission,
    RoleRun,
    RoleRunSnapshot,
)
from codexia_manual_agent.work_core import WorkStore
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowCandidate,
    WorkflowRunSnapshot,
    project_workflow_run,
)
from codexia_manual_agent.workflow_orchestration.step import WorkflowStepResult
from codexia_manual_agent.workflow_runtime import (
    validate_generic_workflow_candidate_ownership,
)

WorkflowProposalAdmissionResult: TypeAlias = (
    WorkflowRunSnapshot
    | RoleRunSnapshot
    | CapabilityNeedSnapshot
    | AttentionNeed
    | None
)


class WorkflowProposalAdmissionError(RuntimeError):
    """Base failure for one bounded G2.17 proposal admission."""


class WorkflowProposalAdmissionBindingError(WorkflowProposalAdmissionError):
    """WorkflowStepResult and proposal do not describe one exact computation."""


class WorkflowProposalAdmissionPackRequiredError(WorkflowProposalAdmissionError):
    """Proposal admission requires the durable Pack pin used by G2.11."""


class WorkflowProposalAdmissionService:
    """Admit at most one proposal from one exact WorkflowStepResult.

    This service owns no proposal computation, scheduler loop, provider
    selection, cognition dispatch, capability host, executor, authority, or
    retry policy. It only validates the G2.11 result/proposal binding and
    routes the typed proposal to its existing admission owner.

    provider_ref remains computation provenance only. It is deliberately not
    interpreted as admission authority.
    """

    def __init__(self, store: WorkStore) -> None:
        self._store = store
        self._workflow = WorkflowAdmission(store)
        self._role = RoleAdmission(store)
        self._capability = CapabilityAdmission(store)
        self._attention = AttentionAdmission(store)

    def admit(
        self,
        result: WorkflowStepResult,
    ) -> WorkflowProposalAdmissionResult:
        if not isinstance(result, WorkflowStepResult):
            raise TypeError("result must be WorkflowStepResult")

        proposal = result.proposal
        if proposal is None:
            return None

        events = self._store.events(result.work_id)
        workflow = project_workflow_run(events, result.workflow_run_id)
        pin = project_workflow_pack_binding(events, result.workflow_run_id)
        if pin is None:
            raise WorkflowProposalAdmissionPackRequiredError(
                "WorkflowStepResult WorkflowRun has no durable Pack binding"
            )

        if workflow.run.work_id != result.work_id:
            raise WorkflowProposalAdmissionBindingError(
                "WorkflowStepResult crossed Work identity"
            )
        if not hmac.compare_digest(
            workflow.run.binding.binding_digest,
            result.workflow_binding_digest,
        ):
            raise WorkflowProposalAdmissionBindingError(
                "WorkflowStepResult changed WorkflowBinding"
            )
        if not hmac.compare_digest(
            pin.pack.binding_digest,
            result.pack_binding_digest,
        ):
            raise WorkflowProposalAdmissionBindingError(
                "WorkflowStepResult changed PackBinding"
            )

        if isinstance(proposal, WorkflowCandidate):
            self._validate_candidate(result, proposal, workflow)
            validate_generic_workflow_candidate_ownership(proposal)
            return self._workflow.admit_candidate(proposal)
        if isinstance(proposal, RoleRun):
            self._validate_role(result, proposal, workflow)
            return self._role.admit_start(proposal)
        if isinstance(proposal, CapabilityNeed):
            self._validate_capability(result, proposal, workflow)
            return self._capability.admit_need(proposal)
        if isinstance(proposal, AttentionNeed):
            self._validate_attention(result, proposal, workflow)
            return self._attention.admit_need(proposal)
        raise TypeError("WorkflowStepResult contains unsupported proposal type")

    @staticmethod
    def _validate_common(
        result: WorkflowStepResult,
        *,
        proposal_work_id: str,
        proposal_workflow_run_id: str,
        proposal_workflow_run_digest: str,
        proposal_revision: int,
        proposal_event_digest: str | None,
        workflow: WorkflowRunSnapshot,
    ) -> None:
        if proposal_work_id != result.work_id:
            raise WorkflowProposalAdmissionBindingError(
                "proposal crossed WorkflowStepResult Work identity"
            )
        if proposal_workflow_run_id != result.workflow_run_id:
            raise WorkflowProposalAdmissionBindingError(
                "proposal crossed WorkflowStepResult WorkflowRun identity"
            )
        if not hmac.compare_digest(
            proposal_workflow_run_digest,
            workflow.run.run_digest,
        ):
            raise WorkflowProposalAdmissionBindingError(
                "proposal changed canonical WorkflowRun binding"
            )
        if proposal_revision != result.work_revision:
            raise WorkflowProposalAdmissionBindingError(
                "proposal does not bind WorkflowStepResult revision"
            )
        if proposal_event_digest != result.work_event_digest:
            raise WorkflowProposalAdmissionBindingError(
                "proposal does not bind WorkflowStepResult chronology"
            )

    @classmethod
    def _validate_candidate(
        cls,
        result: WorkflowStepResult,
        proposal: WorkflowCandidate,
        workflow: WorkflowRunSnapshot,
    ) -> None:
        cls._validate_common(
            result,
            proposal_work_id=proposal.event.work_id,
            proposal_workflow_run_id=proposal.workflow_run_id,
            proposal_workflow_run_digest=proposal.workflow_run_digest,
            proposal_revision=proposal.expected_revision,
            proposal_event_digest=proposal.expected_event_digest,
            workflow=workflow,
        )

    @classmethod
    def _validate_role(
        cls,
        result: WorkflowStepResult,
        proposal: RoleRun,
        workflow: WorkflowRunSnapshot,
    ) -> None:
        cls._validate_common(
            result,
            proposal_work_id=proposal.work_id,
            proposal_workflow_run_id=proposal.workflow_run_id,
            proposal_workflow_run_digest=proposal.workflow_run_digest,
            proposal_revision=proposal.start_revision,
            proposal_event_digest=proposal.start_event_digest,
            workflow=workflow,
        )

    @classmethod
    def _validate_attention(
        cls,
        result: WorkflowStepResult,
        proposal: AttentionNeed,
        workflow: WorkflowRunSnapshot,
    ) -> None:
        cls._validate_common(
            result,
            proposal_work_id=proposal.work_id,
            proposal_workflow_run_id=proposal.workflow_run_id,
            proposal_workflow_run_digest=proposal.workflow_run_digest,
            proposal_revision=proposal.start_revision,
            proposal_event_digest=proposal.start_event_digest,
            workflow=workflow,
        )

    @classmethod
    def _validate_capability(
        cls,
        result: WorkflowStepResult,
        proposal: CapabilityNeed,
        workflow: WorkflowRunSnapshot,
    ) -> None:
        cls._validate_common(
            result,
            proposal_work_id=proposal.work_id,
            proposal_workflow_run_id=proposal.workflow_run_id,
            proposal_workflow_run_digest=proposal.workflow_run_digest,
            proposal_revision=proposal.start_revision,
            proposal_event_digest=proposal.start_event_digest,
            workflow=workflow,
        )
