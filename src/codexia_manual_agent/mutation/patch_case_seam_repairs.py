from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any

from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError
from codexia_manual_agent.mutation import patch_latest_review_repairs as _latest
from codexia_manual_agent.mutation import patch_review_repairs as _review

_base_query_windows_directory_case_sensitive = (
    _review._query_windows_directory_case_sensitive
)
_base_target_namespace_key = _review._target_namespace_key
_WINDOWS_CASE_PARENT_HANDLE: ContextVar[int | None] = ContextVar(
    "m24_windows_case_parent_handle",
    default=None,
)


def _query_windows_directory_case_sensitive(directory: Path) -> bool | None:
    """Preserve the established path-query seam while using a pinned handle live.

    Direct callers without a pinned proposal context retain the previous path-based
    query. During Windows proposal preparation, the context carries the already-held
    parent handle, so production does not reopen a mutable namespace path merely to
    obtain FileCaseSensitiveInfo.
    """

    handle = _WINDOWS_CASE_PARENT_HANDLE.get()
    if handle is not None:
        return _latest._windows_case_sensitive_by_handle(handle)
    return _base_query_windows_directory_case_sensitive(directory)


def _target_namespace_key(
    target_path: Path,
    *,
    sensitivity_cache: dict[str, Any],
):
    """Reject impossible direct targets before live namespace identity work."""

    if "\x00" in str(target_path):
        raise WorkspaceMutationBoundaryError(
            "Patch target must not contain NUL bytes"
        )
    return _base_target_namespace_key(
        target_path,
        sensitivity_cache=sensitivity_cache,
    )


def _windows_parent_namespace(pinned: Any) -> _latest._DirectParentNamespace:
    handles = getattr(pinned, "_windows_handles", None)
    if not handles:
        raise WorkspaceMutationBoundaryError(
            "Patch Windows target parent is not pinned"
        )

    parent_handle = handles[-1]
    parent = getattr(pinned, "parent", None)
    if parent is None:
        # Preserve direct helper compatibility for callers that supply only a held
        # handle. Active proposal preparation always carries the parent path and
        # therefore uses the legacy seam routed through the pinned-handle context.
        sensitivity = _latest._windows_case_sensitive_by_handle(parent_handle)
    else:
        token = _WINDOWS_CASE_PARENT_HANDLE.set(parent_handle)
        try:
            # Keep the long-standing _filesystem_case_sensitive / query helper
            # seams observable to callers and tests. In production both reads are
            # serviced from the same held parent handle by the wrapper above.
            # Requiring stable repeated evidence also fails closed if the directory
            # flag changes while namespace identity is being admitted.
            first = _review._filesystem_case_sensitive(parent)
            second = _review._filesystem_case_sensitive(parent)
        finally:
            _WINDOWS_CASE_PARENT_HANDLE.reset(token)
        sensitivity = first if first == second else None

    return _latest._DirectParentNamespace(
        identity=_latest._windows_directory_identity(parent_handle),
        case_sensitive=sensitivity,
    )


def install_patch_case_seam_repairs() -> None:
    if getattr(_latest, "_M24_CASE_SEAM_REPAIRS_INSTALLED", False):
        return

    _review._query_windows_directory_case_sensitive = (
        _query_windows_directory_case_sensitive
    )
    _review._target_namespace_key = _target_namespace_key
    _latest._windows_parent_namespace = _windows_parent_namespace
    _latest._M24_CASE_SEAM_REPAIRS_INSTALLED = True


install_patch_case_seam_repairs()

prepare_patch_proposal = _latest.prepare_patch_proposal
parse_patch_proposal = _latest.parse_patch_proposal
build_patch_approval_preview = _latest.build_patch_approval_preview

__all__ = [
    "build_patch_approval_preview",
    "parse_patch_proposal",
    "prepare_patch_proposal",
]
