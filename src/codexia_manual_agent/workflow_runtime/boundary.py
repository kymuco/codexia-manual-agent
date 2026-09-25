from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from codexia_manual_agent.attention_core import AttentionNeed, AttentionResponse
from codexia_manual_agent.artifact_core import ArtifactRef
from codexia_manual_agent.capability_core import (
    CapabilityNeed,
    CapabilityNeedSnapshot,
)
from codexia_manual_agent.delegation_core import Delegation
from codexia_manual_agent.evidence_core import EvidenceRef
from codexia_manual_agent.pack_core import (
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.role_core import RoleRun, RoleRunSnapshot
from codexia_manual_agent.work_core import WorkSnapshot, WorkState
from codexia_manual_agent.workflow_core import (
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRunSnapshot,
    WorkflowRunState,
)


class WorkflowImplementationError(RuntimeError):
    """Base failure for the G2.9 workflow implementation boundary."""


class WorkflowImplementationBindingError(WorkflowImplementationError):
    """Implementation or proposal changed exact bound semantics."""


class WorkflowImplementationStateError(WorkflowImplementationError):
    """Implementation cannot safely propose from the supplied derived state."""


class WorkflowImplementationOwnershipError(WorkflowImplementationError):
    """Generic WorkflowCandidate attempted to manufacture Core-owned semantic truth."""


@dataclass(frozen=True, slots=True)
class OwnedChildWorkSnapshot:
    """Ephemeral exact read view of one durable parent-owned child Work."""

    delegation: Delegation
    child: WorkSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.delegation, Delegation):
            raise TypeError("delegation must be Delegation")
        if not isinstance(self.child, WorkSnapshot):
            raise TypeError("child must be WorkSnapshot")
        if self.child.work.work_id != self.delegation.child_work.work_id:
            raise WorkflowImplementationBindingError(
                "owned child snapshot crossed child Work identity"
            )
        if not hmac.compare_digest(
            self.child.work.work_digest,
            self.delegation.child_work.work_digest,
        ):
            raise WorkflowImplementationBindingError(
                "owned child snapshot changed child Work binding"
            )
        if self.child.work.to_dict() != self.delegation.child_work.to_dict():
            raise WorkflowImplementationBindingError(
                "owned child snapshot changed exact child Work bytes"
            )


