from __future__ import annotations

import hmac
from typing import TypeAlias

from codexia_manual_agent.attention_core import AttentionAdmission, AttentionNeed
from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityNeed,
    CapabilityNeedSnapshot,
)
from codexia_manual_agent.completion_core.admission import (
    CompletionAdmissionService,
    CompletionCriterionResolverPort,
)
from codexia_manual_agent.completion_core.models import CompletionClaim
from codexia_manual_agent.delegation_core import (
    Delegation,
    DelegationAdmission,
    project_delegations,
)
from codexia_manual_agent.pack_core import (
    PackWorkflowBinding,
    project_workflow_pack_binding,
)
from codexia_manual_agent.role_core import (
    RoleAdmission,
    RoleRun,
    RoleRunSnapshot,
)
from codexia_manual_agent.work_core import (
    Work,
    WorkEvent,
    WorkSnapshot,
    WorkStore,
)
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
    | CompletionClaim
    | None
)


class WorkflowProposalAdmissionError(RuntimeError):
    """Base failure for one bounded G2.17 proposal admission."""


class WorkflowProposalAdmissionBindingError(WorkflowProposalAdmissionError):
    """WorkflowStepResult and proposal do not describe one exact computation."""


class WorkflowProposalAdmissionPackRequiredError(WorkflowProposalAdmissionError):
    """Proposal admission requires the durable Pack pin used by G2.11."""


class WorkflowProposalAdmissionCompletionResolverRequiredError(
    WorkflowProposalAdmissionError
):
    """CompletionClaim admission requires an injected completion resolver."""


class _ReadSetBoundWorkStore:
    """Bind exact optimistic read preconditions to one typed admission call."""

    def __init__(
        self,
        store: WorkStore,
        read_preconditions: tuple[WorkSnapshot, ...],
    ) -> None:
        self._store = store
        self._read_preconditions = read_preconditions

    def create(self, work: Work) -> WorkSnapshot:
        return self._store.create(work)

    def snapshot(self, work_id: str) -> WorkSnapshot:
        return self._store.snapshot(work_id)

    def events(self, work_id: str) -> tuple[WorkEvent, ...]:
        return self._store.events(work_id)

    def append(
        self,
        work_id: str,
        *,
        expected_revision: int,
        event: WorkEvent,
        read_preconditions: tuple[WorkSnapshot, ...] = (),
    ) -> WorkSnapshot:
        if read_preconditions:
            raise ValueError("nested read_preconditions are not supported")
        return self._store.append(
            work_id,
            expected_revision=expected_revision,
            event=event,
            read_preconditions=self._read_preconditions,
        )

    def append_with_child_create(
        self,
        work_id: str,
        *,
        expected_revision: int,
        event: WorkEvent,
        child_work: Work,
        read_preconditions: tuple[WorkSnapshot, ...] = (),
    ) -> tuple[WorkSnapshot, WorkSnapshot]:
        if read_preconditions:
            raise ValueError("nested read_preconditions are not supported")
        return self._store.append_with_child_create(
            work_id,
            expected_revision=expected_revision,
            event=event,
            child_work=child_work,
            read_preconditions=self._read_preconditions,
        )


