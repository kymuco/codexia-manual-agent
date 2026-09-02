from __future__ import annotations

import os
from pathlib import Path

from codexia_manual_agent.authority import ActionLifecycle, ActionPhase, LocalApprovalAuthority
from codexia_manual_agent.mutation.metadata_executor import WindowsMetadataReplaceExecutor
from codexia_manual_agent.mutation.models import MutationOperation, WorkspaceMutationObservation
from codexia_manual_agent.mutation.secure_executor import (
    WorkspaceMutationExecutor as _SecureWorkspaceMutationExecutor,
)
from codexia_manual_agent.mutation.windows_metadata import validate_windows_relative_target
from codexia_manual_agent.mutation.windows_txf import require_windows_txf_support
from codexia_manual_agent.mutation.workspace import _validate_proposal


def _is_windows_host() -> bool:
    """Return the executor platform without exposing the process-global os module to tests."""

    return os.name == "nt"


def _require_windows_strict_replace_support(target_path: Path) -> str:
    """Require the fail-closed local-NTFS TxF backend before receipt consumption."""

    return require_windows_txf_support(target_path)


class _MutationBackendDispatcher:
    def __init__(self) -> None:
        self._create = _SecureWorkspaceMutationExecutor()
        self._replace = WindowsMetadataReplaceExecutor()

    def execute(
        self,
        lifecycle: ActionLifecycle,
        *,
        authority: LocalApprovalAuthority,
    ) -> WorkspaceMutationObservation:
        plan = _validate_proposal(lifecycle.proposal)
        if plan.operation is MutationOperation.REPLACE:
            return self._replace.execute(lifecycle, authority=authority)
        return self._create.execute(lifecycle, authority=authority)


class WorkspaceMutationExecutor:
    """Preflight namespace/platform capability before any mutation backend runs."""

    def __init__(self, *, delegate=None) -> None:
        self._delegate = delegate or _MutationBackendDispatcher()

    def execute(
        self,
        lifecycle: ActionLifecycle,
        *,
        authority: LocalApprovalAuthority,
    ) -> WorkspaceMutationObservation:
        # Preserve lifecycle errors for malformed/non-authorized calls. Platform
        # capability preflight applies only to a lifecycle that could otherwise run.
        if lifecycle.phase is ActionPhase.AUTHORIZED and lifecycle.authorization is not None:
            plan = _validate_proposal(lifecycle.proposal)
            if _is_windows_host():
                validate_windows_relative_target(plan.target)
                if plan.operation is MutationOperation.REPLACE:
                    _require_windows_strict_replace_support(plan.target_path)

        return self._delegate.execute(lifecycle, authority=authority)
