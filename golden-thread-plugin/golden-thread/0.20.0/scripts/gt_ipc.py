#!/usr/bin/env python3
"""gt_ipc -- the local front door gt's unlock authority and gt-lotr share (0.20.0).

One JSON object per line in each direction: a request {"id", "method", "params"} and a reply
{"id", "result"} or {"id", "error": {"code", "message", "hints"}}. A server may also send
{"id", "need": {...}} mid-request -- a question for the CLIENT's own terminal (a TOTP code typed
at a tty when no dialog can be shown); the client answers {"id", "answer": ...}.

WHO IS ON THE OTHER END is never taken from the message. It is read from the kernel when the
connection is accepted and handed to the handler as `conn.peer`:

  macOS    LOCAL_PEERTOKEN (0x006): the audit token -- euid, pid and the pid VERSION, which is
           never reused, so a recycled pid cannot pass for the process that registered
           (LOCAL_PEERCRED gives only the uid). Process start time from proc_pidinfo.
  Linux    SO_PEERCRED: pid, uid; start time from /proc/<pid>/stat (field 22).
  Windows  a named pipe (CPython has no AF_UNIX there): GetNamedPipeClientProcessId, the
           client's creation time from GetProcessTimes, and its user SID from the token while
           impersonating it.

A peer whose uid / SID is not ours is refused before a single byte of its request is read;
one that cannot be identified is refused too (fail closed).

THE WINDOWS PIPE is created with
  * an explicit DACL granting GENERIC_ALL to the user's own SID only ("D:P(A;;GA;;;<sid>)"):
    the default descriptor lets Everyone and anonymous read;
  * PIPE_REJECT_REMOTE_CLIENTS: never reachable from the network;
  * FILE_FLAG_FIRST_PIPE_INSTANCE on the first instance: a pipe someone else created first
    under our name (squatting) makes the server refuse to start rather than share it.
and the CLIENT checks that the server process runs as the same user before it sends anything,
and opens the pipe at SECURITY_IDENTIFICATION so a squatting server could not impersonate it.

Process helpers (start time, parent, executable, liveness) live here too, because binding a
grant to "this exact process" needs the same three platforms answered the same way.

Standard library only (ctypes for the Windows and macOS kernel calls). Python 3.8+.
"""
import json
import os
import socket
import stat
import struct
import sys
import threading
import time

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

MAX_LINE = 1024 * 1024          # one request or reply, either direction


class IpcError(Exception):
    """A transport failure, named. `code` is machine-readable; the message never carries
    request content."""

    def __init__(self, code, message, hints=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = list(hints or [])

    def to_dict(self):
        return {"code": self.code, "message": self.message, "hints": self.hints}


# ============================================================================ process info

_libproc = None


def _mac_libproc():
    global _libproc
    if _libproc is None:
        import ctypes
        import ctypes.util
        _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib",
                               use_errno=True)
    return _libproc


_PROC_PIDTBSDINFO = 3
_BSDINFO_SIZE = 136             # struct proc_bsdinfo, sys/proc_info.h


def _mac_bsdinfo(pid):
    import ctypes
    buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
    n = _mac_libproc().proc_pidinfo(int(pid), _PROC_PIDTBSDINFO, 0, buf, _BSDINFO_SIZE)
    if n != _BSDINFO_SIZE:
        return None
    raw = buf.raw
    # 12 uint32 (flags, status, xstatus, pid, ppid, uid, gid, ruid, rgid, svuid, svgid, rfu),
    # comm[16], name[32], 6 x 32-bit, then start tv_sec / tv_usec as uint64 at offset 120.
    # comm[16] at 48, name[32] at 64, then nfiles, pgid, pjobc, e_tdev (108), e_tpgid, nice.
    ppid, uid = struct.unpack_from("=II", raw, 16)
    comm = raw[48:64].split(b"\0", 1)[0].decode("utf-8", "replace")
    tdev, = struct.unpack_from("=I", raw, 108)
    sec, usec = struct.unpack_from("=QQ", raw, 120)
    return {"ppid": ppid, "uid": uid, "comm": comm, "start": "%d.%06d" % (sec, usec),
            "tty": tdev not in (0xFFFFFFFF, 0)}


def _linux_stat(pid):
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    # comm is in parentheses and may itself contain spaces or ")": split at the LAST ")".
    lp, rp = data.find("("), data.rfind(")")
    if lp < 0 or rp < 0:
        return None
    rest = data[rp + 2:].split()
    try:
        # state ppid pgrp session tty_nr ...: tty_nr 0 = no controlling terminal
        return {"ppid": int(rest[1]), "comm": data[lp + 1:rp], "start": rest[19],
                "tty": int(rest[4]) != 0}
    except (IndexError, ValueError):
        return None


# -- Windows kernel32 / advapi32 -----------------------------------------------------------

_win = None


