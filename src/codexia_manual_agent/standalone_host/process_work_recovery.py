from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from codexia_manual_agent.capability_core import (
    CapabilityHandoff,
    CapabilityNeedSnapshot,
    CapabilityNeedState,
    project_capability_handoffs,
    project_capability_needs,
)
from codexia_manual_agent.completion_core import (
    COMPLETION_CLAIM_ADMITTED_EVENT,
    CompletionClaim,
    WorkCompletion,
    WorkCompletionAdmissionService,
    project_admitted_completion_claims,
    project_work_completion,
)
from codexia_manual_agent.evidence_core import (
    EvidenceAdmission,
    EvidenceRef,
    project_evidence_refs,
)
from codexia_manual_agent.invariant_bridge import (
    InvariantCompletionCriterionBridge,
    InvariantPackDistributionBridge,
    InvariantWorkflowImplementationBridge,
    ManagedPluginServicePort,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackWorkflowBinding,
    project_workflow_pack_binding,
)
from codexia_manual_agent.standalone_host.process_capability import (
    STANDALONE_PROCESS_HOST_ID,
    StandaloneProcessCapabilityPort,
)
from codexia_manual_agent.standalone_host.process_work import (
    StandaloneProcessWorkBindingError,
    StandaloneProcessWorkResult,
)
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
    WorkflowRunSnapshot,
    WorkflowRunState,
    project_workflow_runs,
)
from codexia_manual_agent.workflow_orchestration import (
    CapabilityProgressionService,
    WorkflowProgressionService,
    WorkflowStepReadPrecondition,
)
from codexia_manual_agent.workflow_runtime import (
    COMPLETION_SUMMARY,
    PROCESS_OUTCOME_EVIDENCE_KIND,
    process_outcome_evidence_locator,
    standalone_process_v2_capability_binding,
    standalone_process_v2_pack_binding,
    standalone_process_v2_parameters,
    standalone_process_v2_workflow_binding,
)


class StandaloneProcessWorkRecoveryState(StrEnum):
    """Derived standalone-host continuation state.

    These values are not canonical Work lifecycle states and are never persisted.
    """

    START_WORKFLOW = "start_workflow"
    PIN_PACK = "pin_pack"
    PROGRESS_CAPABILITY_NEED = "progress_capability_need"
    DISPATCH_CAPABILITY = "dispatch_capability"
    AWAITING_OUTCOME_RECONCILIATION = "awaiting_outcome_reconciliation"
    CAPABILITY_FAILED = "capability_failed"
    CAPABILITY_OUTCOME_UNKNOWN = "capability_outcome_unknown"
    RECORD_OUTCOME_EVIDENCE = "record_outcome_evidence"
    PROGRESS_COMPLETION_CLAIM = "progress_completion_claim"
    FINALIZE_WORK = "finalize_work"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class StandaloneProcessWorkCheckpoint:
    """One exact derived restart checkpoint for the process vertical."""

    state: StandaloneProcessWorkRecoveryState
    snapshot: WorkSnapshot
    workflow: WorkflowRunSnapshot | None = None
    pack_binding: PackWorkflowBinding | None = None
    capability: CapabilityNeedSnapshot | None = None
    handoff: CapabilityHandoff | None = None
    evidence: EvidenceRef | None = None
    claim: CompletionClaim | None = None
    completion: WorkCompletion | None = None

    @property
    def work_id(self) -> str:
        return self.snapshot.work.work_id


