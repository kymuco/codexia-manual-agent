"""DW5 semantic interpretation of ordinary ChatGPT replies into bounded plans.

A plan is untrusted proposed intent. Parsing never authorizes process execution.
The original assistant message and model response remain exact in Gen2 Work history.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

from codexia_manual_agent.pack_core import PackBinding, PackMemberBinding, PackMemberKind
from codexia_manual_agent.role_core import (
    ContextProjection,
    RoleBinding,
    RoleRun,
    RoleRunState,
    project_role_runs,
)
from codexia_manual_agent.workflow_core import WorkflowBinding
from codexia_manual_agent.workflow_runtime.boundary import (
    WorkflowImplementationStateError,
)

WORKFLOW_ID = "codexia:chat-command-interpret"
PACK_ID = "codexia:chat-command-interpret-pack"
ROLE_ID = "codexia:chat-command-interpreter"
VERSION = "1.0.0"
MAX_SOURCE = 10_000
MAX_STEPS = 8
MAX_COMMAND = 6_000
MAX_COMMANDS_TOTAL = 12_000

_REPO = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")

INSTRUCTIONS = (
    "You are Codexia's command INTERPRETER, not an executor. Read a normal "
    "ChatGPT assistant reply in context of the named project and workspace. "
    "Identify only the local terminal commands the assistant genuinely requests "
    "the user to RUN NOW. Do not select demonstrations, hypothetical snippets, "
    "expected output, historical commands, quoted examples, or GitHub plugin "
    "actions. Understand ordinary prose and PowerShell multiline constructs, "
    "including here-strings piped to Python, in their original form. "
    "Preserve execution order, exact command text and intended cwd; do not "
    "invent paths, fill unknown values, alter scripts or merge unrelated code "
    "blocks. If the instruction is ambiguous, choose decision='uncertain', "
    "steps=[], and explain why, instead of guessing. If no local commands are "
    "required, choose decision='no_commands', steps=[]. "
    "Return EXACTLY one JSON object: schema_version=1, "
    "source_sha256=<the exact supplied source_sha256>, "
    "decision='commands'|'no_commands'|'uncertain', "
    "steps=[{step:1-based consecutive integer, shell:'powershell'|'cmd'|'bash', "
    "command:literal complete command text, cwd:'.' or safe relative POSIX path, "
    "purpose:brief reason}], reason:brief nonempty string. "
    "Only include steps for decision='commands'; at most eight. "
    "Keep full multiline commands intact. No Markdown wrappers or additional "
    "keys. This output is a PROPOSAL only: there is no permission to execute."
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _json_object(text: str, keys: set[str]) -> dict:
    if type(text) is not str:
        raise ValueError("contract must be text")
    stripped = text.strip()
    if stripped.startswith("<escape>") and stripped.endswith("</escape>"):
        stripped = stripped[len("<escape>") : -len("</escape>")]
    try:
        value = json.loads(stripped)
    except (ValueError, TypeError) as exc:
        raise ValueError("contract is not a JSON object") from exc
    if type(value) is not dict or set(value) != keys:
        raise ValueError("contract JSON fields are not exact")
    return value


def _uuid(value: str) -> str:
    if type(value) is not str:
        raise ValueError("ID must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("ID must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError("ID must be a canonical UUID")
    return value


def _bounded(value: str, name: str, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be bounded nonempty text")
    if "\x00" in value:
        raise ValueError(f"{name} includes NUL")
    return value


@dataclass(frozen=True, slots=True)
class ChatCommandSource:
    project: str
    repo: str
    conversation_id: str
    source_message_id: str
    source_text: str
    source_sha256: str
    workspace: str

    def __post_init__(self) -> None:
        if not _PROJECT.fullmatch(self.project):
            raise ValueError("project label invalid")
        if not _REPO.fullmatch(self.repo) or ".." in self.repo:
            raise ValueError("repo identity invalid")
        _uuid(self.conversation_id)
        _uuid(self.source_message_id)
        _bounded(self.source_text, "source_text", MAX_SOURCE)
        if type(self.source_sha256) is not str or self.source_sha256 != _sha(self.source_text):
            raise ValueError("source assistant message SHA mismatch")
        _bounded(self.workspace, "workspace", 400)
        if not (
            Path(self.workspace).is_absolute()
            or PureWindowsPath(self.workspace).is_absolute()
        ):
            raise ValueError("workspace must be an absolute path")
        if len(self.to_json()) > 15_000:
            raise ValueError("source Work objective exceeds bounded size")

    @classmethod
    def parse(cls, text: str) -> ChatCommandSource:
        value = _json_object(text, {
            "schema_version", "project", "repo", "conversation_id",
            "source_message_id", "source_text", "source_sha256", "workspace",
        })
        version = value.pop("schema_version")
        if type(version) is not int or version != 1:
            raise ValueError("source schema must be exact integer 1")
        return cls(**value)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1, "project": self.project, "repo": self.repo,
            "conversation_id": self.conversation_id,
            "source_message_id": self.source_message_id,
            "source_text": self.source_text, "source_sha256": self.source_sha256,
            "workspace": self.workspace,
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict())


@dataclass(frozen=True, slots=True)
class PlannedStep:
    step: int
    shell: str
    command: str
    cwd: str
    purpose: str

    def __post_init__(self) -> None:
        if type(self.step) is not int or not 1 <= self.step <= MAX_STEPS:
            raise ValueError("step must be a bounded one-based integer")
        if type(self.shell) is not str or self.shell not in {
            "powershell", "cmd", "bash",
        }:
            raise ValueError("unsupported shell")
        _bounded(self.command, "command", MAX_COMMAND)
        if "\r" in self.command:
            raise ValueError("command must use LF line endings")
        _bounded(self.purpose, "purpose", 360)
        if type(self.cwd) is not str or len(self.cwd) > 240:
            raise ValueError("cwd invalid")
        if self.cwd != ".":
            path = PurePosixPath(self.cwd)
            if (
                path.is_absolute()
                or path.as_posix() != self.cwd
                or any(p in ("", ".", "..") or p.startswith(".") for p in self.cwd.split("/"))
                or "\\" in self.cwd
                or ":" in self.cwd
            ):
                raise ValueError("cwd must be safe workspace-relative path")

    def to_dict(self) -> dict:
        return {
            "step": self.step, "shell": self.shell, "command": self.command,
            "cwd": self.cwd, "purpose": self.purpose,
        }


@dataclass(frozen=True, slots=True)
class CommandPlan:
    source_sha256: str
    decision: str
    steps: tuple[PlannedStep, ...]
    reason: str

    def __post_init__(self) -> None:
        if type(self.source_sha256) is not str or not _SHA.fullmatch(self.source_sha256):
            raise ValueError("plan source hash invalid")
        if self.decision not in {"commands", "no_commands", "uncertain"}:
            raise ValueError("plan decision invalid")
        if type(self.steps) is not tuple or len(self.steps) > MAX_STEPS:
            raise ValueError("plan exceeds bounded step count")
        if any(type(x) is not PlannedStep for x in self.steps):
            raise ValueError("plan steps must be typed")
        if [x.step for x in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("step numbering/order must be exact")
        if (self.decision == "commands") != bool(self.steps):
            raise ValueError("commands must have steps; other decisions must not")
        if sum(len(x.command) for x in self.steps) > MAX_COMMANDS_TOTAL:
            raise ValueError("total command text exceeds bound")
        _bounded(self.reason, "reason", 800)

    @classmethod
    def parse(cls, text: str, *, source: ChatCommandSource) -> CommandPlan:
        value = _json_object(text, {
            "schema_version", "source_sha256", "decision", "steps", "reason",
        })
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("plan schema must equal 1")
        steps = value["steps"]
        if type(steps) is not list or len(steps) > MAX_STEPS:
            raise ValueError("plan steps must be a bounded list")
        typed = []
        for step in steps:
            if type(step) is not dict or set(step) != {
                "step", "shell", "command", "cwd", "purpose",
            }:
                raise ValueError("step keys are not exact")
            typed.append(PlannedStep(**step))
        plan = cls(
            source_sha256=value["source_sha256"],
            decision=value["decision"], steps=tuple(typed),
            reason=value["reason"],
        )
        if plan.source_sha256 != source.source_sha256:
            raise ValueError("plan refers to a different assistant message")
        return plan

    @property
    def digest(self) -> str:
        return _sha(_canonical(self.to_dict()))

    def to_dict(self) -> dict:
        return {
            "schema_version": 1, "source_sha256": self.source_sha256,
            "decision": self.decision,
            "steps": [x.to_dict() for x in self.steps], "reason": self.reason,
        }


def workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=WORKFLOW_ID, version=VERSION,
        definition_digest=_sha("dw5-semantic-command-interpret-workflow-v1"),
    )


def role_binding() -> RoleBinding:
    return RoleBinding.create(
        role_id=ROLE_ID, version=VERSION, instructions_digest=_sha(INSTRUCTIONS)
    )


def pack_binding() -> PackBinding:
    workflow, role = workflow_binding(), role_binding()
    return PackBinding.create(
        pack_id=PACK_ID, version=VERSION,
        definition_digest=_sha("dw5-semantic-command-interpret-pack-v1"),
        members=[
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow.workflow_id,
                version=workflow.version,
                binding_digest=workflow.binding_digest,
            ),
            PackMemberBinding.create(
                kind=PackMemberKind.ROLE,
                semantic_id=role.role_id,
                version=role.version,
                binding_digest=role.binding_digest,
            ),
        ],
    )


class CommandInstructionsMaterialPort:
    def resolve(self, binding: RoleBinding) -> str:
        if binding != role_binding():
            raise ValueError("interpreter RoleBinding drifted")
        return INSTRUCTIONS


class CommandContextMaterialPort:
    """Reconstruct the exact source only from durable Work objective."""

    def __init__(self, store, work_id: str) -> None:
        self.store = store
        self.work_id = work_id

    def resolve(self, projection: ContextProjection) -> str:
        roles = [
            role for role in project_role_runs(self.store.events(self.work_id))
            if role.run.binding == role_binding() and role.run.context == projection
        ]
        if len(roles) != 1:
            raise ValueError("context projection is not an exact interpreter Role")
        objective = self.store.snapshot(self.work_id).work.objective
        ChatCommandSource.parse(objective)
        if _sha(objective) != projection.content_digest:
            raise ValueError("interpreter context changed exact durable objective")
        return objective


class CommandInterpretationWorkflowV1:
    @property
    def binding(self) -> WorkflowBinding:
        return workflow_binding()

    def propose(self, context):
        if (
            context.workflow.run.binding != workflow_binding()
            or context.pack_binding.pack != pack_binding()
            or context.capabilities
            or context.attentions or context.attention_responses
            or context.artifacts or context.evidence_refs or context.owned_children
            or len(context.roles) > 1
        ):
            raise WorkflowImplementationStateError("interpreter Work binding/lanes changed")
        try:
            source = ChatCommandSource.parse(context.work.work.objective)
        except (ValueError, TypeError) as exc:
            raise WorkflowImplementationStateError("invalid command source Work") from exc
        if not context.roles:
            return RoleRun.create(
                workflow=context.workflow, snapshot=context.work,
                binding=role_binding(),
                context=ContextProjection.create(
                    content_digest=_sha(context.work.work.objective)
                ),
            )
        role = context.roles[0]
        if role.run.binding != role_binding():
            raise WorkflowImplementationStateError("interpreter role changed binding")
        if role.state is not RoleRunState.COMPLETED or role.output_text is None:
            raise WorkflowImplementationStateError("interpreter Role has no completed output")
        try:
            CommandPlan.parse(role.output_text, source=source)
        except (ValueError, TypeError) as exc:
            raise WorkflowImplementationStateError("interpreter plan invalid") from exc
        # A parsed plan is only proposed intent. Never return CapabilityNeed.
        return None
