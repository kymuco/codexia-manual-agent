from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.errors import InvalidWorkspaceMutationError
from codexia_manual_agent.mutation import patch_case_seam_repairs as _case
from codexia_manual_agent.mutation import patch_final_review_repairs as _final
from codexia_manual_agent.mutation import patch_hardening as _hard
from codexia_manual_agent.mutation import patch_latest_review_repairs as _latest
from codexia_manual_agent.mutation import patch_namespace_stability_repairs as _stable
from codexia_manual_agent.mutation import patch_portability_repairs as _port
from codexia_manual_agent.mutation import patch_posix_namespace_repairs as _posix_namespace
from codexia_manual_agent.mutation import patch_posix_proposal_stability as _posix_stability
from codexia_manual_agent.mutation import patch_posix_root_anchor as _anchor
from codexia_manual_agent.mutation import patch_review_repairs as _review
from codexia_manual_agent.mutation import patches as _legacy

_base_prepare_patch_proposal = _posix_stability.prepare_patch_proposal
_base_parse_patch_proposal = _posix_stability.parse_patch_proposal
_base_build_patch_approval_preview = _posix_stability.build_patch_approval_preview


def _canonical_preview_bytes(preview: _legacy.PatchApprovalPreview) -> bytes:
    """Serialize the complete human-review surface with deterministic UTF-8 JSON."""

    return json.dumps(
        preview.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _assert_complete_preview_budget(preview: _legacy.PatchApprovalPreview) -> None:
    size = len(_canonical_preview_bytes(preview))
    if size > _legacy.MAX_PATCH_PREVIEW_BYTES:
        raise InvalidWorkspaceMutationError(
            "Patch complete human-readable preview exceeds review budget "
            f"({size} > {_legacy.MAX_PATCH_PREVIEW_BYTES})"
        )


def build_patch_approval_preview(
    proposal: ActionProposal,
) -> _legacy.PatchApprovalPreview:
    preview = _base_build_patch_approval_preview(proposal)
    _assert_complete_preview_budget(preview)
    return preview


def prepare_patch_proposal(
    *,
    workspace: str | Path,
    changes: Iterable[_legacy.PatchFileRequest],
    summary: str | None = None,
) -> ActionProposal:
    proposal = _base_prepare_patch_proposal(
        workspace=workspace,
        changes=changes,
        summary=summary,
    )
    # A proposal is reviewable only if the exact approval object that would be
    # shown to the human remains inside the documented output budget. The older
    # diff-only checks stay in place as an early bound; this is the final gate on
    # the complete serialized surface, including repeated targets and metadata.
    preview = _base_build_patch_approval_preview(proposal)
    _assert_complete_preview_budget(preview)
    return proposal


# No parser semantics are changed by this repair. Preserve the exact existing
# function object so prior self-contained parser sealing remains intact.
parse_patch_proposal = _base_parse_patch_proposal


def install_patch_preview_budget_repairs() -> None:
    if getattr(_legacy, "_M24_COMPLETE_PREVIEW_BUDGET_INSTALLED", False):
        return

    modules: tuple[Any, ...] = (
        _legacy,
        _hard,
        _review,
        _final,
        _port,
        _anchor,
        _posix_namespace,
        _latest,
        _case,
        _stable,
        _posix_stability,
    )
    for module in modules:
        module.prepare_patch_proposal = prepare_patch_proposal
        module.build_patch_approval_preview = build_patch_approval_preview

    _legacy._M24_COMPLETE_PREVIEW_BUDGET_INSTALLED = True


install_patch_preview_budget_repairs()

__all__ = [
    "build_patch_approval_preview",
    "parse_patch_proposal",
    "prepare_patch_proposal",
]
