"""DW5.1: one-file software proposal and human review; no effect authority."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from codexia_manual_agent.attention_core import AttentionNeed
from codexia_manual_agent.pack_core import (
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
)
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
    WorkflowStepContext,
)

SOFTWARE_WORKFLOW_ID = "codexia:software-proposal-work"
SOFTWARE_PACK_ID = "codexia:software-proposal-pack"
SOFTWARE_ROLE_ID = "codexia:software-patch-proposer"
SOFTWARE_VERSION = "1.0.0"

_MAX_FILE_CHARS = 8_000
_MAX_CHANGED_LINES = 200
_MAX_DIFF_CHARS = 12_000
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")

_SOFTWARE_INSTRUCTIONS = (
    "You are the DW5.1 bounded software patch proposer. Analyze the exact "
    "host-supplied JSON repository snapshot and task. Return ONE JSON object "
    "only, with exactly these keys: schema_version=1, stage='proposal', "
    "repo, base_head, path, preimage_sha256, replacement_text, summary, "
    "verification_plan, risks. Copy repo/base_head/path/preimage_sha256 "
    "EXACTLY from the supplied snapshot. replacement_text is the COMPLETE "
    "new contents of the ONE named existing UTF-8 LF file; do not propose "
    "new files, commands or extra candidates. Keep changes small and testable. "
    "verification_plan and risks are explanations, NOT execution requests. "
    "Do not claim tests ran. No Markdown fences, <escape> tags or prose outside JSON."
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _valid_text(value: object, name: str, maximum: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be nonempty bounded text")
    if "\x00" in value:
        raise ValueError(f"{name} cannot contain NUL")
    return value


def _valid_path(path: str) -> str:
    _valid_text(path, "path", 240)
    if (
        path != str(PurePosixPath(path))
        or path.startswith("/")
        or "\\" in path
        or any(
            segment in {"", ".", ".."} or segment.startswith(".")
            for segment in path.split("/")
        )
        or PurePosixPath(path).suffix not in {".py", ".md", ".txt", ".json"}
    ):
        raise ValueError("path must name one safe existing text file")
    return path


def _valid_snapshot_text(value: object, name: str) -> str:
    text = _valid_text(value, name, _MAX_FILE_CHARS)
    if "\r" in text or not text.endswith("\n"):
        raise ValueError(f"{name} must use UTF-8 LF and end in newline")
    return text


def _parse_object(text: str, *, keys: set[str]) -> dict:
    if type(text) is not str:
        raise TypeError("contract value must be a JSON string")
    value = json.loads(text)
    if type(value) is not dict or set(value) != keys:
        raise ValueError("contract JSON fields are not exact")
    return value


@dataclass(frozen=True, slots=True)
class SoftwareSnapshot:
    """Host-supplied facts, NOT attested host observations or action authority."""

    repo: str
    base_head: str
    path: str
    preimage_sha256: str
    original_text: str
    task: str

    @classmethod
    def parse(cls, text: str) -> SoftwareSnapshot:
        value = _parse_object(
            text,
            keys={
                "schema_version", "repo", "base_head", "path",
                "preimage_sha256", "original_text", "task",
            },
        )
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("software snapshot requires schema_version 1")
        source = cls(**{key: val for key, val in value.items() if key != "schema_version"})
        return source

    def __post_init__(self) -> None:
        if not _REPO_RE.fullmatch(self.repo) or ".." in self.repo:
            raise ValueError("repository identity must be owner/repo")
        if not _HEAD_RE.fullmatch(self.base_head):
            raise ValueError("base_head must be exact lowercase commit OID")
        _valid_path(self.path)
        _valid_snapshot_text(self.original_text, "original_text")
        _valid_text(self.task, "task", 1_800)
        if not _SHA_RE.fullmatch(self.preimage_sha256):
            raise ValueError("preimage_sha256 must be lowercase SHA-256")
        if _sha(self.original_text) != self.preimage_sha256:
            raise ValueError("preimage_sha256 does not match original_text")

    def to_dict(self) -> dict:
        return {"schema_version": 1, "repo": self.repo, "base_head": self.base_head,
                "path": self.path, "preimage_sha256": self.preimage_sha256,
                "original_text": self.original_text, "task": self.task}

    def to_json(self) -> str:
        return _json(self.to_dict())


@dataclass(frozen=True, slots=True)
class SoftwarePatchProposal:
    repo: str
    base_head: str
    path: str
    preimage_sha256: str
    replacement_text: str
    summary: str
    verification_plan: str
    risks: str

    @classmethod
    def parse(cls, text: str) -> SoftwarePatchProposal:
        value = _parse_object(
            text,
            keys={
                "schema_version", "stage", "repo", "base_head", "path",
                "preimage_sha256", "replacement_text", "summary",
                "verification_plan", "risks",
            },
        )
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("software proposal requires schema_version 1")
        if value["stage"] != "proposal":
            raise ValueError("software proposal stage must be 'proposal'")
        return cls(**{
            key: val for key, val in value.items()
            if key not in {"schema_version", "stage"}
        })

    def __post_init__(self) -> None:
        if not _REPO_RE.fullmatch(self.repo) or ".." in self.repo:
            raise ValueError("invalid repository identity")
        if not _HEAD_RE.fullmatch(self.base_head):
            raise ValueError("invalid base HEAD")
        if not _SHA_RE.fullmatch(self.preimage_sha256):
            raise ValueError("invalid preimage digest")
        _valid_path(self.path)
        _valid_snapshot_text(self.replacement_text, "replacement_text")
        for key in ("summary", "verification_plan", "risks"):
            _valid_text(getattr(self, key), key, 1_200)

    def to_dict(self) -> dict:
        return {
            "schema_version": 1, "stage": "proposal", "repo": self.repo,
            "base_head": self.base_head, "path": self.path,
            "preimage_sha256": self.preimage_sha256,
            "replacement_text": self.replacement_text,
            "summary": self.summary, "verification_plan": self.verification_plan,
            "risks": self.risks,
        }

    @property
    def digest(self) -> str:
        return _sha(_json(self.to_dict()))

    def review_diff(self, source: SoftwareSnapshot) -> str:
        if (
            self.repo != source.repo
            or self.base_head != source.base_head
            or self.path != source.path
            or self.preimage_sha256 != source.preimage_sha256
        ):
            raise ValueError("proposal changed the exact source snapshot binding")
        if self.replacement_text == source.original_text:
            raise ValueError("no-op change is not a patch proposal")
        diff = "".join(difflib.unified_diff(
            source.original_text.splitlines(keepends=True),
            self.replacement_text.splitlines(keepends=True),
            fromfile="a/" + self.path,
            tofile="b/" + self.path,
        ))
        lines = diff.splitlines()
        changes = sum(
            1 for line in lines
            if line.startswith(("+", "-"))
            and not line.startswith(("+++", "---"))
        )
        if changes == 0 or changes > _MAX_CHANGED_LINES or len(diff) > _MAX_DIFF_CHARS:
            raise ValueError("proposed diff exceeds DW5.1 one-file review bounds")
        return diff


def software_workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=SOFTWARE_WORKFLOW_ID, version=SOFTWARE_VERSION,
        definition_digest=_sha("dw5.1-software-proposal-work-v1"),
    )


def software_role_binding() -> RoleBinding:
    return RoleBinding.create(
        role_id=SOFTWARE_ROLE_ID, version=SOFTWARE_VERSION,
        instructions_digest=_sha(_SOFTWARE_INSTRUCTIONS),
    )


def software_pack_binding() -> PackBinding:
    workflow = software_workflow_binding()
    role = software_role_binding()
    return PackBinding.create(
        pack_id=SOFTWARE_PACK_ID, version=SOFTWARE_VERSION,
        definition_digest=_sha("dw5.1-software-proposal-pack-v1"),
        members=[
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW, semantic_id=workflow.workflow_id,
                version=workflow.version, binding_digest=workflow.binding_digest,
            ),
            PackMemberBinding.create(
                kind=PackMemberKind.ROLE, semantic_id=role.role_id,
                version=role.version, binding_digest=role.binding_digest,
            ),
        ],
    )


def software_role_instructions(binding: RoleBinding) -> str:
    if binding != software_role_binding():
        raise KeyError("unknown Software RoleBinding")
    return _SOFTWARE_INSTRUCTIONS


class SoftwareInstructionsMaterialPort:
    def resolve(self, binding: RoleBinding) -> str:
        return software_role_instructions(binding)


class SoftwareContextMaterialPort:
    """Reconstruct bound text only from durable local Work; no repo access."""

    def __init__(self, *, store, work_id: str) -> None:
        self.store = store
        self.work_id = work_id

    def resolve(self, projection: ContextProjection) -> str:
        events = self.store.events(self.work_id)
        roles = [
            role for role in project_role_runs(events)
            if role.run.context == projection
            and role.run.binding == software_role_binding()
        ]
        if len(roles) != 1:
            raise ValueError("Software ContextProjection does not identify exact Role")
        objective = self.store.snapshot(self.work_id).work.objective
        SoftwareSnapshot.parse(objective)
        if _sha(objective) != projection.content_digest:
            raise ValueError("Software ContextProjection changed immutable objective")
        return objective


class SoftwareProposalWorkflowV1:
    """No capability, artifact, completion or filesystem effects in DW5.1."""

    @property
    def binding(self) -> WorkflowBinding:
        return software_workflow_binding()

    def propose(self, context: WorkflowStepContext):
        if context.workflow.run.binding != self.binding:
            raise WorkflowImplementationStateError("not the pinned Software Workflow")
        if context.pack_binding.pack != software_pack_binding():
            raise WorkflowImplementationStateError("not the pinned Software Pack")
        if context.capabilities or context.artifacts or context.evidence_refs:
            raise WorkflowImplementationStateError("DW5.1 must have no effect/evidence lane")
        if len(context.roles) > 1:
            raise WorkflowImplementationStateError("DW5.1 allows only one proposal role")
        try:
            source = SoftwareSnapshot.parse(context.work.work.objective)
        except (ValueError, TypeError) as exc:
            raise WorkflowImplementationStateError("invalid Software Work snapshot") from exc

        if not context.roles:
            return RoleRun.create(
                workflow=context.workflow, snapshot=context.work,
                binding=software_role_binding(),
                context=ContextProjection.create(
                    content_digest=_sha(context.work.work.objective)
                ),
            )
        role = context.roles[0]
        if role.run.binding != software_role_binding():
            raise WorkflowImplementationStateError("Software RoleBinding changed")
        if role.state is not RoleRunState.COMPLETED or role.output_text is None:
            raise WorkflowImplementationStateError("Software Role has no terminal output")
        try:
            candidate = SoftwarePatchProposal.parse(role.output_text)
            diff = candidate.review_diff(source)
        except (ValueError, TypeError) as exc:
            raise WorkflowImplementationStateError("invalid bounded Software proposal") from exc

        if len(context.attentions) > 1:
            raise WorkflowImplementationStateError("multiple Software review boundaries")
        if not context.attentions:
            question = (
                "Review ONE software patch (NO effect authority).\n"
                f"Repository: {source.repo}\nBase HEAD: {source.base_head}\n"
                f"Path: {source.path}\nPreimage SHA-256: {source.preimage_sha256}\n"
                f"Proposal digest: {candidate.digest}\n"
                f"Summary: {candidate.summary}\n"
                f"Verification plan (NOT executed): {candidate.verification_plan}\n"
                f"Risks: {candidate.risks}\n\n{diff}\n"
                f"Respond exactly APPROVE {candidate.digest} or REJECT {candidate.digest}."
            )
            return AttentionNeed.create(
                workflow=context.workflow, snapshot=context.work,
                question=question,
                reason="Review records human intent only. Host authorization "
                       "and technical execution are outside DW5.1.",
            )

        responses = [
            value for value in context.attention_responses
            if value.attention_id == context.attentions[0].attention_id
        ]
        if not responses:
            return None
        if len(responses) != 1:
            raise WorkflowImplementationStateError("ambiguous human review chronology")
        text = responses[0].response_text
        if text not in {
            f"APPROVE {candidate.digest}", f"REJECT {candidate.digest}"
        }:
            raise WorkflowImplementationStateError(
                "human response does not bind exact proposal digest"
            )
        # Explicit human intent is NOT host-granted effect authority.
        # No CapabilityNeed, CompletionClaim or next Role is created.
        return None
