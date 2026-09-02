from __future__ import annotations

import ctypes
import os
import stat
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar
from ctypes import wintypes
from hashlib import sha256
from itertools import islice
from pathlib import Path
from typing import Any

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.domain.sensitive_paths import is_sensitive_relative_path
from codexia_manual_agent.mutation import patch_hardening as _hard
from codexia_manual_agent.mutation import patch_review_repairs as _review
from codexia_manual_agent.mutation import patches as _legacy
from codexia_manual_agent.mutation.hardened_workspace import (
    preflight_workspace_mutation_target,
)
from codexia_manual_agent.mutation.models import (
    MutationOperation,
    PreimageSnapshot,
    PreimageState,
)
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget

_READ_CHUNK_BYTES = 64 * 1024
_PROTECTED_DIRECTORIES = frozenset({".git", ".codexia"})
_PREPARE_ROOT: ContextVar[Path | None] = ContextVar("m24_prepare_root", default=None)
_SELF_CONTAINED_PARSE: ContextVar[bool] = ContextVar(
    "m24_self_contained_parse",
    default=False,
)

_base_prepare_patch_proposal = _review.prepare_patch_proposal
_base_direct_change_set_post_init = _review._namespace_hardened_change_set_post_init
_base_change_set_create = _legacy.PatchChangeSet.create.__func__
build_patch_approval_preview = _hard.build_patch_approval_preview


def _bounded_change_set_entries(
    changes: Iterable[_legacy.PatchFileChange],
) -> tuple[_legacy.PatchFileChange, ...]:
    try:
        iterator = iter(changes)
    except TypeError as exc:
        raise TypeError("Patch changes must be iterable") from exc
    normalized = tuple(islice(iterator, _legacy.MAX_PATCH_FILES + 1))
    if not 1 <= len(normalized) <= _legacy.MAX_PATCH_FILES:
        raise InvalidWorkspaceMutationError(
            f"Patch change set must contain 1..{_legacy.MAX_PATCH_FILES} files"
        )
    return normalized


def _final_change_set_post_init(self: _legacy.PatchChangeSet) -> None:
    normalized = _bounded_change_set_entries(self.changes)
    object.__setattr__(self, "changes", normalized)
    if _SELF_CONTAINED_PARSE.get():
        # Parsing an already-bound proposal must not consult the live workspace.
        # The prior hardening layer still validates tuple shape, ordering, bounds,
        # per-change digests and the change-set digest.
        _review._base_change_set_post_init(self)
        return
    _base_direct_change_set_post_init(self)


def _final_change_set_create(
    cls: type[_legacy.PatchChangeSet],
    *,
    workspace_root: str,
    changes: Iterable[_legacy.PatchFileChange],
) -> _legacy.PatchChangeSet:
    normalized = _bounded_change_set_entries(changes)
    return _base_change_set_create(
        cls,
        workspace_root=workspace_root,
        changes=normalized,
    )


def _read_fd_payload(fd: int, *, max_bytes: int) -> tuple[bytes, str]:
    os.lseek(fd, 0, os.SEEK_SET)
    payload = bytearray()
    digest = sha256()
    while True:
        remaining = max_bytes + 1 - len(payload)
        if remaining <= 0:
            break
        chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        payload.extend(chunk)
        digest.update(chunk)
    if len(payload) > max_bytes:
        raise InvalidWorkspaceMutationError(
            f"Patch preimage exceeds bounded read budget ({len(payload)} > {max_bytes})"
        )
    return bytes(payload), digest.hexdigest()


def _hash_fd_again(fd: int, *, max_bytes: int) -> tuple[int, str]:
    os.lseek(fd, 0, os.SEEK_SET)
    total = 0
    digest = sha256()
    while True:
        remaining = max_bytes + 1 - total
        if remaining <= 0:
            break
        chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        total += len(chunk)
        digest.update(chunk)
    if total > max_bytes:
        raise InvalidWorkspaceMutationError(
            f"Patch preimage exceeds bounded read budget ({total} > {max_bytes})"
        )
    return total, digest.hexdigest()


def _same_file_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and getattr(left, "st_ctime_ns", None) == getattr(right, "st_ctime_ns", None)
    )


