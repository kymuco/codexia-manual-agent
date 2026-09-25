from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codexia_manual_agent.capability_core import (
    CapabilityNeedSnapshot,
    CapabilityNeedState,
)
from codexia_manual_agent.completion_core import (
    COMPLETION_CLAIM_ADMITTED_EVENT,
    CompletionClaim,
    WorkCompletion,
    WorkCompletionAdmissionService,
)
from codexia_manual_agent.evidence_core import EvidenceAdmission, EvidenceRef
from codexia_manual_agent.invariant_bridge import (
    InvariantCompletionCriterionBridge,
    InvariantPackDistributionBridge,
    InvariantWorkflowImplementationBridge,
    ManagedPluginServicePort,
)
from codexia_manual_agent.pack_core import PackAdmission, PackWorkflowBinding
from codexia_manual_agent.work_core import (
    Work,
    WorkIngressBinding,
    WorkSnapshot,
    WorkState,
    WorkStore,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    CapabilityProgressionService,
    WorkflowProgressionService,
    WorkflowStepReadPrecondition,
)
from codexia_manual_agent.workflow_runtime import (
    PROCESS_OUTCOME_EVIDENCE_KIND,
    process_outcome_evidence_locator,
    standalone_process_v2_capability_binding,
    standalone_process_v2_workflow_binding,
)
from codexia_manual_agent.standalone_host.process_capability import (
    StandaloneProcessCapabilityPort,
)


class StandaloneProcessWorkError(RuntimeError):
    """Base failure for the standalone process Work vertical."""


class StandaloneProcessWorkBindingError(StandaloneProcessWorkError):
    """Resolved provider semantics do not match the v2 process vertical."""


class StandaloneProcessWorkIncompleteError(StandaloneProcessWorkError):
    """A durable process Work stopped before semantic completion."""

    def __init__(
        self,
        *,
        work_id: str,
        capability_state: CapabilityNeedState,
    ) -> None:
        self.work_id = work_id
        self.capability_state = capability_state
        super().__init__(
            "Standalone process Work did not complete: "
            f"capability_state={capability_state.value}, work_id={work_id}"
        )


@dataclass(frozen=True, slots=True)
class StandaloneProcessWorkResult:
    """Completed result of one explicit standalone process vertical."""

    snapshot: WorkSnapshot
    capability: CapabilityNeedSnapshot
    evidence: EvidenceRef
    claim: CompletionClaim
    completion: WorkCompletion

    def __post_init__(self) -> None:
        if self.snapshot.state is not WorkState.COMPLETED:
            raise ValueError("snapshot must be terminal COMPLETED Work")
        if self.capability.state is not CapabilityNeedState.SUCCEEDED:
            raise ValueError("capability must be SUCCEEDED")
        if self.capability.outcome is None:
            raise ValueError("succeeded capability must contain outcome")
        if self.evidence.evidence_id != self.capability.outcome.outcome_id:
            raise ValueError("evidence must reference exact CapabilityOutcome")
        if self.evidence.evidence_digest != self.capability.outcome.outcome_digest:
            raise ValueError("evidence changed CapabilityOutcome integrity")
        if self.claim.evidence_refs != (self.evidence,):
            raise ValueError("claim must use exact process outcome evidence")
        if self.completion.claim_id != self.claim.claim_id:
            raise ValueError("completion must bind admitted claim")
        if self.snapshot.terminal_event_id != self.completion.completion_id:
            raise ValueError("snapshot terminal identity must match WorkCompletion")


