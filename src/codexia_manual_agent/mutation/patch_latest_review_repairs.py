from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
    WorkspaceMutationTargetExistsError,
    WorkspaceMutationTargetMissingError,
)
from codexia_manual_agent.mutation import parent_anchor as _parent_anchor
from codexia_manual_agent.mutation import patch_final_review_repairs as _final
from codexia_manual_agent.mutation import patch_hardening as _hard
from codexia_manual_agent.mutation import patch_portability_repairs as _port
from codexia_manual_agent.mutation import patch_posix_namespace_repairs as _posix_namespace
from codexia_manual_agent.mutation import patch_posix_root_anchor as _posix_anchor
from codexia_manual_agent.mutation import patch_review_repairs as _review
from codexia_manual_agent.mutation import patches as _legacy
from codexia_manual_agent.mutation.models import MutationOperation, PreimageState

_base_prepare_patch_proposal = _posix_namespace.prepare_patch_proposal


@dataclass(frozen=True, slots=True)
class _DirectParentNamespace:
    identity: tuple[int, int]
    case_sensitive: bool | None


def _leaf_namespace_key(name: str, *, case_sensitive: bool | None) -> str:
    normalized = unicodedata.normalize("NFC", name)
    if case_sensitive is True:
        return normalized
    return unicodedata.normalize("NFC", normalized.casefold())


def _direct_target_namespace_key(
    target_path: Path,
    *,
    sensitivity_cache: dict[str, Any],
):
    """Key direct change-set targets by resolved parent identity plus leaf.

    The public direct-construction path has already normalized the target parent.
    Ancestor spelling therefore must not participate in target identity: two
    spellings that reach one parent inode and one leaf are one filesystem target.
    """

    parent = Path(os.path.normpath(os.path.abspath(str(target_path.parent))))
    cache_key = os.path.normcase(str(parent))
    cached = sensitivity_cache.get(cache_key)
    if not isinstance(cached, _DirectParentNamespace):
        try:
            info = os.stat(parent, follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Patch target parent identity cannot be inspected"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceMutationBoundaryError(
                "Patch target parent must be a directory"
            )
        cached = _DirectParentNamespace(
            identity=(int(info.st_dev), int(info.st_ino)),
            case_sensitive=_review._filesystem_case_sensitive(parent),
        )
        sensitivity_cache[cache_key] = cached

    return (
        cached.identity,
        _leaf_namespace_key(
            target_path.name,
            case_sensitive=cached.case_sensitive,
        ),
    )


def _windows_directory_identity(handle: int) -> tuple[int, int]:
    info = _final._win_file_info(handle)
    index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
    return int(info.dwVolumeSerialNumber), index