class StandaloneProcessWorkRecoveryService:
    """Restart-safe standalone composition over frozen process v2 semantics.

    Recovery is projection-only. advance_once performs at most one bounded
    host-level semantic transition from the recovered checkpoint. It owns no
    generic scheduler, queue, retry loop, or new durable state.

    A durable CapabilityHandoff without CapabilityOutcome is never redispatched.
    That state is surfaced as AWAITING_OUTCOME_RECONCILIATION.
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

    def recover(self, work_id: str) -> StandaloneProcessWorkCheckpoint:
        snapshot = self._store.snapshot(work_id)
        events = self._store.events(work_id)
        completion = project_work_completion(events)

        if snapshot.state is WorkState.CANCELLED:
            if completion is not None:
                raise StandaloneProcessWorkBindingError(
                    "Cancelled standalone process Work contains WorkCompletion"
                )
            return StandaloneProcessWorkCheckpoint(
                state=StandaloneProcessWorkRecoveryState.CANCELLED,
                snapshot=snapshot,
            )

        workflows = project_workflow_runs(events)
        if not workflows:
            if snapshot.state is WorkState.COMPLETED:
                raise StandaloneProcessWorkBindingError(
                    "Completed standalone process Work has no WorkflowRun"
                )
            if events:
                raise StandaloneProcessWorkBindingError(
                    "Standalone process Work has chronology before WorkflowRun"
                )
            return StandaloneProcessWorkCheckpoint(
                state=StandaloneProcessWorkRecoveryState.START_WORKFLOW,
                snapshot=snapshot,
            )
        if len(workflows) != 1:
            raise StandaloneProcessWorkBindingError(
                "Standalone process Work requires exactly one WorkflowRun"
            )

        workflow = workflows[0]
        if workflow.run.binding != standalone_process_v2_workflow_binding():
            raise StandaloneProcessWorkBindingError(
                "Recovered WorkflowRun is not exact standalone process v2"
            )
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise StandaloneProcessWorkBindingError(
                "Standalone process v2 WorkflowRun must remain ACTIVE"
            )

        pin = project_workflow_pack_binding(
            events,
            workflow.run.workflow_run_id,
        )
        if pin is None:
            if snapshot.state is WorkState.COMPLETED:
                raise StandaloneProcessWorkBindingError(
                    "Completed standalone process Work has no Pack pin"
                )
            if events[-1].event_id != workflow.run.workflow_run_id:
                raise StandaloneProcessWorkBindingError(
                    "Unpinned WorkflowRun is no longer current Work head"
                )
            return StandaloneProcessWorkCheckpoint(
                state=StandaloneProcessWorkRecoveryState.PIN_PACK,
                snapshot=snapshot,
                workflow=workflow,
            )
        if pin.pack != standalone_process_v2_pack_binding():
            raise StandaloneProcessWorkBindingError(
                "Recovered Pack pin is not exact standalone process v2"
            )

        capabilities = tuple(
            item
            for item in project_capability_needs(events)
            if item.need.workflow_run_id == workflow.run.workflow_run_id
        )
        if not capabilities:
            if snapshot.state is WorkState.COMPLETED:
                raise StandaloneProcessWorkBindingError(
                    "Completed standalone process Work has no CapabilityNeed"
                )
            return StandaloneProcessWorkCheckpoint(
                state=(
                    StandaloneProcessWorkRecoveryState.PROGRESS_CAPABILITY_NEED
                ),
                snapshot=snapshot,
                workflow=workflow,
                pack_binding=pin,
            )
        if len(capabilities) != 1:
            raise StandaloneProcessWorkBindingError(
                "Standalone process Work requires exactly one CapabilityNeed"
            )

        capability = capabilities[0]
        need = capability.need
        if need.binding != standalone_process_v2_capability_binding():
            raise StandaloneProcessWorkBindingError(
                "Recovered CapabilityNeed changed process binding"
            )
        if need.operation != "run":
            raise StandaloneProcessWorkBindingError(
                "Recovered CapabilityNeed changed process operation"
            )
        if need.to_dict()["parameters"] != standalone_process_v2_parameters():
            raise StandaloneProcessWorkBindingError(
                "Recovered CapabilityNeed changed process parameters"
            )

        handoffs = tuple(
            item
            for item in project_capability_handoffs(events)
            if item.need_id == need.need_id
        )
        if len(handoffs) > 1:
            raise StandaloneProcessWorkBindingError(
                "CapabilityNeed has multiple durable host handoffs"
            )
        handoff = handoffs[0] if handoffs else None
        if handoff is not None and handoff.host_id != STANDALONE_PROCESS_HOST_ID:
            raise StandaloneProcessWorkBindingError(
                "CapabilityNeed is routed to another standalone host"
            )

        if capability.state is CapabilityNeedState.PENDING:
            state = (
                StandaloneProcessWorkRecoveryState.DISPATCH_CAPABILITY
                if handoff is None
                else StandaloneProcessWorkRecoveryState
                .AWAITING_OUTCOME_RECONCILIATION
            )
            return StandaloneProcessWorkCheckpoint(
                state=state,
                snapshot=snapshot,
                workflow=workflow,
                pack_binding=pin,
                capability=capability,
                handoff=handoff,
            )

        if handoff is None:
            raise StandaloneProcessWorkBindingError(
                "Terminal process CapabilityNeed has no durable host handoff"
            )

        if capability.state is CapabilityNeedState.FAILED:
            return StandaloneProcessWorkCheckpoint(
                state=StandaloneProcessWorkRecoveryState.CAPABILITY_FAILED,
                snapshot=snapshot,
                workflow=workflow,
                pack_binding=pin,
                capability=capability,
                handoff=handoff,
            )
        if capability.state is CapabilityNeedState.OUTCOME_UNKNOWN:
            return StandaloneProcessWorkCheckpoint(
                state=(
                    StandaloneProcessWorkRecoveryState
                    .CAPABILITY_OUTCOME_UNKNOWN
                ),
                snapshot=snapshot,
                workflow=workflow,
                pack_binding=pin,
                capability=capability,
                handoff=handoff,
            )
        if (
            capability.state is not CapabilityNeedState.SUCCEEDED
            or capability.outcome is None
        ):
            raise StandaloneProcessWorkBindingError(
                "Recovered process CapabilityNeed has invalid terminal state"
            )

        outcome = capability.outcome
        expected_evidence = EvidenceRef.create(
            evidence_id=outcome.outcome_id,
            evidence_digest=outcome.outcome_digest,
            evidence_kind=PROCESS_OUTCOME_EVIDENCE_KIND,
            locator=process_outcome_evidence_locator(
                work_id=work_id,
                outcome_id=outcome.outcome_id,
            ),
        )
        same_identity = tuple(
            evidence
            for evidence in project_evidence_refs(events)
            if evidence.evidence_id == outcome.outcome_id
        )
        if not same_identity:
            if snapshot.state is WorkState.COMPLETED:
                raise StandaloneProcessWorkBindingError(
                    "Completed standalone process Work lacks outcome evidence"
                )
            return StandaloneProcessWorkCheckpoint(
                state=(
                    StandaloneProcessWorkRecoveryState
                    .RECORD_OUTCOME_EVIDENCE
                ),
                snapshot=snapshot,
                workflow=workflow,
                pack_binding=pin,
                capability=capability,
                handoff=handoff,
            )
        if len(same_identity) != 1 or same_identity[0] != expected_evidence:
            raise StandaloneProcessWorkBindingError(
                "Recovered outcome EvidenceRef changed exact semantics"
            )
        evidence = same_identity[0]

        claims = project_admitted_completion_claims(events)
        if not claims:
            if snapshot.state is WorkState.COMPLETED:
                raise StandaloneProcessWorkBindingError(
                    "Completed standalone process Work lacks CompletionClaim"
                )
            return StandaloneProcessWorkCheckpoint(
                state=(
                    StandaloneProcessWorkRecoveryState
                    .PROGRESS_COMPLETION_CLAIM
                ),
                snapshot=snapshot,
                workflow=workflow,
                pack_binding=pin,
                capability=capability,
                handoff=handoff,
                evidence=evidence,
            )
        if len(claims) != 1:
            raise StandaloneProcessWorkBindingError(
                "Standalone process Work requires exactly one admitted claim"
            )
        claim = claims[0]
        if claim.summary != COMPLETION_SUMMARY:
            raise StandaloneProcessWorkBindingError(
                "Recovered CompletionClaim changed process summary"
            )
        if claim.evidence_refs != (evidence,):
            raise StandaloneProcessWorkBindingError(
                "Recovered CompletionClaim changed process outcome evidence"
            )
        if claim.artifact_refs or claim.child_completion_refs:
            raise StandaloneProcessWorkBindingError(
                "Recovered CompletionClaim contains unsupported basis"
            )

        if snapshot.state is WorkState.COMPLETED:
            if completion is None:
                raise StandaloneProcessWorkBindingError(
                    "Completed Work lacks structured WorkCompletion"
                )
            if completion.claim_id != claim.claim_id:
                raise StandaloneProcessWorkBindingError(
                    "Recovered WorkCompletion changed CompletionClaim"
                )
            return StandaloneProcessWorkCheckpoint(
                state=StandaloneProcessWorkRecoveryState.COMPLETED,
                snapshot=snapshot,
                workflow=workflow,
                pack_binding=pin,
                capability=capability,
                handoff=handoff,
                evidence=evidence,
                claim=claim,
                completion=completion,
            )

        if completion is not None:
            raise StandaloneProcessWorkBindingError(
                "Active Work unexpectedly projects WorkCompletion"
            )
        head = events[-1]
        if (
            head.kind != COMPLETION_CLAIM_ADMITTED_EVENT
            or head.event_id != claim.claim_id
        ):
            raise StandaloneProcessWorkBindingError(
                "Admitted process CompletionClaim is not current Work head"
            )
        return StandaloneProcessWorkCheckpoint(
            state=StandaloneProcessWorkRecoveryState.FINALIZE_WORK,
            snapshot=snapshot,
            workflow=workflow,
            pack_binding=pin,
            capability=capability,
            handoff=handoff,
            evidence=evidence,
            claim=claim,
        )

    def advance_once(
        self,
        *,
        objective: str,
        source_namespace: str,
        source_id: str,
        payload_digest: str,
        approved: bool,
        actor: str = "local-human",
        reason: str | None = None,
    ) -> StandaloneProcessWorkCheckpoint:
        """Start or resume one Work and perform at most one safe transition."""

        self._require_exact_distribution()

        candidate = Work.create(
            objective=objective,
            ingress=WorkIngressBinding.create(
                source_namespace=source_namespace,
                source_id=source_id,
                payload_digest=payload_digest,
            ),
        )
        snapshot = self._store.create(candidate)
        created_now = snapshot.work.work_id == candidate.work_id
        checkpoint = self.recover(snapshot.work.work_id)
        if created_now:
            return checkpoint

        state = checkpoint.state
        if state is StandaloneProcessWorkRecoveryState.START_WORKFLOW:
            WorkflowAdmission(self._store).admit_start(
                WorkflowRun.create(
                    snapshot=checkpoint.snapshot,
                    binding=standalone_process_v2_workflow_binding(),
                )
            )
            return self.recover(checkpoint.work_id)

        if state is StandaloneProcessWorkRecoveryState.PIN_PACK:
            workflow = self._require_workflow(checkpoint)
            PackAdmission(self._store).admit_workflow_binding(
                PackWorkflowBinding.create(
                    workflow=workflow,
                    snapshot=self._store.snapshot(checkpoint.work_id),
                    pack=standalone_process_v2_pack_binding(),
                )
            )
            return self.recover(checkpoint.work_id)

        if state in {
            StandaloneProcessWorkRecoveryState.PROGRESS_CAPABILITY_NEED,
            StandaloneProcessWorkRecoveryState.PROGRESS_COMPLETION_CLAIM,
        }:
            workflow = self._require_workflow(checkpoint)
            WorkflowProgressionService(
                store=self._store,
                resolver=InvariantWorkflowImplementationBridge(
                    self._plugin_service
                ),
                completion_resolver=InvariantCompletionCriterionBridge(
                    self._plugin_service
                ),
            ).progress_once(
                work_id=checkpoint.work_id,
                workflow_run_id=workflow.run.workflow_run_id,
                provider_ref=self._provider_ref,
                precondition=WorkflowStepReadPrecondition.from_snapshot(
                    self._store.snapshot(checkpoint.work_id)
                ),
            )
            return self.recover(checkpoint.work_id)

        if state is StandaloneProcessWorkRecoveryState.DISPATCH_CAPABILITY:
            capability = self._require_capability(checkpoint)
            CapabilityProgressionService(self._store).progress_once(
                work_id=checkpoint.work_id,
                need_id=capability.need.need_id,
                port=StandaloneProcessCapabilityPort(
                    workspace=self._workspace,
                    binding=standalone_process_v2_capability_binding(),
                    approved=approved,
                    actor=actor,
                    reason=reason,
                ),
            )
            return self.recover(checkpoint.work_id)

        if state is StandaloneProcessWorkRecoveryState.RECORD_OUTCOME_EVIDENCE:
            capability = self._require_capability(checkpoint)
            if capability.outcome is None:
                raise StandaloneProcessWorkBindingError(
                    "Outcome evidence requires durable CapabilityOutcome"
                )
            outcome = capability.outcome
            EvidenceAdmission(self._store).record(
                self._store.snapshot(checkpoint.work_id),
                EvidenceRef.create(
                    evidence_id=outcome.outcome_id,
                    evidence_digest=outcome.outcome_digest,
                    evidence_kind=PROCESS_OUTCOME_EVIDENCE_KIND,
                    locator=process_outcome_evidence_locator(
                        work_id=checkpoint.work_id,
                        outcome_id=outcome.outcome_id,
                    ),
                ),
            )
            return self.recover(checkpoint.work_id)

        if state is StandaloneProcessWorkRecoveryState.FINALIZE_WORK:
            claim = checkpoint.claim
            if claim is None:
                raise StandaloneProcessWorkBindingError(
                    "Finalization checkpoint lacks admitted CompletionClaim"
                )
            events = self._store.events(checkpoint.work_id)
            head = events[-1]
            completion = WorkCompletion.create(
                snapshot=self._store.snapshot(checkpoint.work_id),
                claim=claim,
                claim_admission_event=head,
            )
            WorkCompletionAdmissionService(self._store).admit(completion)
            return self.recover(checkpoint.work_id)

        return checkpoint

    def start_or_resume_once(
        self,
        **kwargs,
    ) -> StandaloneProcessWorkCheckpoint:
        """Alias emphasizing ingress-idempotent restart behavior."""

        return self.advance_once(**kwargs)

    @staticmethod
    def completed_result(
        checkpoint: StandaloneProcessWorkCheckpoint,
    ) -> StandaloneProcessWorkResult:
        if checkpoint.state is not StandaloneProcessWorkRecoveryState.COMPLETED:
            raise StandaloneProcessWorkBindingError(
                "Standalone process checkpoint is not completed"
            )
        if (
            checkpoint.capability is None
            or checkpoint.evidence is None
            or checkpoint.claim is None
            or checkpoint.completion is None
        ):
            raise StandaloneProcessWorkBindingError(
                "Completed checkpoint lacks exact semantic records"
            )
        return StandaloneProcessWorkResult(
            snapshot=checkpoint.snapshot,
            capability=checkpoint.capability,
            evidence=checkpoint.evidence,
            claim=checkpoint.claim,
            completion=checkpoint.completion,
        )

    def _require_exact_distribution(self) -> None:
        distribution = InvariantPackDistributionBridge(
            self._plugin_service
        ).resolve(self._provider_ref)
        if distribution.pack != standalone_process_v2_pack_binding():
            raise StandaloneProcessWorkBindingError(
                "Provider does not distribute exact standalone process v2 Pack"
            )
        if distribution.workflows != (
            standalone_process_v2_workflow_binding(),
        ):
            raise StandaloneProcessWorkBindingError(
                "Provider does not distribute exact standalone process v2 Workflow"
            )
        if distribution.roles:
            raise StandaloneProcessWorkBindingError(
                "Standalone process v2 Pack unexpectedly contains Role bindings"
            )
        if distribution.capabilities != (
            standalone_process_v2_capability_binding(),
        ):
            raise StandaloneProcessWorkBindingError(
                "Provider does not distribute exact process Capability binding"
            )

    @staticmethod
    def _require_workflow(
        checkpoint: StandaloneProcessWorkCheckpoint,
    ) -> WorkflowRunSnapshot:
        if checkpoint.workflow is None:
            raise StandaloneProcessWorkBindingError(
                "Recovery checkpoint lacks WorkflowRun"
            )
        return checkpoint.workflow

    @staticmethod
    def _require_capability(
        checkpoint: StandaloneProcessWorkCheckpoint,
    ) -> CapabilityNeedSnapshot:
        if checkpoint.capability is None:
            raise StandaloneProcessWorkBindingError(
                "Recovery checkpoint lacks CapabilityNeed"
            )
        return checkpoint.capability