def _w():
    """Lazily bound Windows APIs. Every handle-returning call is typed so a 64-bit handle is
    never truncated to a C int."""
    global _win
    if _win is not None:
        return _win
    import ctypes
    from ctypes import wintypes as wt

    class _W:
        pass
    w = _W()
    w.ctypes, w.wt = ctypes, wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    w.k32, w.adv = k32, adv
    H = wt.HANDLE

    def bind(lib, name, res, *args):
        fn = getattr(lib, name)
        fn.restype = res
        fn.argtypes = list(args)
        setattr(w, name, fn)
    bind(k32, "OpenProcess", H, wt.DWORD, wt.BOOL, wt.DWORD)
    bind(k32, "CloseHandle", wt.BOOL, H)
    bind(k32, "GetProcessTimes", wt.BOOL, H, ctypes.c_void_p, ctypes.c_void_p,
         ctypes.c_void_p, ctypes.c_void_p)
    bind(k32, "GetExitCodeProcess", wt.BOOL, H, ctypes.POINTER(wt.DWORD))
    bind(k32, "QueryFullProcessImageNameW", wt.BOOL, H, wt.DWORD, wt.LPWSTR,
         ctypes.POINTER(wt.DWORD))
    bind(k32, "CreateToolhelp32Snapshot", H, wt.DWORD, wt.DWORD)
    bind(k32, "Process32FirstW", wt.BOOL, H, ctypes.c_void_p)
    bind(k32, "Process32NextW", wt.BOOL, H, ctypes.c_void_p)
    bind(k32, "CreateNamedPipeW", H, wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
         wt.DWORD, wt.DWORD, ctypes.c_void_p)
    bind(k32, "ConnectNamedPipe", wt.BOOL, H, ctypes.c_void_p)
    bind(k32, "DisconnectNamedPipe", wt.BOOL, H)
    bind(k32, "CreateFileW", H, wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD,
         wt.DWORD, H)
    bind(k32, "ReadFile", wt.BOOL, H, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
         ctypes.c_void_p)
    bind(k32, "WriteFile", wt.BOOL, H, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
         ctypes.c_void_p)
    bind(k32, "FlushFileBuffers", wt.BOOL, H)
    bind(k32, "WaitNamedPipeW", wt.BOOL, wt.LPCWSTR, wt.DWORD)
    bind(k32, "GetNamedPipeClientProcessId", wt.BOOL, H, ctypes.POINTER(wt.ULONG))
    bind(k32, "GetNamedPipeServerProcessId", wt.BOOL, H, ctypes.POINTER(wt.ULONG))
    bind(k32, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
    bind(k32, "GetCurrentProcess", H)
    bind(k32, "GetCurrentThread", H)
    bind(adv, "OpenProcessToken", wt.BOOL, H, wt.DWORD, ctypes.POINTER(H))
    bind(adv, "OpenThreadToken", wt.BOOL, H, wt.DWORD, wt.BOOL, ctypes.POINTER(H))
    bind(adv, "GetTokenInformation", wt.BOOL, H, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
         ctypes.POINTER(wt.DWORD))
    bind(adv, "ConvertSidToStringSidW", wt.BOOL, ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR))
    bind(adv, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wt.BOOL, wt.LPCWSTR,
         wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.ULONG))
    bind(adv, "ImpersonateNamedPipeClient", wt.BOOL, H)
    bind(adv, "RevertToSelf", wt.BOOL)
    w.INVALID = ctypes.c_void_p(-1).value
    _win = w
    return w


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TokenUser = 1


def _win_sid_of_token(w, token):
    ctypes = w.ctypes
    need = w.wt.DWORD(0)
    w.GetTokenInformation(token, _TokenUser, None, 0, ctypes.byref(need))
    if not need.value:
        return None
    buf = ctypes.create_string_buffer(need.value)
    if not w.GetTokenInformation(token, _TokenUser, buf, need, ctypes.byref(need)):
        return None
    psid = ctypes.c_void_p.from_buffer(buf).value      # TOKEN_USER.User.Sid is the first field
    out = w.wt.LPWSTR()
    if not w.ConvertSidToStringSidW(psid, ctypes.byref(out)):
        return None
    try:
        return out.value
    finally:
        w.LocalFree(ctypes.cast(out, ctypes.c_void_p))


def _win_process_sid(pid):
    w = _w()
    h = w.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        tok = w.wt.HANDLE()
        if not w.OpenProcessToken(h, _TOKEN_QUERY, w.ctypes.byref(tok)):
            return None
        try:
            return _win_sid_of_token(w, tok)
        finally:
            w.CloseHandle(tok)
    finally:
        w.CloseHandle(h)


_own_sid = None


def own_sid():
    """The current user's SID string (Windows). Cached: it cannot change for this process."""
    global _own_sid
    if _own_sid is None:
        w = _w()
        tok = w.wt.HANDLE()
        if not w.OpenProcessToken(w.GetCurrentProcess(), _TOKEN_QUERY, w.ctypes.byref(tok)):
            raise IpcError("no_identity", "cannot read this process's own token")
        try:
            _own_sid = _win_sid_of_token(w, tok)
        finally:
            w.CloseHandle(tok)
        if not _own_sid:
            raise IpcError("no_identity", "cannot read this process's own user SID")
    return _own_sid


