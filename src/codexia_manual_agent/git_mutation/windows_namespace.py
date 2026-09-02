from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.domain.errors import GitRepositoryBoundaryError
from codexia_manual_agent.mutation.windows_txf import (
    WindowsTxFTransaction,
    create_transaction,
    require_windows_txf_support,
)


_MAX_LOCKED_CONFIG_BYTES = 1024 * 1024
_INCLUDE_SECTION_RE = re.compile(r"^\[\s*include(?:if)?(?:\s|\])", re.IGNORECASE)


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    junction = getattr(path, "is_junction", None)
    return bool(junction is not None and junction())


def _normalize_final_path(value: str) -> str:
    rendered = value
    if rendered.startswith("\\\\?\\UNC\\"):
        rendered = "\\\\" + rendered[len("\\\\?\\UNC\\") :]
    elif rendered.startswith("\\\\?\\"):
        rendered = rendered[len("\\\\?\\") :]
    return os.path.normcase(os.path.abspath(rendered))


def _fd_final_path(fd: int) -> str:
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    get_final.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    handle = msvcrt.get_osfhandle(fd)
    written = get_final(wintypes.HANDLE(handle), buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise GitRepositoryBoundaryError(
            "Pinned Git handle final path cannot be resolved"
        )
    return _normalize_final_path(buffer.value)


def _create_transacted_marker(
    transaction: WindowsTxFTransaction,
    directory: Path,
) -> int:
    import msvcrt
    from ctypes import wintypes

    marker = directory / f".codexia-git-namespace-{uuid4().hex}.pin"
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
        str(marker),
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
        raise GitRepositoryBoundaryError(
            f"Git namespace TxF marker cannot be created (winerror={error})"
        )

    raw_handle = int(value)
    try:
        fd = msvcrt.open_osfhandle(
            raw_handle,
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        kernel32.CloseHandle(wintypes.HANDLE(raw_handle))
        raise

    try:
        if os.write(fd, b"\0") != 1:
            raise OSError("Git namespace marker write made no progress")
        os.fsync(fd)
        expected = os.path.normcase(os.path.abspath(str(marker)))
        if _fd_final_path(fd) != expected:
            raise GitRepositoryBoundaryError(
                "Git namespace changed while the TxF pin was being admitted"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_read_locked_file(path: Path) -> int:
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
    FILE_SHARE_READ = 0x00000001
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

    handle = create_file(
        str(path),
        GENERIC_READ,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = ctypes.cast(handle, ctypes.c_void_p).value
    invalid = ctypes.c_void_p(-1).value
    if value is None or value == invalid:
        error = ctypes.get_last_error()
        raise GitRepositoryBoundaryError(
            f"Critical Git file cannot be locked read-only (winerror={error})"
        )
    raw_handle = int(value)
    try:
        fd = msvcrt.open_osfhandle(
            raw_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        kernel32.CloseHandle(wintypes.HANDLE(raw_handle))
        raise
    expected = os.path.normcase(os.path.abspath(str(path)))
    try:
        if _fd_final_path(fd) != expected:
            raise GitRepositoryBoundaryError(
                "Critical Git file changed namespace while being locked"
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def _reject_external_object_semantics(root: Path) -> None:
    if not (root / "objects").is_dir() or not (root / "refs").is_dir():
        return
    for relative in (
        "info/grafts",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "commondir",
    ):
        candidate = root / relative
        if candidate.exists() or candidate.is_symlink():
            raise GitRepositoryBoundaryError(
                f"M2.5 v1 rejects external Git object/ancestry semantics: {candidate}"
            )


def _validate_locked_git_config(fd: int) -> None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        payload = os.read(fd, _MAX_LOCKED_CONFIG_BYTES + 1)
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise GitRepositoryBoundaryError("Locked Git config cannot be read") from exc
    if len(payload) > _MAX_LOCKED_CONFIG_BYTES:
        raise GitRepositoryBoundaryError("Locked Git config exceeds the M2.5 budget")
    text = payload.decode("latin-1")
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if _INCLUDE_SECTION_RE.match(stripped):
            raise GitRepositoryBoundaryError(
                "M2.5 v1 rejects include/includeIf Git config sections"
            )


@dataclass(slots=True)
class WindowsGitNamespacePin:
    """Short-lived TxF directory pins plus deny-write critical-file handles.

    The marker transaction is never committed and contains no Git data. Keeping
    transacted marker handles alive prevents rename/reparse substitution of every
    directory component in each marker path. Critical config/index files are
    opened read-only without write/delete sharing so their approved bytes cannot
    change while the authorized Git mutation is revalidated and executed.
    """

    transaction: WindowsTxFTransaction
    marker_fds: list[int]
    locked_file_fds: list[int]
    closed: bool = False

    @classmethod
    def acquire(
        cls,
        directories: tuple[Path, ...],
        *,
        locked_files: tuple[Path, ...] = (),
    ) -> "WindowsGitNamespacePin":
        if os.name != "nt":
            raise GitRepositoryBoundaryError(
                "M2.5 Git mutation execution requires Windows TxF namespace pinning"
            )
        if not directories:
            raise GitRepositoryBoundaryError("Git namespace pin requires directories")

        unique: list[Path] = []
        seen: set[str] = set()
        drive: str | None = None
        for raw in directories:
            path = Path(raw)
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise GitRepositoryBoundaryError(
                    f"Git namespace pin directory does not resolve: {path}"
                ) from exc
            if _is_link_like(path) or not path.is_dir():
                raise GitRepositoryBoundaryError(
                    f"Git namespace pin directory is redirected or not a directory: {path}"
                )
            wanted = os.path.normcase(os.path.abspath(str(path)))
            actual = os.path.normcase(os.path.abspath(str(resolved)))
            if wanted != actual:
                raise GitRepositoryBoundaryError(
                    f"Git namespace pin directory changed identity: {path}"
                )
            current_drive = os.path.splitdrive(actual)[0].casefold()
            if drive is None:
                drive = current_drive
            elif current_drive != drive:
                raise GitRepositoryBoundaryError(
                    "One Git namespace pin group cannot span Windows volumes"
                )
            if actual not in seen:
                seen.add(actual)
                unique.append(path)

        for directory in unique:
            _reject_external_object_semantics(directory)

        effective_locked_files = list(locked_files)
        for directory in unique:
            if directory.name.casefold() == ".git":
                index = directory / "index"
                if index.is_file() and index not in effective_locked_files:
                    effective_locked_files.append(index)

        require_windows_txf_support(unique[0])
        transaction = create_transaction()
        marker_fds: list[int] = []
        file_fds: list[int] = []
        try:
            for directory in unique:
                marker_fds.append(_create_transacted_marker(transaction, directory))
            for path in effective_locked_files:
                if _is_link_like(path) or not path.is_file():
                    raise GitRepositoryBoundaryError(
                        f"Critical Git file is redirected or not a regular file: {path}"
                    )
                fd = _open_read_locked_file(path)
                file_fds.append(fd)
                if path.name.casefold() == "config":
                    _validate_locked_git_config(fd)
            return cls(
                transaction=transaction,
                marker_fds=marker_fds,
                locked_file_fds=file_fds,
            )
        except BaseException:
            for fd in reversed(file_fds):
                try:
                    os.close(fd)
                except OSError:
                    pass
            for fd in reversed(marker_fds):
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                transaction.rollback()
            except BaseException:
                pass
            try:
                transaction.close()
            except BaseException:
                pass
            raise

    def close(self) -> str | None:
        if self.closed:
            return None
        errors: list[str] = []
        for fd in reversed(self.locked_file_fds):
            try:
                os.close(fd)
            except OSError as exc:
                errors.append(f"critical-file close: {type(exc).__name__}: {exc}")
        self.locked_file_fds.clear()
        for fd in reversed(self.marker_fds):
            try:
                os.close(fd)
            except OSError as exc:
                errors.append(f"marker close: {type(exc).__name__}: {exc}")
        self.marker_fds.clear()
        try:
            self.transaction.rollback()
        except BaseException as exc:
            errors.append(f"TxF rollback: {type(exc).__name__}: {exc}")
        try:
            self.transaction.close()
        except BaseException as exc:
            errors.append(f"TxF close: {type(exc).__name__}: {exc}")
        self.closed = True
        return "; ".join(errors) if errors else None