class WorkflowProposalAdmissionService:
    """Admit at most one proposal from one exact WorkflowStepResult.

    This service owns no proposal computation, scheduler loop, provider
    selection, cognition dispatch, capability host, executor, authority, or
    retry policy. It only validates the G2.11 result/proposal binding and
    routes the typed proposal to its existing admission owner.

    provider_ref remains computation provenance, not authority. CompletionClaim
    routing may use it as a technical criterion-provider locator; the completion
    owner still validates the exact pinned Pack and Workflow semantics.
    """

    def __init__(
        self,
        store: WorkStore,
        *,
        completion_resolver: CompletionCriterionResolverPort | None = None,
    ) -> None:
        self._store = store
        self._completion_resolver = completion_resolver

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

        read_preconditions = self._validated_child_read_preconditions(
            result,
            events,
        )
        guarded_store = _ReadSetBoundWorkStore(
            self._store,
            read_preconditions,
        )

        if isinstance(proposal, WorkflowCandidate):
            self._validate_candidate(result, proposal, workflow)
            validate_generic_workflow_candidate_ownership(proposal)
            return WorkflowAdmission(guarded_store).admit_candidate(proposal)
        if isinstance(proposal, RoleRun):
            self._validate_role(result, proposal, workflow)
            return RoleAdmission(guarded_store).admit_start(proposal)
        if isinstance(proposal, CapabilityNeed):
            self._validate_capability(result, proposal, workflow)
            return CapabilityAdmission(guarded_store).admit_need(proposal)
        if isinstance(proposal, AttentionNeed):
            self._validate_attention(result, proposal, workflow)
            return AttentionAdmission(guarded_store).admit_need(proposal)
        if isinstance(proposal, WorkflowDelegationProposal):
            self._validate_delegation(result, proposal, workflow)
            return DelegationAdmission(guarded_store).admit(
                proposal.delegation
            )
        if isinstance(proposal, CompletionClaim):
            self._validate_completion_claim(
                result,
                proposal,
                workflow,
                pin,
            )
            if self._completion_resolver is None:
                raise WorkflowProposalAdmissionCompletionResolverRequiredError(
                    "CompletionClaim proposal requires completion resolver"
                )
            return CompletionAdmissionService(
                store=guarded_store,
                resolver=self._completion_resolver,
            ).admit(
                proposal,
                provider_ref=result.provider_ref,
            )
        raise TypeError("WorkflowStepResult contains unsupported proposal type")

    @staticmethod
    def _validated_child_read_preconditions(
        result: WorkflowStepResult,
        events: tuple[WorkEvent, ...],
    ) -> tuple[WorkSnapshot, ...]:
        if result.work_revision > len(events):
            raise WorkflowProposalAdmissionBindingError(
                "WorkflowStepResult revision exceeds durable parent chronology"
            )

        observed_events = events[: result.work_revision]
        observed_digest = (
            None if not observed_events else observed_events[-1].event_digest
        )
        if observed_digest != result.work_event_digest:
            raise WorkflowProposalAdmissionBindingError(
                "WorkflowStepResult parent read prefix no longer matches chronology"
            )

        delegations = project_delegations(observed_events)
        if len(result.child_reads) != len(delegations):
            raise WorkflowProposalAdmissionBindingError(
                "WorkflowStepResult child read set is incomplete"
            )

        snapshots: list[WorkSnapshot] = []
        for delegation, read in zip(
            delegations,
            result.child_reads,
            strict=True,
        ):
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
            if read.child.work.work_id != delegation.child_work.work_id:
                raise WorkflowProposalAdmissionBindingError(
                    "WorkflowStepResult changed child Work identity"
                )
            if read.child.work.to_dict() != delegation.child_work.to_dict():
                raise WorkflowProposalAdmissionBindingError(
                    "WorkflowStepResult changed exact child Work binding"
                )
            snapshots.append(read.child)

        return tuple(snapshots)

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
    def _validate_completion_claim(
        cls,
        result: WorkflowStepResult,
        proposal: CompletionClaim,
        workflow: WorkflowRunSnapshot,
        pin: PackWorkflowBinding,
    ) -> None:
        cls._validate_common(
            result,
            proposal_work_id=proposal.work_id,
            proposal_workflow_run_id=proposal.workflow_run_id,
            proposal_workflow_run_digest=proposal.workflow_run_digest,
            proposal_revision=proposal.work_revision,
            proposal_event_digest=proposal.work_event_digest,
            workflow=workflow,
        )
        if not hmac.compare_digest(
            proposal.work_digest,
            workflow.run.work_digest,
        ):
            raise WorkflowProposalAdmissionBindingError(
                "CompletionClaim changed canonical Work binding"
            )
        if not hmac.compare_digest(
            proposal.pack_binding_digest,
            pin.pack.binding_digest,
        ):
            raise WorkflowProposalAdmissionBindingError(
                "CompletionClaim changed canonical PackBinding"
            )
        if proposal.pack_workflow_binding_id != pin.binding_id:
            raise WorkflowProposalAdmissionBindingError(
                "CompletionClaim changed canonical PackWorkflowBinding identity"
            )
        if not hmac.compare_digest(
            proposal.pack_workflow_binding_digest,
            pin.pin_digest,
        ):
            raise WorkflowProposalAdmissionBindingError(
                "CompletionClaim changed canonical PackWorkflowBinding digest"
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
