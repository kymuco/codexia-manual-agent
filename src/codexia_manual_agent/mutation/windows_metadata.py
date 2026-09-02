from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath

from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError


_OWNER_SECURITY_INFORMATION = 0x00000001
_GROUP_SECURITY_INFORMATION = 0x00000002
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
_SE_FILE_OBJECT = 1
_SE_DACL_PROTECTED = 0x1000
_FILE_STREAM_INFO_CLASS = 7
_FILE_BASIC_INFO_CLASS = 0

_FILE_ATTRIBUTE_READONLY = 0x00000001
_FILE_ATTRIBUTE_HIDDEN = 0x00000002
_FILE_ATTRIBUTE_SYSTEM = 0x00000004
_FILE_ATTRIBUTE_ARCHIVE = 0x00000020
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_NOT_CONTENT_INDEXED = 0x00002000
_SUPPORTED_ATTRIBUTES = (
    _FILE_ATTRIBUTE_READONLY
    | _FILE_ATTRIBUTE_HIDDEN
    | _FILE_ATTRIBUTE_SYSTEM
    | _FILE_ATTRIBUTE_ARCHIVE
    | _FILE_ATTRIBUTE_NORMAL
    | _FILE_ATTRIBUTE_NOT_CONTENT_INDEXED
)

_RESERVED_DOS_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "com¹",
        "com²",
        "com³",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_INVALID_WIN32_CHARS = frozenset('<>:"|?*')


def validate_windows_relative_target(target: str) -> None:
    """Reject Win32 namespace spellings that can alias a different file object."""

    parts = PurePosixPath(target.replace("\\", "/")).parts
    for part in parts:
        if not part or part in {".", ".."}:
            raise WorkspaceMutationBoundaryError("Windows mutation target contains invalid path parts")
        if part.endswith((" ", ".")):
            raise WorkspaceMutationBoundaryError(
                "Windows mutation target components must not end in a space or period"
            )
        if any(ord(ch) < 32 for ch in part):
            raise WorkspaceMutationBoundaryError(
                "Windows mutation target contains a control character"
            )
        if any(ch in _INVALID_WIN32_CHARS for ch in part):
            raise WorkspaceMutationBoundaryError(
                "Windows mutation target contains reserved Win32 namespace characters"
            )
        stem = part.split(".", 1)[0].casefold()
        if stem in _RESERVED_DOS_NAMES:
            raise WorkspaceMutationBoundaryError(
                f"Windows mutation target uses reserved DOS device name: {part}"
            )


def _normalize_windows_final_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.abspath(value))


def _handle_value(fd: int) -> int:
    import msvcrt

    return int(msvcrt.get_osfhandle(fd))


def _final_path_for_handle(handle: int) -> str:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    written = get_final(wintypes.HANDLE(handle), buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise WorkspaceMutationBoundaryError(
            f"Windows replace target final path cannot be read (winerror={ctypes.get_last_error()})"
        )
    return _normalize_windows_final_path(buffer.value)


def _security_sddl_and_components(handle: int) -> tuple[str, int, int, int, int, int]:
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    owner = ctypes.c_void_p()
    group = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    security_info = (
        _OWNER_SECURITY_INFORMATION
        | _GROUP_SECURITY_INFORMATION
        | _DACL_SECURITY_INFORMATION
    )

    get_security = advapi32.GetSecurityInfo
    get_security.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    result = int(
        get_security(
            wintypes.HANDLE(handle),
            _SE_FILE_OBJECT,
            security_info,
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
    )
    if result != 0:
        raise WorkspaceMutationBoundaryError(
            f"Windows replace security descriptor cannot be read (winerror={result})"
        )

    try:
        convert = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.ULONG),
        ]
        convert.restype = wintypes.BOOL
        rendered = wintypes.LPWSTR()
        rendered_len = wintypes.ULONG()
        if not convert(
            descriptor,
            1,
            security_info,
            ctypes.byref(rendered),
            ctypes.byref(rendered_len),
        ):
            raise WorkspaceMutationBoundaryError(
                "Windows replace security descriptor cannot be normalized "
                f"(winerror={ctypes.get_last_error()})"
            )
        try:
            sddl = rendered.value or ""
        finally:
            ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(rendered)

        control = wintypes.WORD()
        revision = wintypes.DWORD()
        get_control = advapi32.GetSecurityDescriptorControl
        get_control.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_control.restype = wintypes.BOOL
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise WorkspaceMutationBoundaryError(
                "Windows replace security control cannot be inspected "
                f"(winerror={ctypes.get_last_error()})"
            )
        return (
            sddl,
            int(owner.value or 0),
            int(group.value or 0),
            int(dacl.value or 0),
            int(descriptor.value or 0),
            int(control.value),
        )
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(descriptor)
        raise