def _win_times(pid):
    w = _w()
    h = w.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        ft = (w.ctypes.c_ulonglong * 4)()
        if not w.GetProcessTimes(h, w.ctypes.byref(ft, 0), w.ctypes.byref(ft, 8),
                                 w.ctypes.byref(ft, 16), w.ctypes.byref(ft, 24)):
            return None
        code = w.wt.DWORD(0)
        alive = bool(w.GetExitCodeProcess(h, w.ctypes.byref(code))) and code.value == 259
        return {"start": str(ft[0]), "alive": alive}
    finally:
        w.CloseHandle(h)


def _win_parent(pid):
    """Parent pid from a Toolhelp snapshot. A Windows parent pid is only a number recorded at
    creation and may since have been reused, so a caller that trusts it must also check that
    the parent STARTED BEFORE the child (see ancestry())."""
    w = _w()
    ctypes, wt = w.ctypes, w.wt

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    snap = w.CreateToolhelp32Snapshot(0x2, 0)           # TH32CS_SNAPPROCESS
    if not snap or snap == w.INVALID:
        return None
    try:
        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        ok = w.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.th32ProcessID == int(pid):
                return {"ppid": pe.th32ParentProcessID, "comm": pe.szExeFile}
            ok = w.Process32NextW(snap, ctypes.byref(pe))
    finally:
        w.CloseHandle(snap)
    return None


def process_info(pid):
    """{pid, ppid, start, comm, tty} for a live process, or None when it is gone or unreadable.
    `start` is an opaque string that changes when a pid is reused: compare it, never parse it.
    `tty` is True when the process has a controlling terminal, False when it has none, None
    where the platform cannot say (Windows)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if IS_MAC:
        b = _mac_bsdinfo(pid)
        if not b:
            return None
        return {"pid": pid, "ppid": b["ppid"], "start": b["start"], "comm": b["comm"],
                "tty": b["tty"]}
    if IS_LINUX:
        s = _linux_stat(pid)
        if not s:
            return None
        return {"pid": pid, "ppid": s["ppid"], "start": s["start"], "comm": s["comm"],
                "tty": s["tty"]}
    if IS_WINDOWS:
        t = _win_times(pid)
        if not t or not t["alive"]:
            return None
        p = _win_parent(pid) or {}
        return {"pid": pid, "ppid": p.get("ppid"), "start": t["start"],
                "comm": p.get("comm") or "", "tty": None}
    return None


def process_path(pid):
    """The executable's full path, or "" when it cannot be read."""
    try:
        pid = int(pid)
        if IS_MAC:
            import ctypes
            buf = ctypes.create_string_buffer(4096)
            n = _mac_libproc().proc_pidpath(pid, buf, 4096)
            return buf.value.decode("utf-8", "replace") if n > 0 else ""
        if IS_LINUX:
            return os.readlink("/proc/%d/exe" % pid)
        if IS_WINDOWS:
            w = _w()
            h = w.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return ""
            try:
                buf = w.ctypes.create_unicode_buffer(1024)
                size = w.wt.DWORD(1024)
                if w.QueryFullProcessImageNameW(h, 0, buf, w.ctypes.byref(size)):
                    return buf.value
            finally:
                w.CloseHandle(h)
    except (OSError, ValueError, AttributeError):
        pass
    return ""


def process_args(pid):
    """argv of a process as a list, best effort ([] when unreadable). Used to NAME a requester
    in a prompt, and as one narrow consumer check (gt_unlockd.is_lotr_daemon: which FILE a
    python process is running as __main__) -- never as proof of who started it."""
    try:
        pid = int(pid)
        if IS_LINUX:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        if IS_MAC:
            got = _mac_procargs(pid)
            return got[0] if got else []
        if IS_WINDOWS:
            return _win_args(pid)
    except (OSError, ValueError, AttributeError, struct.error):
        pass
    return []


