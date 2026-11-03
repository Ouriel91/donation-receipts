"""Own a Windows process tree before its first thread is allowed to run."""

import ctypes
import time
from ctypes import wintypes


_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _BasicLimits(ctypes.Structure):
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
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def _kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
        "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
        "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(api, name)
        function.argtypes = argtypes
        function.restype = restype
    return api


class WindowsJob:
    """Kill-on-close job for a Popen child created with CREATE_SUSPENDED."""

    def __init__(self, pid: int):
        self.api = _kernel()
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise RuntimeError("Windows job creation failed")
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(
                self.handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise RuntimeError("Windows job limits failed")
            process = self.api.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not process:
                raise RuntimeError("Windows process handle failed")
            try:
                if not self.api.AssignProcessToJobObject(self.handle, process):
                    raise RuntimeError("Windows job assignment failed")
            finally:
                self.api.CloseHandle(process)
            self._resume_primary_thread(pid)
        except Exception:
            self.close()
            raise

    def _resume_primary_thread(self, pid: int) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = self.api.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
            if snapshot == _INVALID_HANDLE:
                raise RuntimeError("Windows thread snapshot failed")
            try:
                entry = _ThreadEntry()
                entry.dwSize = ctypes.sizeof(entry)
                found = self.api.Thread32First(snapshot, ctypes.byref(entry))
                while found:
                    if entry.th32OwnerProcessID == pid:
                        thread = self.api.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                        if not thread:
                            raise RuntimeError("Windows thread handle failed")
                        try:
                            if self.api.ResumeThread(thread) != 1:
                                raise RuntimeError("Windows thread resume failed")
                        finally:
                            self.api.CloseHandle(thread)
                        return
                    found = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            finally:
                self.api.CloseHandle(snapshot)
            time.sleep(0.01)
        raise RuntimeError("Windows primary thread was not found")

    def terminate(self) -> None:
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise RuntimeError("Windows process tree termination failed")

    def close(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                raise RuntimeError("Windows job close failed")
