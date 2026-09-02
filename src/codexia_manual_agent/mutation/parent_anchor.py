from __future__ import annotations

import errno
import os
import stat
import sys
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


@dataclass(slots=True)
class _StagedFile:
    fd: int
    token: str | None
    device: int
    inode: int
    size_bytes: int
    sha256: str


class PinnedMutationTarget:
    """Pin parent and staged-file identities across an M2.3 commit.

    Linux uses a directory fd plus an anonymous ``O_TMPFILE`` staging inode.
    Windows keeps verified directory handles open without ``FILE_SHARE_DELETE``
    and uses an exclusive staged-file handle renamed by handle at commit.
    """

    def __init__(self, *, root: Path, parent: Path, target_name: str) -> None:
        self.root = root
        self.parent = parent
        self.target_name = target_name
        self.target_path = parent / target_name
        self._dir_fd: int | None = None
        self._windows_handles: list[int] = []

    def __enter__(self) -> "PinnedMutationTarget":
        try:
            if os.name == "nt":
                self._pin_windows_chain()
            else:
                self._pin_posix_parent()
            self.verify_parent_identity()
            return self
        except BaseException:
            self._release_pins()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        self._release_pins()

    def _release_pins(self) -> None:
        if self._dir_fd is not None:
            try:
                os.close(self._dir_fd)
            finally:
                self._dir_fd = None
        if self._windows_handles:
            handles = self._windows_handles
            self._windows_handles = []
            for handle in reversed(handles):
                _win_close_handle(handle)

    def verify_parent_identity(self) -> None:
        if os.name == "nt":
            if not self._windows_handles:
                raise WorkspaceMutationBoundaryError("Mutation parent is not pinned")
            for handle, expected in zip(
                self._windows_handles,
                self._windows_chain_paths(),
                strict=True,
            ):
                _win_verify_directory_handle(handle, expected)
            return

        if self._dir_fd is None:
            raise WorkspaceMutationBoundaryError("Mutation parent is not pinned")
        try:
            pinned = os.fstat(self._dir_fd)
            current = os.stat(self.parent, follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Mutation target parent identity cannot be revalidated"
            ) from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or pinned.st_dev != current.st_dev
            or pinned.st_ino != current.st_ino
        ):
            raise WorkspaceMutationBoundaryError(
                "Mutation target parent identity changed after authorization"
            )

    def capture_preimage(self, *, max_bytes: int) -> PreimageSnapshot:
        self.verify_parent_identity()
        if os.name == "nt":
            return _capture_path_preimage(self.target_path, max_bytes=max_bytes)
        return self._capture_posix_preimage(max_bytes=max_bytes)

    def write_temp(self, content: bytes, *, mode: int) -> _StagedFile:
        self.verify_parent_identity()
        if os.name == "nt":
            fd, path = _win_create_exclusive_staging(self.parent)
            token: str | None = str(path)
        elif sys.platform.startswith("linux"):
            if self._dir_fd is None or not getattr(os, "O_TMPFILE", 0):
                raise WorkspaceMutationBoundaryError(
                    "Secure anonymous staging is unavailable on this Linux host"
                )
            flags = os.O_RDWR | os.O_TMPFILE | getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(".", flags, 0o600, dir_fd=self._dir_fd)
            except OSError as exc:
                raise WorkspaceMutationBoundaryError(
                    "Secure anonymous staging could not be created"
                ) from exc
            token = None
        else:
            raise WorkspaceMutationBoundaryError(
                "M2.3 secure staged-inode commits currently require Linux or Windows"
            )

        try:
            _write_all(fd, content)
            os.fsync(fd)
            if os.name != "nt":
                os.fchmod(fd, mode)
            info = os.fstat(fd)
            staged = _StagedFile(
                fd=fd,
                token=token,
                device=info.st_dev,
                inode=info.st_ino,
                size_bytes=len(content),
                sha256=sha256(content).hexdigest(),
            )
            self.verify_staged_identity(staged)
            return staged
        except BaseException:
            try:
                if os.name == "nt":
                    _win_discard_fd(fd)
                else:
                    os.close(fd)
            except OSError:
                pass
            raise

    def verify_staged_identity(self, staged: _StagedFile) -> None:
        self.verify_parent_identity()
        if staged.fd < 0:
            raise WorkspaceMutationBoundaryError("Staged mutation inode is already closed")
        try:
            info = os.fstat(staged.fd)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Staged mutation inode can no longer be inspected"
            ) from exc
        if (
            info.st_dev != staged.device
            or info.st_ino != staged.inode
            or info.st_size != staged.size_bytes
        ):
            raise WorkspaceMutationBoundaryError(
                "Staged mutation inode identity changed before commit"
            )

        duplicate = os.dup(staged.fd)
        try:
            os.lseek(duplicate, 0, os.SEEK_SET)
            with os.fdopen(duplicate, "rb", closefd=True) as handle:
                size, digest = hash_bounded_stream(
                    handle,
                    max_bytes=staged.size_bytes,
                    label="Staged mutation content",
                )
                duplicate = -1
        finally:
            if duplicate >= 0:
                os.close(duplicate)
        if size != staged.size_bytes or digest != staged.sha256:
            raise WorkspaceMutationBoundaryError(
                "Staged mutation content changed before commit"
            )

        if staged.token is not None and os.name != "nt":
            self._verify_posix_staged_entry(staged)

    def commit_create(self, staged: _StagedFile) -> None:
        self.verify_staged_identity(staged)
        if os.name == "nt":
            _win_rename_staged_fd(staged.fd, self.target_path, replace=False)
            staged.token = None
            return
        if not sys.platform.startswith("linux") or self._dir_fd is None:
            raise WorkspaceMutationBoundaryError("Secure create commit is unavailable")
        _linux_link_fd(staged.fd, self._dir_fd, self.target_name)

    def commit_replace(self, staged: _StagedFile) -> None:
        self.verify_staged_identity(staged)
        if os.name == "nt":
            _win_rename_staged_fd(staged.fd, self.target_path, replace=True)
            staged.token = None
            return
        if not sys.platform.startswith("linux") or self._dir_fd is None:
            raise WorkspaceMutationBoundaryError("Secure replace commit is unavailable")
        _linux_replace_from_fd(staged, self._dir_fd, self.target_name)

    def discard_staged(self, staged: _StagedFile) -> None:
        if staged.fd < 0:
            return
        if os.name == "nt":
            _win_discard_fd(staged.fd)
        else:
            os.close(staged.fd)
        staged.fd = -1
        staged.token = None

    def close_staged(self, staged: _StagedFile) -> None:
        if staged.fd < 0:
            return
        os.close(staged.fd)
        staged.fd = -1
        staged.token = None

    def cleanup_temp(self, staged: _StagedFile) -> None:
        self.discard_staged(staged)

    def fsync_parent(self) -> None:
        if os.name == "nt" or self._dir_fd is None:
            return
        try:
            os.fsync(self._dir_fd)
        except OSError:
            pass

    def _pin_posix_parent(self) -> None:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.parent, flags)
            info = os.fstat(fd)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Mutation target parent cannot be pinned"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            os.close(fd)
            raise WorkspaceMutationBoundaryError("Mutation target parent is not a directory")
        self._dir_fd = fd

    def _capture_posix_preimage(self, *, max_bytes: int) -> PreimageSnapshot:
        if self._dir_fd is None:
            raise WorkspaceMutationBoundaryError("Mutation parent is not pinned")
        try:
            info = os.stat(
                self.target_name,
                dir_fd=self._dir_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return PreimageSnapshot.absent()
        except OSError as exc:
            raise InvalidWorkspaceMutationError("Cannot stat mutation target") from exc

        if stat.S_ISLNK(info.st_mode):
            raise WorkspaceMutationBoundaryError("Mutation target must not be a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise InvalidWorkspaceMutationError("Mutation target must be a regular file")
        if info.st_size > max_bytes:
            raise InvalidWorkspaceMutationError(
                f"Mutation preimage exceeds hashing budget ({info.st_size} > {max_bytes})"
            )

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.target_name, flags, dir_fd=self._dir_fd)
        except OSError as exc:
            raise InvalidWorkspaceMutationError("Cannot open mutation target") from exc

        try:
            opened = os.fstat(fd)
            if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                raise WorkspaceMutationPreimageChangedError(
                    "Mutation target changed while its preimage was being opened"
                )
            with os.fdopen(fd, "rb", closefd=True) as handle:
                size, digest = hash_bounded_stream(
                    handle,
                    max_bytes=max_bytes,
                    label="Mutation preimage",
                )
                after = os.fstat(handle.fileno())
                fd = -1
            entry_after = os.stat(
                self.target_name,
                dir_fd=self._dir_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise WorkspaceMutationPreimageChangedError(
                "Mutation target disappeared while its preimage was being inspected"
            ) from exc
        except OSError as exc:
            raise InvalidWorkspaceMutationError("Cannot read mutation target") from exc
        finally:
            if fd >= 0:
                os.close(fd)

        if (
            size != after.st_size
            or opened.st_dev != after.st_dev
            or opened.st_ino != after.st_ino
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or after.st_dev != entry_after.st_dev
            or after.st_ino != entry_after.st_ino
        ):
            raise WorkspaceMutationPreimageChangedError(
                "Mutation target changed while its preimage was being inspected"
            )
        return PreimageSnapshot.present(
            size_bytes=after.st_size,
            digest=digest,
            mode=stat.S_IMODE(after.st_mode),
        )

    def _verify_posix_staged_entry(self, staged: _StagedFile) -> None:
        if self._dir_fd is None or staged.token is None:
            return
        try:
            entry = os.stat(staged.token, dir_fd=self._dir_fd, follow_symlinks=False)
            held = os.fstat(staged.fd)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Staged mutation entry changed before commit"
            ) from exc
        if entry.st_dev != held.st_dev or entry.st_ino != held.st_ino:
            raise WorkspaceMutationBoundaryError(
                "Staged mutation entry was replaced before commit"
            )

    def _windows_chain_paths(self) -> tuple[Path, ...]:
        relative = self.parent.relative_to(self.root)
        paths = [self.root]
        current = self.root
        for part in relative.parts:
            current = current / part
            paths.append(current)
        return tuple(paths)

    def _pin_windows_chain(self) -> None:
        handles: list[int] = []
        try:
            for path in self._windows_chain_paths():
                handle = _win_open_directory(path)
                _win_verify_directory_handle(handle, path)
                handles.append(handle)
        except BaseException:
            for handle in reversed(handles):
                _win_close_handle(handle)
            raise
        self._windows_handles = handles


def _capture_path_preimage(path: Path, *, max_bytes: int) -> PreimageSnapshot:
    if not os.path.lexists(path):
        return PreimageSnapshot.absent()
    if path.is_symlink():
        raise WorkspaceMutationBoundaryError("Mutation target must not be a symlink")
    if not path.is_file():
        raise InvalidWorkspaceMutationError("Mutation target must be a regular file")
    try:
        before = path.stat()
    except OSError as exc:
        raise InvalidWorkspaceMutationError("Cannot stat mutation target") from exc
    if before.st_size > max_bytes:
        raise InvalidWorkspaceMutationError(
            f"Mutation preimage exceeds hashing budget ({before.st_size} > {max_bytes})"
        )
    try:
        with path.open("rb") as handle:
            size, digest = hash_bounded_stream(
                handle,
                max_bytes=max_bytes,
                label="Mutation preimage",
            )
        after = path.stat()
    except OSError as exc:
        raise InvalidWorkspaceMutationError("Cannot read mutation target") from exc
    if (
        size != after.st_size
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_ino", 0) != getattr(after, "st_ino", 0)
    ):
        raise WorkspaceMutationPreimageChangedError(
            "Mutation target changed while its preimage was being inspected"
        )
    return PreimageSnapshot.present(
        size_bytes=after.st_size,
        digest=digest,
        mode=stat.S_IMODE(after.st_mode),
    )


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("Staged mutation write made no progress")
        written += count
    os.lseek(fd, 0, os.SEEK_SET)


def _linux_link_fd(fd: int, dir_fd: int, name: str) -> None:
    import ctypes

    AT_EMPTY_PATH = 0x1000
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = libc.linkat
    linkat.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    linkat.restype = ctypes.c_int
    if linkat(fd, b"", dir_fd, os.fsencode(name), AT_EMPTY_PATH) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def _linux_replace_from_fd(staged: _StagedFile, dir_fd: int, target_name: str) -> None:
    commit_name = f".codexia-commit-{uuid4().hex}"
    _linux_link_fd(staged.fd, dir_fd, commit_name)
    try:
        entry = os.stat(commit_name, dir_fd=dir_fd, follow_symlinks=False)
        held = os.fstat(staged.fd)
        if entry.st_dev != held.st_dev or entry.st_ino != held.st_ino:
            raise WorkspaceMutationBoundaryError(
                "Commit-point staging link does not match the held inode"
            )
        os.replace(
            commit_name,
            target_name,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
        )
    except Exception as exc:
        try:
            os.unlink(commit_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        except OSError as cleanup_exc:
            raise WorkspaceMutationBoundaryError(
                "Replace commit aborted and its staging link could not be cleaned up: "
                f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            ) from exc
        raise


def _win_create_exclusive_staging(parent: Path) -> tuple[int, Path]:
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
    GENERIC_WRITE = 0x40000000
    DELETE = 0x00010000
    CREATE_NEW = 1
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    invalid = ctypes.c_void_p(-1).value

    for _ in range(16):
        path = parent / f".codexia-write-{uuid4().hex}"
        handle = create_file(
            str(path),
            GENERIC_READ | GENERIC_WRITE | DELETE,
            0,
            None,
            CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        value = ctypes.cast(handle, ctypes.c_void_p).value
        if value is not None and value != invalid:
            try:
                flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
                fd = msvcrt.open_osfhandle(int(value), flags)
            except BaseException:
                _win_close_handle(int(value))
                raise
            return fd, path
        error = ctypes.get_last_error()
        if error not in {80, 183}:
            raise OSError(error, f"Cannot create exclusive staging file: {path}")
    raise WorkspaceMutationBoundaryError("Unable to allocate a unique staging file")


def _win_rename_staged_fd(fd: int, target: Path, *, replace: bool) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FILE_RENAME_INFO(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    encoded = str(target).encode("utf-16-le")
    offset = _FILE_RENAME_INFO.FileName.offset
    buffer = ctypes.create_string_buffer(offset + len(encoded))
    info = _FILE_RENAME_INFO.from_buffer(buffer)
    info.ReplaceIfExists = 1 if replace else 0
    info.RootDirectory = None
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    if not set_info(wintypes.HANDLE(handle), 3, buffer, len(buffer)):
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
        raise OSError(error, f"Cannot rename staged mutation handle to {target}")


def _win_discard_fd(fd: int) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    info = _FILE_DISPOSITION_INFO(True)
    if not set_info(
        wintypes.HANDLE(handle),
        4,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        os.close(fd)
        raise OSError(error, "Cannot discard staged mutation handle")
    os.close(fd)


def _win_open_directory(path: Path) -> int:
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
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    handle = create_file(
        str(path),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = ctypes.cast(handle, ctypes.c_void_p).value
    invalid = ctypes.c_void_p(-1).value
    if value is None or value == invalid:
        raise WorkspaceMutationBoundaryError(
            f"Mutation directory cannot be pinned: {path} (winerror={ctypes.get_last_error()})"
        )
    return int(value)


def _win_verify_directory_handle(handle: int, expected: Path) -> None:
    import ctypes
    from ctypes import wintypes

    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

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
    if not get_info(wintypes.HANDLE(handle), ctypes.byref(info)):
        raise WorkspaceMutationBoundaryError(
            f"Pinned mutation directory cannot be inspected (winerror={ctypes.get_last_error()})"
        )
    if not info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY:
        raise WorkspaceMutationBoundaryError("Pinned mutation parent is not a directory")
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise WorkspaceMutationBoundaryError(
            "Pinned mutation parent must not be a reparse point"
        )

    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    written = get_final(wintypes.HANDLE(handle), buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise WorkspaceMutationBoundaryError(
            f"Pinned mutation directory path cannot be read (winerror={ctypes.get_last_error()})"
        )
    actual = _normalize_windows_final_path(buffer.value)
    wanted = os.path.normcase(os.path.abspath(str(expected)))
    if actual != wanted:
        raise WorkspaceMutationBoundaryError(
            "Mutation directory identity changed after authorization"
        )


def _normalize_windows_final_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.abspath(value))


def _win_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    close_handle(wintypes.HANDLE(handle))