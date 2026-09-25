from __future__ import annotations

from hashlib import sha256

from codexia_manual_agent.capability_core import (
    CapabilityBinding,
    CapabilityNeed,
    CapabilityNeedState,
)
from codexia_manual_agent.completion_core import (
    CompletionClaim,
    CompletionCriterionContext,
    CompletionCriterionResult,
)
from codexia_manual_agent.pack_core import (
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
)
from codexia_manual_agent.workflow_core import WorkflowBinding
from codexia_manual_agent.workflow_runtime.boundary import (
    WorkflowImplementationBindingError,
    WorkflowImplementationStateError,
    WorkflowProposal,
    WorkflowStepContext,
)

WORKFLOW_ID = "codexia:standalone-process"
WORKFLOW_VERSION = "2.0.0"
CAPABILITY_ID = "process"
CAPABILITY_VERSION = "1.0.0"
PACK_ID = "codexia:standalone-process-pack"
PACK_VERSION = "2.0.0"

PROCESS_OUTCOME_EVIDENCE_KIND = "codexia.capability-outcome.succeeded.v1"
COMPLETION_SUMMARY = "Standalone process capability completed successfully."

_PROCESS_PARAMETERS = {
    "argv": ["python", "-V"],
    "cwd_ref": "workspace",
    "cwd": ".",
    "limits": {
        "timeout_seconds": 30.0,
        "max_stdout_bytes": 65_536,
        "max_stderr_bytes": 65_536,
    },
}


def standalone_process_v2_workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=WORKFLOW_ID,
        version=WORKFLOW_VERSION,
        definition_digest=sha256(
            b"standalone-process-workflow-v2"
        ).hexdigest(),
    )


def standalone_process_v2_capability_binding() -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id=CAPABILITY_ID,
        version=CAPABILITY_VERSION,
        contract_digest=sha256(
            b"process-run-contract-v1"
        ).hexdigest(),
    )


def standalone_process_v2_pack_binding() -> PackBinding:
    workflow = standalone_process_v2_workflow_binding()
    capability = standalone_process_v2_capability_binding()
    return PackBinding.create(
        pack_id=PACK_ID,
        version=PACK_VERSION,
        definition_digest=sha256(
            b"standalone-process-pack-v2"
        ).hexdigest(),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow.workflow_id,
                version=workflow.version,
                binding_digest=workflow.binding_digest,
            ),
            PackMemberBinding.create(
                kind=PackMemberKind.CAPABILITY,
                semantic_id=capability.capability_id,
                version=capability.version,
                binding_digest=capability.binding_digest,
            ),
        ),
    )


def process_outcome_evidence_locator(
    *,
    work_id: str,
    outcome_id: str,
) -> str:
    if not isinstance(work_id, str) or not work_id:
        raise TypeError("work_id must be non-empty text")
    if not isinstance(outcome_id, str) or not outcome_id:
        raise TypeError("outcome_id must be non-empty text")
    return f"work-event://{work_id}/{outcome_id}"


class StandaloneProcessWorkflowImplementationV2:
    """Standalone process semantics using the Gen2 completion pipeline.

    State machine:
      no process Need
        -> propose exact process/run CapabilityNeed
      pending Need
        -> no proposal
      succeeded Need without exact outcome EvidenceRef
        -> no proposal
      succeeded Need with exact outcome EvidenceRef
        -> propose CompletionClaim
      failed/unknown Need
        -> no policy decision

    The implementation owns no admission, host selection, execution authority,
    evidence admission, completion admission, Work terminal transition, or
    scheduling.
    """

    def __init__(self) -> None:
        self._binding = standalone_process_v2_workflow_binding()
        self._capability_binding = standalone_process_v2_capability_binding()

    @property
    def binding(self) -> WorkflowBinding:
        return self._binding

    @property
    def capability_binding(self) -> CapabilityBinding:
        return self._capability_binding

    def propose(
        self,
        context: WorkflowStepContext,
    ) -> WorkflowProposal | None:
        if context.workflow.run.binding != self._binding:
            raise WorkflowImplementationBindingError(
                "Standalone process v2 received another WorkflowBinding"
            )
        if context.roles:
            raise WorkflowImplementationStateError(
                "Standalone process v2 does not own RoleRun state"
            )
        if context.attentions or context.attention_responses:
            raise WorkflowImplementationStateError(
                "Standalone process v2 does not own Attention state"
            )
        if context.owned_children:
            raise WorkflowImplementationStateError(
                "Standalone process v2 does not own delegated child Work"
            )

        capability_member = PackMemberBinding.create(
            kind=PackMemberKind.CAPABILITY,
            semantic_id=self._capability_binding.capability_id,
            version=self._capability_binding.version,
            binding_digest=self._capability_binding.binding_digest,
        )
        if not context.pack_binding.pack.contains(capability_member):
            raise WorkflowImplementationBindingError(
                "Pinned Pack lacks standalone process CapabilityBinding"
            )

        if not context.capabilities:
            return CapabilityNeed.create(
                workflow=context.workflow,
                snapshot=context.work,
                binding=self._capability_binding,
                operation="run",
                parameters=_PROCESS_PARAMETERS,
            )

        if len(context.capabilities) != 1:
            raise WorkflowImplementationStateError(
                "Standalone process v2 requires exactly one CapabilityNeed"
            )

        capability = context.capabilities[0]
        need = capability.need
        if need.binding != self._capability_binding:
            raise WorkflowImplementationBindingError(
                "Existing CapabilityNeed changed process binding"
            )
        if need.operation != "run":
            raise WorkflowImplementationBindingError(
                "Existing CapabilityNeed changed process operation"
            )
        if need.to_dict()["parameters"] != _PROCESS_PARAMETERS:
            raise WorkflowImplementationBindingError(
                "Existing CapabilityNeed changed process parameters"
            )

        if capability.state is CapabilityNeedState.PENDING:
            return None
        if capability.state is not CapabilityNeedState.SUCCEEDED:
            return None
        if capability.outcome is None:
            raise WorkflowImplementationStateError(
                "Succeeded CapabilityNeed lacks outcome"
            )

        outcome = capability.outcome
        expected_locator = process_outcome_evidence_locator(
            work_id=context.work.work.work_id,
            outcome_id=outcome.outcome_id,
        )
        matching = tuple(
            evidence
            for evidence in context.evidence_refs
            if evidence.evidence_id == outcome.outcome_id
            and evidence.evidence_digest == outcome.outcome_digest
            and evidence.evidence_kind == PROCESS_OUTCOME_EVIDENCE_KIND
            and evidence.locator == expected_locator
        )
        if not matching:
            return None
        if len(matching) != 1:
            raise WorkflowImplementationStateError(
                "Standalone process v2 observed duplicate exact outcome evidence"
            )

        return CompletionClaim.create(
            snapshot=context.work,
            workflow=context.workflow,
            pack_binding=context.pack_binding,
            summary=COMPLETION_SUMMARY,
            evidence_refs=matching,
        )


