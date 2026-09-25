from __future__ import annotations

import hmac
from typing import Protocol

from codexia_manual_agent.artifact_core import project_artifact_refs
from codexia_manual_agent.capability_core import project_capability_needs
from codexia_manual_agent.completion_core.boundary import (
    CompletionCriterionBoundary,
    CompletionCriterionContext,
    CompletionCriterionPort,
)
from codexia_manual_agent.completion_core.models import (
    COMPLETION_CLAIM_ADMITTED_EVENT,
    CompletionClaim,
)
from codexia_manual_agent.completion_core.projection import (
    project_admitted_completion_claim,
    project_admitted_completion_claims,
)
from codexia_manual_agent.completion_core.work_completion_projection import (
    WorkCompletionProjectionError,
    project_work_completion,
)
from codexia_manual_agent.delegation_core import project_delegations
from codexia_manual_agent.evidence_core import project_evidence_refs
from codexia_manual_agent.pack_core import (
    PackWorkflowBinding,
    project_workflow_pack_binding,
)
from codexia_manual_agent.work_core import (
    WorkConcurrencyError,
    WorkEvent,
    WorkState,
    WorkStore,
)
from codexia_manual_agent.workflow_core import (
    WorkflowBinding,
    WorkflowRunSnapshot,
    WorkflowRunState,
    project_workflow_run,
)


class ResolvedCompletionCriterionPort(Protocol):
    """Resolved criterion semantics required by CompletionAdmissionService."""

    @property
    def workflow_binding(self) -> WorkflowBinding: ...

    @property
    def pack_binding_digest(self) -> str: ...

    @property
    def criterion(self) -> CompletionCriterionPort: ...


class CompletionCriterionResolverPort(Protocol):
    """Technical resolution surface consumed by completion admission."""

    def resolve(
        self,
        *,
        provider_ref: str,
        workflow: WorkflowRunSnapshot,
        pack_binding: PackWorkflowBinding,
    ) -> ResolvedCompletionCriterionPort: ...


class CompletionAdmissionError(RuntimeError):
    """Base failure for durable Gen2 CompletionClaim admission."""


class CompletionClaimBindingError(CompletionAdmissionError):
    """CompletionClaim changed exact canonical Work/Workflow/Pack semantics."""


class CompletionClaimStateError(CompletionAdmissionError):
    """CompletionClaim cannot be admitted from the recovered semantic state."""


class CompletionClaimBasisError(CompletionAdmissionError):
    """CompletionClaim basis is not exact durable semantic truth."""


class CompletionClaimIdentityConflictError(CompletionAdmissionError):
    """One claim identity was reused for different exact semantics."""


class CompletionCriteriaRejected(CompletionAdmissionError):
    """Pinned Pack criterion rejected the CompletionClaim."""


