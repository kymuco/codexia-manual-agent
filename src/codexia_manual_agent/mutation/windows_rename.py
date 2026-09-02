from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path

from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError


def _build_file_rename_buffer(target: Path, *, flags: int) -> ctypes.Array[ctypes.c_char]:
    """Build an ABI-correct FILE_RENAME_INFO buffer with explicit UTF-16 terminator.

    Windows uses a 4-byte union at the head of FILE_RENAME_INFO even when the
    legacy ReplaceIfExists boolean view is selected.  The filename length is the
    exact UTF-16 byte count and intentionally excludes the trailing NUL.  The
    extra WCHAR is retained in the backing buffer because SetFileInformationByHandle
    has shown host-specific reads past the variable-length field when the buffer
    ends exactly at FileNameLength.
    """

    from ctypes import wintypes

    class _RENAME_UNION(ctypes.Union):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("Flags", wintypes.DWORD),
        ]

    class _FILE_RENAME_INFO(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [
            ("u", _RENAME_UNION),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    encoded = str(target).encode("utf-16-le")
    name_offset = _FILE_RENAME_INFO.FileName.offset
    wchar_size = ctypes.sizeof(wintypes.WCHAR)
    buffer = ctypes.create_string_buffer(name_offset + len(encoded) + wchar_size)
    info = _FILE_RENAME_INFO.from_buffer(buffer)
    info.Flags = int(flags)
    info.RootDirectory = None
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded, len(encoded))
    # create_string_buffer zero-initializes the explicit trailing WCHAR.
    return buffer


def _set_rename_info(fd: int, *, target: Path, info_class: int, flags: int) -> None:
    if os.name != "nt":
        raise WorkspaceMutationBoundaryError("Windows rename commit is available only on Windows")

    import msvcrt
    from ctypes import wintypes

    buffer = _build_file_rename_buffer(target, flags=flags)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    if not set_info(
        wintypes.HANDLE(handle),
        info_class,
        buffer,
        len(buffer),
    ):
        error = ctypes.get_last_error()
        if info_class == 3 and error in {80, 183}:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
        raise WorkspaceMutationBoundaryError(
            f"Windows rename commit failed closed (class={info_class}, winerror={error})"
        )


def rename_staged_fd(fd: int, target: Path, *, replace: bool) -> None:
    # FILE_INFO_BY_HANDLE_CLASS.FileRenameInfo
    _set_rename_info(
        fd,
        target=target,
        info_class=3,
        flags=1 if replace else 0,
    )


def strict_replace_staged_fd(fd: int, target: Path) -> None:
    # FILE_INFO_BY_HANDLE_CLASS.FileRenameInfoEx
    FILE_RENAME_REPLACE_IF_EXISTS = 0x00000001
    FILE_RENAME_POSIX_SEMANTICS = 0x00000002
    _set_rename_info(
        fd,
        target=target,
        info_class=22,
        flags=FILE_RENAME_REPLACE_IF_EXISTS | FILE_RENAME_POSIX_SEMANTICS,
    )
