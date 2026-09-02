from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
    WorkspaceMutationTargetExistsError,
    WorkspaceMutationTargetMissingError,
)
from codexia_manual_agent.mutation import patch_case_seam_repairs as _case
from codexia_manual_agent.mutation import patch_latest_review_repairs as _latest
from codexia_manual_agent.mutation import patch_posix_namespace_repairs as _posix_namespace
from codexia_manual_agent.mutation.models import MutationOperation, PreimageState


def _stable_windows_case_sensitive(parent: Path, handle: int) -> bool | None:
    """Read case semantics twice through the same already-held directory handle."""

    token = _case._WINDOWS_CASE_PARENT_HANDLE.set(handle)
    try:
        first = _latest._review._filesystem_case_sensitive(parent)
        second = _latest._review._filesystem_case_sensitive(parent)
    finally:
        _case._WINDOWS_CASE_PARENT_HANDLE.reset(token)
    if first != second:
        raise WorkspaceMutationPreimageChangedError(
            "Patch target parent case semantics changed during validation"
        )
    return first


def _stable_windows_parent_namespace(pinned: Any) -> _latest._DirectParentNamespace:
    handles = getattr(pinned, "_windows_handles", None)
    parent = getattr(pinned, "parent", None)
    if not handles or parent is None:
        raise WorkspaceMutationBoundaryError(
            "Patch Windows target parent is not pinned"
        )
    parent_handle = handles[-1]
    identity = _latest._windows_directory_identity(parent_handle)
    sensitivity = _stable_windows_case_sensitive(parent, parent_handle)
    if _latest._windows_directory_identity(parent_handle) != identity:
        raise WorkspaceMutationPreimageChangedError(
            "Patch target parent identity changed during namespace validation"
        )
    return _latest._DirectParentNamespace(
        identity=identity,
        case_sensitive=sensitivity,
    )


