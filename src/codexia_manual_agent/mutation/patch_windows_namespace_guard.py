from __future__ import annotations

import ctypes
import os
import stat
from collections.abc import Callable
from contextvars import ContextVar, Token
from ctypes import wintypes
from pathlib import Path

from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation import parent_anchor as _parent_anchor
from codexia_manual_agent.mutation import patch_final_review_repairs as _final
from codexia_manual_agent.mutation.models import PreimageSnapshot

_WINDOWS_PARENT_VERIFY: ContextVar[Callable[[], None] | None] = ContextVar(
    "m24_windows_parent_verify",
    default=None,
)
_WINDOWS_PINNED_TARGET: ContextVar[tuple[int, Path, str] | None] = ContextVar(
    "m24_windows_pinned_target",
    default=None,
)

_BasePinnedMutationTarget = _final.PinnedMutationTarget
_base_read_fd_payload = _final._read_fd_payload
_base_capture_windows_exact_path = _final._capture_windows_exact_path


class _VerifiedPinnedMutationTarget(_BasePinnedMutationTarget):
    """Expose the held Windows parent to exact target capture.

    A path-based target open is not authority-bearing even when the parent is
    revalidated before and after the read: a rename/replacement/restore sequence
    can redirect the open itself. Keep the already-pinned parent handle in a
    turn-local context so exact proposal capture can open only the leaf name
    relative to that handle.
    """

    def __enter__(self) -> "_VerifiedPinnedMutationTarget":
        super().__enter__()
        self._m24_parent_verify_token: Token[Callable[[], None] | None] | None = None
        self._m24_pinned_target_token: Token[tuple[int, Path, str] | None] | None = None
        if os.name == "nt":
            if not self._windows_handles:
                raise WorkspaceMutationBoundaryError(
                    "Patch Windows target parent is not pinned"
                )
            self._m24_parent_verify_token = _WINDOWS_PARENT_VERIFY.set(
                self.verify_parent_identity
            )
            self._m24_pinned_target_token = _WINDOWS_PINNED_TARGET.set(
                (self._windows_handles[-1], self.parent, self.target_name)
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        target_token = self._m24_pinned_target_token
        verify_token = self._m24_parent_verify_token
        try:
            if target_token is not None:
                _WINDOWS_PINNED_TARGET.reset(target_token)
            if verify_token is not None:
                _WINDOWS_PARENT_VERIFY.reset(verify_token)
        finally:
            super().__exit__(exc_type, exc, tb)


def _verified_read_fd_payload(fd: int, *, max_bytes: int) -> tuple[bytes, str]:
    verifier = _WINDOWS_PARENT_VERIFY.get()
    if verifier is not None:
        # The target handle is already bound to the held parent. Revalidating the
        # live path here preserves the established fail-closed namespace contract,
        # but it is no longer relied upon to determine which file was opened.
        verifier()
    return _base_read_fd_payload(fd, max_bytes=max_bytes)


def _windows_case_sensitive_by_handle(handle: int) -> bool | None:
    class _FileCaseSensitiveInfo(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD)]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None
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


