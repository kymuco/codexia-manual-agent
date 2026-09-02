from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import (
    InvalidActionTransitionError,
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation.bounded_io import hash_bounded_stream
from codexia_manual_agent.mutation.models import (
    MutationOperation,
    MutationTerminationReason,
    PreimageSnapshot,
    PreimageState,
    WorkspaceMutationObservation,
)
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget, _StagedFile
from codexia_manual_agent.mutation.workspace import (
    _MAX_PREIMAGE_BYTES,
    _PendingOutcome,
    _append_error,
    _failure_reason,
    _inspection_failure,
    _pending_inspection_failure,
    _preimage_reason,
    _record,
    _staging_mode,
    _validate_proposal,
)


@dataclass(slots=True)
class _PinnedWindowsReplaceTarget:
    fd: int
    snapshot: PreimageSnapshot

    def close(self) -> None:
        if self.fd < 0:
            return
        os.close(self.fd)
        self.fd = -1


def _win_pin_exact_replace_target(
    path: Path,
    *,
    max_bytes: int,
) -> _PinnedWindowsReplaceTarget | None:
    """Open the current Windows replace target exclusively and bind its exact preimage.

    The zero share mode is deliberate: once this succeeds, no pre-existing handle
    with read/write/delete access can coexist and no new such handle can appear.
    The held handle therefore keeps the exact destination present and immutable
    until the FileRenameInfoEx commit completes.
    """

    if os.name != "nt":
        raise WorkspaceMutationBoundaryError(
            "Strict replace target pinning is available only on Windows"
        )

    import ctypes
    import msvcrt
    from ctypes import wintypes

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
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    invalid = ctypes.c_void_p(-1).value

    handle = create_file(
        str(path),
        GENERIC_READ,
        0,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = ctypes.cast(handle, ctypes.c_void_p).value
    if value is None or value == invalid:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            return None
        raise WorkspaceMutationBoundaryError(
            f"Replace target cannot be exclusively pinned (winerror={error})"
        )

    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        fd = msvcrt.open_osfhandle(int(value), flags)
    except BaseException:
        from codexia_manual_agent.mutation.parent_anchor import _win_close_handle

        _win_close_handle(int(value))
        raise

    try:
        attributes = _win_file_attributes(fd)
        FILE_ATTRIBUTE_DIRECTORY = 0x00000010
        FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
        if attributes & FILE_ATTRIBUTE_DIRECTORY:
            raise WorkspaceMutationBoundaryError(
                "Replace target must remain a regular file at commit"
            )
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise WorkspaceMutationBoundaryError(
                "Replace target must not become a reparse point at commit"
            )

        before = os.fstat(fd)
        if before.st_size > max_bytes:
            raise InvalidWorkspaceMutationError(
                f"Mutation preimage exceeds hashing budget ({before.st_size} > {max_bytes})"
            )
        duplicate = os.dup(fd)
        try:
            os.lseek(duplicate, 0, os.SEEK_SET)
            with os.fdopen(duplicate, "rb", closefd=True) as stream:
                size, digest = hash_bounded_stream(
                    stream,
                    max_bytes=max_bytes,
                    label="Pinned replace preimage",
                )
                duplicate = -1
        finally:
            if duplicate >= 0:
                os.close(duplicate)
        after = os.fstat(fd)
        if (
            size != before.st_size
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or getattr(before, "st_ino", 0) != getattr(after, "st_ino", 0)
        ):
            raise WorkspaceMutationPreimageChangedError(
                "Replace target changed while its exclusive commit handle was being pinned"
            )
        snapshot = PreimageSnapshot.present(
            size_bytes=after.st_size,
            digest=digest,
            mode=stat.S_IMODE(after.st_mode),
        )
        return _PinnedWindowsReplaceTarget(fd=fd, snapshot=snapshot)
    except BaseException:
        os.close(fd)
        raise


def _win_file_attributes(fd: int) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
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

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    get_info.restype = wintypes.BOOL
    info = _BY_HANDLE_FILE_INFORMATION()
    handle = msvcrt.get_osfhandle(fd)
    if not get_info(wintypes.HANDLE(handle), ctypes.byref(info)):
        error = ctypes.get_last_error()
        raise WorkspaceMutationBoundaryError(
            f"Pinned replace target cannot be inspected (winerror={error})"
        )
    return int(info.dwFileAttributes)


def _win_strict_replace_staged_fd(fd: int, target: Path) -> None:
    """Atomically replace an existing pinned Windows target from the held staged fd.

    FileRenameInfoEx with REPLACE_IF_EXISTS | POSIX_SEMANTICS is required here:
    the destination is deliberately held open with zero sharing so it cannot be
    deleted or changed after exact-preimage verification, while POSIX semantics
    permits the rename to replace that still-open destination object.
    """

    if os.name != "nt":
        raise WorkspaceMutationBoundaryError(
            "Strict replace commit is available only on Windows"
        )

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FILE_RENAME_INFO_EX(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    FILE_RENAME_REPLACE_IF_EXISTS = 0x00000001
    FILE_RENAME_POSIX_SEMANTICS = 0x00000002
    FILE_RENAME_INFO_EX_CLASS = 22

    encoded = str(target).encode("utf-16-le")
    offset = _FILE_RENAME_INFO_EX.FileName.offset
    buffer = ctypes.create_string_buffer(offset + len(encoded))
    info = _FILE_RENAME_INFO_EX.from_buffer(buffer)
    info.Flags = FILE_RENAME_REPLACE_IF_EXISTS | FILE_RENAME_POSIX_SEMANTICS
    info.RootDirectory = None
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    if not set_info(
        wintypes.HANDLE(handle),
        FILE_RENAME_INFO_EX_CLASS,
        buffer,
        len(buffer),
    ):
        error = ctypes.get_last_error()
        raise WorkspaceMutationBoundaryError(
            f"Strict Windows replace commit failed closed (winerror={error})"
        )


class WorkspaceMutationExecutor:
    """M2.3 secure executor: Windows-only commit backend with strict replace."""

    def execute(
        self,
        lifecycle: ActionLifecycle,
        *,
        authority: LocalApprovalAuthority,
    ) -> WorkspaceMutationObservation:
        if lifecycle.phase is not ActionPhase.AUTHORIZED:
            raise InvalidActionTransitionError("Workspace mutation requires AUTHORIZED lifecycle")
        if lifecycle.authorization is None:
            raise InvalidActionTransitionError("Authorized workspace mutation has no receipt")

        plan = _validate_proposal(lifecycle.proposal)
        receipt = lifecycle.authorization

        # Fifth-review P1 governance: the held-dirfd Linux implementation prevents
        # symlink redirection but cannot stop relocation of the held directory inode
        # outside the workspace between validation and commit. Until a constrained
        # Linux backend exists, execution is intentionally unavailable and the
        # one-shot receipt remains unconsumed.
        if os.name != "nt":
            raise WorkspaceMutationBoundaryError(
                "M2.3 workspace mutation execution is currently supported only on Windows; "
                "Linux execution is fail-closed pending a constrained commit backend"
            )

        with PinnedMutationTarget(
            root=plan.root,
            parent=plan.parent,
            target_name=plan.target_path.name,
        ) as pinned:
            before_consume = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
            if before_consume != plan.expected_preimage:
                raise WorkspaceMutationPreimageChangedError(
                    "Workspace mutation preimage changed before authorization consumption"
                )

            mutation_id = __import__("uuid").uuid4().hex
            lifecycle.consume_authorization(authority=authority)
            lifecycle.record_executed(mutation_id)

            try:
                observed = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
            except (
                InvalidWorkspaceMutationError,
                WorkspaceMutationBoundaryError,
                WorkspaceMutationPreimageChangedError,
            ) as exc:
                return _inspection_failure(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    receipt=receipt,
                    exc=exc,
                )
            if observed != plan.expected_preimage:
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=False,
                    reason=_preimage_reason(plan.operation, plan.expected_preimage, observed),
                    receipt=receipt,
                )

            staged: _StagedFile | None = None
            replace_target: _PinnedWindowsReplaceTarget | None = None
            committed = False
            cleanup_error: str | None = None
            pending: _PendingOutcome | None = None

            try:
                staged = pinned.write_temp(plan.postimage, mode=_staging_mode(plan))

                try:
                    observed = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
                except (
                    InvalidWorkspaceMutationError,
                    WorkspaceMutationBoundaryError,
                    WorkspaceMutationPreimageChangedError,
                ) as exc:
                    pending = _pending_inspection_failure(plan, exc)
                else:
                    if observed != plan.expected_preimage:
                        pending = _PendingOutcome(
                            observed_preimage=observed,
                            reason=_preimage_reason(
                                plan.operation,
                                plan.expected_preimage,
                                observed,
                            ),
                        )

                if pending is None:
                    if plan.operation is MutationOperation.CREATE:
                        try:
                            pinned.commit_create(staged)
                        except FileExistsError:
                            try:
                                observed = pinned.capture_preimage(
                                    max_bytes=_MAX_PREIMAGE_BYTES
                                )
                            except (
                                InvalidWorkspaceMutationError,
                                WorkspaceMutationBoundaryError,
                                WorkspaceMutationPreimageChangedError,
                            ) as exc:
                                pending = _pending_inspection_failure(plan, exc)
                            else:
                                pending = _PendingOutcome(
                                    observed_preimage=observed,
                                    reason=MutationTerminationReason.TARGET_APPEARED,
                                )
                        else:
                            committed = True
                    else:
                        replace_target = _win_pin_exact_replace_target(
                            plan.target_path,
                            max_bytes=_MAX_PREIMAGE_BYTES,
                        )
                        if replace_target is None:
                            observed = PreimageSnapshot.absent()
                            pending = _PendingOutcome(
                                observed_preimage=observed,
                                reason=MutationTerminationReason.TARGET_DISAPPEARED,
                            )
                        elif replace_target.snapshot != plan.expected_preimage:
                            observed = replace_target.snapshot
                            pending = _PendingOutcome(
                                observed_preimage=observed,
                                reason=_preimage_reason(
                                    plan.operation,
                                    plan.expected_preimage,
                                    observed,
                                ),
                            )
                        else:
                            pinned.verify_parent_identity()
                            pinned.verify_staged_identity(staged)
                            _win_strict_replace_staged_fd(staged.fd, plan.target_path)
                            staged.token = None
                            committed = True

                if committed:
                    try:
                        pinned.close_staged(staged)
                    except OSError as exc:
                        cleanup_error = _append_error(
                            cleanup_error,
                            "staging handle cleanup failed after commit: "
                            f"{type(exc).__name__}: {exc}",
                        )
                    finally:
                        staged = None
                    pinned.fsync_parent()
            except (
                InvalidWorkspaceMutationError,
                WorkspaceMutationBoundaryError,
                WorkspaceMutationPreimageChangedError,
                OSError,
            ) as exc:
                if committed:
                    cleanup_error = _append_error(
                        cleanup_error,
                        f"post-commit housekeeping failed: {type(exc).__name__}: {exc}",
                    )
                elif pending is None:
                    pending = _PendingOutcome(
                        observed_preimage=observed,
                        reason=_failure_reason(exc),
                        error=f"{type(exc).__name__}: {exc}",
                    )
            finally:
                if replace_target is not None:
                    try:
                        replace_target.close()
                    except OSError as exc:
                        cleanup_error = _append_error(
                            cleanup_error,
                            f"replace target pin cleanup failed: {type(exc).__name__}: {exc}",
                        )
                    finally:
                        replace_target = None
                if staged is not None:
                    try:
                        if committed:
                            pinned.close_staged(staged)
                        else:
                            pinned.discard_staged(staged)
                    except OSError as exc:
                        cleanup_error = _append_error(
                            cleanup_error,
                            f"staging cleanup failed: {type(exc).__name__}: {exc}",
                        )
                    finally:
                        staged = None

            if pending is not None:
                error = pending.error
                if cleanup_error:
                    error = _append_error(error, cleanup_error)
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=pending.observed_preimage,
                    applied=False,
                    reason=pending.reason,
                    receipt=receipt,
                    error=error,
                )

            if not committed:
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=False,
                    reason=MutationTerminationReason.WRITE_ERROR,
                    receipt=receipt,
                    error=_append_error(
                        cleanup_error,
                        "Mutation ended without a commit or an explicit abort outcome",
                    ),
                )

            try:
                post = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
            except (
                InvalidWorkspaceMutationError,
                WorkspaceMutationBoundaryError,
                WorkspaceMutationPreimageChangedError,
            ) as exc:
                error = str(exc)
                if cleanup_error:
                    error = _append_error(cleanup_error, error)
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=True,
                    reason=MutationTerminationReason.POSTIMAGE_MISMATCH,
                    receipt=receipt,
                    error=error,
                )
            if (
                post.state is not PreimageState.PRESENT
                or post.size_bytes != len(plan.postimage)
                or post.sha256 != plan.postimage_sha256
            ):
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=True,
                    reason=MutationTerminationReason.POSTIMAGE_MISMATCH,
                    receipt=receipt,
                    postimage_size_bytes=post.size_bytes,
                    postimage_sha256=post.sha256,
                    error=cleanup_error,
                )
            return _record(
                lifecycle,
                mutation_id=mutation_id,
                plan=plan,
                observed_preimage=observed,
                applied=True,
                reason=MutationTerminationReason.APPLIED,
                receipt=receipt,
                postimage_size_bytes=post.size_bytes,
                postimage_sha256=post.sha256,
                error=cleanup_error,
            )
