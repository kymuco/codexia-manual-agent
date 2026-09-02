from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from codexia_manual_agent.mutation import patch_posix_root_anchor as _anchor
from codexia_manual_agent.mutation import patch_review_repairs as _review

_base_namespace_key = _anchor._namespace_key


@dataclass(frozen=True, slots=True)
class _PinnedParentNamespace:
    identity: tuple[int, int]
    case_sensitive: bool | None


def _probe_parent_case_sensitivity(parent_fd: int) -> _PinnedParentNamespace:
    """Bounded, fd-relative namespace probe with parent identity attached."""

    parent_identity = _anchor._stat_identity(os.fstat(parent_fd))
    sensitivity: bool | None = None

    try:
        with os.scandir(parent_fd) as entries:
            for entry in islice(entries, _anchor._CASE_PROBE_SCAN_LIMIT):
                name = entry.name
                alternate = _review._case_variant(name)
                if alternate is None or alternate == name:
                    continue
                try:
                    original = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    continue
                try:
                    candidate = os.stat(
                        alternate,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    sensitivity = True
                    break
                except OSError:
                    continue
                sensitivity = (
                    _anchor._stat_identity(original)
                    != _anchor._stat_identity(candidate)
                )
                break
    except (OSError, TypeError):
        # Unknown namespace behavior remains fail-closed at key construction.
        sensitivity = None

    return _PinnedParentNamespace(
        identity=parent_identity,
        case_sensitive=sensitivity,
    )


def _leaf_namespace_key(name: str, *, case_sensitive: bool | None) -> str:
    normalized = unicodedata.normalize("NFC", name)
    if case_sensitive is True:
        return normalized
    return unicodedata.normalize("NFC", normalized.casefold())


def _namespace_key(
    rendered: str,
    *,
    case_sensitive: bool | None | _PinnedParentNamespace,
):
    """Identify a target by pinned parent inode plus normalized leaf spelling.

    Ancestor spellings are deliberately excluded: two different lexical parent
    paths that resolve through an insensitive ancestor to the same pinned parent
    inode must identify the same target when their leaf names alias there.
    """

    if not isinstance(case_sensitive, _PinnedParentNamespace):
        # Preserve compatibility for direct helper callers. Active anchored
        # proposal preparation always receives _PinnedParentNamespace from the
        # patched probe below.
        return _base_namespace_key(rendered, case_sensitive=case_sensitive)

    leaf = Path(rendered).name
    return (
        case_sensitive.identity,
        _leaf_namespace_key(
            leaf,
            case_sensitive=case_sensitive.case_sensitive,
        ),
    )


def install_patch_posix_namespace_repairs() -> None:
    if getattr(_anchor, "_M24_POSIX_NAMESPACE_REPAIRS_INSTALLED", False):
        return

    _anchor._probe_parent_case_sensitivity = _probe_parent_case_sensitivity
    _anchor._namespace_key = _namespace_key
    _anchor._M24_POSIX_NAMESPACE_REPAIRS_INSTALLED = True


install_patch_posix_namespace_repairs()

prepare_patch_proposal = _anchor.prepare_patch_proposal
parse_patch_proposal = _anchor.parse_patch_proposal
build_patch_approval_preview = _anchor.build_patch_approval_preview

__all__ = [
    "build_patch_approval_preview",
    "parse_patch_proposal",
    "prepare_patch_proposal",
]