def _mac_procargs(pid):
    """(argv, env NAMES) of a process from KERN_PROCARGS2, or None. Environment VALUES are
    dropped here and never leave this function: only the names are returned."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, int(pid))           # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    raw = buf.raw[:size.value]
    argc = struct.unpack_from("=i", raw, 0)[0]
    rest = raw[4:]
    # exec path, then NUL padding, then argc NUL-terminated strings, then the environment
    end = rest.find(b"\0")
    rest = rest[end:].lstrip(b"\0")
    parts = rest.split(b"\0")
    args = [p.decode("utf-8", "replace") for p in parts[:argc]]
    names = set()
    for p in parts[argc:]:
        if not p:
            break
        if b"=" in p:
            names.add(p.split(b"=", 1)[0].decode("utf-8", "replace"))
    return args, names


def process_env_names(pid):
    """The NAMES of a process's environment variables (a set), or None where they cannot be
    read: macOS KERN_PROCARGS2 (the initial environment), Linux /proc/<pid>/environ. Windows:
    None -- reading another process's environment needs its PEB, which gt does not do (best
    effort; the -I check in isolation_problem() covers it). Values are never returned."""
    try:
        pid = int(pid)
        if IS_LINUX:
            with open("/proc/%d/environ" % pid, "rb") as f:
                return {e.split(b"=", 1)[0].decode("utf-8", "replace")
                        for e in f.read().split(b"\0") if b"=" in e}
        if IS_MAC:
            got = _mac_procargs(pid)
            return got[1] if got else None
    except (OSError, ValueError, AttributeError, struct.error):
        pass
    return None


# Environment variables that make a python interpreter run code that is not the script it was
# asked to run (review L1, 2026-10-04: PYTHONPATH=<dir with sitecustomize.py> ran arbitrary code
# under the realpath identity of the INSTALLED lotr_mcp.py). -I (isolated mode) ignores every
# PYTHON* variable and the user site-packages; -E ignores the variables, -s the user site.
PYTHON_INJECTION_VARS = ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE",
                         "PYTHONINSPECT", "PYTHONEXECUTABLE")


def python_flags(args):
    """The single-letter interpreter options before the script in a python argv
    (["python3", "-IB", "x.py"] -> {"I", "B"}). Options that take a value (-X, -W) consume it;
    -c / -m end the options."""
    flags = set()
    i = 1
    while i < len(args):
        a = args[i]
        if a in ("-", "--") or not a.startswith("-") or a.startswith("--"):
            break
        j = 1
        while j < len(a):
            ch = a[j]
            if ch in "XWQ":
                if j == len(a) - 1:
                    i += 1                              # the value is the next argument
                break
            flags.add(ch)
            if ch in "cm":
                return flags
            j += 1
        i += 1
    return flags


def isolation_problem(pid):
    """None when the python process `pid` was started isolated -- with -I, or with both -E and
    -s -- so no PYTHON* variable and no user site-packages could have run code before its
    script; else the reason (naming variables, never their values). A process whose argv cannot
    be read is a problem too (fail closed)."""
    args = process_args(pid)
    if not args:
        return "its command line could not be read"
    flags = python_flags(args)
    if "I" in flags or ("E" in flags and "s" in flags):
        return None
    names = process_env_names(pid) or set()
    bad = sorted(n for n in names if n in PYTHON_INJECTION_VARS)
    if bad:
        return ("it runs with %s in its environment and without -I, so code other than its "
                "script may have run" % ", ".join(bad))
    return "it was not started with python -I (isolated mode)"


# Shells, for telling a person's terminal from an orphan (review H2). A LOGIN shell is what
# login(1), sshd, Terminal / iTerm2 and tmux start: argv[0] begins with "-", or -l / --login.
SHELL_NAMES = ("sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "tcsh", "csh", "nu", "xonsh")
# Windows has no login shell: the interactive shells and terminal hosts a person types into.
WINDOWS_SHELLS = ("cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
                  "openconsole.exe", "conhost.exe", "explorer.exe", "bash.exe", "mintty.exe")


def is_login_shell(args):
    if not args:
        return False
    a0 = args[0]
    base = os.path.basename(a0.lstrip("-")).lower()
    if a0.startswith("-") and base in SHELL_NAMES:
        return True
    if base not in SHELL_NAMES:
        return False
    for a in args[1:]:
        if a == "--login":
            return True
        if a.startswith("-") and not a.startswith("--") and "l" in a[1:] \
                and a[1:].isalpha():
            return True
        if not a.startswith("-"):
            break
    return False


def interactive_chain(chain, args_of=None, windows=None):
    """Is `chain` (ancestry(): the process first, then its parents) a person's terminal --
    rather than an ORPHAN, a process re-parented to init/launchd (or, on Windows, whose
    parent is gone or was started after it) with no login shell above it? Review H2: an orphan
    that setsid()s and takes a controlling tty with openpty + TIOCSCTTY looked like a terminal.
    POSIX: some process in the chain is a login shell. Windows: the chain has a parent at all,
    and some ancestor is an interactive shell or terminal host (ancestry() already stops at a
    parent created after its child, so a reused pid ends the chain)."""
    windows = IS_WINDOWS if windows is None else windows
    args_of = args_of or process_args
    if not chain:
        return False
    if windows:
        if len(chain) < 2:
            return False
        return any(os.path.basename((i.get("comm") or "").replace("\\", "/")).lower()
                   in WINDOWS_SHELLS for i in chain[1:])
    return any(is_login_shell(args_of(i["pid"])) for i in chain)


_win_args_cache = {}


def _win_cmdline_nt(pid):
    """The command line of `pid` from the kernel (NtQueryInformationProcess, class 60
    ProcessCommandLineInformation, Windows 8.1+), or None. Fast: no child process."""
    try:
        import ctypes
        from ctypes import wintypes as wt
        w = _w()
        nt = ctypes.WinDLL("ntdll")
        nt.NtQueryInformationProcess.restype = ctypes.c_long
        nt.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                 wt.ULONG, ctypes.POINTER(wt.ULONG)]
        h = w.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            size = wt.ULONG(0)
            nt.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))
            if not size.value or size.value > 1 << 20:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if nt.NtQueryInformationProcess(h, 60, buf, size, ctypes.byref(size)) != 0:
                return None

            class US(ctypes.Structure):
                _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT),
                            ("Buffer", ctypes.c_void_p)]
            us = US.from_buffer(buf)
            if not us.Buffer or not us.Length:
                return None
            return ctypes.wstring_at(us.Buffer, us.Length // 2)
        finally:
            w.CloseHandle(h)
    except (OSError, AttributeError, ValueError):
        return None


def _win_args(pid):
    """argv of a Windows process: its command line from the kernel (NtQueryInformationProcess),
    else from WMI (PowerShell, ~1 s), cached per pid + start time, split by the shell's own
    CommandLineToArgvW."""
    info = process_info(pid)
    if not info:
        return []
    key = (pid, info["start"])
    if key in _win_args_cache:
        return _win_args_cache[key]
    line = _win_cmdline_nt(pid)
    if line:
        out = _split_win(line)
        if out and process_info(pid) and process_info(pid)["start"] == info["start"]:
            _win_args_cache[key] = out
        return out
    import subprocess
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    ps = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command",
                            "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine"
                            % int(pid)], capture_output=True, text=True, timeout=30,
                           creationflags=0x08000000)
        line = (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return []
    if not line:
        return []
    out = _split_win(line)
    if out and process_info(pid) and process_info(pid)["start"] == info["start"]:
        _win_args_cache[key] = out
    return out


def _split_win(line):
    import ctypes
    from ctypes import wintypes as wt
    sh = ctypes.WinDLL("shell32", use_last_error=True)
    sh.CommandLineToArgvW.restype = ctypes.POINTER(wt.LPWSTR)
    sh.CommandLineToArgvW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    n = ctypes.c_int(0)
    arr = sh.CommandLineToArgvW(line, ctypes.byref(n))
    if not arr:
        return []
    try:
        return [arr[i] for i in range(n.value)]
    finally:
        ctypes.WinDLL("kernel32").LocalFree(ctypes.cast(arr, ctypes.c_void_p))


_PY_OPTS_WITH_VALUE = ("-X", "-W", "-Q")


def main_script(pid):
    """The ABSOLUTE path of the file a python process `pid` runs as __main__, or None: None for
    `python -c`, `python -m`, `python -` (stdin), an interpreter with no script, or a process
    whose argv cannot be read. The path is resolved against the process's own working directory
    only when it is already absolute; a relative script path gives None (it cannot be pinned).

    Used to tell gt's own processes from look-alikes BY WHICH FILE THEY RUN (gt_unlockd's
    consumer / shim / server checks): the caller compares os.path.realpath() of this with the
    realpath of the installed file. A same-user process can still run the REAL file with its
    own arguments or environment -- that residual is documented (SECURITY.md, L1/L2)."""
    args = process_args(pid)
    i = 1
    while i < len(args):
        a = args[i]
        if a in ("-c", "-m", "-"):
            return None
        if a in _PY_OPTS_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        if not os.path.isabs(a):
            return None
        return a
    return None


def peer_of(client):
    """{pid, start} of the process SERVING a connected Client, from the kernel -- macOS
    LOCAL_PEERTOKEN, Linux SO_PEERCRED (both report the listener's process), Windows
    GetNamedPipeServerProcessId -- or None when it cannot be told. A client that must know it
    reached the real server (gt_unlock_client) refuses None."""
    conn = getattr(client, "_conn", None)
    try:
        if IS_WINDOWS:
            spid = getattr(conn, "server_pid", None)
            info = process_info(spid) if spid else None
            return {"pid": int(spid), "start": info["start"]} if info else None
        sock = getattr(conn, "sock", None)
        if sock is None:
            return None
        p = unix_peer(sock)
        return {"pid": p["pid"], "start": p["start"]} if p else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def alive(pid, start):
    """True while the process `pid` that started at `start` is still running."""
    info = process_info(pid)
    return bool(info) and info["start"] == start


def ancestry(pid, limit=12):
    """[process_info(pid), its parent, ...] up to `limit` levels, stopping at pid 0/1 or a
    parent that started AFTER its child (a reused pid on Windows, or a re-parented orphan)."""
    out = []
    info = process_info(pid)
    while info and len(out) < limit:
        out.append(info)
        ppid = info.get("ppid")
        if not ppid or ppid in (0, 1) or ppid == info["pid"]:
            break
        parent = process_info(ppid)
        if not parent:
            break
        if IS_WINDOWS:
            try:
                if int(parent["start"]) > int(info["start"]):
                    break
            except ValueError:
                break
        info = parent
    return out


# ============================================================================ peer identity

_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001
_LOCAL_PEERPID = 0x002
_LOCAL_PEERTOKEN = 0x006
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)


def unix_peer(sock):
    """{pid, uid, start, pidversion?} of the process on the other end of a unix socket, from
    the kernel. None when the platform or the call cannot say (the caller then refuses)."""
    try:
        if IS_MAC:
            raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERTOKEN, 32)
            vals = struct.unpack("=8I", raw[:32])
            # audit_token_t: [0] auid [1] euid [2] egid [3] ruid [4] rgid [5] pid [6] asid
            # [7] pidversion
            pid, uid, pidversion = vals[5], vals[1], vals[7]
            info = process_info(pid)
            if not info:
                return None
            return {"pid": pid, "uid": uid, "start": info["start"], "pidversion": pidversion}
        if IS_LINUX:
            raw = sock.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, _gid = struct.unpack("3i", raw)
            info = process_info(pid)
            if not info:
                return None
            return {"pid": pid, "uid": uid, "start": info["start"]}
    except (OSError, struct.error):
        return None
    return None


# ============================================================================ addresses

def default_address(home, name):
    """Where a server named `name` listens for this user: <home>/<name>.sock on POSIX, a named
    pipe whose name carries a hash of the user's SID and of `home` on Windows (so two homes --
    a test's and the real one -- never share a pipe)."""
    if IS_WINDOWS:
        import hashlib
        tag = hashlib.sha256((own_sid() + "|" + os.path.abspath(str(home))).encode("utf-8"))
        return r"\\.\pipe\gt-%s-%s" % (name, tag.hexdigest()[:16])
    path = os.path.join(str(home), name + ".sock")
    if len(path.encode("utf-8")) <= _SUN_PATH_MAX:
        return path
    # A socket path is capped at 104 bytes (macOS) / 108 (Linux), and a home deep in a temp
    # tree passes that. Fall back to a short PRIVATE directory of this user's under /tmp,
    # named from the home so two homes never share it; serve() still refuses it unless it
    # is ours and mode 700.
    import hashlib
    tag = hashlib.sha256(os.path.abspath(str(home)).encode("utf-8")).hexdigest()[:12]
    return os.path.join("/tmp", "gt-%d-%s" % (os.getuid(), tag), name + ".sock")


_SUN_PATH_MAX = 100


def check_private_dir(path):
    """Refuse a socket directory anyone but its owner can enter, list or write (POSIX)."""
    if IS_WINDOWS:
        return
    st = os.stat(path)
    if st.st_uid != os.getuid():
        raise IpcError("insecure_dir", "%s is not owned by this user" % path)
    if st.st_mode & 0o077:
        raise IpcError("insecure_dir", "%s is mode %s; must be 700"
                       % (path, oct(st.st_mode & 0o777)), hints=["chmod 700 %s" % path])


# ============================================================================ connections

class _Conn:
    """One accepted connection: line-oriented JSON over a socket or a pipe handle."""

    def __init__(self, peer):
        self.peer = peer
        self._buf = b""

    def readline(self):
        while b"\n" not in self._buf:
            chunk = self._recv()
            if not chunk:
                line, self._buf = self._buf, b""
                return line
            self._buf += chunk
            if len(self._buf) > MAX_LINE + 1:
                raise IpcError("too_large", "message over 1 MB")
        line, self._buf = self._buf.split(b"\n", 1)
        return line + b"\n"

    def recv_obj(self):
        line = self.readline()
        if not line:
            return None
        if len(line) > MAX_LINE:
            raise IpcError("too_large", "message over 1 MB")
        obj = json.loads(line.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("not an object")
        return obj

    def send_obj(self, obj):
        self._send((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))

    def ask(self, rid, need):
        """Ask the client a question mid-request (its own terminal answers). -> the answer, or
        None when the client has none (it is not a terminal) or goes away."""
        self.send_obj({"id": rid, "need": need})
        reply = self.recv_obj()
        if not isinstance(reply, dict) or reply.get("id") != rid:
            return None
        return reply.get("answer")


class _SockConn(_Conn):
    def __init__(self, sock, peer):
        super().__init__(peer)
        self.sock = sock

    def _recv(self):
        return self.sock.recv(65536)

    def _send(self, data):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class _PipeConn(_Conn):
    def __init__(self, handle, peer):
        super().__init__(peer)
        self.h = handle

    def _recv(self):
        w = _w()
        buf = w.ctypes.create_string_buffer(65536)
        n = w.wt.DWORD(0)
        if not w.ReadFile(self.h, buf, 65536, w.ctypes.byref(n), None):
            err = w.ctypes.get_last_error()
            if err == 234:                  # ERROR_MORE_DATA: a message pipe; byte mode never
                return buf.raw[:n.value]
            return b""                      # broken pipe / closed: end of stream
        return buf.raw[:n.value]

    def _send(self, data):
        w = _w()
        n = w.wt.DWORD(0)
        view = memoryview(data)
        while view:
            chunk = bytes(view[:65536])
            if not w.WriteFile(self.h, chunk, len(chunk), w.ctypes.byref(n), None):
                raise IpcError("broken_pipe", "the other end went away")
            view = view[n.value:]

    def close(self):
        w = _w()
        try:
            w.FlushFileBuffers(self.h)
            w.DisconnectNamedPipe(self.h)
        finally:
            w.CloseHandle(self.h)


# ============================================================================ server

def _serve_conn(conn, handler):
    try:
        handler(conn)
    except Exception:                               # noqa: BLE001 - a server keeps serving
        pass
    finally:
        conn.close()


def refuse(conn, message="this door serves only its owner"):
    try:
        conn.send_obj({"id": None, "error": {"code": "peer_refused", "message": message,
                                             "hints": []}})
    except (OSError, IpcError):
        pass


def serve(address, handler, *, stop_event, on_ready=None):
    """Serve `address` until `stop_event` is set. `handler(conn)` runs on its own thread per
    connection with `conn.peer` already established and checked (same user); it reads with
    conn.recv_obj() and replies with conn.send_obj()."""
    if IS_WINDOWS:
        return _serve_pipe(address, handler, stop_event, on_ready)
    return _serve_unix(address, handler, stop_event, on_ready)


def alive_at(address, timeout=1.0):
    """True when something ACCEPTS a connection at `address`. Any listener counts -- a server
    must never unlink a socket another live process is serving, whatever protocol it speaks."""
    try:
        c = connect(address, timeout=timeout)
    except IpcError:
        return False
    c.close()
    return True


def _serve_unix(path, handler, stop_event, on_ready):
    if os.path.lexists(path):
        if alive_at(path):
            raise IpcError("already_running", "a server already answers on %s" % path)
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise IpcError("path_taken", "%s exists and is not a socket" % path)
        os.unlink(path)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    check_private_dir(d)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)                           # 600 from the first instant
    try:
        srv.bind(path)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    ino = os.stat(path).st_ino
    srv.listen(16)
    srv.settimeout(0.2)
    me = os.getuid()
    if on_ready:
        on_ready(path)
    try:
        while not stop_event.is_set():
            try:
                s, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                if stop_event.is_set():
                    break
                continue
            s.settimeout(None)
            peer = unix_peer(s)
            conn = _SockConn(s, peer)
            if not peer or peer.get("uid") != me:
                refuse(conn)
                conn.close()
                continue
            threading.Thread(target=_serve_conn, args=(conn, handler), daemon=True).start()
    finally:
        srv.close()
        try:
            if os.stat(path).st_ino == ino:
                os.unlink(path)
        except OSError:
            pass


_PIPE_ACCESS_DUPLEX = 0x3
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_REJECT_REMOTE_CLIENTS = 0x8
_PIPE_UNLIMITED = 255
_ERROR_PIPE_CONNECTED = 535


def pipe_sddl():
    """The only descriptor the pipe is created with: the user's SID, full access, protected
    (no inherited ACEs). Exposed so a test can assert exactly this."""
    return "D:P(A;;GA;;;%s)" % own_sid()


def _security_attributes(w):
    ctypes, wt = w.ctypes, w.wt
    psd = ctypes.c_void_p()
    if not w.ConvertStringSecurityDescriptorToSecurityDescriptorW(pipe_sddl(), 1,
                                                                   ctypes.byref(psd), None):
        raise IpcError("pipe_acl", "could not build the pipe's security descriptor")

    class SA(ctypes.Structure):
        _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wt.BOOL)]
    sa = SA(ctypes.sizeof(SA), psd, False)
    return sa, psd


