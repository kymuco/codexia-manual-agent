from __future__ import annotations

import ctypes
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation.bounded_io import hash_bounded_stream
from codexia_manual_agent.mutation.models import PreimageSnapshot
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget, _StagedFile, _write_all


_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_WRITE_DAC = 0x00040000
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_CREATE_NEW = 1
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008
_DRIVE_REMOTE = 4
_FILE_READ_ONLY_VOLUME = 0x00080000
_FILE_SUPPORTS_TRANSACTIONS = 0x00200000


@dataclass(slots=True)
class PinnedTxFReplaceTarget:
    fd: int
    snapshot: PreimageSnapshot

    def close(self) -> None:
        if self.fd < 0:
            return
        os.close(self.fd)
        self.fd = -1


@dataclass(slots=True)
class WindowsTxFTransaction:
    handle: int
    finished: bool = False

    def commit(self) -> None:
        if self.finished:
            raise WorkspaceMutationBoundaryError("Windows TxF transaction is already finished")
        ktm = ctypes.WinDLL("KtmW32.dll", use_last_error=True)
        commit = ktm.CommitTransaction
        from ctypes import wintypes

        commit.argtypes = [wintypes.HANDLE]
        commit.restype = wintypes.BOOL
        if not commit(wintypes.HANDLE(self.handle)):
            error = ctypes.get_last_error()
            raise OSError(error, f"Windows TxF commit failed (winerror={error})")
        self.finished = True

    def rollback(self) -> None:
        if self.finished:
            return
        ktm = ctypes.WinDLL("KtmW32.dll", use_last_error=True)
        rollback = ktm.RollbackTransaction
        from ctypes import wintypes

        rollback.argtypes = [wintypes.HANDLE]
        rollback.restype = wintypes.BOOL
        if not rollback(wintypes.HANDLE(self.handle)):
            error = ctypes.get_last_error()
            raise OSError(error, f"Windows TxF rollback failed (winerror={error})")
        self.finished = True

    def close(self) -> None:
        if not self.handle:
            return
        handle = self.handle
        _close_transaction_handle(handle)
        self.handle = 0


def _raw_handle_value(handle) -> int:
    value = ctypes.cast(handle, ctypes.c_void_p).value
    return int(value or 0)


def _close_transaction_handle(handle: int) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        error = ctypes.get_last_error()
        raise OSError(error, f"Windows TxF transaction handle cleanup failed (winerror={error})")


def _close_raw_handle(handle: int) -> None:
    if not handle:
        return
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    close_handle(wintypes.HANDLE(handle))


def _require_windows() -> None:
    if os.name != "nt":
        raise WorkspaceMutationBoundaryError("Windows TxF replace is available only on Windows")


def _require_txf_api_surface() -> None:
    _require_windows()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ktm = ctypes.WinDLL("KtmW32.dll", use_last_error=True)
        getattr(kernel32, "CreateFileTransactedW")
        getattr(kernel32, "MoveFileTransactedW")
        getattr(ktm, "CreateTransaction")
        getattr(ktm, "CommitTransaction")
        getattr(ktm, "RollbackTransaction")
    except (AttributeError, OSError) as exc:
        raise WorkspaceMutationBoundaryError(
            "M2.3 strict replace requires the Windows TxF API surface; authorization was not consumed"
        ) from exc


def _volume_capabilities(path: Path) -> tuple[str, int]:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_volume_path = kernel32.GetVolumePathNameW
    get_volume_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_volume_path.restype = wintypes.BOOL
    root = ctypes.create_unicode_buffer(32768)
    if not get_volume_path(str(path), root, len(root)):
        error = ctypes.get_last_error()
        raise WorkspaceMutationBoundaryError(
            f"Windows TxF volume root cannot be resolved (winerror={error})"
        )

    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = [wintypes.LPCWSTR]
    get_drive_type.restype = wintypes.UINT
    if int(get_drive_type(root.value)) == _DRIVE_REMOTE:
        raise WorkspaceMutationBoundaryError(
            "M2.3 strict replace requires a local NTFS volume; network volumes are unsupported"
        )

    get_volume_info = kernel32.GetVolumeInformationW
    get_volume_info.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    get_volume_info.restype = wintypes.BOOL
    filesystem = ctypes.create_unicode_buffer(64)
    flags = wintypes.DWORD()
    if not get_volume_info(
        root.value,
        None,
        0,
        None,
        None,
        ctypes.byref(flags),
        filesystem,
        len(filesystem),
    ):
        error = ctypes.get_last_error()
        raise WorkspaceMutationBoundaryError(
            f"Windows TxF filesystem cannot be inspected (winerror={error})"
        )
    return filesystem.value, int(flags.value)


def require_windows_txf_support(path: Path) -> str:
    """Fail closed before authorization if strict TxF replacement is unavailable."""

    _require_txf_api_surface()
    filesystem, flags = _volume_capabilities(path)
    if filesystem.casefold() != "ntfs":
        raise WorkspaceMutationBoundaryError(
            "M2.3 strict replace requires local NTFS TxF support; "
            f"detected filesystem {filesystem or '<unknown>'}. Authorization was not consumed."
        )
    if flags & _FILE_READ_ONLY_VOLUME:
        raise WorkspaceMutationBoundaryError(
            "M2.3 strict replace requires a writable NTFS volume; "
            "FILE_READ_ONLY_VOLUME is set. Authorization was not consumed."
        )
    if not flags & _FILE_SUPPORTS_TRANSACTIONS:
        raise WorkspaceMutationBoundaryError(
            "M2.3 strict replace requires FILE_SUPPORTS_TRANSACTIONS on the target volume; "
            "authorization was not consumed."
        )

    transaction = create_transaction()
    try:
        transaction.rollback()
    finally:
        transaction.close()
    return filesystem


