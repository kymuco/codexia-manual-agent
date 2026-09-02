from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    ApprovalMode,
    AuthorizationReceipt,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import AuthorizationDeniedError
from codexia_manual_agent.execution import (
    ProcessExecutionObservation,
    ProcessExecutor,
    ProcessLimits,
    prepare_process_proposal,
)


@dataclass(frozen=True, slots=True)
class ProcessExecutionResult:
    proposal: ActionProposal
    authorization: AuthorizationReceipt
    observation: ProcessExecutionObservation

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal": self.proposal.to_dict(),
            "authorization": self.authorization.to_dict(),
            "observation": self.observation.to_dict(),
        }


class ExecuteProcessService:
    """Local human-governed orchestration for the M2.1 process executor."""

    def __init__(
        self,
        *,
        authority: LocalApprovalAuthority | None = None,
        executor: ProcessExecutor | None = None,
    ) -> None:
        self._authority = authority or LocalApprovalAuthority()
        self._executor = executor or ProcessExecutor()

    def run(
        self,
        *,
        workspace: str | Path,
        argv: Sequence[str],
        cwd: str | Path = ".",
        mode: ApprovalMode = ApprovalMode.RISKY,
        approved: bool | None = None,
        actor: str = "local-human",
        reason: str | None = None,
        limits: ProcessLimits | None = None,
        summary: str | None = None,
    ) -> ProcessExecutionResult:
        mode = ApprovalMode(mode)
        proposal = prepare_process_proposal(
            workspace=workspace,
            argv=argv,
            cwd=cwd,
            limits=limits,
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
                receipt.reason or "Local policy denied process execution"
            )
        observation = self._executor.execute(
            lifecycle,
            authority=self._authority,
        )
        return ProcessExecutionResult(
            proposal=proposal,
            authorization=receipt,
            observation=observation,
        )
