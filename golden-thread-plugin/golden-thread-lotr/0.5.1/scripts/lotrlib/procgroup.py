"""Keep a stdio server's whole process tree under the gateway's control (0.5.0).

A stdio server is often a launcher (npx, uvx, a shell) that starts the real server as a child.
Killing only the launcher left the grandchild running -- and on Windows it survived even a hard
kill of lotrd itself. So each server gets its own tree handle:

  * Windows: a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. The OS kills every process in
    the job when the last handle to it closes, and that includes lotrd dying by TerminateProcess.
  * POSIX: its own session (so its own process group); stopping the server kills the group.

Best effort by design: if the tree handle cannot be made (a job that forbids nesting, an
exotic platform) the server still runs and is stopped the old way.
"""
import os
import signal

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _JobObjectExtendedLimitInformation = 9
    _KILL_ON_JOB_CLOSE = 0x2000
    _ULONG_PTR = ctypes.c_size_t

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", _ULONG_PTR),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL


def popen_kwargs():
    """Extra Popen arguments that give the child its own process group (POSIX only)."""
    return {} if IS_WINDOWS else {"start_new_session": True}


def attach(proc):
    """Put a just-started Popen into its tree handle. -> an opaque handle for kill()/release(),
    or None when none could be made. Never raises."""
    if not IS_WINDOWS:
        return proc.pid
    job = None
    try:
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXTENDED()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
        if not _k32.AssignProcessToJobObject(job, int(proc._handle)):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        return job
    except Exception:                                       # noqa: BLE001 -- best effort
        if job:
            _k32.CloseHandle(job)
        return None


def kill(handle):
    """Kill every process in the tree. Safe on None and on a tree that is already gone."""
    if handle is None:
        return
    if IS_WINDOWS:
        try:
            _k32.TerminateJobObject(handle, 1)
        except Exception:                                   # noqa: BLE001
            pass
        return
    try:
        os.killpg(handle, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def release(handle):
    """Drop the handle (Windows: closing the last handle kills anything left in the job)."""
    if handle is not None and IS_WINDOWS:
        try:
            _k32.CloseHandle(handle)
        except Exception:                                   # noqa: BLE001
            pass
