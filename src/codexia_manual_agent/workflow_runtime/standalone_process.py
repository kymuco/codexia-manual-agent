from __future__ import annotations

from hashlib import sha256

from codexia_manual_agent.capability_core import (
    CapabilityBinding,
    CapabilityNeed,
    CapabilityNeedState,
)
from codexia_manual_agent.pack_core import PackMemberBinding, PackMemberKind
from codexia_manual_agent.workflow_core import (
    WORKFLOW_COMPLETED_EVENT,
    WorkflowBinding,
    WorkflowCandidate,
)
from codexia_manual_agent.workflow_runtime.boundary import (
    WorkflowImplementationBindingError,
    WorkflowImplementationStateError,
    WorkflowProposal,
    WorkflowStepContext,
)

WORKFLOW_ID = "codexia:standalone-process"
WORKFLOW_VERSION = "1.0.0"
CAPABILITY_ID = "process"
CAPABILITY_VERSION = "1.0.0"

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


def standalone_process_workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=WORKFLOW_ID,
        version=WORKFLOW_VERSION,
        definition_digest=sha256(
            b"standalone-process-workflow-v1"
        ).hexdigest(),
    )


def standalone_process_capability_binding() -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id=CAPABILITY_ID,
        version=CAPABILITY_VERSION,
        contract_digest=sha256(
            b"process-run-contract-v1"
        ).hexdigest(),
    )


class StandaloneProcessWorkflowImplementation:
    """One-step process workflow with no admission or execution authority.

    State machine:
      no process Need -> propose exact process/run CapabilityNeed
      pending Need    -> no proposal
      succeeded Need  -> propose workflow.completed
      failed/unknown  -> no policy decision in G2.9
    """

    def __init__(self) -> None:
        self._binding = standalone_process_workflow_binding()
        self._capability_binding = standalone_process_capability_binding()

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
                "Standalone process implementation received another WorkflowBinding"
            )
        if context.roles:
            raise WorkflowImplementationStateError(
                "Standalone process workflow does not own RoleRun state"
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
                "Standalone process workflow requires at most one CapabilityNeed"
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
        if capability.state is CapabilityNeedState.SUCCEEDED:
            if capability.outcome is None:
                raise WorkflowImplementationStateError(
                    "Succeeded CapabilityNeed lacks outcome"
                )
            return WorkflowCandidate.create(
                run_snapshot=context.workflow,
                work_snapshot=context.work,
                event_kind=WORKFLOW_COMPLETED_EVENT,
                payload={
                    "capability_need_id": need.need_id,
                    "capability_outcome_id": capability.outcome.outcome_id,
                    "capability_outcome_digest": (
                        capability.outcome.outcome_digest
                    ),
                },
            )

        # Failure interpretation is deliberately not a G2.9 concern.
        return None
