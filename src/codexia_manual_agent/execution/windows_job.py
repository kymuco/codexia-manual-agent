from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_ERROR_NO_MORE_FILES = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class WindowsJobObject:
    """Own a Windows process tree independently of the root process lifetime."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("WindowsJobObject is available only on Windows")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        self._configure_signatures()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            self._raise_last_error("CreateJobObjectW")
        self._handle = handle

        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            self._handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            try:
                self._raise_last_error("SetInformationJobObject")
            finally:
                self.close()

    def _configure_signatures(self) -> None:
        kernel32 = self._kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        ]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        ]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

    def assign(self, process_handle: int) -> None:
        if self._handle is None:
            raise OSError("Windows job object is closed")
        if not self._kernel32.AssignProcessToJobObject(
            self._handle,
            wintypes.HANDLE(process_handle),
        ):
            self._raise_last_error("AssignProcessToJobObject")

    def resume(self, process_id: int) -> None:
        """Resume a process that was created with CREATE_SUSPENDED."""

        self._resume_suspended_process(process_id)

    def assign_and_resume(self, process_handle: int, process_id: int) -> None:
        """Assign a CREATE_SUSPENDED process to the job, then let it run."""

        self.assign(process_handle)
        self.resume(process_id)

    def _resume_suspended_process(self, process_id: int) -> None:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        snapshot_value = int(snapshot) if snapshot is not None else 0
        if not snapshot or snapshot_value == _INVALID_HANDLE_VALUE:
            self._raise_last_error("CreateToolhelp32Snapshot")

        resumed = 0
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            if not self._kernel32.Thread32First(snapshot, ctypes.byref(entry)):
                self._raise_last_error("Thread32First")
            while True:
                if int(entry.th32OwnerProcessID) == int(process_id):
                    thread = self._kernel32.OpenThread(
                        _THREAD_SUSPEND_RESUME,
                        False,
                        entry.th32ThreadID,
                    )
                    if not thread:
                        self._raise_last_error("OpenThread")
                    try:
                        previous_count = self._kernel32.ResumeThread(thread)
                        if previous_count == 0xFFFFFFFF:
                            self._raise_last_error("ResumeThread")
                        resumed += 1
                    finally:
                        self._kernel32.CloseHandle(thread)

                if self._kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    continue
                error = ctypes.get_last_error()
                if error not in {0, _ERROR_NO_MORE_FILES}:
                    raise ctypes.WinError(error)
                break
        finally:
            self._kernel32.CloseHandle(snapshot)

        if resumed == 0:
            raise OSError(f"No suspended thread found for process {process_id}")

    def active_processes(self) -> int:
        if self._handle is None:
            return 0
        info = _JobObjectBasicAccountingInformation()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            self._raise_last_error("QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle is None:
            return
        if not self._kernel32.TerminateJobObject(self._handle, exit_code):
            error = ctypes.get_last_error()
            if error not in {0, 5}:
                raise ctypes.WinError(error)

    def close(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        self._kernel32.CloseHandle(handle)

    def _raise_last_error(self, operation: str) -> None:
        error = ctypes.get_last_error()
        exc = ctypes.WinError(error)
        raise OSError(error, f"{operation} failed: {exc}")

    def __enter__(self) -> WindowsJobObject:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