class StandaloneProcessCompletionCriterionV2:
    """Pack-defined semantic gate for standalone process completion."""

    def __init__(self) -> None:
        self._binding = standalone_process_v2_workflow_binding()
        self._capability_binding = standalone_process_v2_capability_binding()

    @property
    def binding(self) -> WorkflowBinding:
        return self._binding

    def evaluate(
        self,
        context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        if context.pack_binding.pack != standalone_process_v2_pack_binding():
            return CompletionCriterionResult(
                accepted=False,
                reason="Standalone process completion Pack is not exact.",
            )

        claim = context.claim
        if claim.summary != COMPLETION_SUMMARY:
            return CompletionCriterionResult(
                accepted=False,
                reason="Standalone process completion summary is not exact.",
            )
        if claim.artifact_refs:
            return CompletionCriterionResult(
                accepted=False,
                reason=(
                    "Standalone process completion does not use "
                    "ArtifactRef basis."
                ),
            )
        if claim.child_completion_refs:
            return CompletionCriterionResult(
                accepted=False,
                reason=(
                    "Standalone process completion does not use "
                    "child completion basis."
                ),
            )
        if len(context.capabilities) != 1:
            return CompletionCriterionResult(
                accepted=False,
                reason=(
                    "Standalone process completion requires one exact "
                    "CapabilityNeed lifecycle."
                ),
            )

        capability = context.capabilities[0]
        need = capability.need
        if need.binding != self._capability_binding:
            return CompletionCriterionResult(
                accepted=False,
                reason="Standalone process CapabilityBinding is not exact.",
            )
        if need.operation != "run":
            return CompletionCriterionResult(
                accepted=False,
                reason="Standalone process capability operation is not exact.",
            )
        if need.to_dict()["parameters"] != _PROCESS_PARAMETERS:
            return CompletionCriterionResult(
                accepted=False,
                reason="Standalone process capability parameters are not exact.",
            )
        if (
            capability.state is not CapabilityNeedState.SUCCEEDED
            or capability.outcome is None
        ):
            return CompletionCriterionResult(
                accepted=False,
                reason=(
                    "Standalone process completion requires a durable "
                    "succeeded CapabilityOutcome."
                ),
            )

        if len(claim.evidence_refs) != 1:
            return CompletionCriterionResult(
                accepted=False,
                reason=(
                    "Standalone process completion requires one "
                    "CapabilityOutcome EvidenceRef."
                ),
            )

        outcome = capability.outcome
        evidence = claim.evidence_refs[0]
        if (
            evidence.evidence_id != outcome.outcome_id
            or evidence.evidence_digest != outcome.outcome_digest
        ):
            return CompletionCriterionResult(
                accepted=False,
                reason=(
                    "Standalone process completion evidence does not bind "
                    "the durable CapabilityOutcome."
                ),
            )
        if evidence.evidence_kind != PROCESS_OUTCOME_EVIDENCE_KIND:
            return CompletionCriterionResult(
                accepted=False,
                reason="Standalone process completion evidence kind is not exact.",
            )
        if evidence.locator != process_outcome_evidence_locator(
            work_id=claim.work_id,
            outcome_id=outcome.outcome_id,
        ):
            return CompletionCriterionResult(
                accepted=False,
                reason=(
                    "Standalone process completion evidence locator is not exact."
                ),
            )

        return CompletionCriterionResult(
            accepted=True,
            reason=(
                "Standalone process completion basis binds the durable "
                "succeeded CapabilityOutcome."
            ),
        )