class StandaloneProcessWorkService:
    """Compose one process Work from ingress through WorkCompletion.

    This is a standalone-host composition root, not Core scheduling policy.
    The sequence is deliberately explicit and finite:

      Work ingress
      -> WorkflowRun + exact Pack pin
      -> one Workflow step -> CapabilityNeed
      -> one standalone capability dispatch
      -> exact CapabilityOutcome EvidenceRef
      -> one Workflow step -> CompletionClaim admission
      -> WorkCompletion terminal admission

    Each inner service retains its existing authority, CAS, no-redispatch and
    semantic validation boundaries. This service introduces no generic queue,
    scheduler, retry loop, or new durable state.
    """

    def __init__(
        self,
        *,
        store: WorkStore,
        plugin_service: ManagedPluginServicePort,
        provider_ref: str,
        workspace: str | Path,
    ) -> None:
        if not isinstance(provider_ref, str) or not provider_ref:
            raise TypeError("provider_ref must be non-empty text")
        self._store = store
        self._plugin_service = plugin_service
        self._provider_ref = provider_ref
        self._workspace = Path(workspace)

    def run(
        self,
        *,
        objective: str,
        source_namespace: str,
        source_id: str,
        payload_digest: str,
        approved: bool,
        actor: str = "local-human",
        reason: str | None = None,
    ) -> StandaloneProcessWorkResult:
        distribution = InvariantPackDistributionBridge(
            self._plugin_service
        ).resolve(self._provider_ref)
        workflow_binding = standalone_process_v2_workflow_binding()
        capability_binding = standalone_process_v2_capability_binding()

        if distribution.workflows != (workflow_binding,):
            raise StandaloneProcessWorkBindingError(
                "Provider does not distribute exact standalone process v2 Workflow"
            )
        if distribution.roles:
            raise StandaloneProcessWorkBindingError(
                "Standalone process v2 Pack unexpectedly contains Role bindings"
            )
        if distribution.capabilities != (capability_binding,):
            raise StandaloneProcessWorkBindingError(
                "Provider does not distribute exact process Capability binding"
            )

        work = Work.create(
            objective=objective,
            ingress=WorkIngressBinding.create(
                source_namespace=source_namespace,
                source_id=source_id,
                payload_digest=payload_digest,
            ),
        )
        initial = self._store.create(work)
        workflow = WorkflowAdmission(self._store).admit_start(
            WorkflowRun.create(
                snapshot=initial,
                binding=workflow_binding,
            )
        )
        PackAdmission(self._store).admit_workflow_binding(
            PackWorkflowBinding.create(
                workflow=workflow,
                snapshot=self._store.snapshot(work.work_id),
                pack=distribution.pack,
            )
        )

        workflow_progression = WorkflowProgressionService(
            store=self._store,
            resolver=InvariantWorkflowImplementationBridge(
                self._plugin_service
            ),
            completion_resolver=InvariantCompletionCriterionBridge(
                self._plugin_service
            ),
        )

        first = workflow_progression.progress_once(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=self._provider_ref,
            precondition=WorkflowStepReadPrecondition.from_snapshot(
                self._store.snapshot(work.work_id)
            ),
        )
        pending = first.admitted
        if not isinstance(pending, CapabilityNeedSnapshot):
            raise StandaloneProcessWorkBindingError(
                "First standalone process v2 Workflow step must admit CapabilityNeed"
            )

        capability = CapabilityProgressionService(self._store).progress_once(
            work_id=work.work_id,
            need_id=pending.need.need_id,
            port=StandaloneProcessCapabilityPort(
                workspace=self._workspace,
                binding=capability_binding,
                approved=approved,
                actor=actor,
                reason=reason,
            ),
        )
        if capability.state is not CapabilityNeedState.SUCCEEDED:
            raise StandaloneProcessWorkIncompleteError(
                work_id=work.work_id,
                capability_state=capability.state,
            )
        if capability.outcome is None:
            raise StandaloneProcessWorkBindingError(
                "Succeeded process CapabilityNeed lacks exact outcome"
            )

        outcome = capability.outcome
        evidence = EvidenceRef.create(
            evidence_id=outcome.outcome_id,
            evidence_digest=outcome.outcome_digest,
            evidence_kind=PROCESS_OUTCOME_EVIDENCE_KIND,
            locator=process_outcome_evidence_locator(
                work_id=work.work_id,
                outcome_id=outcome.outcome_id,
            ),
        )
        EvidenceAdmission(self._store).record(
            self._store.snapshot(work.work_id),
            evidence,
        )

        second = workflow_progression.progress_once(
            work_id=work.work_id,
            workflow_run_id=workflow.run.workflow_run_id,
            provider_ref=self._provider_ref,
            precondition=WorkflowStepReadPrecondition.from_snapshot(
                self._store.snapshot(work.work_id)
            ),
        )
        claim = second.admitted
        if not isinstance(claim, CompletionClaim):
            raise StandaloneProcessWorkBindingError(
                "Second standalone process v2 Workflow step must admit CompletionClaim"
            )

        events = self._store.events(work.work_id)
        if not events or events[-1].kind != COMPLETION_CLAIM_ADMITTED_EVENT:
            raise StandaloneProcessWorkBindingError(
                "CompletionClaim admission is not exact current Work head"
            )
        completion = WorkCompletion.create(
            snapshot=self._store.snapshot(work.work_id),
            claim=claim,
            claim_admission_event=events[-1],
        )
        terminal = WorkCompletionAdmissionService(self._store).admit(
            completion
        )

        return StandaloneProcessWorkResult(
            snapshot=terminal,
            capability=capability,
            evidence=evidence,
            claim=claim,
            completion=completion,
        )