def _stream_names(handle: int) -> tuple[str, ...]:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL

    size = 4096
    while size <= 1_048_576:
        buffer = ctypes.create_string_buffer(size)
        if get_info(wintypes.HANDLE(handle), _FILE_STREAM_INFO_CLASS, buffer, size):
            names: list[str] = []
            offset = 0
            while True:
                next_offset = int.from_bytes(buffer.raw[offset : offset + 4], "little")
                name_len = int.from_bytes(buffer.raw[offset + 4 : offset + 8], "little")
                name_start = offset + 24
                name_end = name_start + name_len
                if name_end > size:
                    raise WorkspaceMutationBoundaryError(
                        "Windows stream enumeration returned an invalid buffer"
                    )
                names.append(buffer.raw[name_start:name_end].decode("utf-16-le"))
                if next_offset == 0:
                    break
                if next_offset < 24 or offset + next_offset >= size:
                    raise WorkspaceMutationBoundaryError(
                        "Windows stream enumeration returned an invalid entry offset"
                    )
                offset += next_offset
            return tuple(names)
        error = ctypes.get_last_error()
        if error not in {122, 234}:
            raise WorkspaceMutationBoundaryError(
                "Windows replace filesystem cannot enumerate data streams "
                f"(winerror={error})"
            )
        size *= 2
    raise WorkspaceMutationBoundaryError("Windows replace stream metadata exceeds inspection budget")


def _file_attributes(handle: int) -> int:
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
    if not get_info(wintypes.HANDLE(handle), ctypes.byref(info)):
        raise WorkspaceMutationBoundaryError(
            f"Windows replace file attributes cannot be inspected (winerror={ctypes.get_last_error()})"
        )
    attributes = int(info.dwFileAttributes)
    unsupported = attributes & ~_SUPPORTED_ATTRIBUTES
    if unsupported:
        raise WorkspaceMutationBoundaryError(
            "Windows replace target uses unsupported filesystem attributes "
            f"(mask=0x{unsupported:08x})"
        )
    return attributes


def _binding(sddl: str, attributes: int, stream_names: tuple[str, ...]) -> dict[str, object]:
    defaults = {"", "::$DATA"}
    if len(stream_names) != 1 or stream_names[0] not in defaults:
        raise WorkspaceMutationBoundaryError(
            "M2.3 strict replace does not support targets with named data streams"
        )
    return {
        "security_descriptor_sha256": sha256(sddl.encode("utf-8")).hexdigest(),
        "file_attributes": attributes,
        "stream_policy": "default_only",
    }


@dataclass(slots=True)
class WindowsReplaceMetadata:
    binding: dict[str, object]
    sddl: str
    owner: int
    group: int
    dacl: int
    descriptor: int
    control: int

    def close(self) -> None:
        if self.descriptor:
            ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(
                ctypes.c_void_p(self.descriptor)
            )
            self.descriptor = 0
            self.owner = 0
            self.group = 0
            self.dacl = 0