def create_pipe_instance(name, first):
    """One server instance of `name`; `first` adds FILE_FLAG_FIRST_PIPE_INSTANCE, so creation
    FAILS if anyone already holds the name (a squatter). Returns the handle."""
    w = _w()
    sa, psd = _security_attributes(w)
    try:
        flags = _PIPE_ACCESS_DUPLEX | (_FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
        h = w.CreateNamedPipeW(name, flags, _PIPE_REJECT_REMOTE_CLIENTS, _PIPE_UNLIMITED,
                               65536, 65536, 0, w.ctypes.byref(sa))
    finally:
        w.LocalFree(psd)
    if not h or h == w.INVALID:
        err = w.ctypes.get_last_error()
        if first and err in (5, 231):           # ACCESS_DENIED / PIPE_BUSY: the name is held
            raise IpcError("pipe_squatted", "the pipe %s already exists: another process "
                           "holds this name, so this server refuses to share it" % name)
        raise IpcError("pipe_failed", "CreateNamedPipe failed (error %d)" % err)
    return h


def _pipe_peer(h):
    """{pid, sid, start} of the client of pipe handle `h`, or None."""
    w = _w()
    ctypes = w.ctypes
    pid = w.wt.ULONG(0)
    if not w.GetNamedPipeClientProcessId(h, ctypes.byref(pid)):
        return None
    sid = None
    # The designed route: impersonate the client and read the THREAD token's user. A client
    # that opened the pipe at SECURITY_IDENTIFICATION still lets the server read its token.
    if w.ImpersonateNamedPipeClient(h):
        try:
            tok = w.wt.HANDLE()
            if w.OpenThreadToken(w.GetCurrentThread(), _TOKEN_QUERY, True, ctypes.byref(tok)):
                try:
                    sid = _win_sid_of_token(w, tok)
                finally:
                    w.CloseHandle(tok)
        finally:
            w.RevertToSelf()
    if sid is None:
        sid = _win_process_sid(pid.value)
    t = _win_times(pid.value)
    if not sid or not t:
        return None
    return {"pid": int(pid.value), "sid": sid, "start": t["start"]}


def _serve_pipe(name, handler, stop_event, on_ready):
    w = _w()
    me = own_sid()
    h = create_pipe_instance(name, first=True)
    if on_ready:
        on_ready(name)

    def poke():
        # ConnectNamedPipe blocks; a stop is delivered by connecting once ourselves.
        stop_event.wait()
        try:
            c = w.CreateFileW(name, 0xC0000000, 0, None, 3, 0, None)
            if c and c != w.INVALID:
                w.CloseHandle(c)
        except OSError:
            pass
    threading.Thread(target=poke, daemon=True).start()
    while True:
        ok = w.ConnectNamedPipe(h, None)
        if not ok and w.ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
            w.CloseHandle(h)
            if stop_event.is_set():
                return
            h = create_pipe_instance(name, first=False)
            continue
        if stop_event.is_set():
            w.DisconnectNamedPipe(h)
            w.CloseHandle(h)
            return
        nxt = create_pipe_instance(name, first=False)   # the next caller's instance, now
        peer = _pipe_peer(h)
        conn = _PipeConn(h, peer)
        if not peer or peer.get("sid") != me:
            refuse(conn)
            conn.close()
        else:
            threading.Thread(target=_serve_conn, args=(conn, handler), daemon=True).start()
        h = nxt


# ============================================================================ client

class Client:
    """A connection to a server: call(method, params) -> result, raising IpcError for an
    error reply. `answer` (optional) answers a server's mid-request question."""

    def __init__(self, conn, answer=None):
        self._conn = conn
        self._id = 0
        self.answer = answer

    def call(self, method, params=None):
        self._id += 1
        rid = self._id
        self._conn.send_obj({"id": rid, "method": method, "params": params or {}})
        while True:
            try:
                reply = self._conn.recv_obj()
            except ValueError:
                raise IpcError("bad_reply", "the server sent something that is not JSON")
            if reply is None:
                raise IpcError("unreachable", "the server closed the connection")
            if "need" in reply:
                ans = self.answer(reply["need"]) if self.answer else None
                self._conn.send_obj({"id": reply.get("id"), "answer": ans})
                continue
            if reply.get("id") not in (rid, None):
                continue
            if "error" in reply:
                e = reply.get("error") or {}
                raise IpcError(str(e.get("code") or "error"), str(e.get("message") or ""),
                               e.get("hints") or [])
            return reply.get("result")

    def close(self):
        self._conn.close()


def connect(address, timeout=5.0, answer=None):
    """Open a client connection to `address` (socket path or pipe name)."""
    if IS_WINDOWS:
        return Client(_connect_pipe(address, timeout), answer)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(address))
    except (OSError, socket.timeout):
        s.close()
        raise IpcError("unreachable", "nothing answering on %s" % address)
    s.settimeout(None if timeout is None else max(timeout, 300.0))
    return Client(_SockConn(s, None), answer)


