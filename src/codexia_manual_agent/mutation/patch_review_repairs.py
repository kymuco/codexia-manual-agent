from __future__ import annotations

import ctypes
import os
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from ctypes import wintypes
from itertools import islice
from pathlib import Path
from typing import Any

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
)
from codexia_manual_agent.mutation import patch_hardening as _hard
from codexia_manual_agent.mutation import patches as _legacy
from codexia_manual_agent.mutation.hardened_workspace import (
    _is_windows_host,
    preflight_workspace_mutation_target,
)

_CASE_PROBE_SCAN_LIMIT = 64

_FILE_CASE_SENSITIVE_INFO_CLASS = 23
_FILE_CS_FLAG_CASE_SENSITIVE_DIR = 0x00000001
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

_base_prepare_patch_proposal = _hard.prepare_patch_proposal
_base_parse_patch_proposal = _hard.parse_patch_proposal
_base_change_set_post_init = _legacy.PatchChangeSet.__post_init__
build_patch_approval_preview = _hard.build_patch_approval_preview


class _FileCaseSensitiveInfo(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD)]


def _bounded_requests(
    changes: Iterable[_legacy.PatchFileRequest],
) -> tuple[_legacy.PatchFileRequest, ...]:
    try:
        iterator = iter(changes)
    except TypeError as exc:
        raise TypeError("Patch changes must be iterable") from exc
    requests = tuple(islice(iterator, _legacy.MAX_PATCH_FILES + 1))
    if not 1 <= len(requests) <= _legacy.MAX_PATCH_FILES:
        raise InvalidWorkspaceMutationError(
            f"Patch proposal must contain 1..{_legacy.MAX_PATCH_FILES} files"
        )
    return requests


def _case_variant(name: str) -> str | None:
    for index, char in enumerate(name):
        swapped = char.swapcase()
        if swapped != char and len(swapped) == 1:
            return name[:index] + swapped + name[index + 1 :]
    return None


def _stat_identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


def _probe_name_case_sensitivity(parent: Path, name: str) -> bool | None:
    alternate_name = _case_variant(name)
    if alternate_name is None or alternate_name == name:
        return None
    original = parent / name
    alternate = parent / alternate_name
    try:
        original_stat = os.lstat(original)
    except OSError:
        return None
    try:
        alternate_stat = os.lstat(alternate)
    except FileNotFoundError:
        return True
    except OSError:
        return None
    return _stat_identity(original_stat) != _stat_identity(alternate_stat)