def _win_open_workspace_anchor(path: Path) -> int:
    """Open the workspace directory object while allowing canonical alias input."""

    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    FILE_READ_ATTRIBUTES = 0x0080
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

    handle = create_file(
        str(path),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    value = ctypes.cast(handle, ctypes.c_void_p).value
    invalid = ctypes.c_void_p(-1).value
    if value is None or value == invalid:
        raise WorkspaceMutationBoundaryError(
            f"Patch workspace cannot be pinned: {path} "
            f"(winerror={ctypes.get_last_error()})"
        )
    return int(value)


def _pin_windows_workspace_before_validation(
    workspace: str | Path,
) -> tuple[Path, int, tuple[int, int]]:
    """Pin the Windows workspace directory object before canonical validation."""

    raw = str(Path(workspace).expanduser())
    if "\x00" in raw:
        raise WorkspaceMutationBoundaryError(
            "Patch workspace must not contain NUL bytes"
        )
    absolute_input = Path(os.path.normpath(os.path.abspath(raw)))
    handle = _win_open_workspace_anchor(absolute_input)
    try:
        identity = _windows_directory_identity(handle)

        root = _legacy._workspace_root(absolute_input)
        # Workspace aliases remain compatible with the established canonical
        # contract: the canonical path is accepted only if the object pinned by
        # the very first open still reports that exact final path.
        _parent_anchor._win_verify_directory_handle(handle, root)
        return root, handle, identity
    except BaseException:
        _parent_anchor._win_close_handle(handle)
        raise


def _assert_windows_chain_uses_root(
    pinned: Any,
    expected_root_identity: tuple[int, int],
) -> None:
    handles = getattr(pinned, "_windows_handles", None)
    if not handles:
        raise WorkspaceMutationBoundaryError(
            "Patch Windows target chain is not pinned"
        )
    if _windows_directory_identity(handles[0]) != expected_root_identity:
        raise WorkspaceMutationPreimageChangedError(
            "Patch workspace identity changed before target validation"
        )


def _windows_case_sensitive_by_handle(handle: int) -> bool | None:
    import ctypes
    from ctypes import wintypes

    class _FileCaseSensitiveInfo(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    get_info.restype = wintypes.BOOL

    info = _FileCaseSensitiveInfo()
    if not get_info(
        wintypes.HANDLE(handle),
        23,  # FileCaseSensitiveInfo
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        return None
    return bool(int(info.Flags) & 0x00000001)


def _windows_parent_namespace(pinned: Any) -> _DirectParentNamespace:
    handles = getattr(pinned, "_windows_handles", None)
    if not handles:
        raise WorkspaceMutationBoundaryError(
            "Patch Windows target parent is not pinned"
        )
    parent_handle = handles[-1]
    return _DirectParentNamespace(
        identity=_windows_directory_identity(parent_handle),
        case_sensitive=_windows_case_sensitive_by_handle(parent_handle),
    )


def _prepare_windows_patch_proposal(
    *,
    workspace: str | Path,
    changes: Iterable[_legacy.PatchFileRequest],
    summary: str | None,
) -> ActionProposal:
    root, root_handle, root_identity = _pin_windows_workspace_before_validation(workspace)
    try:
        requests = _review._bounded_requests(changes)
        if any(not isinstance(request, _legacy.PatchFileRequest) for request in requests):
            raise TypeError("Patch changes must be PatchFileRequest instances")

        total_content = sum(len(request.content) for request in requests)
        if total_content > _legacy.MAX_PATCH_TOTAL_CONTENT_BYTES:
            raise InvalidWorkspaceMutationError(
                "Patch exact before/after content exceeds total proposal budget "
                f"({total_content} > {_legacy.MAX_PATCH_TOTAL_CONTENT_BYTES})"
            )

        prepared: list[tuple[str, _legacy.PatchFileChange]] = []
        seen: set[Any] = set()
        namespace_cache: dict[tuple[int, int], _DirectParentNamespace] = {}

        for request in requests:
            _final.preflight_workspace_mutation_target(request.target)
            rendered = _port._lexical_target(request.target)
            parts = Path(rendered).parts
            parent_parts = tuple(parts[:-1])
            target_name = parts[-1]
            lexical_parent = root.joinpath(*parent_parts) if parent_parts else root

            # Pin the workspace-to-parent chain before live target validation.
            # The active PinnedMutationTarget subclass also installs the pre-read
            # parent verifier used by _capture_windows_exact_path().
            with _final.PinnedMutationTarget(
                root=root,
                parent=lexical_parent,
                target_name=target_name,
            ) as pinned:
                _assert_windows_chain_uses_root(pinned, root_identity)

                normalized, target_path, parent = _legacy._normalize_target(root, rendered)
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

                # This revalidation occurs after _normalize_target and before any
                # target handle can be opened/read, closing the path-first window.
                pinned.verify_parent_identity()
                _parent_anchor._win_verify_directory_handle(root_handle, root)

                parent_identity = _windows_directory_identity(pinned._windows_handles[-1])
                namespace = namespace_cache.get(parent_identity)
                if namespace is None:
                    namespace = _windows_parent_namespace(pinned)
                    namespace_cache[parent_identity] = namespace
                key = (
                    namespace.identity,
                    _leaf_namespace_key(
                        target_name,
                        case_sensitive=namespace.case_sensitive,
                    ),
                )
                if key in seen:
                    raise InvalidWorkspaceMutationError(
                        f"Patch proposal contains duplicate target: {rendered}"
                    )
                seen.add(key)

                snapshot, preimage = _final._capture_windows_exact_path(
                    pinned.target_path,
                    max_bytes=_legacy.MAX_PATCH_FILE_BYTES,
                )
                pinned.verify_parent_identity()
                _parent_anchor._win_verify_directory_handle(root_handle, root)

            if snapshot.state is PreimageState.PRESENT:
                if snapshot.size_bytes is None or snapshot.size_bytes > _legacy.MAX_PATCH_FILE_BYTES:
                    raise InvalidWorkspaceMutationError(
                        f"Patch preimage exceeds {_legacy.MAX_PATCH_FILE_BYTES} bytes"
                    )
                if snapshot.size_bytes > _legacy.MAX_PATCH_TOTAL_CONTENT_BYTES - total_content:
                    raise InvalidWorkspaceMutationError(
                        "Patch exact before/after content exceeds total proposal budget"
                    )
                if preimage is None or len(preimage) != snapshot.size_bytes:
                    raise WorkspaceMutationPreimageChangedError(
                        "Patch preimage payload does not match pinned snapshot"
                    )
                _legacy._text(preimage, f"Patch preimage for {target_name}")
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
                    _legacy.PatchFileChange.create(
                        operation=request.operation,
                        target=rendered,
                        expected_preimage=snapshot,
                        preimage=preimage,
                        postimage=request.content,
                    ),
                )
            )

        prepared.sort(key=lambda item: item[0])
        token = _final._SELF_CONTAINED_PARSE.set(True)
        try:
            change_set = _legacy.PatchChangeSet.create(
                workspace_root=str(root),
                changes=[item[1] for item in prepared],
            )
        finally:
            _final._SELF_CONTAINED_PARSE.reset(token)

        _hard._check_preview_budget(change_set.changes)
        _parent_anchor._win_verify_directory_handle(root_handle, root)

        return ActionProposal.create(
            capability=Capability.WRITE_WORKSPACE,
            action=_legacy.PATCH_ACTION,
            workspace_root=str(root),
            parameters=change_set.to_parameters(),
            summary=summary or f"Apply {len(change_set.changes)}-file workspace patch.",
        )
    finally:
        _parent_anchor._win_close_handle(root_handle)


def prepare_patch_proposal(
    *,
    workspace: str | Path,
    changes: Iterable[_legacy.PatchFileRequest],
    summary: str | None = None,
) -> ActionProposal:
    if os.name == "nt":
        return _prepare_windows_patch_proposal(
            workspace=workspace,
            changes=changes,
            summary=summary,
        )
    return _base_prepare_patch_proposal(
        workspace=workspace,
        changes=changes,
        summary=summary,
    )


def install_patch_latest_review_repairs() -> None:
    if getattr(_final, "_M24_LATEST_REVIEW_REPAIRS_INSTALLED", False):
        return

    # Direct public PatchChangeSet construction reaches this helper through
    # _assert_unique_namespace_targets().
    _review._target_namespace_key = _direct_target_namespace_key

    # Seal every ordinary proposal-preparation entrypoint to the latest wrapper.
    _posix_namespace.prepare_patch_proposal = prepare_patch_proposal
    _posix_anchor.prepare_patch_proposal = prepare_patch_proposal
    _port.prepare_patch_proposal = prepare_patch_proposal
    _final.prepare_patch_proposal = prepare_patch_proposal
    _review.prepare_patch_proposal = prepare_patch_proposal
    _hard.prepare_patch_proposal = prepare_patch_proposal
    _legacy.prepare_patch_proposal = prepare_patch_proposal
    _final._M24_LATEST_REVIEW_REPAIRS_INSTALLED = True


install_patch_latest_review_repairs()

parse_patch_proposal = _posix_namespace.parse_patch_proposal
build_patch_approval_preview = _posix_namespace.build_patch_approval_preview

__all__ = [
    "build_patch_approval_preview",
    "parse_patch_proposal",
    "prepare_patch_proposal",
]
