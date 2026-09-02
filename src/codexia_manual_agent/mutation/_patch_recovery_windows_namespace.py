from __future__ import annotations

import os
from pathlib import Path

from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError
from codexia_manual_agent.mutation.parent_anchor import (
    _normalize_windows_final_path,
    _win_close_handle,
)
from codexia_manual_agent.mutation.windows_txf import WindowsTxFTransaction


def _rollback_and_close_namespace_transaction(
    transaction: WindowsTxFTransaction,
) -> None:
    rollback_error: BaseException | None = None
    try:
        transaction.rollback()
    except BaseException as exc:
        rollback_error = exc
    try:
        transaction.close()
    except BaseException as close_exc:
        if rollback_error is not None:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal namespace transaction could not be "
                "rolled back or closed"
            ) from close_exc
        raise WorkspaceMutationBoundaryError(
            "Patch recovery journal namespace transaction could not be closed"
        ) from close_exc
    # Closing the last uncommitted KTM handle is itself a rollback boundary.
    # If the explicit rollback failed but close succeeded, the namespace pin is
    # still safely aborted and should not invalidate an already durable marker.


def _win_create_transacted_namespace_marker(
    transaction: WindowsTxFTransaction,
    path: Path,
) -> int:
    import ctypes
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

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    CREATE_NEW = 1
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_WRITE_THROUGH = 0x80000000

    handle = create_file(
        str(path),
        GENERIC_READ | GENERIC_WRITE,
        0,
        None,
        CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_WRITE_THROUGH,
        None,
        wintypes.HANDLE(transaction.handle),
        None,
        None,
    )
    value = ctypes.cast(handle, ctypes.c_void_p).value
    invalid = ctypes.c_void_p(-1).value
    if value is None or value == invalid:
        error = ctypes.get_last_error()
        raise WorkspaceMutationBoundaryError(
            "Patch recovery journal namespace pin could not be created "
            f"(winerror={error})"
        )

    raw_handle = int(value)
    try:
        fd = msvcrt.open_osfhandle(
            raw_handle,
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        _win_close_handle(raw_handle)
        raise

    try:
        if os.write(fd, b"\0") != 1:
            raise OSError("Patch recovery journal namespace pin write made no progress")
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _win_verify_journal_fd_path(
    fd: int,
    *,
    expected: Path,
    workspace_root: Path,
) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = msvcrt.get_osfhandle(fd)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    written = get_final(wintypes.HANDLE(handle), buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise WorkspaceMutationBoundaryError(
            "Patch recovery journal resolved path cannot be read "
            f"(winerror={ctypes.get_last_error()})"
        )

    actual = _normalize_windows_final_path(buffer.value)
    wanted = os.path.normcase(os.path.abspath(str(expected)))
    root = os.path.normcase(os.path.abspath(str(workspace_root)))
    if actual == root or actual.startswith(root.rstrip("\\/") + os.sep):
        raise WorkspaceMutationBoundaryError(
            "Patch recovery journal resolved into the patch workspace"
        )
    if actual != wanted:
        raise WorkspaceMutationBoundaryError(
            "Patch recovery journal namespace changed before I/O admission"
        )


def _win_open_journal_file(path: Path, *, create: bool, writable: bool) -> int:
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

    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    get_info.restype = wintypes.BOOL

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    CREATE_NEW = 1
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

    access = GENERIC_READ | (GENERIC_WRITE if writable else 0)
    disposition = CREATE_NEW if create else OPEN_EXISTING
    handle = create_file(
        str(path),
        access,
        FILE_SHARE_READ,
        None,
        disposition,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = ctypes.cast(handle, ctypes.c_void_p).value
    invalid = ctypes.c_void_p(-1).value
    if value is None or value == invalid:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(error, "Patch recovery journal does not exist", str(path))
        if error in {80, 183}:
            raise FileExistsError(error, "Patch recovery journal already exists", str(path))
        raise WorkspaceMutationBoundaryError(
            f"Patch recovery journal cannot be opened securely: {path} (winerror={error})"
        )

    raw_handle = int(value)
    info = _BY_HANDLE_FILE_INFORMATION()
    if not get_info(wintypes.HANDLE(raw_handle), ctypes.byref(info)):
        error = ctypes.get_last_error()
        _win_close_handle(raw_handle)
        raise WorkspaceMutationBoundaryError(
            f"Patch recovery journal handle cannot be inspected (winerror={error})"
        )
    if info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY:
        _win_close_handle(raw_handle)
        raise WorkspaceMutationBoundaryError(
            "Patch recovery journal must remain a regular file"
        )
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        _win_close_handle(raw_handle)
        raise WorkspaceMutationBoundaryError(
            "Patch recovery journal entry must not be a reparse point"
        )

    fd_flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
    try:
        return msvcrt.open_osfhandle(raw_handle, fd_flags)
    except BaseException:
        _win_close_handle(raw_handle)
        raise