def _query_windows_directory_case_sensitive(directory: Path) -> bool | None:
    """Query the per-directory Windows case-sensitive flag without mutating it."""

    if not _is_windows_host():
        return None

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None

    create_file = kernel32.CreateFileW
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

    get_file_information = kernel32.GetFileInformationByHandleEx
    get_file_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    get_file_information.restype = wintypes.BOOL

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(directory),
        0,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        return None

    try:
        info = _FileCaseSensitiveInfo()
        ok = get_file_information(
            handle,
            _FILE_CASE_SENSITIVE_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            return None
        return bool(info.Flags & _FILE_CS_FLAG_CASE_SENSITIVE_DIR)
    finally:
        close_handle(handle)


def _filesystem_case_sensitive(directory: Path) -> bool | None:
    """Infer only this directory namespace's case-sensitivity, read-only."""

    directory = Path(os.path.abspath(str(directory)))
    if _is_windows_host():
        return _query_windows_directory_case_sensitive(directory)

    # Non-Windows evidence must come from the mounted target namespace itself.
    # Never infer an empty mount's semantics from its name in an ancestor mount.
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= _CASE_PROBE_SCAN_LIMIT:
                    break
                result = _probe_name_case_sensitivity(directory, entry.name)
                if result is not None:
                    return result
    except OSError:
        return None
    return None


def _normalized_identity_path(value: str) -> str:
    """Conservatively collapse canonically equivalent Unicode path spellings."""

    return unicodedata.normalize("NFC", value)


def _target_namespace_key(
    target_path: Path,
    *,
    sensitivity_cache: dict[str, bool | None],
) -> str:
    absolute = os.path.normpath(os.path.abspath(str(target_path)))
    parent = os.path.normpath(os.path.abspath(str(target_path.parent)))
    if parent not in sensitivity_cache:
        sensitivity_cache[parent] = _filesystem_case_sensitive(Path(parent))

    # Unicode normalization aliases are independent of case sensitivity. This
    # is deliberately conservative on normalization-sensitive filesystems: a
    # proposal may reject two physically distinct spellings rather than risk
    # admitting one contradictory change set on normalization-insensitive ones.
    normalized = _normalized_identity_path(absolute)
    if sensitivity_cache[parent] is True:
        return normalized

    # Fail closed when the filesystem is case-insensitive or cannot be proven
    # case-sensitive. Normalize again after casefold because casefold itself can
    # introduce combining code points.
    return _normalized_identity_path(normalized.casefold())


def _filesystem_target_identity_key(
    target_path: Path,
    *,
    cache: dict[str, Any],
) -> str:
    return _target_namespace_key(
        target_path,
        sensitivity_cache=cache,
    )


def _assert_unique_namespace_targets(
    root: Path,
    targets: Iterable[str],
    *,
    label: str,
) -> None:
    seen: set[str] = set()
    sensitivity_cache: dict[str, bool | None] = {}
    for target in targets:
        preflight_workspace_mutation_target(target)
        rendered, target_path, _ = _legacy._normalize_target(root, target)
        key = _target_namespace_key(
            target_path,
            sensitivity_cache=sensitivity_cache,
        )
        if key in seen:
            raise InvalidWorkspaceMutationError(f"{label}: {rendered}")
        seen.add(key)


def _assert_canonical_change_set_paths(
    change_set: _legacy.PatchChangeSet,
    root: Path,
) -> None:
    if str(root) != change_set.workspace_root:
        raise WorkspaceMutationBoundaryError(
            "Patch change-set workspace root is not canonical"
        )

    for change in change_set.changes:
        preflight_workspace_mutation_target(change.target)
        rendered, _, _ = _legacy._normalize_target(root, change.target)
        if rendered != change.target:
            raise WorkspaceMutationBoundaryError(
                f"Patch change-set target is not canonical: {change.target}"
            )


def _namespace_hardened_change_set_post_init(self: _legacy.PatchChangeSet) -> None:
    """Seal direct PatchChangeSet construction to canonical namespace invariants."""

    _base_change_set_post_init(self)
    root = _legacy._workspace_root(self.workspace_root)
    _assert_canonical_change_set_paths(self, root)
    _assert_unique_namespace_targets(
        root,
        (change.target for change in self.changes),
        label="Patch changes must have unique namespace targets",
    )


def prepare_patch_proposal(
    *,
    workspace: str | Path,
    changes: Iterable[_legacy.PatchFileRequest],
    summary: str | None = None,
) -> ActionProposal:
    root = _legacy._workspace_root(workspace)
    requests = _bounded_requests(changes)
    if any(not isinstance(request, _legacy.PatchFileRequest) for request in requests):
        raise TypeError("Patch changes must be PatchFileRequest instances")
    _assert_unique_namespace_targets(
        root,
        (request.target for request in requests),
        label="Patch proposal contains duplicate target",
    )
    return _base_prepare_patch_proposal(
        workspace=root,
        changes=requests,
        summary=summary,
    )


def _proposal_alias_targets(proposal: ActionProposal) -> tuple[Path, tuple[str, ...]] | None:
    if not isinstance(proposal, ActionProposal):
        return None
    if proposal.action != _legacy.PATCH_ACTION:
        return None
    try:
        params: Any = proposal.to_dict()["parameters"]
    except (KeyError, TypeError):
        return None
    if not isinstance(params, Mapping):
        return None
    raw_changes = params.get("changes")
    if not isinstance(raw_changes, Sequence) or isinstance(raw_changes, (str, bytes)):
        return None
    if not 1 <= len(raw_changes) <= _legacy.MAX_PATCH_FILES:
        return None

    targets: list[str] = []
    for raw in raw_changes:
        if not isinstance(raw, Mapping):
            return None
        target = raw.get("target")
        if not isinstance(target, str):
            return None
        targets.append(target)

    root = _legacy._workspace_root(proposal.workspace_root)
    return root, tuple(targets)


def parse_patch_proposal(proposal: ActionProposal) -> _legacy.PatchChangeSet:
    alias_inputs = _proposal_alias_targets(proposal)
    if alias_inputs is not None:
        root, targets = alias_inputs
        _assert_unique_namespace_targets(
            root,
            targets,
            label="Patch proposal contains duplicate targets",
        )
    return _base_parse_patch_proposal(proposal)


def install_patch_review_repairs() -> None:
    if getattr(_legacy, "_M24_PATCH_REVIEW_REPAIRS_INSTALLED", False):
        return
    _legacy.PatchChangeSet.__post_init__ = _namespace_hardened_change_set_post_init
    _hard._target_identity_key = _filesystem_target_identity_key
    _hard.prepare_patch_proposal = prepare_patch_proposal
    _hard.parse_patch_proposal = parse_patch_proposal
    _legacy.prepare_patch_proposal = prepare_patch_proposal
    _legacy.parse_patch_proposal = parse_patch_proposal
    _legacy.build_patch_approval_preview = build_patch_approval_preview
    _legacy._M24_PATCH_REVIEW_REPAIRS_INSTALLED = True


install_patch_review_repairs()
