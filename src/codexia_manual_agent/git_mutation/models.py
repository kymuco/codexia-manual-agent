from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from codexia_manual_agent.authority.models import ActionProposal


GIT_COMMIT_ACTION = "git.commit.v1"
GIT_PUSH_ACTION = "git.push.v1"
GIT_MUTATION_SCHEMA_VERSION = 1


class GitMutationOutcome(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"
    MISMATCH = "mismatch"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class GitExecutableIdentity:
    path: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class GitIndexEntry:
    mode: str
    object_id: str
    stage: int
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "object_id": self.object_id,
            "stage": self.stage,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class GitCommitApprovalPreview:
    head_ref: str
    head_oid: str
    expected_tree_oid: str
    expected_commit_oid: str
    index_sha256: str
    index_manifest_digest: str
    staged_diff_sha256: str
    staged_diff: str
    staged_entries: tuple[GitIndexEntry, ...]
    message: str
    author_name: str
    author_email: str
    commit_timestamp: str
    backend: str
    pack_size_bytes: int
    pack_sha256: str
    requires_human: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": GIT_COMMIT_ACTION,
            "head_ref": self.head_ref,
            "head_oid": self.head_oid,
            "expected_tree_oid": self.expected_tree_oid,
            "expected_commit_oid": self.expected_commit_oid,
            "index_sha256": self.index_sha256,
            "index_manifest_digest": self.index_manifest_digest,
            "staged_diff_sha256": self.staged_diff_sha256,
            "staged_diff": self.staged_diff,
            "staged_entries": [entry.to_dict() for entry in self.staged_entries],
            "message": self.message,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "commit_timestamp": self.commit_timestamp,
            "backend": self.backend,
            "pack_size_bytes": self.pack_size_bytes,
            "pack_sha256": self.pack_sha256,
            "requires_human": self.requires_human,
        }


@dataclass(frozen=True, slots=True)
class GitPushApprovalPreview:
    local_oid: str
    local_ref: str
    remote_name: str
    remote_url: str
    remote_path: str
    destination_ref: str
    expected_remote_oid: str
    backend: str
    pack_size_bytes: int
    pack_sha256: str
    requires_human: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": GIT_PUSH_ACTION,
            "local_oid": self.local_oid,
            "local_ref": self.local_ref,
            "remote_name": self.remote_name,
            "remote_url": self.remote_url,
            "remote_path": self.remote_path,
            "destination_ref": self.destination_ref,
            "expected_remote_oid": self.expected_remote_oid,
            "backend": self.backend,
            "pack_size_bytes": self.pack_size_bytes,
            "pack_sha256": self.pack_sha256,
            "requires_human": self.requires_human,
        }


@dataclass(frozen=True, slots=True)
class GitCommitPreparation:
    proposal: ActionProposal
    approval_preview: GitCommitApprovalPreview
    pack_bytes: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, ActionProposal):
            raise TypeError("proposal must be ActionProposal")
        if not isinstance(self.approval_preview, GitCommitApprovalPreview):
            raise TypeError("approval_preview must be GitCommitApprovalPreview")
        if not isinstance(self.pack_bytes, bytes) or not self.pack_bytes:
            raise TypeError("pack_bytes must be non-empty bytes")


@dataclass(frozen=True, slots=True)
class GitPushPreparation:
    proposal: ActionProposal
    approval_preview: GitPushApprovalPreview
    pack_bytes: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, ActionProposal):
            raise TypeError("proposal must be ActionProposal")
        if not isinstance(self.approval_preview, GitPushApprovalPreview):
            raise TypeError("approval_preview must be GitPushApprovalPreview")
        if not isinstance(self.pack_bytes, bytes) or not self.pack_bytes:
            raise TypeError("pack_bytes must be non-empty bytes")


@dataclass(frozen=True, slots=True)
class GitCommitObservation:
    execution_id: str
    proposal_id: str
    proposal_digest: str
    outcome: GitMutationOutcome
    head_ref: str
    previous_head_oid: str
    expected_commit_oid: str
    observed_head_oid: str | None
    tree_oid: str | None
    index_manifest_digest: str
    message_sha256: str
    backend: str
    pack_size_bytes: int
    pack_sha256: str
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GIT_MUTATION_SCHEMA_VERSION,
            "execution_id": self.execution_id,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "outcome": self.outcome.value,
            "head_ref": self.head_ref,
            "previous_head_oid": self.previous_head_oid,
            "expected_commit_oid": self.expected_commit_oid,
            "observed_head_oid": self.observed_head_oid,
            "tree_oid": self.tree_oid,
            "index_manifest_digest": self.index_manifest_digest,
            "message_sha256": self.message_sha256,
            "backend": self.backend,
            "pack_size_bytes": self.pack_size_bytes,
            "pack_sha256": self.pack_sha256,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class GitPushObservation:
    execution_id: str
    proposal_id: str
    proposal_digest: str
    outcome: GitMutationOutcome
    local_oid: str
    remote_url: str
    remote_path: str
    destination_ref: str
    expected_remote_oid: str
    observed_remote_oid: str | None
    backend: str
    pack_size_bytes: int
    pack_sha256: str
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GIT_MUTATION_SCHEMA_VERSION,
            "execution_id": self.execution_id,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "outcome": self.outcome.value,
            "local_oid": self.local_oid,
            "remote_url": self.remote_url,
            "remote_path": self.remote_path,
            "destination_ref": self.destination_ref,
            "expected_remote_oid": self.expected_remote_oid,
            "observed_remote_oid": self.observed_remote_oid,
            "backend": self.backend,
            "pack_size_bytes": self.pack_size_bytes,
            "pack_sha256": self.pack_sha256,
            "error": self.error,
        }