def create_transaction() -> WindowsTxFTransaction:
    _require_txf_api_surface()
    from ctypes import wintypes

    ktm = ctypes.WinDLL("KtmW32.dll", use_last_error=True)
    create = ktm.CreateTransaction
    create.argtypes = [
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
    ]
    create.restype = wintypes.HANDLE
    handle = create(None, None, 0, 0, 0, 0, "Codexia M2.3 strict workspace replace")
    value = _raw_handle_value(handle)
    invalid = ctypes.c_void_p(-1).value
    if not value or value == invalid:
        error = ctypes.get_last_error()
        raise WorkspaceMutationBoundaryError(
            f"Windows TxF transaction cannot be created (winerror={error})"
        )
    return WindowsTxFTransaction(handle=value)


def _file_attributes(fd: int) -> int:
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
            f"Pinned TxF replace target cannot be inspected (winerror={error})"
        )
    return int(info.dwFileAttributes)


def pin_exact_replace_target(
    transaction: WindowsTxFTransaction,
    path: Path,
    *,
    max_bytes: int,
) -> PinnedTxFReplaceTarget | None:
    """Pin the exact destination transactionally with zero sharing before approval consumption."""

    _require_windows()
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileTransactedW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.USHORT),
        wintypes.LPVOID,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        _GENERIC_READ,
        0,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
        wintypes.HANDLE(transaction.handle),
        None,
        None,
    )
    value = _raw_handle_value(handle)
    invalid = ctypes.c_void_p(-1).value
    if not value or value == invalid:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            return None
        raise WorkspaceMutationBoundaryError(
            f"Replace target cannot be transactionally pinned (winerror={error})"
        )

    try:
        fd = msvcrt.open_osfhandle(value, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except BaseException:
        _close_raw_handle(value)
        raise

    try:
        attributes = _file_attributes(fd)
        if attributes & 0x00000010:
            raise WorkspaceMutationBoundaryError("Replace target must remain a regular file at commit")
        if attributes & 0x00000400:
            raise WorkspaceMutationBoundaryError("Replace target must not become a reparse point at commit")

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
                    label="Pinned TxF replace preimage",
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
                "Replace target changed while its transacted exact handle was being pinned"
            )
        return PinnedTxFReplaceTarget(
            fd=fd,
            snapshot=PreimageSnapshot.present(
                size_bytes=after.st_size,
                digest=digest,
                mode=stat.S_IMODE(after.st_mode),
            ),
        )
    except BaseException:
        os.close(fd)
        raise


def create_metadata_stage(
    transaction: WindowsTxFTransaction,
    pinned: PinnedMutationTarget,
    content: bytes,
    *,
    mode: int,
) -> _StagedFile:
    del mode
    _require_windows()
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileTransactedW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.USHORT),
        wintypes.LPVOID,
    ]
    create_file.restype = wintypes.HANDLE

    for _ in range(16):
        path = pinned.parent / f".codexia-txf-stage-{uuid4().hex}"
        handle = create_file(
            str(path),
            _GENERIC_READ | _GENERIC_WRITE | _DELETE | _WRITE_DAC,
            _FILE_SHARE_DELETE,
            None,
            _CREATE_NEW,
            _FILE_ATTRIBUTE_NORMAL,
            None,
            wintypes.HANDLE(transaction.handle),
            None,
            None,
        )
        value = _raw_handle_value(handle)
        invalid = ctypes.c_void_p(-1).value
        if not value or value == invalid:
            error = ctypes.get_last_error()
            if error in {80, 183}:
                continue
            raise WorkspaceMutationBoundaryError(
                f"Cannot create transacted metadata-preserving staging file (winerror={error})"
            )
        try:
            fd = msvcrt.open_osfhandle(value, os.O_RDWR | getattr(os, "O_BINARY", 0))
        except BaseException:
            _close_raw_handle(value)
            raise

        try:
            _write_all(fd, content)
            os.fsync(fd)
            info = os.fstat(fd)
            staged = _StagedFile(
                fd=fd,
                token=str(path),
                device=info.st_dev,
                inode=info.st_ino,
                size_bytes=len(content),
                sha256=sha256(content).hexdigest(),
            )
            pinned.verify_staged_identity(staged)
            return staged
        except BaseException:
            os.close(fd)
            raise

    raise WorkspaceMutationBoundaryError("Unable to allocate a unique transacted staging file")


def move_replace_staged(
    transaction: WindowsTxFTransaction,
    staged: _StagedFile,
    target: Path,
) -> None:
    _require_windows()
    if staged.fd < 0 or staged.token is None:
        raise WorkspaceMutationBoundaryError("Transacted staged mutation object is not publishable")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel32.MoveFileTransactedW
    move.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    move.restype = wintypes.BOOL
    if not move(
        staged.token,
        str(target),
        None,
        None,
        _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,
        wintypes.HANDLE(transaction.handle),
    ):
        error = ctypes.get_last_error()
        raise WorkspaceMutationBoundaryError(
            f"Windows TxF replace move failed closed (winerror={error})"
        )
    staged.token = None