@dataclass(frozen=True, slots=True)
class WorkflowDelegationProposal:
    """Ephemeral exact-Workflow provenance wrapper around durable Delegation.

    Delegation itself remains Work-level truth. This wrapper exists only while
    one Workflow proposal is being validated/admitted and is never persisted as
    a separate canonical entity.
    """

    workflow_run_id: str
    workflow_run_digest: str
    delegation: Delegation

    def __post_init__(self) -> None:
        if not isinstance(self.workflow_run_id, str) or not self.workflow_run_id:
            raise TypeError("workflow_run_id must be non-empty text")
        if (
            not isinstance(self.workflow_run_digest, str)
            or len(self.workflow_run_digest) != 64
        ):
            raise TypeError("workflow_run_digest must be SHA-256 text")
        if not isinstance(self.delegation, Delegation):
            raise TypeError("delegation must be Delegation")

    @classmethod
    def create(
        cls,
        *,
        workflow: WorkflowRunSnapshot,
        snapshot: WorkSnapshot,
        child_objective: str,
    ) -> WorkflowDelegationProposal:
        if not isinstance(workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if not isinstance(snapshot, WorkSnapshot):
            raise TypeError("snapshot must be WorkSnapshot")
        run = workflow.run
        if run.work_id != snapshot.work.work_id:
            raise WorkflowImplementationBindingError(
                "WorkflowDelegationProposal crossed Work identity"
            )
        if not hmac.compare_digest(run.work_digest, snapshot.work.work_digest):
            raise WorkflowImplementationBindingError(
                "WorkflowDelegationProposal changed Work binding"
            )
        return cls(
            workflow_run_id=run.workflow_run_id,
            workflow_run_digest=run.run_digest,
            delegation=Delegation.create(
                parent=snapshot,
                child_objective=child_objective,
            ),
        )


WorkflowProposal: TypeAlias = (
    WorkflowCandidate
    | RoleRun
    | CapabilityNeed
    | AttentionNeed
    | WorkflowDelegationProposal
)

_RESERVED_CORE_EVENT_PREFIXES = (
    "role.",
    "capability.",
    "pack.",
    "attention.",
    "delegation.",
    "artifact.",
    "evidence.",
)


def validate_generic_workflow_candidate_ownership(
    candidate: WorkflowCandidate,
) -> None:
    """Reject generic candidates that occupy specialized Core namespaces."""

    if not isinstance(candidate, WorkflowCandidate):
        raise TypeError("candidate must be WorkflowCandidate")
    if candidate.event.kind.startswith(_RESERVED_CORE_EVENT_PREFIXES):
        raise WorkflowImplementationOwnershipError(
            "Generic WorkflowCandidate cannot manufacture "
            "role/capability/pack/attention/delegation/artifact/evidence events"
        )


class WorkflowImplementationPort(Protocol):
    """One exact pure proposal implementation for one WorkflowBinding."""

    @property
    def binding(self) -> WorkflowBinding: ...

    def propose(
        self,
        context: WorkflowStepContext,
    ) -> WorkflowProposal | None: ...


@dataclass(frozen=True, slots=True)
class WorkflowStepContext:
    """Immutable derived state supplied to one workflow implementation call.

    This object carries no WorkStore, admission surface, authority, executor,
    scheduler, model provider, host handle, or retry permission.
    """

    work: WorkSnapshot
    workflow: WorkflowRunSnapshot
    pack_binding: PackWorkflowBinding
    roles: tuple[RoleRunSnapshot, ...] = ()
    capabilities: tuple[CapabilityNeedSnapshot, ...] = ()
    attentions: tuple[AttentionNeed, ...] = ()
    attention_responses: tuple[AttentionResponse, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    owned_children: tuple[OwnedChildWorkSnapshot, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.work, WorkSnapshot):
            raise TypeError("work must be WorkSnapshot")
        if not isinstance(self.workflow, WorkflowRunSnapshot):
            raise TypeError("workflow must be WorkflowRunSnapshot")
        if not isinstance(self.pack_binding, PackWorkflowBinding):
            raise TypeError("pack_binding must be PackWorkflowBinding")
        if self.work.state is not WorkState.ACTIVE:
            raise WorkflowImplementationStateError(
                "Workflow implementation requires active Work"
            )
        if self.workflow.state is not WorkflowRunState.ACTIVE:
            raise WorkflowImplementationStateError(
                "Workflow implementation requires active WorkflowRun"
            )

        run = self.workflow.run
        if run.work_id != self.work.work.work_id:
            raise WorkflowImplementationBindingError(
                "WorkflowRun belongs to another Work"
            )
        if not hmac.compare_digest(
            run.work_digest,
            self.work.work.work_digest,
        ):
            raise WorkflowImplementationBindingError(
                "WorkflowRun changed Work binding"
            )

        pin = self.pack_binding
        if pin.work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "Pack binding crossed Work identity"
            )
        if not hmac.compare_digest(pin.work_digest, run.work_digest):
            raise WorkflowImplementationBindingError(
                "Pack binding changed Work semantics"
            )
        if pin.workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "Pack binding changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            pin.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "Pack binding changed WorkflowRun semantics"
            )

        workflow_member = PackMemberBinding.create(
            kind=PackMemberKind.WORKFLOW,
            semantic_id=run.binding.workflow_id,
            version=run.binding.version,
            binding_digest=run.binding.binding_digest,
        )
        if not pin.pack.contains(workflow_member):
            raise WorkflowImplementationBindingError(
                "Pack does not contain exact WorkflowBinding"
            )

        role_ids: set[str] = set()
        for snapshot in self.roles:
            if not isinstance(snapshot, RoleRunSnapshot):
                raise TypeError("roles must contain RoleRunSnapshot values")
            role = snapshot.run
            if role.role_run_id in role_ids:
                raise WorkflowImplementationStateError(
                    "roles contains duplicate RoleRun identity"
                )
            role_ids.add(role.role_run_id)
            self._validate_child_binding(
                child_work_id=role.work_id,
                child_work_digest=role.work_digest,
                workflow_run_id=role.workflow_run_id,
                workflow_run_digest=role.workflow_run_digest,
            )
            role_member = PackMemberBinding.create(
                kind=PackMemberKind.ROLE,
                semantic_id=role.binding.role_id,
                version=role.binding.version,
                binding_digest=role.binding.binding_digest,
            )
            if not pin.pack.contains(role_member):
                raise WorkflowImplementationBindingError(
                    "derived RoleRun state is outside pinned Pack"
                )

        need_ids: set[str] = set()
        for snapshot in self.capabilities:
            if not isinstance(snapshot, CapabilityNeedSnapshot):
                raise TypeError(
                    "capabilities must contain CapabilityNeedSnapshot values"
                )
            need = snapshot.need
            if need.need_id in need_ids:
                raise WorkflowImplementationStateError(
                    "capabilities contains duplicate CapabilityNeed identity"
                )
            need_ids.add(need.need_id)
            self._validate_child_binding(
                child_work_id=need.work_id,
                child_work_digest=need.work_digest,
                workflow_run_id=need.workflow_run_id,
                workflow_run_digest=need.workflow_run_digest,
            )
            capability_member = PackMemberBinding.create(
                kind=PackMemberKind.CAPABILITY,
                semantic_id=need.binding.capability_id,
                version=need.binding.version,
                binding_digest=need.binding.binding_digest,
            )
            if not pin.pack.contains(capability_member):
                raise WorkflowImplementationBindingError(
                    "derived CapabilityNeed state is outside pinned Pack"
                )

        attention_ids: set[str] = set()
        attention_by_id: dict[str, AttentionNeed] = {}
        for attention in self.attentions:
            if not isinstance(attention, AttentionNeed):
                raise TypeError(
                    "attentions must contain AttentionNeed values"
                )
            if attention.attention_id in attention_ids:
                raise WorkflowImplementationStateError(
                    "attentions contains duplicate AttentionNeed identity"
                )
            attention_ids.add(attention.attention_id)
            attention_by_id[attention.attention_id] = attention
            self._validate_child_binding(
                child_work_id=attention.work_id,
                child_work_digest=attention.work_digest,
                workflow_run_id=attention.workflow_run_id,
                workflow_run_digest=attention.workflow_run_digest,
            )

        response_ids: set[str] = set()
        response_sources: set[tuple[str, str]] = set()
        for response in self.attention_responses:
            if not isinstance(response, AttentionResponse):
                raise TypeError(
                    "attention_responses must contain AttentionResponse values"
                )
            if response.response_id in response_ids:
                raise WorkflowImplementationStateError(
                    "attention_responses contains duplicate AttentionResponse identity"
                )
            response_ids.add(response.response_id)
            source_key = (response.source_namespace, response.source_id)
            if source_key in response_sources:
                raise WorkflowImplementationStateError(
                    "attention_responses contains duplicate capture source identity"
                )
            response_sources.add(source_key)
            self._validate_child_binding(
                child_work_id=response.work_id,
                child_work_digest=response.work_digest,
                workflow_run_id=response.workflow_run_id,
                workflow_run_digest=response.workflow_run_digest,
            )
            attention = attention_by_id.get(response.attention_id)
            if attention is None:
                raise WorkflowImplementationBindingError(
                    "AttentionResponse references AttentionNeed outside context"
                )
            if not hmac.compare_digest(
                response.attention_need_digest,
                attention.need_digest,
            ):
                raise WorkflowImplementationBindingError(
                    "AttentionResponse changed AttentionNeed binding"
                )

        artifact_ids: set[str] = set()
        for artifact in self.artifacts:
            if not isinstance(artifact, ArtifactRef):
                raise TypeError("artifacts must contain ArtifactRef values")
            if artifact.artifact_id in artifact_ids:
                raise WorkflowImplementationStateError(
                    "artifacts contains duplicate ArtifactRef identity"
                )
            artifact_ids.add(artifact.artifact_id)

        evidence_ids: set[str] = set()
        for evidence in self.evidence_refs:
            if not isinstance(evidence, EvidenceRef):
                raise TypeError("evidence_refs must contain EvidenceRef values")
            if evidence.evidence_id in evidence_ids:
                raise WorkflowImplementationStateError(
                    "evidence_refs contains duplicate EvidenceRef identity"
                )
            evidence_ids.add(evidence.evidence_id)

        delegation_ids: set[str] = set()
        child_work_ids: set[str] = set()
        for owned in self.owned_children:
            if not isinstance(owned, OwnedChildWorkSnapshot):
                raise TypeError(
                    "owned_children must contain OwnedChildWorkSnapshot values"
                )
            delegation = owned.delegation
            if delegation.delegation_id in delegation_ids:
                raise WorkflowImplementationStateError(
                    "owned_children contains duplicate Delegation identity"
                )
            if delegation.child_work.work_id in child_work_ids:
                raise WorkflowImplementationStateError(
                    "owned_children contains duplicate child Work identity"
                )
            delegation_ids.add(delegation.delegation_id)
            child_work_ids.add(delegation.child_work.work_id)
            if delegation.parent_work_id != run.work_id:
                raise WorkflowImplementationBindingError(
                    "owned child Delegation crossed parent Work identity"
                )
            if not hmac.compare_digest(
                delegation.parent_work_digest,
                run.work_digest,
            ):
                raise WorkflowImplementationBindingError(
                    "owned child Delegation changed parent Work binding"
                )

    def _validate_child_binding(
        self,
        *,
        child_work_id: str,
        child_work_digest: str,
        workflow_run_id: str,
        workflow_run_digest: str,
    ) -> None:
        run = self.workflow.run
        if child_work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "derived child state crossed Work identity"
            )
        if not hmac.compare_digest(child_work_digest, run.work_digest):
            raise WorkflowImplementationBindingError(
                "derived child state changed Work binding"
            )
        if workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "derived child state changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "derived child state changed WorkflowRun binding"
            )