class CompletionAdmissionService:
    """Admit one exact CompletionClaim without completing Work.

    Admission proves exact current Work chronology, durable Workflow/Pack
    provenance, exact ArtifactRef/EvidenceRef/child-WorkCompletion basis
    membership, and acceptance
    by the criterion implementation resolved from the exact pinned Pack.

    The admitted event is non-terminal. This service may verify exact owned
    child WorkCompletion references, but it does not create work.completed,
    infer parent meaning from child completion, grant authority, execute
    effects, schedule work, or prove cleanup.
    """

    def __init__(
        self,
        *,
        store: WorkStore,
        resolver: CompletionCriterionResolverPort,
        boundary: CompletionCriterionBoundary | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._boundary = boundary or CompletionCriterionBoundary()

    def admit(
        self,
        claim: CompletionClaim,
        *,
        provider_ref: str,
    ) -> CompletionClaim:
        if not isinstance(claim, CompletionClaim):
            raise TypeError("claim must be CompletionClaim")

        events = self._store.events(claim.work_id)

        # Exact retry after an ambiguous caller acknowledgement does not
        # re-resolve or re-evaluate extension code.
        for existing in project_admitted_completion_claims(events):
            if existing.claim_id != claim.claim_id:
                continue
            if existing == claim:
                return existing
            raise CompletionClaimIdentityConflictError(
                "CompletionClaim identity is already bound to different exact semantics"
            )

        current = self._store.snapshot(claim.work_id)
        if current.work.work_id != claim.work_id:
            raise CompletionClaimBindingError(
                "CompletionClaim crossed Work identity"
            )
        if not hmac.compare_digest(
            current.work.work_digest,
            claim.work_digest,
        ):
            raise CompletionClaimBindingError(
                "CompletionClaim changed Work binding"
            )
        if current.state is not WorkState.ACTIVE:
            raise CompletionClaimStateError(
                "CompletionClaim cannot be admitted on terminal Work"
            )
        if current.revision != claim.work_revision:
            raise WorkConcurrencyError(
                f"Stale Work revision: expected={claim.work_revision} "
                f"actual={current.revision}"
            )
        if current.last_event_digest != claim.work_event_digest:
            raise WorkConcurrencyError(
                "CompletionClaim does not bind exact current chronology"
            )

        workflow = project_workflow_run(
            events,
            claim.workflow_run_id,
        )
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise CompletionClaimStateError(
                "CompletionClaim requires active WorkflowRun"
            )
        run = workflow.run
        if not hmac.compare_digest(
            run.run_digest,
            claim.workflow_run_digest,
        ):
            raise CompletionClaimBindingError(
                "CompletionClaim changed WorkflowRun binding"
            )
        if run.work_id != claim.work_id:
            raise CompletionClaimBindingError(
                "WorkflowRun belongs to another Work"
            )
        if not hmac.compare_digest(
            run.work_digest,
            claim.work_digest,
        ):
            raise CompletionClaimBindingError(
                "WorkflowRun changed Work binding"
            )

        pin = project_workflow_pack_binding(
            events,
            claim.workflow_run_id,
        )
        if pin is None:
            raise CompletionClaimBindingError(
                "CompletionClaim PackWorkflowBinding is not durably admitted"
            )
        if pin.binding_id != claim.pack_workflow_binding_id:
            raise CompletionClaimBindingError(
                "CompletionClaim changed PackWorkflowBinding identity"
            )
        if not hmac.compare_digest(
            pin.pin_digest,
            claim.pack_workflow_binding_digest,
        ):
            raise CompletionClaimBindingError(
                "CompletionClaim changed PackWorkflowBinding digest"
            )
        if not hmac.compare_digest(
            pin.pack.binding_digest,
            claim.pack_binding_digest,
        ):
            raise CompletionClaimBindingError(
                "CompletionClaim changed PackBinding semantics"
            )

        self._validate_basis(events, claim)

        capabilities = tuple(
            capability
            for capability in project_capability_needs(events)
            if capability.need.workflow_run_id == run.workflow_run_id
        )
        context = CompletionCriterionContext(
            claim=claim,
            work=current,
            workflow=workflow,
            pack_binding=pin,
            capabilities=capabilities,
        )
        resolved = self._resolver.resolve(
            provider_ref=provider_ref,
            workflow=workflow,
            pack_binding=pin,
        )
        if resolved.workflow_binding != run.binding:
            raise CompletionClaimBindingError(
                "Resolved criterion changed WorkflowBinding semantics"
            )
        if not hmac.compare_digest(
            resolved.pack_binding_digest,
            claim.pack_binding_digest,
        ):
            raise CompletionClaimBindingError(
                "Resolved criterion changed PackBinding semantics"
            )

        result = self._boundary.evaluate(
            resolved.criterion,
            context,
        )
        if not result.accepted:
            raise CompletionCriteriaRejected(result.reason)

        event = current.next_event(
            kind=COMPLETION_CLAIM_ADMITTED_EVENT,
            payload={"completion_claim": claim.to_dict()},
            event_id=claim.claim_id,
        )
        self._store.append(
            claim.work_id,
            expected_revision=claim.work_revision,
            event=event,
        )
        return project_admitted_completion_claim(
            self._store.events(claim.work_id),
            claim.claim_id,
        )

    def _validate_basis(
        self,
        events: tuple[WorkEvent, ...],
        claim: CompletionClaim,
    ) -> None:
        artifacts = {
            artifact.artifact_id: artifact
            for artifact in project_artifact_refs(events)
        }
        for artifact in claim.artifact_refs:
            durable = artifacts.get(artifact.artifact_id)
            if durable is None:
                raise CompletionClaimBasisError(
                    "CompletionClaim references ArtifactRef outside Work chronology"
                )
            if durable != artifact:
                raise CompletionClaimBasisError(
                    "CompletionClaim changed durable ArtifactRef semantics"
                )

        evidence_refs = {
            evidence.evidence_id: evidence
            for evidence in project_evidence_refs(events)
        }
        for evidence in claim.evidence_refs:
            durable = evidence_refs.get(evidence.evidence_id)
            if durable is None:
                raise CompletionClaimBasisError(
                    "CompletionClaim references EvidenceRef outside Work chronology"
                )
            if durable != evidence:
                raise CompletionClaimBasisError(
                    "CompletionClaim changed durable EvidenceRef semantics"
                )

        delegations = {
            delegation.child_work.work_id: delegation
            for delegation in project_delegations(events)
        }
        for child_ref in claim.child_completion_refs:
            delegation = delegations.get(child_ref.work_id)
            if delegation is None:
                raise CompletionClaimBasisError(
                    "CompletionClaim references WorkCompletion outside owned children"
                )

            child_events = self._store.events(child_ref.work_id)
            child = self._store.snapshot(child_ref.work_id)
            if child.work.to_dict() != delegation.child_work.to_dict():
                raise CompletionClaimBasisError(
                    "CompletionClaim child Work changed exact Delegation binding"
                )
            if child.revision != len(child_events):
                raise WorkConcurrencyError(
                    "CompletionClaim child Work changed while recovering completion"
                )
            observed_digest = (
                None
                if not child_events
                else child_events[-1].event_digest
            )
            if child.last_event_digest != observed_digest:
                raise WorkConcurrencyError(
                    "CompletionClaim child Work chronology changed during read"
                )
            if child.state is not WorkState.COMPLETED:
                raise CompletionClaimBasisError(
                    "CompletionClaim child WorkCompletion is not terminal"
                )

            try:
                completion = project_work_completion(child_events)
            except WorkCompletionProjectionError as exc:
                raise CompletionClaimBasisError(
                    "CompletionClaim child lacks valid structured WorkCompletion"
                ) from exc
            if completion is None:
                raise CompletionClaimBasisError(
                    "CompletionClaim child lacks structured WorkCompletion"
                )
            if child.terminal_event_id != child_ref.completion_event_id:
                raise CompletionClaimBasisError(
                    "CompletionClaim changed child completion event identity"
                )
            if completion.completion_id != child_ref.completion_event_id:
                raise CompletionClaimBasisError(
                    "CompletionClaim changed child WorkCompletion identity"
                )
            if not hmac.compare_digest(
                completion.completion_digest,
                child_ref.completion_digest,
            ):
                raise CompletionClaimBasisError(
                    "CompletionClaim changed child WorkCompletion digest"
                )
