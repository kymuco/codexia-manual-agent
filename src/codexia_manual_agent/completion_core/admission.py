from __future__ import annotations

import hmac

from codexia_manual_agent.artifact_core import project_artifact_refs
from codexia_manual_agent.completion_core.boundary import (
    CompletionCriterionBoundary,
    CompletionCriterionContext,
)
from codexia_manual_agent.completion_core.models import (
    COMPLETION_CLAIM_ADMITTED_EVENT,
    CompletionClaim,
)
from codexia_manual_agent.completion_core.projection import (
    project_admitted_completion_claim,
    project_admitted_completion_claims,
)
from codexia_manual_agent.evidence_core import project_evidence_refs
from codexia_manual_agent.invariant_bridge.completion_criterion import (
    InvariantCompletionCriterionBridge,
)
from codexia_manual_agent.pack_core import project_workflow_pack_binding
from codexia_manual_agent.work_core import (
    WorkConcurrencyError,
    WorkState,
    WorkStore,
)
from codexia_manual_agent.workflow_core import (
    WorkflowRunState,
    project_workflow_run,
)


class CompletionAdmissionError(RuntimeError):
    """Base failure for durable Gen2 CompletionClaim admission."""


class CompletionClaimBindingError(CompletionAdmissionError):
    """CompletionClaim changed exact canonical Work/Workflow/Pack semantics."""


class CompletionClaimStateError(CompletionAdmissionError):
    """CompletionClaim cannot be admitted from the recovered semantic state."""


class CompletionClaimBasisError(CompletionAdmissionError):
    """CompletionClaim basis is not exact durable Work-level truth."""


class CompletionClaimIdentityConflictError(CompletionAdmissionError):
    """One claim identity was reused for different exact semantics."""


class CompletionCriteriaRejected(CompletionAdmissionError):
    """Pinned Pack criterion rejected the CompletionClaim."""


class CompletionAdmissionService:
    """Admit one exact CompletionClaim without completing Work.

    Admission proves exact current Work chronology, durable Workflow/Pack
    provenance, exact ArtifactRef/EvidenceRef basis membership, and acceptance
    by the criterion implementation resolved from the exact pinned Pack.

    The admitted event is non-terminal. This service does not create
    work.completed, inspect child completion, grant authority, execute effects,
    schedule work, or prove cleanup.
    """

    def __init__(
        self,
        *,
        store: WorkStore,
        resolver: InvariantCompletionCriterionBridge,
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

        context = CompletionCriterionContext(
            claim=claim,
            work=current,
            workflow=workflow,
            pack_binding=pin,
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

    @staticmethod
    def _validate_basis(
        events,
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
