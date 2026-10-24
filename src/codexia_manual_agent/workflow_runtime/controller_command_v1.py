"""DW5: bind a saved Codexia controller's decision to exact worker evidence.

No additional interpreter Role, model conversation, provider send or host effect.
The Work's AttentionResponse is review intent, never execution authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from codexia_manual_agent.attention_core import AttentionNeed
from codexia_manual_agent.pack_core import PackBinding, PackMemberBinding, PackMemberKind
from codexia_manual_agent.workflow_core import WorkflowBinding
from codexia_manual_agent.workflow_runtime.boundary import (
    WorkflowImplementationStateError,
)
from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import (
    ChatCommandSource,
    CommandPlan,
)

WORKFLOW_ID = "codexia:saved-controller-command-review"
PACK_ID = "codexia:saved-controller-command-review-pack"
VERSION = "1.0.0"


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ControllerCommandDecision:
    """One controller reply, tied to the exact worker message and project."""

    source: ChatCommandSource
    controller_conversation_id: str
    controller_message_id: str
    controller_text: str
    controller_sha256: str
    plan: CommandPlan

    def __post_init__(self) -> None:
        from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import _uuid

        _uuid(self.controller_conversation_id)
        _uuid(self.controller_message_id)
        if (
            type(self.controller_text) is not str
            or not self.controller_text.strip()
            or len(self.controller_text) > 14_000
            or self.controller_sha256 != _sha(self.controller_text)
        ):
            raise ValueError("controller message text/digest invalid")
        if self.controller_conversation_id == self.source.conversation_id:
            raise ValueError("controller chat must differ from worker chat")
        if self.plan.source_sha256 != self.source.source_sha256:
            raise ValueError("controller plan changed the source worker message")
        # Parse again to prohibit bypassing the exact validated JSON contract.
        CommandPlan.parse(_json(self.plan.to_dict()), source=self.source)

    @classmethod
    def from_messages(
        cls, *, source: ChatCommandSource,
        controller_conversation_id: str, controller_message_id: str,
        controller_text: str,
    ) -> ControllerCommandDecision:
        # Only Codexia's final response is constrained. The worker can write
        # arbitrary prose/Markdown and examples, as it already does.
        clean = controller_text.strip()
        if clean.startswith("RUN_COMMANDS:\n"):
            clean = clean[len("RUN_COMMANDS:\n"):].strip()
        plan = CommandPlan.parse(clean, source=source)
        return cls(
            source=source,
            controller_conversation_id=controller_conversation_id,
            controller_message_id=controller_message_id,
            controller_text=controller_text,
            controller_sha256=_sha(controller_text),
            plan=plan,
        )

    @classmethod
    def parse(cls, text: str) -> ControllerCommandDecision:
        data = json.loads(text)
        if type(data) is not dict or set(data) != {
            "schema_version", "source", "controller_conversation_id",
            "controller_message_id", "controller_text", "controller_sha256", "plan",
        }:
            raise ValueError("controller Work objective keys differ")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("controller Work schema version differs")
        source = ChatCommandSource.parse(_json(data["source"]))
        decision = cls.from_messages(
            source=source,
            controller_conversation_id=data["controller_conversation_id"],
            controller_message_id=data["controller_message_id"],
            controller_text=data["controller_text"],
        )
        if decision.controller_sha256 != data["controller_sha256"]:
            raise ValueError("controller response digest changed")
        if decision.plan.to_dict() != data["plan"]:
            raise ValueError("recorded controller plan changed")
        return decision

    @property
    def digest(self) -> str:
        return _sha(_json(self.to_dict()))

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "source": self.source.to_dict(),
            "controller_conversation_id": self.controller_conversation_id,
            "controller_message_id": self.controller_message_id,
            "controller_text": self.controller_text,
            "controller_sha256": self.controller_sha256,
            "plan": self.plan.to_dict(),
        }

    def to_json(self) -> str:
        return _json(self.to_dict())


def workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=WORKFLOW_ID,
        version=VERSION,
        definition_digest=_sha("dw5-existing-codexia-controller-decision-v1"),
    )


def pack_binding() -> PackBinding:
    workflow = workflow_binding()
    return PackBinding.create(
        pack_id=PACK_ID,
        version=VERSION,
        definition_digest=_sha("dw5-existing-codexia-controller-pack-v1"),
        members=[
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow.workflow_id,
                version=workflow.version,
                binding_digest=workflow.binding_digest,
            )
        ],
    )


class ControllerCommandReviewWorkflow:
    """Capture one plan review only; M2.1 explicitly blocks shell execution."""

    @property
    def binding(self) -> WorkflowBinding:
        return workflow_binding()

    def propose(self, context):
        if (
            context.workflow.run.binding != self.binding
            or context.pack_binding.pack != pack_binding()
            or context.roles or context.capabilities
            or context.artifacts or context.owned_children or context.evidence_refs
            or len(context.attentions) > 1
        ):
            raise WorkflowImplementationStateError(
                "Codexia controller Work has unrelated or ambiguous lanes"
            )
        try:
            decision = ControllerCommandDecision.parse(context.work.work.objective)
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowImplementationStateError("invalid controller command Work") from exc

        if decision.plan.decision != "commands":
            return None
        if not context.attentions:
            commands = "\n\n".join(
                f"Step {item.step} ({item.shell}, cwd={item.cwd}):\n"
                f"{item.command}\nPurpose: {item.purpose}"
                for item in decision.plan.steps
            )
            question = (
                "Codexia proposes the following LOCAL commands. REVIEW ONLY.\n"
                f"Project: {decision.source.project}\n"
                f"Repository: {decision.source.repo}\n"
                f"Workspace: {decision.source.workspace}\n"
                f"Worker conversation: {decision.source.conversation_id}\n"
                f"Controller conversation: {decision.controller_conversation_id}\n"
                f"Source SHA-256: {decision.source.source_sha256}\n"
                f"Plan SHA-256: {decision.plan.digest}\n"
                f"{commands}\n"
                f"Respond APPROVE {decision.plan.digest} or REJECT {decision.plan.digest}.\n"
                "Neither response authorizes shell execution in this version."
            )
            return AttentionNeed.create(
                workflow=context.workflow, snapshot=context.work,
                question=question,
                reason="Human review of Codexia decision only; process authority "
                       "is separately host-governed, and shell execution is blocked.",
            )

        attention = context.attentions[0]
        answers = [
            item for item in context.attention_responses
            if item.attention_id == attention.attention_id
        ]
        if not answers:
            return None
        if len(answers) != 1 or answers[0].response_text not in {
            f"APPROVE {decision.plan.digest}",
            f"REJECT {decision.plan.digest}",
        }:
            raise WorkflowImplementationStateError(
                "human review does not match the exact Codexia plan"
            )
        # Intent is not host authority. No CapabilityNeed, no WorkCompletion.
        return None
