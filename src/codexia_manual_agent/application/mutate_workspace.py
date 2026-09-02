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
from codexia_manual_agent.mutation import (
    MutationOperation,
    WorkspaceMutationExecutor,
    WorkspaceMutationObservation,
    prepare_create_proposal,
    prepare_replace_proposal,
)


@dataclass(frozen=True, slots=True)
class WorkspaceMutationResult:
    proposal: ActionProposal
    authorization: AuthorizationReceipt
    observation: WorkspaceMutationObservation

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal": self.proposal.to_dict(),
            "authorization": self.authorization.to_dict(),
            "observation": self.observation.to_dict(),
        }


class MutateWorkspaceService:
    """Local human-governed orchestration for M2.3 create/replace mutations."""

    def __init__(
        self,
        *,
        authority: LocalApprovalAuthority | None = None,
        executor: WorkspaceMutationExecutor | None = None,
    ) -> None:
        self._authority = authority or LocalApprovalAuthority()
        self._executor = executor or WorkspaceMutationExecutor()

    def run(
        self,
        *,
        workspace: str | Path,
        operation: MutationOperation,
        target: str | Path,
        content: bytes,
        mode: ApprovalMode = ApprovalMode.RISKY,
        approved: bool | None = None,
        actor: str = "local-human",
        reason: str | None = None,
        summary: str | None = None,
    ) -> WorkspaceMutationResult:
        operation = MutationOperation(operation)
        mode = ApprovalMode(mode)
        if operation is MutationOperation.CREATE:
            proposal = prepare_create_proposal(
                workspace=workspace,
                target=target,
                content=content,
                summary=summary,
            )
        else:
            proposal = prepare_replace_proposal(
                workspace=workspace,
                target=target,
                content=content,
                summary=summary,
            )

        receipt = self._authority.decide(
            proposal,
            mode=mode,
            approved=approved,
            actor=actor,
            reason=reason,
        )
        lifecycle = ActionLifecycle(proposal, mode)
        phase = lifecycle.apply_receipt(receipt, authority=self._authority)
        if phase is ActionPhase.DENIED:
            raise AuthorizationDeniedError(
                receipt.reason or "Local policy denied workspace mutation"
            )
        observation = self._executor.execute(lifecycle, authority=self._authority)
        return WorkspaceMutationResult(
            proposal=proposal,
            authorization=receipt,
            observation=observation,
        )
