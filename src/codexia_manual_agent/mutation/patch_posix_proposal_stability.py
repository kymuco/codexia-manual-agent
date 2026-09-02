from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codexia_manual_agent.domain.errors import WorkspaceMutationPreimageChangedError
from codexia_manual_agent.mutation import patch_namespace_stability_repairs as _stable
from codexia_manual_agent.mutation import patch_posix_root_anchor as _anchor


@dataclass(frozen=True, slots=True)
class _PosixParentNamespaceSnapshot:
    identity: tuple[int, int]
    case_sensitive: bool | None
    probe_evidence: Any = field(compare=False, repr=False)


@dataclass(slots=True)
class _PosixProposalNamespaceState:
    by_parent_parts: dict[tuple[str, ...], _PosixParentNamespaceSnapshot] = field(
        default_factory=dict
    )
    by_identity: dict[tuple[int, int], _PosixParentNamespaceSnapshot] = field(
        default_factory=dict
    )


_POSIX_PROPOSAL_NAMESPACE_STATE: ContextVar[_PosixProposalNamespaceState | None] = (
    ContextVar("m24_posix_proposal_namespace_state", default=None)
)

_base_open_parent_from_root_fd = _anchor._open_parent_from_root_fd
_base_probe_parent_case_sensitivity = _anchor._probe_parent_case_sensitivity
_base_verify_parent_still_names_anchor = _anchor._verify_parent_still_names_anchor
_base_prepare_posix_patch_proposal = _anchor._prepare_posix_patch_proposal


def _namespace_snapshot(parent_fd: int) -> _PosixParentNamespaceSnapshot:
    """Read identity and case evidence from the same already-held parent fd."""

    info = os.fstat(parent_fd)
    identity = _anchor._stat_identity(info)
    # Deliberately use the captured base probe here. The active anchor probe is
    # replaced below so duplicate-key construction reuses this observation
    # instead of independently probing mutable namespace semantics again.
    evidence: Any = _base_probe_parent_case_sensitivity(parent_fd)

    evidence_identity = getattr(evidence, "identity", identity)
    if tuple(evidence_identity) != identity:
        raise WorkspaceMutationPreimageChangedError(
            "Patch POSIX parent identity changed during namespace observation"
        )

    sensitivity = getattr(evidence, "case_sensitive", evidence)
    if sensitivity not in (True, False, None):
        sensitivity = None

    return _PosixParentNamespaceSnapshot(
        identity=identity,
        case_sensitive=sensitivity,
        probe_evidence=evidence,
    )


def _remember_namespace_observation(
    state: _PosixProposalNamespaceState,
    parent_parts: tuple[str, ...],
    observed: _PosixParentNamespaceSnapshot,
) -> None:
    previous_path = state.by_parent_parts.get(parent_parts)
    if previous_path is not None and previous_path != observed:
        raise WorkspaceMutationPreimageChangedError(
            "Patch POSIX target parent namespace changed across proposal requests"
        )

    previous_identity = state.by_identity.get(observed.identity)
    if previous_identity is not None and previous_identity != observed:
        raise WorkspaceMutationPreimageChangedError(
            "Patch POSIX parent case semantics changed for held namespace identity"
        )

    state.by_parent_parts[parent_parts] = observed
    state.by_identity[observed.identity] = observed


def _assert_lexical_parent_stable(
    parent_parts: tuple[str, ...],
    observed: _PosixParentNamespaceSnapshot,
) -> None:
    state = _POSIX_PROPOSAL_NAMESPACE_STATE.get()
    if state is None:
        return
    _remember_namespace_observation(state, parent_parts, observed)


def _probe_parent_case_sensitivity(parent_fd: int) -> Any:
    """Reuse the exact namespace evidence already admitted for this held parent.

    The base POSIX prepare path asks for case sensitivity again when building its
    duplicate-target key. During proposal preparation the parent was already
    observed by _open_parent_from_root_fd through the same held fd/inode. Reusing
    the original probe object preserves the pinned-parent identity + normalized
    leaf-key contract while preventing a transient second probe from disagreeing
    with the namespace evidence that the stability layer later revalidates.
    """

    state = _POSIX_PROPOSAL_NAMESPACE_STATE.get()
    if state is None:
        return _base_probe_parent_case_sensitivity(parent_fd)

    identity = _anchor._stat_identity(os.fstat(parent_fd))
    observed = state.by_identity.get(identity)
    if observed is None:
        raise WorkspaceMutationPreimageChangedError(
            "Patch POSIX namespace key lacks an admitted parent observation"
        )
    return observed.probe_evidence


def _open_parent_from_root_fd(root_fd: int, parent_parts: tuple[str, ...]) -> int:
    """Pin a parent and bind its lexical path to one namespace for the proposal."""

    parent_fd = _base_open_parent_from_root_fd(root_fd, parent_parts)
    if _POSIX_PROPOSAL_NAMESPACE_STATE.get() is None:
        return parent_fd

    try:
        _assert_lexical_parent_stable(
            parent_parts,
            _namespace_snapshot(parent_fd),
        )
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _verify_parent_still_names_anchor(
    root_fd: int,
    parent_parts: tuple[str, ...],
    pinned_parent: os.stat_result,
) -> None:
    """Retain identity verification and recheck namespace semantics after capture."""

    _base_verify_parent_still_names_anchor(root_fd, parent_parts, pinned_parent)
    state = _POSIX_PROPOSAL_NAMESPACE_STATE.get()
    if state is None:
        return

    current_fd = _base_open_parent_from_root_fd(root_fd, parent_parts)
    try:
        observed = _namespace_snapshot(current_fd)
    finally:
        os.close(current_fd)

    if observed.identity != _anchor._stat_identity(pinned_parent):
        raise WorkspaceMutationPreimageChangedError(
            "Patch POSIX target parent identity changed after preimage capture"
        )
    _assert_lexical_parent_stable(parent_parts, observed)


def _prepare_posix_patch_proposal(
    *,
    workspace: str | Path,
    changes,
    summary: str | None,
):
    """Scope lexical-parent namespace history to one POSIX proposal preparation."""

    token = _POSIX_PROPOSAL_NAMESPACE_STATE.set(_PosixProposalNamespaceState())
    try:
        return _base_prepare_posix_patch_proposal(
            workspace=workspace,
            changes=changes,
            summary=summary,
        )
    finally:
        _POSIX_PROPOSAL_NAMESPACE_STATE.reset(token)


def install_patch_posix_proposal_stability() -> None:
    if getattr(_anchor, "_M24_POSIX_PROPOSAL_STABILITY_INSTALLED", False):
        return

    # The existing anchor prepare function resolves these helpers dynamically.
    # Public proposal entrypoints therefore remain the same sealed function
    # objects while the POSIX branch gains proposal-wide lexical-parent history.
    _anchor._open_parent_from_root_fd = _open_parent_from_root_fd
    _anchor._probe_parent_case_sensitivity = _probe_parent_case_sensitivity
    _anchor._verify_parent_still_names_anchor = _verify_parent_still_names_anchor
    _anchor._prepare_posix_patch_proposal = _prepare_posix_patch_proposal
    _anchor._M24_POSIX_PROPOSAL_STABILITY_INSTALLED = True


install_patch_posix_proposal_stability()

prepare_patch_proposal = _stable.prepare_patch_proposal
parse_patch_proposal = _stable.parse_patch_proposal
build_patch_approval_preview = _stable.build_patch_approval_preview

__all__ = [
    "build_patch_approval_preview",
    "parse_patch_proposal",
    "prepare_patch_proposal",
]