def _nt_open_relative_target(parent_handle: int, target_name: str) -> int | None:
    """Open one leaf relative to a held Windows directory handle.

    NtOpenFile's OBJECT_ATTRIBUTES.RootDirectory is the authority anchor. The
    mutable path that originally named the parent is not consulted when choosing
    the target object, so a rename/replacement/restore race cannot redirect the
    opened preimage handle.
    """

    if not target_name or target_name in {".", ".."} or "\\" in target_name or "/" in target_name:
        raise WorkspaceMutationBoundaryError(
            "Patch relative target must be one canonical leaf name"
        )

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusUnion(ctypes.Union):
        _fields_ = [
            ("Status", ctypes.c_long),
            ("Pointer", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("u", _IoStatusUnion),
            ("Information", ctypes.c_size_t),
        ]

    encoded = target_name.encode("utf-16-le")
    if len(encoded) > 0xFFFC:
        raise WorkspaceMutationBoundaryError("Patch target leaf is too long")
    name_buffer = ctypes.create_unicode_buffer(target_name)
    unicode_name = _UnicodeString(
        len(encoded),
        len(encoded) + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )

    OBJ_CASE_INSENSITIVE = 0x00000040
    parent_case_sensitive = _windows_case_sensitive_by_handle(parent_handle)
    attributes = 0 if parent_case_sensitive is True else OBJ_CASE_INSENSITIVE
    object_attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        wintypes.HANDLE(parent_handle),
        ctypes.pointer(unicode_name),
        attributes,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    opened = wintypes.HANDLE()

    FILE_READ_DATA = 0x00000001
    FILE_READ_ATTRIBUTES = 0x00000080
    SYNCHRONIZE = 0x00100000
    FILE_SHARE_READ = 0x00000001
    FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    FILE_OPEN_REPARSE_POINT = 0x00200000

    ntdll = ctypes.WinDLL("ntdll")
    nt_open_file = ntdll.NtOpenFile
    nt_open_file.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    nt_open_file.restype = ctypes.c_long

    status = int(
        nt_open_file(
            ctypes.byref(opened),
            FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            ctypes.byref(object_attributes),
            ctypes.byref(io_status),
            FILE_SHARE_READ,
            FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT,
        )
    )
    if status >= 0:
        value = ctypes.cast(opened, ctypes.c_void_p).value
        if value is None:
            raise WorkspaceMutationBoundaryError(
                "Patch relative target open returned an invalid handle"
            )
        return int(value)

    unsigned_status = status & 0xFFFFFFFF
    if unsigned_status in {
        0xC000000F,  # STATUS_NO_SUCH_FILE
        0xC0000034,  # STATUS_OBJECT_NAME_NOT_FOUND
        0xC000003A,  # STATUS_OBJECT_PATH_NOT_FOUND
    }:
        return None

    rtl_status_to_dos = ntdll.RtlNtStatusToDosError
    rtl_status_to_dos.argtypes = [ctypes.c_long]
    rtl_status_to_dos.restype = wintypes.ULONG
    winerror = int(rtl_status_to_dos(status))
    raise WorkspaceMutationBoundaryError(
        "Patch preimage cannot be opened relative to the pinned parent "
        f"(ntstatus=0x{unsigned_status:08X}, winerror={winerror})"
    )


def _capture_windows_exact_handle(
    raw_handle: int,
    *,
    max_bytes: int,
) -> tuple[PreimageSnapshot, bytes | None]:
    import msvcrt

    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

    try:
        before = _final._win_file_info(raw_handle)
        if before.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise WorkspaceMutationBoundaryError(
                "Patch target must not be a reparse point"
            )
        if before.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY:
            raise InvalidWorkspaceMutationError("Patch target must be a regular file")
        before_identity = _final._win_info_identity(before)
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
            payload, digest = _final._read_fd_payload(fd, max_bytes=max_bytes)
            after_stat = os.fstat(fd)
            second_size, second_digest = _final._hash_fd_again(
                fd,
                max_bytes=max_bytes,
            )
            after_handle = int(msvcrt.get_osfhandle(fd))
            after_identity = _final._win_info_identity(
                _final._win_file_info(after_handle)
            )
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
            _parent_anchor._win_close_handle(raw_handle)


def _capture_windows_exact_path(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[PreimageSnapshot, bytes | None]:
    pinned = _WINDOWS_PINNED_TARGET.get()
    if os.name != "nt" or pinned is None:
        # Preserve the standalone helper surface. Authority-bearing proposal
        # preparation always enters _VerifiedPinnedMutationTarget first.
        return _base_capture_windows_exact_path(path, max_bytes=max_bytes)

    parent_handle, parent, target_name = pinned
    if path.name != target_name or os.path.normcase(
        os.path.abspath(str(path.parent))
    ) != os.path.normcase(os.path.abspath(str(parent))):
        raise WorkspaceMutationBoundaryError(
            "Patch target capture does not match the pinned parent context"
        )

    verifier = _WINDOWS_PARENT_VERIFY.get()
    if verifier is None:
        raise WorkspaceMutationBoundaryError(
            "Patch Windows target capture requires a parent verifier"
        )
    verifier()

    raw_handle = _nt_open_relative_target(parent_handle, target_name)
    if raw_handle is None:
        verifier()
        return PreimageSnapshot.absent(), None

    # A mutable path may change here without redirecting the target handle: the
    # object was selected relative to parent_handle above. The existing read-time
    # verifier still requires the authorized live parent spelling to be restored
    # before any bytes are admitted.
    return _capture_windows_exact_handle(raw_handle, max_bytes=max_bytes)


def install_patch_windows_namespace_guard() -> None:
    if getattr(_final, "_M24_WINDOWS_NAMESPACE_GUARD_INSTALLED", False):
        return
    _final.PinnedMutationTarget = _VerifiedPinnedMutationTarget
    _final._read_fd_payload = _verified_read_fd_payload
    _final._capture_windows_exact_path = _capture_windows_exact_path
    _final._M24_WINDOWS_NAMESPACE_GUARD_INSTALLED = True


install_patch_windows_namespace_guard()