def capture_windows_replace_metadata_fd(
    fd: int,
    *,
    expected_path: Path | None = None,
) -> WindowsReplaceMetadata:
    if os.name != "nt":
        raise WorkspaceMutationBoundaryError("Windows replace metadata requires Windows")
    handle = _handle_value(fd)
    if expected_path is not None:
        actual = _final_path_for_handle(handle)
        wanted = os.path.normcase(os.path.abspath(str(expected_path)))
        if actual != wanted:
            raise WorkspaceMutationBoundaryError(
                "Windows replace target resolved through a filesystem alias"
            )
    sddl, owner, group, dacl, descriptor, control = _security_sddl_and_components(handle)
    try:
        attributes = _file_attributes(handle)
        streams = _stream_names(handle)
        binding = _binding(sddl, attributes, streams)
        return WindowsReplaceMetadata(
            binding=binding,
            sddl=sddl,
            owner=owner,
            group=group,
            dacl=dacl,
            descriptor=descriptor,
            control=control,
        )
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(
            ctypes.c_void_p(descriptor)
        )
        raise


def capture_windows_replace_binding(path: Path) -> dict[str, object]:
    if os.name != "nt":
        raise WorkspaceMutationBoundaryError("Windows replace metadata requires Windows")
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
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    invalid = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    value = ctypes.cast(handle, ctypes.c_void_p).value
    if value is None or value == invalid:
        raise WorkspaceMutationBoundaryError(
            f"Windows replace metadata target cannot be opened (winerror={ctypes.get_last_error()})"
        )
    try:
        fd = msvcrt.open_osfhandle(int(value), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except BaseException:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise
    try:
        metadata = capture_windows_replace_metadata_fd(fd, expected_path=path)
        try:
            return dict(metadata.binding)
        finally:
            metadata.close()
    finally:
        os.close(fd)


def apply_windows_replace_metadata_fd(fd: int, expected: WindowsReplaceMetadata) -> None:
    if os.name != "nt":
        raise WorkspaceMutationBoundaryError("Windows replace metadata requires Windows")
    from ctypes import wintypes

    before = capture_windows_replace_metadata_fd(fd)
    try:
        dacl_already_matches = before.sddl == expected.sddl
        if before.sddl:
            owner_prefix = expected.sddl.split("D:", 1)[0]
            current_prefix = before.sddl.split("D:", 1)[0]
            if owner_prefix != current_prefix:
                raise WorkspaceMutationBoundaryError(
                    "Windows staging owner/group would change during replacement"
                )
    finally:
        before.close()

    handle = _handle_value(fd)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    set_security = advapi32.SetSecurityInfo
    set_security.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_security.restype = wintypes.DWORD
    dacl_flag = (
        _PROTECTED_DACL_SECURITY_INFORMATION
        if expected.control & _SE_DACL_PROTECTED
        else _UNPROTECTED_DACL_SECURITY_INFORMATION
    )
    result = 0
    if not dacl_already_matches:
        result = int(
            set_security(
                wintypes.HANDLE(handle),
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | dacl_flag,
                None,
                None,
                ctypes.c_void_p(expected.dacl),
                None,
            )
        )
    if result != 0:
        raise WorkspaceMutationBoundaryError(
            f"Windows staging DACL cannot preserve target security (winerror={result})"
        )

    class _FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    info = _FILE_BASIC_INFO(0, 0, 0, 0, int(expected.binding["file_attributes"]))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    if not set_info(
        wintypes.HANDLE(handle),
        _FILE_BASIC_INFO_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise WorkspaceMutationBoundaryError(
            "Windows staging attributes cannot preserve target metadata "
            f"(winerror={ctypes.get_last_error()})"
        )

    after = capture_windows_replace_metadata_fd(fd)
    try:
        if after.binding != expected.binding:
            raise WorkspaceMutationBoundaryError(
                "Windows staging security metadata does not match the approved target"
            )
    finally:
        after.close()
