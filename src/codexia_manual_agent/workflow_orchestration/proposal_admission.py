from __future__ import annotations

import hmac
from typing import TypeAlias

from codexia_manual_agent.attention_core import AttentionAdmission, AttentionNeed
from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityNeed,
    CapabilityNeedSnapshot,
)
from codexia_manual_agent.delegation_core import (
    Delegation,
    DelegationAdmission,
    project_delegations,
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
    WorkflowDelegationProposal,
    validate_generic_workflow_candidate_ownership,
)

WorkflowProposalAdmissionResult: TypeAlias = (
    WorkflowRunSnapshot
    | RoleRunSnapshot
    | CapabilityNeedSnapshot
    | AttentionNeed
    | Delegation
    | None
)


class WorkflowProposalAdmissionError(RuntimeError):
    """Base failure for one bounded G2.17 proposal admission."""


class WorkflowProposalAdmissionBindingError(WorkflowProposalAdmissionError):
    """WorkflowStepResult and proposal do not describe one exact computation."""


class WorkflowProposalAdmissionPackRequiredError(WorkflowProposalAdmissionError):
    """Proposal admission requires the durable Pack pin used by G2.11."""


class WorkflowProposalAdmissionChildReadConflictError(
    WorkflowProposalAdmissionError
):
    """A child Work changed after the Workflow computation read it."""


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
        self._delegation = DelegationAdmission(store)

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
            self._validate_child_reads_before_new_mutation(
                result,
                events,
                proposal.event.event_id,
            )
            return self._workflow.admit_candidate(proposal)
        if isinstance(proposal, RoleRun):
            self._validate_role(result, proposal, workflow)
            self._validate_child_reads_before_new_mutation(
                result,
                events,
                proposal.role_run_id,
            )
            return self._role.admit_start(proposal)
        if isinstance(proposal, CapabilityNeed):
            self._validate_capability(result, proposal, workflow)
            self._validate_child_reads_before_new_mutation(
                result,
                events,
                proposal.need_id,
            )
            return self._capability.admit_need(proposal)
        if isinstance(proposal, AttentionNeed):
            self._validate_attention(result, proposal, workflow)
            self._validate_child_reads_before_new_mutation(
                result,
                events,
                proposal.attention_id,
            )
            return self._attention.admit_need(proposal)
        if isinstance(proposal, WorkflowDelegationProposal):
            self._validate_delegation(result, proposal, workflow)
            self._validate_child_reads_before_new_mutation(
                result,
                events,
                proposal.delegation.delegation_id,
            )
            return self._delegation.admit(proposal.delegation)
        raise TypeError("WorkflowStepResult contains unsupported proposal type")

    def _validate_child_reads_before_new_mutation(
        self,
        result: WorkflowStepResult,
        events,
        proposal_event_id: str,
    ) -> None:
        # Exact retry after an ambiguous acknowledgement must remain idempotent
        # even if owned children advanced after the already-canonical proposal.
        # The typed admission owner will still verify exact proposal identity.
        if any(event.event_id == proposal_event_id for event in events):
            return

        delegations = project_delegations(events)
        if len(result.child_reads) != len(delegations):
            raise WorkflowProposalAdmissionBindingError(
                "WorkflowStepResult child read set is incomplete"
            )

        for delegation, read in zip(delegations, result.child_reads, strict=True):
            if read.delegation_id != delegation.delegation_id:
                raise WorkflowProposalAdmissionBindingError(
                    "WorkflowStepResult changed Delegation read identity"
                )
            if not hmac.compare_digest(
                read.delegation_digest,
                delegation.delegation_digest,
            ):
                raise WorkflowProposalAdmissionBindingError(
                    "WorkflowStepResult changed Delegation read binding"
                )
            if read.child_work_id != delegation.child_work.work_id:
                raise WorkflowProposalAdmissionBindingError(
                    "WorkflowStepResult changed child Work identity"
                )
            if not hmac.compare_digest(
                read.child_work_digest,
                delegation.child_work.work_digest,
            ):
                raise WorkflowProposalAdmissionBindingError(
                    "WorkflowStepResult changed child Work binding"
                )

            current = self._store.snapshot(read.child_work_id)
            if current.work.to_dict() != delegation.child_work.to_dict():
                raise WorkflowProposalAdmissionBindingError(
                    "durable child Work no longer matches Delegation binding"
                )
            if current.revision != read.revision:
                raise WorkflowProposalAdmissionChildReadConflictError(
                    "owned child Work revision changed after Workflow read"
                )
            if current.last_event_digest != read.event_digest:
                raise WorkflowProposalAdmissionChildReadConflictError(
                    "owned child Work chronology changed after Workflow read"
                )

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
    def _validate_delegation(
        cls,
        result: WorkflowStepResult,
        proposal: WorkflowDelegationProposal,
        workflow: WorkflowRunSnapshot,
    ) -> None:
        delegation = proposal.delegation
        cls._validate_common(
            result,
            proposal_work_id=delegation.parent_work_id,
            proposal_workflow_run_id=proposal.workflow_run_id,
            proposal_workflow_run_digest=proposal.workflow_run_digest,
            proposal_revision=delegation.start_revision,
            proposal_event_digest=delegation.start_event_digest,
            workflow=workflow,
        )
        if not hmac.compare_digest(
            delegation.parent_work_digest,
            workflow.run.work_digest,
        ):
            raise WorkflowProposalAdmissionBindingError(
                "Delegation changed canonical parent Work binding"
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
