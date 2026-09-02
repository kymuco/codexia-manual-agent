from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError
from codexia_manual_agent.mutation import patch_final_review_repairs as _final

_base_lexical_target = _final._lexical_target
_base_lexical_workspace_root = _final._lexical_workspace_root


def _open_posix_directory_chain(path: Path) -> int:
    """Open an absolute POSIX directory chain without leaking raw component errors."""

    if not path.is_absolute():
        raise WorkspaceMutationBoundaryError("Patch parent path must be absolute")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path.anchor or os.sep, flags)
    except OSError as exc:
        raise WorkspaceMutationBoundaryError(
            "Patch workspace root cannot be pinned"
        ) from exc

    try:
        for component in path.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=fd)
            except OSError as exc:
                raise WorkspaceMutationBoundaryError(
                    "Patch target parent cannot be pinned without link traversal"
                ) from exc
            os.close(fd)
            fd = next_fd

        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceMutationBoundaryError("Patch target parent is not a directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _reject_nul(value: Any, *, label: str) -> None:
    if isinstance(value, str) and "\x00" in value:
        raise WorkspaceMutationBoundaryError(f"{label} must not contain NUL bytes")


def _lexical_target(value: Any) -> str:
    _reject_nul(value, label="Patch target")
    return _base_lexical_target(value)


def _lexical_workspace_root(value: Any) -> Path:
    _reject_nul(value, label="Patch proposal workspace root")
    return _base_lexical_workspace_root(value)


def install_patch_portability_repairs() -> None:
    if getattr(_final, "_M24_PATCH_PORTABILITY_REPAIRS_INSTALLED", False):
        return

    # Runtime global lookup inside the final repair functions reaches these
    # replacements, preserving the public API while tightening two review edges.
    _final._open_posix_directory_chain = _open_posix_directory_chain
    _final._lexical_target = _lexical_target
    _final._lexical_workspace_root = _lexical_workspace_root
    _final._M24_PATCH_PORTABILITY_REPAIRS_INSTALLED = True


install_patch_portability_repairs()

prepare_patch_proposal = _final.prepare_patch_proposal
parse_patch_proposal = _final.parse_patch_proposal
build_patch_approval_preview = _final.build_patch_approval_preview

__all__ = [
    "build_patch_approval_preview",
    "parse_patch_proposal",
    "prepare_patch_proposal",
]
