from __future__ import annotations

import ctypes
from contextvars import ContextVar
from dataclasses import dataclass
from ctypes import wintypes
from typing import Any

from codexia_manual_agent.domain.errors import (
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation import patch_latest_review_repairs as _latest
from codexia_manual_agent.mutation import patch_namespace_stability_repairs as _stable
from codexia_manual_agent.mutation import patch_windows_namespace_guard as _guard


@dataclass(frozen=True, slots=True)
class _AdmittedWindowsLookup:
    parent_handle: int
    case_sensitive: bool | None


_WINDOWS_ADMITTED_LOOKUP: ContextVar[_AdmittedWindowsLookup | None] = ContextVar(
    "m24_windows_admitted_lookup",
    default=None,
)
_WINDOWS_NT_LOOKUP_ACTIVE: ContextVar[bool] = ContextVar(
    "m24_windows_nt_lookup_active",
    default=False,
)

_base_stable_windows_parent_namespace = _stable._stable_windows_parent_namespace
_base_prepare_windows_patch_proposal = _stable._prepare_windows_patch_proposal
_base_windows_case_sensitive_by_handle = _guard._windows_case_sensitive_by_handle
_base_nt_open_relative_target = _guard._nt_open_relative_target


def _stable_windows_parent_namespace(pinned: Any) -> _latest._DirectParentNamespace:
    """Record the exact case semantics admitted for the next leaf lookup."""

    namespace = _base_stable_windows_parent_namespace(pinned)
    handles = getattr(pinned, "_windows_handles", None)
    if not handles:
        raise WorkspaceMutationBoundaryError(
            "Patch Windows target parent is not pinned for case-bound lookup"
        )
    _WINDOWS_ADMITTED_LOOKUP.set(
        _AdmittedWindowsLookup(
            parent_handle=int(handles[-1]),
            case_sensitive=namespace.case_sensitive,
        )
    )
    return namespace


def _windows_case_sensitive_by_handle(handle: int) -> bool | None:
    """Use admitted semantics only inside the authority-bearing NtOpenFile call.

    All ordinary namespace observations continue to query the live held directory
    handle. This prevents the binding layer from masking a real case-flag change
    during namespace_before/namespace_after revalidation.
    """

    if _WINDOWS_NT_LOOKUP_ACTIVE.get():
        admitted = _WINDOWS_ADMITTED_LOOKUP.get()
        if admitted is None or admitted.parent_handle != int(handle):
            raise WorkspaceMutationBoundaryError(
                "Patch Windows leaf lookup is missing admitted case semantics"
            )
        return admitted.case_sensitive
    return _base_windows_case_sensitive_by_handle(handle)


def _opened_leaf_name(handle: int) -> str:
    """Return the actual final leaf spelling for an already-open Windows handle."""

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise WorkspaceMutationBoundaryError(
            "Patch Windows opened-target identity cannot be queried"
        ) from exc

    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_final_path.restype = wintypes.DWORD

    required = int(get_final_path(wintypes.HANDLE(handle), None, 0, 0))
    if required <= 0:
        raise WorkspaceMutationBoundaryError(
            "Patch Windows opened-target final path cannot be measured"
        )

    buffer = ctypes.create_unicode_buffer(required + 1)
    written = int(
        get_final_path(
            wintypes.HANDLE(handle),
            buffer,
            len(buffer),
            0,
        )
    )
    if written <= 0 or written >= len(buffer):
        raise WorkspaceMutationBoundaryError(
            "Patch Windows opened-target final path cannot be read"
        )

    rendered = buffer.value.rstrip("\\/").replace("/", "\\")
    leaf = rendered.rsplit("\\", 1)[-1]
    if not leaf:
        raise WorkspaceMutationBoundaryError(
            "Patch Windows opened-target final leaf is unavailable"
        )
    return leaf


def _nt_open_relative_target(parent_handle: int, target_name: str) -> int | None:
    """Open a leaf using the already-admitted case semantics, never a fresh query."""

    admitted = _WINDOWS_ADMITTED_LOOKUP.get()
    if admitted is None or admitted.parent_handle != int(parent_handle):
        # Preserve the standalone diagnostic/helper surface. The authority-bearing
        # proposal path always records namespace_before immediately before capture.
        return _base_nt_open_relative_target(parent_handle, target_name)

    token = _WINDOWS_NT_LOOKUP_ACTIVE.set(True)
    try:
        opened = _base_nt_open_relative_target(parent_handle, target_name)
    finally:
        _WINDOWS_NT_LOOKUP_ACTIVE.reset(token)

    if opened is None:
        return None

    if admitted.case_sensitive is True:
        try:
            actual_leaf = _opened_leaf_name(int(opened))
        except BaseException:
            _latest._parent_anchor._win_close_handle(int(opened))
            raise
        if actual_leaf != target_name:
            _latest._parent_anchor._win_close_handle(int(opened))
            raise WorkspaceMutationPreimageChangedError(
                "Patch Windows target leaf changed case identity during relative open"
            )
    return int(opened)


def _prepare_windows_patch_proposal(*, workspace, changes, summary):
    """Scope admitted leaf-lookup evidence to one Windows proposal preparation."""

    token = _WINDOWS_ADMITTED_LOOKUP.set(None)
    try:
        return _base_prepare_windows_patch_proposal(
            workspace=workspace,
            changes=changes,
            summary=summary,
        )
    finally:
        _WINDOWS_ADMITTED_LOOKUP.reset(token)


def install_patch_windows_case_binding_repairs() -> None:
    if getattr(_guard, "_M24_WINDOWS_CASE_BINDING_REPAIRS_INSTALLED", False):
        return

    # namespace_before records the admitted semantics; the existing Windows
    # preparation function resolves this helper dynamically for each request.
    _stable._stable_windows_parent_namespace = _stable_windows_parent_namespace

    # NtOpenFile still lives in the established relative-open implementation.
    # Its case query is replaced only while that one open is executing, so live
    # namespace revalidation remains independent and race-detecting.
    _guard._windows_case_sensitive_by_handle = _windows_case_sensitive_by_handle
    _guard._nt_open_relative_target = _nt_open_relative_target

    # The public latest-review dispatcher resolves this branch dynamically.
    _stable._prepare_windows_patch_proposal = _prepare_windows_patch_proposal
    _latest._prepare_windows_patch_proposal = _prepare_windows_patch_proposal

    _guard._M24_WINDOWS_CASE_BINDING_REPAIRS_INSTALLED = True


install_patch_windows_case_binding_repairs()

__all__ = []