class WorkflowImplementationBoundary:
    """Invoke one exact implementation and validate one prepared proposal.

    The boundary performs no admission and owns no durable state.
    """

    def prepare(
        self,
        implementation: WorkflowImplementationPort,
        context: WorkflowStepContext,
    ) -> WorkflowProposal | None:
        if not isinstance(context, WorkflowStepContext):
            raise TypeError("context must be WorkflowStepContext")

        binding = implementation.binding
        if not isinstance(binding, WorkflowBinding):
            raise TypeError("implementation.binding must be WorkflowBinding")

        expected = context.workflow.run.binding
        if binding != expected:
            raise WorkflowImplementationBindingError(
                "Workflow implementation does not match exact WorkflowBinding"
            )

        proposal = implementation.propose(context)
        if proposal is None:
            return None
        if isinstance(proposal, WorkflowCandidate):
            self._validate_candidate(proposal, context)
            return proposal
        if isinstance(proposal, RoleRun):
            self._validate_role_run(proposal, context)
            return proposal
        if isinstance(proposal, CapabilityNeed):
            self._validate_capability_need(proposal, context)
            return proposal
        if isinstance(proposal, AttentionNeed):
            self._validate_attention_need(proposal, context)
            return proposal
        if isinstance(proposal, WorkflowDelegationProposal):
            self._validate_delegation(proposal, context)
            return proposal
        raise WorkflowImplementationError(
            "Workflow implementation returned unsupported proposal type"
        )

    @staticmethod
    def _validate_candidate(
        candidate: WorkflowCandidate,
        context: WorkflowStepContext,
    ) -> None:
        validate_generic_workflow_candidate_ownership(candidate)

        run = context.workflow.run
        if candidate.workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "WorkflowCandidate changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            candidate.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "WorkflowCandidate changed WorkflowRun binding"
            )
        if candidate.event.work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "WorkflowCandidate crossed Work identity"
            )
        if candidate.expected_revision != context.work.revision:
            raise WorkflowImplementationStateError(
                "WorkflowCandidate does not bind current Work revision"
            )
        if (
            candidate.expected_event_digest
            != context.work.last_event_digest
        ):
            raise WorkflowImplementationStateError(
                "WorkflowCandidate does not bind current Work chronology"
            )

    @staticmethod
    def _validate_role_run(
        role: RoleRun,
        context: WorkflowStepContext,
    ) -> None:
        run = context.workflow.run
        if role.work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "RoleRun crossed Work identity"
            )
        if not hmac.compare_digest(role.work_digest, run.work_digest):
            raise WorkflowImplementationBindingError(
                "RoleRun changed Work binding"
            )
        if role.workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "RoleRun changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            role.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "RoleRun changed WorkflowRun binding"
            )
        if role.start_revision != context.work.revision:
            raise WorkflowImplementationStateError(
                "RoleRun does not bind current Work revision"
            )
        if role.start_event_digest != context.work.last_event_digest:
            raise WorkflowImplementationStateError(
                "RoleRun does not bind current Work chronology"
            )
        member = PackMemberBinding.create(
            kind=PackMemberKind.ROLE,
            semantic_id=role.binding.role_id,
            version=role.binding.version,
            binding_digest=role.binding.binding_digest,
        )
        if not context.pack_binding.pack.contains(member):
            raise WorkflowImplementationBindingError(
                "RoleRun proposal is outside pinned Pack"
            )

    @staticmethod
    def _validate_attention_need(
        need: AttentionNeed,
        context: WorkflowStepContext,
    ) -> None:
        run = context.workflow.run
        if need.work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "AttentionNeed crossed Work identity"
            )
        if not hmac.compare_digest(need.work_digest, run.work_digest):
            raise WorkflowImplementationBindingError(
                "AttentionNeed changed Work binding"
            )
        if need.workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "AttentionNeed changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            need.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "AttentionNeed changed WorkflowRun binding"
            )
        if need.start_revision != context.work.revision:
            raise WorkflowImplementationStateError(
                "AttentionNeed does not bind current Work revision"
            )
        if need.start_event_digest != context.work.last_event_digest:
            raise WorkflowImplementationStateError(
                "AttentionNeed does not bind current Work chronology"
            )

    @staticmethod
    def _validate_delegation(
        proposal: WorkflowDelegationProposal,
        context: WorkflowStepContext,
    ) -> None:
        run = context.workflow.run
        delegation = proposal.delegation
        if proposal.workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "WorkflowDelegationProposal changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            proposal.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "WorkflowDelegationProposal changed WorkflowRun binding"
            )
        if delegation.parent_work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "Delegation crossed parent Work identity"
            )
        if not hmac.compare_digest(
            delegation.parent_work_digest,
            run.work_digest,
        ):
            raise WorkflowImplementationBindingError(
                "Delegation changed parent Work binding"
            )
        if delegation.start_revision != context.work.revision:
            raise WorkflowImplementationStateError(
                "Delegation does not bind current Work revision"
            )
        if delegation.start_event_digest != context.work.last_event_digest:
            raise WorkflowImplementationStateError(
                "Delegation does not bind current Work chronology"
            )

    @staticmethod
    def _validate_capability_need(
        need: CapabilityNeed,
        context: WorkflowStepContext,
    ) -> None:
        run = context.workflow.run
        if need.work_id != run.work_id:
            raise WorkflowImplementationBindingError(
                "CapabilityNeed crossed Work identity"
            )
        if not hmac.compare_digest(need.work_digest, run.work_digest):
            raise WorkflowImplementationBindingError(
                "CapabilityNeed changed Work binding"
            )
        if need.workflow_run_id != run.workflow_run_id:
            raise WorkflowImplementationBindingError(
                "CapabilityNeed changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            need.workflow_run_digest,
            run.run_digest,
        ):
            raise WorkflowImplementationBindingError(
                "CapabilityNeed changed WorkflowRun binding"
            )
        if need.start_revision != context.work.revision:
            raise WorkflowImplementationStateError(
                "CapabilityNeed does not bind current Work revision"
            )
        if need.start_event_digest != context.work.last_event_digest:
            raise WorkflowImplementationStateError(
                "CapabilityNeed does not bind current Work chronology"
            )
        member = PackMemberBinding.create(
            kind=PackMemberKind.CAPABILITY,
            semantic_id=need.binding.capability_id,
            version=need.binding.version,
            binding_digest=need.binding.binding_digest,
        )
        if not context.pack_binding.pack.contains(member):
            raise WorkflowImplementationBindingError(
                "CapabilityNeed proposal is outside pinned Pack"
            )