def _open_posix_directory_chain(path: Path) -> int:
    """Open an absolute directory without following any path-component symlink."""

    if not path.is_absolute():
        raise WorkspaceMutationBoundaryError("Patch parent path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path.anchor or os.sep, flags)
    except OSError as exc:
        raise WorkspaceMutationBoundaryError("Patch workspace root cannot be pinned") from exc
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceMutationBoundaryError("Patch target parent is not a directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _verify_posix_parent_still_current(parent: Path, pinned: os.stat_result) -> None:
    try:
        current_fd = _open_posix_directory_chain(parent)
    except WorkspaceMutationBoundaryError as exc:
        raise WorkspaceMutationPreimageChangedError(
            "Patch target parent changed while preimage bytes were being captured"
        ) from exc
    try:
        current_parent = os.fstat(current_fd)
    finally:
        os.close(current_fd)
    if pinned.st_dev != current_parent.st_dev or pinned.st_ino != current_parent.st_ino:
        raise WorkspaceMutationPreimageChangedError(
            "Patch target parent identity changed during preimage capture"
        )


def _capture_posix_exact_preimage(
    *,
    root: Path,
    parent: Path,
    target_name: str,
    max_bytes: int,
) -> tuple[PreimageSnapshot, bytes | None]:
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise WorkspaceMutationBoundaryError("Patch target parent escapes workspace") from exc

    dir_fd = _open_posix_directory_chain(parent)
    try:
        pinned_parent = os.fstat(dir_fd)
        try:
            before = os.stat(target_name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            _verify_posix_parent_still_current(parent, pinned_parent)
            return PreimageSnapshot.absent(), None
        except OSError as exc:
            raise InvalidWorkspaceMutationError("Cannot stat patch preimage") from exc

        if stat.S_ISLNK(before.st_mode):
            raise WorkspaceMutationBoundaryError("Patch target must not be a symlink")
        if not stat.S_ISREG(before.st_mode):
            raise InvalidWorkspaceMutationError("Patch target must be a regular file")
        if before.st_size > max_bytes:
            raise InvalidWorkspaceMutationError(
                f"Patch preimage exceeds {max_bytes} bytes"
            )

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target_name, flags, dir_fd=dir_fd)
        except OSError as exc:
            raise InvalidWorkspaceMutationError("Cannot safely open patch preimage") from exc
        try:
            opened = os.fstat(fd)
            if not _same_file_metadata(before, opened):
                raise WorkspaceMutationPreimageChangedError(
                    "Patch target changed while its preimage was being opened"
                )
            payload, digest = _read_fd_payload(fd, max_bytes=max_bytes)
            after = os.fstat(fd)
            second_size, second_digest = _hash_fd_again(fd, max_bytes=max_bytes)
        finally:
            os.close(fd)

        try:
            entry_after = os.stat(target_name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise WorkspaceMutationPreimageChangedError(
                "Patch target disappeared while its preimage was being captured"
            ) from exc
        except OSError as exc:
            raise InvalidWorkspaceMutationError("Cannot re-stat patch preimage") from exc

        if (
            not _same_file_metadata(opened, after)
            or after.st_dev != entry_after.st_dev
            or after.st_ino != entry_after.st_ino
            or after.st_size != entry_after.st_size
            or len(payload) != after.st_size
            or second_size != after.st_size
            or digest != second_digest
        ):
            raise WorkspaceMutationPreimageChangedError(
                "Patch target changed while exact preimage bytes were being captured"
            )

        _verify_posix_parent_still_current(parent, pinned_parent)

        return (
            PreimageSnapshot.present(
                size_bytes=after.st_size,
                digest=digest,
                mode=stat.S_IMODE(after.st_mode),
            ),
            payload,
        )
    finally:
        os.close(dir_fd)


class _WinFileInfo(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _win_file_info(handle: int) -> _WinFileInfo:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_WinFileInfo)]
    get_info.restype = wintypes.BOOL
    info = _WinFileInfo()
    if not get_info(wintypes.HANDLE(handle), ctypes.byref(info)):
        raise InvalidWorkspaceMutationError(
            f"Cannot inspect patch preimage handle (winerror={ctypes.get_last_error()})"
        )
    return info


def _win_info_identity(info: _WinFileInfo) -> tuple[int, int, int, int, int]:
    size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
    index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
    write_time = (int(info.ftLastWriteTime.dwHighDateTime) << 32) | int(
        info.ftLastWriteTime.dwLowDateTime
    )
    return (
        int(info.dwVolumeSerialNumber),
        index,
        size,
        write_time,
        int(info.dwFileAttributes),
    )


def _capture_windows_exact_path(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[PreimageSnapshot, bytes | None]:
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

    handle = create_file(
        str(path),
        GENERIC_READ,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = ctypes.cast(handle, ctypes.c_void_p).value
    invalid = ctypes.c_void_p(-1).value
    if value is None or value == invalid:
        error = ctypes.get_last_error()
        if error == 2:  # ERROR_FILE_NOT_FOUND
            return PreimageSnapshot.absent(), None
        raise WorkspaceMutationBoundaryError(
            f"Patch preimage cannot be safely opened (winerror={error})"
        )

    raw_handle = int(value)
    try:
        before = _win_file_info(raw_handle)
        if before.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise WorkspaceMutationBoundaryError(
                "Patch target must not be a reparse point"
            )
        if before.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY:
            raise InvalidWorkspaceMutationError("Patch target must be a regular file")
        before_identity = _win_info_identity(before)
        if before_identity[2] > max_bytes:
            raise InvalidWorkspaceMutationError(
                f"Patch preimage exceeds {max_bytes} bytes"
            )

        fd = msvcrt.open_osfhandle(
            raw_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        raw_handle = -1
        try:
            payload, digest = _read_fd_payload(fd, max_bytes=max_bytes)
            after_stat = os.fstat(fd)
            second_size, second_digest = _hash_fd_again(fd, max_bytes=max_bytes)
            after_handle = int(msvcrt.get_osfhandle(fd))
            after_identity = _win_info_identity(_win_file_info(after_handle))
        finally:
            os.close(fd)

        if (
            before_identity != after_identity
            or len(payload) != before_identity[2]
            or second_size != before_identity[2]
            or after_stat.st_size != before_identity[2]
            or digest != second_digest
        ):
            raise WorkspaceMutationPreimageChangedError(
                "Patch target changed while exact preimage bytes were being captured"
            )
        return (
            PreimageSnapshot.present(
                size_bytes=before_identity[2],
                digest=digest,
                mode=stat.S_IMODE(after_stat.st_mode),
            ),
            payload,
        )
    finally:
        if raw_handle >= 0:
            kernel32.CloseHandle(wintypes.HANDLE(raw_handle))


def _capture_exact_preimage_pinned(
    path: Path,
    *,
    remaining_total_bytes: int,
) -> tuple[PreimageSnapshot, bytes | None]:
    root = _PREPARE_ROOT.get()
    if root is None:
        raise WorkspaceMutationBoundaryError(
            "Patch preimage capture requires a pinned workspace context"
        )
    parent = path.parent
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise WorkspaceMutationBoundaryError("Patch target parent escapes workspace") from exc

    if os.name == "nt":
        with PinnedMutationTarget(
            root=root,
            parent=parent,
            target_name=path.name,
        ) as pinned:
            snapshot, payload = _capture_windows_exact_path(
                pinned.target_path,
                max_bytes=_legacy.MAX_PATCH_FILE_BYTES,
            )
            pinned.verify_parent_identity()
    else:
        snapshot, payload = _capture_posix_exact_preimage(
            root=root,
            parent=parent,
            target_name=path.name,
            max_bytes=_legacy.MAX_PATCH_FILE_BYTES,
        )

    if snapshot.state is PreimageState.PRESENT:
        if snapshot.size_bytes is None or snapshot.size_bytes > _legacy.MAX_PATCH_FILE_BYTES:
            raise InvalidWorkspaceMutationError(
                f"Patch preimage exceeds {_legacy.MAX_PATCH_FILE_BYTES} bytes"
            )
        if snapshot.size_bytes > remaining_total_bytes:
            raise InvalidWorkspaceMutationError(
                "Patch exact before/after content exceeds total proposal budget"
            )
        if payload is None or len(payload) != snapshot.size_bytes:
            raise WorkspaceMutationPreimageChangedError(
                "Patch preimage payload does not match pinned snapshot"
            )
        _legacy._text(payload, f"Patch preimage for {path.name}")
    elif payload is not None:
        raise InvalidWorkspaceMutationError("Absent patch preimage must not carry bytes")
    return snapshot, payload


def _lexical_workspace_root(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise InvalidWorkspaceMutationError("Patch workspace_root must be non-empty text")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise WorkspaceMutationBoundaryError("Patch proposal workspace root must be absolute")
    canonical = os.path.normpath(os.path.abspath(value))
    if canonical != value:
        raise WorkspaceMutationBoundaryError(
            "Patch proposal workspace root is not lexically canonical"
        )
    return Path(canonical)


def _lexical_target(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidWorkspaceMutationError("Patch target must be non-empty text")
    preflight_workspace_mutation_target(value)
    supplied = Path(value)
    if supplied.is_absolute():
        raise WorkspaceMutationBoundaryError("Patch target must be workspace-relative")
    if not supplied.parts or supplied == Path("."):
        raise WorkspaceMutationBoundaryError("Patch target must name a file")
    if any(part in {"", ".", ".."} for part in supplied.parts):
        raise WorkspaceMutationBoundaryError("Patch target contains invalid path traversal")

    normalized = Path(*supplied.parts)
    rendered = normalized.as_posix()
    if rendered != value:
        raise WorkspaceMutationBoundaryError("Patch target is not canonical")
    folded_parts = tuple(part.casefold() for part in normalized.parts)
    if any(part in _PROTECTED_DIRECTORIES for part in folded_parts):
        raise WorkspaceMutationBoundaryError(
            "Patch targets inside .git or .codexia are not allowed"
        )
    if is_sensitive_relative_path(rendered):
        raise WorkspaceMutationBoundaryError(
            f"Sensitive target is excluded from workspace mutation: {rendered}"
        )
    return rendered


def prepare_patch_proposal(
    *,
    workspace: str | Path,
    changes: Iterable[_legacy.PatchFileRequest],
    summary: str | None = None,
) -> ActionProposal:
    root = _legacy._workspace_root(workspace)
    token = _PREPARE_ROOT.set(root)
    try:
        return _base_prepare_patch_proposal(
            workspace=root,
            changes=changes,
            summary=summary,
        )
    finally:
        _PREPARE_ROOT.reset(token)


def parse_patch_proposal(proposal: ActionProposal) -> _legacy.PatchChangeSet:
    """Parse a bound proposal without consulting mutable live target parents."""

    if not isinstance(proposal, ActionProposal):
        raise TypeError("proposal must be an ActionProposal")
    if proposal.capability is not Capability.WRITE_WORKSPACE:
        raise InvalidWorkspaceMutationError(
            "Patch proposal requires write_workspace capability"
        )
    if proposal.action != _legacy.PATCH_ACTION:
        raise InvalidWorkspaceMutationError(
            "Action proposal is not an M2.4 patch proposal"
        )

    params = proposal.to_dict()["parameters"]
    if set(params) != {"schema_version", "change_set_digest", "changes"}:
        raise InvalidWorkspaceMutationError("Patch proposal parameter schema is invalid")
    if params["schema_version"] != _legacy.PATCH_SCHEMA_VERSION:
        raise InvalidWorkspaceMutationError("Unsupported patch proposal schema version")
    _legacy._require_digest(params["change_set_digest"], "Patch change-set digest")
    if not isinstance(params["changes"], Sequence) or isinstance(
        params["changes"], (str, bytes)
    ):
        raise InvalidWorkspaceMutationError("Patch changes must be a sequence")
    if not 1 <= len(params["changes"]) <= _legacy.MAX_PATCH_FILES:
        raise InvalidWorkspaceMutationError(
            f"Patch proposal must contain 1..{_legacy.MAX_PATCH_FILES} files"
        )

    root = _lexical_workspace_root(proposal.workspace_root)
    parsed: list[_legacy.PatchFileChange] = []
    seen_normalized: set[str] = set()
    total_content = 0
    for raw in params["changes"]:
        if not isinstance(raw, Mapping):
            raise InvalidWorkspaceMutationError("Patch change entry must be an object")
        if set(raw) != {
            "operation",
            "target",
            "expected_preimage",
            "preimage_data_base64",
            "postimage",
            "change_digest",
        }:
            raise InvalidWorkspaceMutationError("Patch change schema is invalid")
        try:
            operation = MutationOperation(raw["operation"])
        except (TypeError, ValueError) as exc:
            raise InvalidWorkspaceMutationError("Patch operation is invalid") from exc
        rendered = _lexical_target(raw["target"])
        normalized_key = unicodedata.normalize("NFC", rendered)
        if normalized_key in seen_normalized:
            raise InvalidWorkspaceMutationError("Patch proposal contains duplicate targets")
        seen_normalized.add(normalized_key)

        snapshot = _legacy._parse_preimage(raw["expected_preimage"])
        preimage_size = 0
        if snapshot.state is PreimageState.PRESENT:
            if snapshot.size_bytes is None or snapshot.size_bytes > _legacy.MAX_PATCH_FILE_BYTES:
                raise InvalidWorkspaceMutationError(
                    f"Patch preimage exceeds {_legacy.MAX_PATCH_FILE_BYTES} bytes"
                )
            preimage_size = snapshot.size_bytes
        postimage_size = _hard._postimage_declared_size(raw["postimage"])
        projected_total = total_content + preimage_size + postimage_size
        if projected_total > _legacy.MAX_PATCH_TOTAL_CONTENT_BYTES:
            raise InvalidWorkspaceMutationError(
                "Patch exact before/after content exceeds total proposal budget "
                f"({projected_total} > {_legacy.MAX_PATCH_TOTAL_CONTENT_BYTES})"
            )

        preimage = _hard._decode_preimage(snapshot, raw["preimage_data_base64"])
        postimage, _ = _hard._decode_postimage_bounded(raw["postimage"])
        total_content = projected_total
        _legacy._text(postimage, f"Patch postimage for {rendered}")
        if preimage is not None:
            _legacy._text(preimage, f"Patch preimage for {rendered}")
        if operation is MutationOperation.CREATE and snapshot.state is not PreimageState.ABSENT:
            raise InvalidWorkspaceMutationError("Create patch must bind an absent preimage")
        if operation is MutationOperation.REPLACE and snapshot.state is not PreimageState.PRESENT:
            raise InvalidWorkspaceMutationError("Replace patch must bind a present preimage")
        if operation is MutationOperation.REPLACE and preimage == postimage:
            raise InvalidWorkspaceMutationError("Replace patch cannot be a no-op")
        parsed.append(
            _legacy.PatchFileChange(
                operation=operation,
                target=rendered,
                expected_preimage=snapshot,
                preimage=preimage,
                postimage=postimage,
                change_digest=raw["change_digest"],
            )
        )

    token = _SELF_CONTAINED_PARSE.set(True)
    try:
        change_set = _legacy.PatchChangeSet(
            workspace_root=str(root),
            changes=tuple(parsed),
            change_set_digest=params["change_set_digest"],
        )
    finally:
        _SELF_CONTAINED_PARSE.reset(token)
    _hard._check_preview_budget(change_set.changes)
    return change_set


def install_patch_final_review_repairs() -> None:
    if getattr(_legacy, "_M24_PATCH_FINAL_REVIEW_REPAIRS_INSTALLED", False):
        return

    # Safe exact-preimage capture is reached by the already-hardened base prepare
    # implementation while the public wrapper supplies the canonical root context.
    _hard._capture_exact_preimage = _capture_exact_preimage_pinned

    # Bound both direct construction and the factory before any tuple conversion.
    _legacy.PatchChangeSet.__post_init__ = _final_change_set_post_init
    _legacy.PatchChangeSet.create = classmethod(_final_change_set_create)

    # Seal every ordinary entrypoint to the final prepare/parse semantics.
    _review.prepare_patch_proposal = prepare_patch_proposal
    _review.parse_patch_proposal = parse_patch_proposal
    _review.build_patch_approval_preview = build_patch_approval_preview
    _hard.prepare_patch_proposal = prepare_patch_proposal
    _hard.parse_patch_proposal = parse_patch_proposal
    _legacy.prepare_patch_proposal = prepare_patch_proposal
    _legacy.parse_patch_proposal = parse_patch_proposal
    _legacy.build_patch_approval_preview = build_patch_approval_preview
    _legacy._M24_PATCH_FINAL_REVIEW_REPAIRS_INSTALLED = True


install_patch_final_review_repairs()
