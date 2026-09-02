from __future__ import annotations

import os
from pathlib import Path

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.mutation.windows_metadata import validate_windows_relative_target
from codexia_manual_agent.mutation.workspace import (
    prepare_create_proposal as _legacy_prepare_create_proposal,
    prepare_replace_proposal as _legacy_prepare_replace_proposal,
)


def _is_windows_host() -> bool:
    return os.name == "nt"


def preflight_workspace_mutation_target(target: str | Path) -> None:
    if _is_windows_host():
        validate_windows_relative_target(str(target).replace("\\", "/"))


def _preflight_target(target: str | Path) -> None:
    # Compatibility alias for the existing M2.3 internal surface.
    preflight_workspace_mutation_target(target)


def prepare_create_proposal(
    *,
    workspace: str | Path,
    target: str | Path,
    content: bytes,
    summary: str | None = None,
) -> ActionProposal:
    preflight_workspace_mutation_target(target)
    return _legacy_prepare_create_proposal(
        workspace=workspace,
        target=target,
        content=content,
        summary=summary,
    )


def prepare_replace_proposal(
    *,
    workspace: str | Path,
    target: str | Path,
    content: bytes,
    summary: str | None = None,
) -> ActionProposal:
    preflight_workspace_mutation_target(target)
    return _legacy_prepare_replace_proposal(
        workspace=workspace,
        target=target,
        content=content,
        summary=summary,
    )