def _inspect_direct_parent_namespace(parent: Path) -> _latest._DirectParentNamespace:
    """Derive direct-target identity and case behavior from one held parent object."""

    if os.name == "nt":
        handle = _latest._parent_anchor._win_open_directory(parent)
        try:
            _latest._parent_anchor._win_verify_directory_handle(handle, parent)
            identity = _latest._windows_directory_identity(handle)
            sensitivity = _stable_windows_case_sensitive(parent, handle)
            _latest._parent_anchor._win_verify_directory_handle(handle, parent)
            if _latest._windows_directory_identity(handle) != identity:
                raise WorkspaceMutationPreimageChangedError(
                    "Patch target parent identity changed during direct validation"
                )
            return _latest._DirectParentNamespace(
                identity=identity,
                case_sensitive=sensitivity,
            )
        finally:
            _latest._parent_anchor._win_close_handle(handle)

    try:
        fd = _latest._final._open_posix_directory_chain(parent)
    except WorkspaceMutationBoundaryError:
        raise
    except OSError as exc:
        raise WorkspaceMutationBoundaryError(
            "Patch target parent cannot be pinned"
        ) from exc
    try:
        first = _posix_namespace._probe_parent_case_sensitivity(fd)
        second = _posix_namespace._probe_parent_case_sensitivity(fd)
        if first != second:
            raise WorkspaceMutationPreimageChangedError(
                "Patch target parent namespace changed during direct validation"
            )
        try:
            current = os.stat(parent, follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceMutationPreimageChangedError(
                "Patch target parent changed during direct validation"
            ) from exc
        current_identity = (int(current.st_dev), int(current.st_ino))
        if not stat.S_ISDIR(current.st_mode) or current_identity != first.identity:
            raise WorkspaceMutationPreimageChangedError(
                "Patch target parent identity changed during direct validation"
            )
        return _latest._DirectParentNamespace(
            identity=first.identity,
            case_sensitive=first.case_sensitive,
        )
    finally:
        os.close(fd)


def _direct_target_namespace_key(
    target_path: Path,
    *,
    sensitivity_cache: dict[str, Any],
):
    """Revalidate direct namespace evidence instead of trusting cached semantics."""

    parent = Path(os.path.normpath(os.path.abspath(str(target_path.parent))))
    cache_key = os.path.normcase(str(parent))
    observed = _inspect_direct_parent_namespace(parent)
    previous = sensitivity_cache.get(cache_key)
    if isinstance(previous, _latest._DirectParentNamespace):
        if previous != observed:
            raise WorkspaceMutationPreimageChangedError(
                "Patch target parent namespace changed during change-set validation"
            )
    else:
        sensitivity_cache[cache_key] = observed

    return (
        observed.identity,
        _latest._leaf_namespace_key(
            target_path.name,
            case_sensitive=observed.case_sensitive,
        ),
    )


def _assert_same_namespace(
    expected: _latest._DirectParentNamespace,
    observed: _latest._DirectParentNamespace,
) -> None:
    if observed != expected:
        raise WorkspaceMutationPreimageChangedError(
            "Patch target parent namespace changed during proposal preparation"
        )


def _prepare_windows_patch_proposal(
    *,
    workspace: str | Path,
    changes: Iterable[_latest._legacy.PatchFileRequest],
    summary: str | None,
) -> ActionProposal:
    root, root_handle, root_identity = _latest._pin_windows_workspace_before_validation(
        workspace
    )
    try:
        requests = _latest._review._bounded_requests(changes)
        if any(not isinstance(request, _latest._legacy.PatchFileRequest) for request in requests):
            raise TypeError("Patch changes must be PatchFileRequest instances")

        total_content = sum(len(request.content) for request in requests)
        if total_content > _latest._legacy.MAX_PATCH_TOTAL_CONTENT_BYTES:
            raise InvalidWorkspaceMutationError(
                "Patch exact before/after content exceeds total proposal budget "
                f"({total_content} > {_latest._legacy.MAX_PATCH_TOTAL_CONTENT_BYTES})"
            )

        prepared: list[tuple[str, _latest._legacy.PatchFileChange]] = []
        seen: set[Any] = set()
        namespace_by_path: dict[str, _latest._DirectParentNamespace] = {}

        for request in requests:
            _latest._final.preflight_workspace_mutation_target(request.target)
            rendered = _latest._port._lexical_target(request.target)
            parts = Path(rendered).parts
            parent_parts = tuple(parts[:-1])
            target_name = parts[-1]
            lexical_parent = root.joinpath(*parent_parts) if parent_parts else root

            with _latest._final.PinnedMutationTarget(
                root=root,
                parent=lexical_parent,
                target_name=target_name,
            ) as pinned:
                _latest._assert_windows_chain_uses_root(pinned, root_identity)

                normalized, target_path, parent = _latest._legacy._normalize_target(
                    root, rendered
                )
                if normalized != rendered:
                    raise WorkspaceMutationBoundaryError(
                        "Patch target changed canonical spelling during validation"
                    )
                if os.path.normcase(os.path.abspath(str(parent))) != os.path.normcase(
                    os.path.abspath(str(lexical_parent))
                ):
                    raise WorkspaceMutationBoundaryError(
                        "Patch target parent changed during validation"
                    )
                if os.path.normcase(os.path.abspath(str(target_path))) != os.path.normcase(
                    os.path.abspath(str(pinned.target_path))
                ):
                    raise WorkspaceMutationBoundaryError(
                        "Patch target path changed during validation"
                    )

                pinned.verify_parent_identity()
                _latest._parent_anchor._win_verify_directory_handle(root_handle, root)

                namespace_before = _stable_windows_parent_namespace(pinned)
                parent_cache_key = os.path.normcase(
                    os.path.abspath(str(lexical_parent))
                )
                previous_namespace = namespace_by_path.get(parent_cache_key)
                if previous_namespace is not None:
                    _assert_same_namespace(previous_namespace, namespace_before)

                key = (
                    namespace_before.identity,
                    _latest._leaf_namespace_key(
                        target_name,
                        case_sensitive=namespace_before.case_sensitive,
                    ),
                )
                if key in seen:
                    raise InvalidWorkspaceMutationError(
                        f"Patch proposal contains duplicate target: {rendered}"
                    )
                seen.add(key)

                snapshot, preimage = _latest._final._capture_windows_exact_path(
                    pinned.target_path,
                    max_bytes=_latest._legacy.MAX_PATCH_FILE_BYTES,
                )
                pinned.verify_parent_identity()
                _latest._parent_anchor._win_verify_directory_handle(root_handle, root)

                namespace_after = _stable_windows_parent_namespace(pinned)
                _assert_same_namespace(namespace_before, namespace_after)
                if previous_namespace is not None:
                    _assert_same_namespace(previous_namespace, namespace_after)
                else:
                    namespace_by_path[parent_cache_key] = namespace_after

            if snapshot.state is PreimageState.PRESENT:
                if (
                    snapshot.size_bytes is None
                    or snapshot.size_bytes > _latest._legacy.MAX_PATCH_FILE_BYTES
                ):
                    raise InvalidWorkspaceMutationError(
                        f"Patch preimage exceeds {_latest._legacy.MAX_PATCH_FILE_BYTES} bytes"
                    )
                if (
                    snapshot.size_bytes
                    > _latest._legacy.MAX_PATCH_TOTAL_CONTENT_BYTES - total_content
                ):
                    raise InvalidWorkspaceMutationError(
                        "Patch exact before/after content exceeds total proposal budget"
                    )
                if preimage is None or len(preimage) != snapshot.size_bytes:
                    raise WorkspaceMutationPreimageChangedError(
                        "Patch preimage payload does not match pinned snapshot"
                    )
                _latest._legacy._text(
                    preimage,
                    f"Patch preimage for {target_name}",
                )
                total_content += len(preimage)
            elif preimage is not None:
                raise InvalidWorkspaceMutationError(
                    "Absent patch preimage must not carry bytes"
                )

            if request.operation is MutationOperation.CREATE:
                if snapshot.state is not PreimageState.ABSENT:
                    raise WorkspaceMutationTargetExistsError(
                        f"Create target already exists: {rendered}"
                    )
            else:
                if snapshot.state is not PreimageState.PRESENT:
                    raise WorkspaceMutationTargetMissingError(
                        f"Replace target does not exist: {rendered}"
                    )
                if preimage == request.content:
                    raise InvalidWorkspaceMutationError(
                        f"Replace patch is a no-op for {rendered}"
                    )

            prepared.append(
                (
                    rendered,
                    _latest._legacy.PatchFileChange.create(
                        operation=request.operation,
                        target=rendered,
                        expected_preimage=snapshot,
                        preimage=preimage,
                        postimage=request.content,
                    ),
                )
            )

        prepared.sort(key=lambda item: item[0])
        token = _latest._final._SELF_CONTAINED_PARSE.set(True)
        try:
            change_set = _latest._legacy.PatchChangeSet.create(
                workspace_root=str(root),
                changes=[item[1] for item in prepared],
            )
        finally:
            _latest._final._SELF_CONTAINED_PARSE.reset(token)

        _latest._hard._check_preview_budget(change_set.changes)
        _latest._parent_anchor._win_verify_directory_handle(root_handle, root)

        return ActionProposal.create(
            capability=_latest.Capability.WRITE_WORKSPACE,
            action=_latest._legacy.PATCH_ACTION,
            workspace_root=str(root),
            parameters=change_set.to_parameters(),
            summary=summary or f"Apply {len(change_set.changes)}-file workspace patch.",
        )
    finally:
        _latest._parent_anchor._win_close_handle(root_handle)


def install_patch_namespace_stability_repairs() -> None:
    if getattr(_latest, "_M24_NAMESPACE_STABILITY_REPAIRS_INSTALLED", False):
        return

    # Direct PatchChangeSet authoring reaches the case-seam NUL guard first;
    # valid targets then delegate here for held-parent identity/case evidence.
    _latest._direct_target_namespace_key = _direct_target_namespace_key
    _case._base_target_namespace_key = _direct_target_namespace_key

    # Public prepare_patch_proposal remains the same function object. Its Windows
    # dispatcher resolves this replacement at runtime, preserving all sealed
    # package/submodule entrypoints while replacing only the vulnerable branch.
    _latest._prepare_windows_patch_proposal = _prepare_windows_patch_proposal
    _latest._M24_NAMESPACE_STABILITY_REPAIRS_INSTALLED = True


install_patch_namespace_stability_repairs()

prepare_patch_proposal = _latest.prepare_patch_proposal
parse_patch_proposal = _latest.parse_patch_proposal
build_patch_approval_preview = _latest.build_patch_approval_preview

__all__ = [
    "build_patch_approval_preview",
    "parse_patch_proposal",
    "prepare_patch_proposal",
]
