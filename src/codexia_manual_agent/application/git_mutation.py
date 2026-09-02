from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    ApprovalMode,
    AuthorizationReceipt,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import AuthorizationDeniedError
from codexia_manual_agent.git_mutation import (
    GitCommitApprovalPreview,
    GitCommitObservation,
    GitMutationOutcome,
    GitPushApprovalPreview,
    GitPushObservation,
    execute_git_commit,
    execute_git_push,
    prepare_git_commit_proposal,
    prepare_git_push_proposal,
)


@dataclass(frozen=True, slots=True)
class GitCommitResult:
    proposal: ActionProposal
    approval_preview: GitCommitApprovalPreview
    authorization: AuthorizationReceipt
    observation: GitCommitObservation

    @property
    def succeeded(self) -> bool:
        return self.observation.outcome is GitMutationOutcome.APPLIED

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal": self.proposal.to_dict(),
            "approval_preview": self.approval_preview.to_dict(),
            "authorization": self.authorization.to_dict(),
            "observation": self.observation.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class GitPushResult:
    proposal: ActionProposal
    approval_preview: GitPushApprovalPreview
    authorization: AuthorizationReceipt
    observation: GitPushObservation

    @property
    def succeeded(self) -> bool:
        return self.observation.outcome is GitMutationOutcome.APPLIED

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal": self.proposal.to_dict(),
            "approval_preview": self.approval_preview.to_dict(),
            "authorization": self.authorization.to_dict(),
            "observation": self.observation.to_dict(),
        }


class GitMutationService:
    """Local human-governed orchestration for exact M2.5 commit/push actions."""

    def __init__(self, *, authority: LocalApprovalAuthority | None = None) -> None:
        self._authority = authority or LocalApprovalAuthority()

    def commit(
        self,
        *,
        workspace: str | Path,
        message: str,
        mode: ApprovalMode = ApprovalMode.RISKY,
        approved: bool | None = None,
        actor: str = "local-human",
        reason: str | None = None,
    ) -> GitCommitResult:
        mode = ApprovalMode(mode)
        preparation = prepare_git_commit_proposal(workspace=workspace, message=message)
        receipt = self._authority.decide(
            preparation.proposal,
            mode=mode,
            approved=approved,
            actor=actor,
            reason=reason,
        )
        lifecycle = ActionLifecycle(preparation.proposal, mode)
        phase = lifecycle.apply_receipt(receipt, authority=self._authority)
        if phase is ActionPhase.DENIED:
            raise AuthorizationDeniedError(receipt.reason or "Local policy denied Git commit")
        observation = execute_git_commit(
            preparation,
            lifecycle=lifecycle,
            authority=self._authority,
        )
        return GitCommitResult(
            proposal=preparation.proposal,
            approval_preview=preparation.approval_preview,
            authorization=receipt,
            observation=observation,
        )

    def push(
        self,
        *,
        workspace: str | Path,
        remote: str,
        destination_ref: str,
        mode: ApprovalMode = ApprovalMode.RISKY,
        approved: bool | None = None,
        actor: str = "local-human",
        reason: str | None = None,
    ) -> GitPushResult:
        mode = ApprovalMode(mode)
        preparation = prepare_git_push_proposal(
            workspace=workspace,
            remote=remote,
            destination_ref=destination_ref,
        )
        receipt = self._authority.decide(
            preparation.proposal,
            mode=mode,
            approved=approved,
            actor=actor,
            reason=reason,
        )
        lifecycle = ActionLifecycle(preparation.proposal, mode)
        phase = lifecycle.apply_receipt(receipt, authority=self._authority)
        if phase is ActionPhase.DENIED:
            raise AuthorizationDeniedError(receipt.reason or "Local policy denied Git push")
        observation = execute_git_push(
            preparation,
            lifecycle=lifecycle,
            authority=self._authority,
        )
        return GitPushResult(
            proposal=preparation.proposal,
            approval_preview=preparation.approval_preview,
            authorization=receipt,
            observation=observation,
        )