_SECURITY_SQOS_PRESENT = 0x00100000
_SECURITY_IDENTIFICATION = 0x00010000


def _connect_pipe(name, timeout):
    w = _w()
    deadline = time.monotonic() + (timeout or 5.0)
    while True:
        h = w.CreateFileW(name, 0xC0000000, 0, None, 3,
                          _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION, None)
        if h and h != w.INVALID:
            break
        err = w.ctypes.get_last_error()
        if err == 231 and time.monotonic() < deadline:        # ERROR_PIPE_BUSY
            w.WaitNamedPipeW(name, 200)
            continue
        raise IpcError("unreachable", "nothing answering on %s" % name)
    # The server must run as US before a byte is sent: a pipe name is first-come, and a
    # server another user created would otherwise receive whatever we say.
    spid = w.wt.ULONG(0)
    if not w.GetNamedPipeServerProcessId(h, w.ctypes.byref(spid)) \
            or _win_process_sid(spid.value) != own_sid():
        w.CloseHandle(h)
        raise IpcError("server_refused", "the process serving %s does not run as this user"
                       % name)
    return _ClientPipe(h, int(spid.value))


class _ClientPipe(_PipeConn):
    def __init__(self, h, server_pid=None):
        super().__init__(h, None)
        self.server_pid = server_pid

    def close(self):
        _w().CloseHandle(self.h)
