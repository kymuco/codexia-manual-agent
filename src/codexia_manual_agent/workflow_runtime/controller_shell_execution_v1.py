"""Existing Codexia controller decisions -> reviewed sequential Gen2 capabilities.

One durable Work binds the controller + worker chats. Every command is a
separate CapabilityNeed with independently recorded handoff and outcome.
Human review is never host process authority.
"""

from __future__ import annotations

from hashlib import sha256

from codexia_manual_agent.attention_core import AttentionNeed
from codexia_manual_agent.capability_core import CapabilityNeed, CapabilityNeedState
from codexia_manual_agent.pack_core import PackBinding, PackMemberBinding, PackMemberKind
from codexia_manual_agent.workflow_core import WorkflowBinding
from codexia_manual_agent.workflow_runtime.boundary import WorkflowImplementationStateError
from codexia_manual_agent.workflow_runtime.controller_command_v1 import (
    ControllerCommandDecision,
)
from codexia_manual_agent.workflow_runtime.standalone_process_v2 import (
    standalone_process_v2_capability_binding,
)

WORKFLOW_ID = "codexia:controller-shell-plan"
PACK_ID = "codexia:controller-shell-plan-pack"
VERSION = "1.0.0"


def workflow_binding():
    return WorkflowBinding.create(
        workflow_id=WORKFLOW_ID, version=VERSION,
        definition_digest=sha256(b"dw5-controller-shell-plan-v1").hexdigest(),
    )


def pack_binding():
    workflow = workflow_binding()
    capability = standalone_process_v2_capability_binding()
    return PackBinding.create(
        pack_id=PACK_ID, version=VERSION,
        definition_digest=sha256(b"dw5-controller-shell-pack-v1").hexdigest(),
        members=[
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
        ],
    )


def parameters(step):
    if step.shell != "powershell":
        raise ValueError("first Windows execution pilot supports PowerShell only")
    return {
        "argv": [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-Command", step.command,
        ],
        "cwd_ref": "workspace",
        "cwd": step.cwd,
        "limits": {
            "timeout_seconds": 60.0,
            "max_stdout_bytes": 16384,
            "max_stderr_bytes": 16384,
        },
    }


class ControllerShellExecutionWorkflow:
    @property
    def binding(self):
        return workflow_binding()

    def propose(self, context):
        if (
            context.workflow.run.binding != self.binding
            or context.pack_binding.pack != pack_binding()
            or context.roles or context.artifacts
            or context.owned_children or context.evidence_refs
            or len(context.attentions) > 1
        ):
            raise WorkflowImplementationStateError("controller shell pack binding/lanes changed")
        try:
            decision = ControllerCommandDecision.parse(context.work.work.objective)
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowImplementationStateError("invalid controller decision") from exc

        if decision.plan.decision != "commands":
            if context.attentions or context.capabilities:
                raise WorkflowImplementationStateError("non-command plan gained effect lanes")
            return None
        steps = decision.plan.steps
        if any(step.shell != "powershell" for step in steps):
            # Not silently rewritten into commands of another shell.
            return None

        if not context.attentions:
            if context.capabilities or context.attention_responses:
                raise WorkflowImplementationStateError("command effect before human review")
            description = "\n\n".join(
                f"STEP {step.step}, cwd={step.cwd}:\n{step.command}\n"
                f"Purpose: {step.purpose}"
                for step in steps
            )
            question = (
                "LOCAL POWERSHELL PLAN: human review ONLY, not technical authority.\n"
                f"Work ID: {context.work.work.work_id}\n"
                f"Project: {decision.source.project}\n"
                f"Repository: {decision.source.repo}\n"
                f"Workspace: {decision.source.workspace}\n"
                f"Worker chat: {decision.source.conversation_id}\n"
                f"Codexia chat: {decision.controller_conversation_id}\n"
                f"Source SHA-256: {decision.source.source_sha256}\n"
                f"Plan SHA-256: {decision.plan.digest}\n"
                f"Commands:\n{description}\n"
                f"Respond APPROVE {decision.plan.digest} or REJECT {decision.plan.digest}."
            )
            return AttentionNeed.create(
                workflow=context.workflow, snapshot=context.work,
                question=question,
                reason="Review a fixed ordered local command plan. Host authority separately required.",
            )

        attention = context.attentions[0]
        responses = [
            item for item in context.attention_responses
            if item.attention_id == attention.attention_id
        ]
        if not responses:
            if context.capabilities:
                raise WorkflowImplementationStateError("process admitted before review")
            return None
        if len(responses) != 1:
            raise WorkflowImplementationStateError("ambiguous review chronology")
        answer = responses[0].response_text
        if answer == f"REJECT {decision.plan.digest}":
            if context.capabilities:
                raise WorkflowImplementationStateError("rejected plan received capabilities")
            return None
        if answer != f"APPROVE {decision.plan.digest}":
            raise WorkflowImplementationStateError("review does not bind exact plan SHA")
        if len(context.capabilities) > len(steps):
            raise WorkflowImplementationStateError("unexpected extra process capability")

        cap_binding = standalone_process_v2_capability_binding()
        for index, item in enumerate(context.capabilities):
            need = item.need
            if (
                need.binding != cap_binding
                or need.operation != "run"
                or need.to_dict()["parameters"] != parameters(steps[index])
            ):
                raise WorkflowImplementationStateError("process step drifted from approved plan")
            if item.state is not CapabilityNeedState.SUCCEEDED:
                # Pending, failed or ambiguous stops all later steps.
                if index != len(context.capabilities) - 1:
                    raise WorkflowImplementationStateError("downstream command after non-success")
                return None
            if item.outcome is None:
                raise WorkflowImplementationStateError("success without outcome")

        if len(context.capabilities) == len(steps):
            return None
        return CapabilityNeed.create(
            workflow=context.workflow,
            snapshot=context.work,
            binding=cap_binding,
            operation="run",
            parameters=parameters(steps[len(context.capabilities)]),
        )
