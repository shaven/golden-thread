#!/usr/bin/env python3
"""gt_sandbox_launch.py -- the one launcher for code that must not touch what it should not (0.22.0).

    run(argv, stdin=b"", timeout=10.0, read_paths=(), max_out=1048576, backend=None,
        write_paths=(), env=None, shell=False, cwd=None) -> dict

Starts `argv` with an EMPTY environment, `stdin` on its input, its output capped and a time limit,
and returns {rc, out, err, backend, isolated, timed_out, truncated}. It never raises.

BACKENDS, tried in this order, and the result always says which one ran:

  seatbelt  macOS `sandbox-exec` with a deny-by-default profile. MEASURED 2026-10-18 (Darwin 25.6.0):
            no writes, no network, no reads of the home folder, no view of other processes' command
            lines (the kern.proc sysctls are denied -- fencing only files and network left 652 of 991
            readable), and no Mach service lookups (the keychain daemon is refused). `sandbox-exec` is
            deprecated, so when it cannot apply a profile (a nested sandbox refuses it) the run falls
            back to `none` and says so.
  bwrap     Linux `bwrap` (bubblewrap) in new user, network, pid, ipc and uts namespaces: the whole file
            system read-only, the home folder (and /home, /root) replaced by an empty read-only tmpfs, a
            private /proc that shows only the child (the 2026-09-15 process-listing finding), no network,
            an empty environment. Offered only when `bwrap` exists AND a self-test shows this kernel lets
            it build those namespaces (many containers and hardened hosts refuse); otherwise `none`.
  appcontainer  Windows: the child runs in an AppContainer (no capabilities, so no network and no
            access to the user's files or any folder not shared with all packages) inside a Job Object
            (kill on close, one process, 512 MB). MEASURED 2026-10-19 (Windows 11 22621): writes, the
            home folder, the network and child processes are refused; the environment is SYSTEMROOT
            and LOCALAPPDATA only (Windows will not start an AppContainer without LOCALAPPDATA, a
            path and no secret). The container cannot read the user's folders, so a read path under
            the profile is COPIED (its top-level .py files) to a staging folder that only the container's
            identity may read, and the command line is rewritten to it; the user's own files are never
            re-permissioned. The interpreter must sit where an AppContainer can read it (Program Files,
            not a per-user install): a self-test decides, and a failure means `none`. NOT measured: whether
            native code in the container could read other processes' command lines (Python's ctypes will
            not load there, and tasklist and PowerShell do not start; wmic printed nothing).
  none      the command runs in a clean environment and nothing more. isolated=False.

WRITE MODE (agent farming). `write_paths` are then the ONLY writable places (a workspace and a temp dir); in
this mode the seatbelt profile also hides the other temp areas (/private/var/folders, /private/tmp, /Volumes)
and bwrap hides /tmp and /var/tmp, so a sibling folder cannot be read either. `env` is merged onto a minimal
safe base (PATH, LANG); `shell=True` runs argv[0] as a command line through /bin/sh INSIDE the sandbox; `cwd`
is the working folder. In write mode this module never falls back to an unsandboxed run: when the backend is
`none`, was refused, or cannot honour write_paths (appcontainer: not implemented), the command is NOT run and
the result says isolated=False with a `reason`. Without those four arguments the behaviour is unchanged.

Nothing requires Docker. A caller that needs isolation checks `isolated`; this module never claims it
when it is not there. See research/3rdparty-ideas/isolation-backends.md for the measurements.

The profile reads: everything except the home folder, plus the interpreter and `read_paths`. It writes
nothing. In write mode the seatbelt profile instead reads NOTHING by default and opens only READ_ALLOW (the system
libraries and tools, measured), the interpreter, `read_paths` and the write paths, and the command tree is capped:
RLIMIT_NPROC (the user's process count now + 256, floor 512), RLIMIT_CPU (timeout + 10 s) and RLIMIT_FSIZE (1 GB). Output is capped after the run (an unbounded writer is stopped by `timeout`, not by memory).
"""
import glob
import ntpath
import os
import shutil
import signal
import subprocess
import sys
import time

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


# WRITE MODE reads (macOS). Write mode is deny-by-default for reads too: a sandboxed command could otherwise read
# /Library, /opt, /private/etc or /usr/local/etc. Only what an interpreter, the shell and the usual tools need is
# re-allowed, each entry MEASURED on Darwin 25.6.0 by running `python3 -m unittest`, a shell battery (ls, cat, grep,
# sed, awk, find, perl) and git inside the profile and removing entries until something broke. System-owned files only:
# nothing here holds a user's data, configuration or credentials. /etc, /Library (apart from the two below), /opt/homebrew/etc
# and /usr/local/etc stay unreadable.
READ_ALLOW = (
    ("/usr/lib", "shared libraries and the dyld cache fallback every program is linked against"),
    ("/System/Library", "system frameworks and the dyld shared cache: perl and awk fail without it"),
    ("/System/Cryptexes", "on macOS 13+ part of the system libraries live in the OS cryptex"),
    ("/System/Volumes/Preboot/Cryptexes", "the same cryptex under its other name"),
    ("/private/var/db/dyld", "the dyld shared cache on macOS 11+"),
    ("/usr/share", "locale, terminfo and zoneinfo data; perl and awk fail without it"),
    ("/usr/bin", "system tools; a #! script in here is read by its interpreter"),
    ("/bin", "sh and the basic tools"),
    ("/usr/sbin", "system tools"),
    ("/sbin", "system tools"),
    ("/usr/libexec", "helpers the system tools run"),
    ("/private/var/select", "/bin/sh reads its default-shell selection here; without it every command prints an error"),
    ("/Library/Developer/CommandLineTools", "the real git, its exec-path and its system gitconfig (the /usr/bin/git shim "
                                            "needs a Mach service this profile refuses, so the real binary is used)"),
    ("/Library/Apple/usr/libexec/oah", "Rosetta: an Intel interpreter (python3 from an Intel Homebrew) cannot start on Apple silicon without it"),
)
# Homebrew keeps an interpreter's libraries (sqlite, openssl, the Python framework) under these prefixes, reached through
# links. Only the library-bearing folders are allowed, never etc, var or share, where site configuration lives.
READ_ALLOW_BREW_PREFIXES = ("/opt/homebrew", "/usr/local")
READ_ALLOW_BREW_DIRS = ("Cellar", "opt", "lib", "Frameworks")
# Nothing under /dev is readable except these (a terminal is not among them).
READ_ALLOW_DEV = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/dtracehelper")


def _read_allow_paths():
    out = [p for p, _why in READ_ALLOW if os.path.exists(p)]
    for base in READ_ALLOW_BREW_PREFIXES:
        out += [os.path.join(base, d) for d in READ_ALLOW_BREW_DIRS if os.path.isdir(os.path.join(base, d))]
    out += [os.path.join(d, "Contents", "Developer") for d in sorted(glob.glob("/Applications/Xcode*.app"))
            if os.path.isdir(os.path.join(d, "Contents", "Developer"))]
    return out


def _sb(p):
    return '(subpath "%s")' % os.path.realpath(p).replace('"', '')


def _profile(read_paths, write_paths=()):
    home = os.path.realpath(os.path.expanduser("~"))
    allow = [p for p in read_paths if p]
    rules = ['(version 1)', '(deny default)',
             '(allow process-exec*) (allow process-fork) (allow signal (target self))']
    if write_paths:
        # write mode: reads are denied by default and only READ_ALLOW, the interpreter, read_paths and the write paths
        # are opened. Metadata (stat) is allowed everywhere outside the home folder, because a command cannot even be
        # found on PATH without it; names and contents still need the read rights below.
        rules += ['(allow file-read-metadata)',
                  '(deny file-read* (subpath "/Users") (subpath "%s"))' % home.replace('"', ''),
                  '(allow file-read* %s)' % " ".join('(subpath "%s")' % p.replace('"', '') for p in _read_allow_paths()),
                  '(allow file-read* (literal "/") %s)' % " ".join('(literal "%s")' % d for d in READ_ALLOW_DEV)]
    else:
        rules += ['(allow file-read*)',
                  '(deny file-read* (subpath "/Users") (subpath "%s"))' % home.replace('"', '')]
    rules += ['(allow file-write* (literal "/dev/null"))',
              '(allow sysctl-read) (allow ipc-posix-shm*)',
              '(deny process-info*)',
              '(deny sysctl-read (sysctl-name "kern.procargs2") (sysctl-name-prefix "kern.proc"))']
    if allow:
        rules.append("(allow file-read* %s)" % " ".join(_sb(p) for p in allow))
    if write_paths:
        # the other temp areas stay hidden even where READ_ALLOW or a read path would reach them
        rules.append('(deny file-read* (subpath "/private/var/folders") (subpath "/private/tmp") '
                     '(subpath "/Volumes"))')
        if allow:
            rules.append("(allow file-read* %s)" % " ".join(_sb(p) for p in allow))
        wp = " ".join(_sb(p) for p in write_paths if p)
        rules.append("(allow file-read* %s) (allow file-write* %s)" % (wp, wp))
    return "\n".join(rules)


_BWRAP_OK = {}


def _bwrap_usable():
    """True when `bwrap` can build the namespaces here (checked once): installed is not the same as allowed."""
    exe = shutil.which("bwrap")
    if not exe:
        return False
    if exe not in _BWRAP_OK:
        try:
            _BWRAP_OK[exe] = subprocess.run([exe, "--unshare-all", "--ro-bind", "/", "/", "true"],
                                            capture_output=True, timeout=10, env={}).returncode == 0
        except Exception:                                    # noqa: BLE001
            _BWRAP_OK[exe] = False
    return _BWRAP_OK[exe]


def _bwrap_cmd(exe, argv, read_paths, write_paths=(), env=None, cwd=None):
    """Everything read-only; home hidden under an empty tmpfs (then made read-only); `read_paths` re-allowed
    read-only on top of the hiding; a private /proc, so no other process is visible."""
    hide = []
    cands = sorted({os.path.realpath(os.path.expanduser("~")), "/home", "/root"}, key=lambda d: (len(d), d))
    for d in cands:
        # a folder inside one already hidden is hidden with it; one that does not exist cannot be mounted over
        if d != "/" and os.path.exists(d) and not any(d.startswith(h.rstrip("/") + "/") for h in hide):
            if write_paths and (d + "/").startswith(("/tmp/", "/var/tmp/")):
                continue               # a home under /tmp is hidden by the empty /tmp below; a mount point there would not exist
            hide.append(d)
    cmd = [exe, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
           "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev"]
    for d in hide:
        cmd += ["--tmpfs", d]
    if write_paths:
        cmd += ["--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]     # before the binds, so a write path under /tmp stays
    for p in read_paths:
        if p:
            rp = os.path.realpath(p)
            cmd += ["--ro-bind-try", rp, rp]
    for p in write_paths:
        if p:
            rp = os.path.realpath(p)
            cmd += ["--bind", rp, rp]
    for k, v in sorted((env or {}).items()):
        cmd += ["--setenv", str(k), str(v)]
    if cwd:
        cmd += ["--chdir", os.path.realpath(cwd)]
    for d in hide:
        cmd += ["--remount-ro", d]
    return cmd + ["--"] + list(argv)


# ---------------------------------------------------------------------------------- Windows
_AC_NAME = "gt-island-scan"
_AC_OK = {}


def _readable_roots():
    """Folders every AppContainer can read where they are: Program Files and Windows."""
    env = os.environ
    return (env.get("ProgramFiles", "C:\\Program Files"), env.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
            env.get("SystemRoot", "C:\\Windows"))


def _needs_staging(path, roots=None):
    """A folder is staged unless an AppContainer can read it where it is. Decided by WHERE it lives, not by
    what the profile variable says: the test harness moves USERPROFILE to a throwaway folder, and a rule of
    "staged when under the profile" then skipped staging and the container could not open the script."""
    n = ntpath.normcase(path).rstrip("\\")
    for r in (roots if roots is not None else _readable_roots()):
        nr = ntpath.normcase(r).rstrip("\\")
        if nr and (n == nr or n.startswith(nr + "\\")):
            return False
    return True


def _map_argv(argv, mapping):
    """Rewrite any argument that is, or lies inside, a staged folder to its staged copy."""
    out = []
    for a in argv:
        for src, dst in mapping.items():
            s = src.rstrip("\\/")
            n, ns = ntpath.normcase(a), ntpath.normcase(s)
            if n == ns or n.startswith(ns + "\\"):
                a = dst.rstrip("\\/") + a[len(s):]
                break
        out.append(a)
    return out


def _win_api():
    """ctypes bindings for CreateProcess in an AppContainer and a Job Object; built on first use."""
    import ctypes
    import ctypes.wintypes as w
    from types import SimpleNamespace as NS
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    usr = ctypes.WinDLL("userenv", use_last_error=True)
    H, V, PSID = w.HANDLE, ctypes.c_void_p, ctypes.c_void_p

    class SA(ctypes.Structure):
        _fields_ = [("n", w.DWORD), ("sd", V), ("inherit", w.BOOL)]

    class SECCAP(ctypes.Structure):
        _fields_ = [("sid", PSID), ("caps", V), ("count", w.DWORD), ("reserved", w.DWORD)]

    class SI(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("r", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
                    ("x", w.DWORD), ("y", w.DWORD), ("xs", w.DWORD), ("ys", w.DWORD), ("xc", w.DWORD),
                    ("yc", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD), ("show", w.WORD),
                    ("r2n", w.WORD), ("r2", V), ("hin", H), ("hout", H), ("herr", H)]

    class SIEX(ctypes.Structure):
        _fields_ = [("si", SI), ("attrs", V)]

    class PI(ctypes.Structure):
        _fields_ = [("hProcess", H), ("hThread", H), ("pid", w.DWORD), ("tid", w.DWORD)]

    class IOC(ctypes.Structure):
        _fields_ = [(c, ctypes.c_ulonglong) for c in "abcdef"]

    class BASIC(ctypes.Structure):
        _fields_ = [("pu", ctypes.c_longlong), ("ju", ctypes.c_longlong), ("flags", w.DWORD),
                    ("minws", ctypes.c_size_t), ("maxws", ctypes.c_size_t), ("active", w.DWORD),
                    ("aff", ctypes.c_size_t), ("prio", w.DWORD), ("sched", w.DWORD)]

    class EXT(ctypes.Structure):
        _fields_ = [("basic", BASIC), ("io", IOC), ("pmem", ctypes.c_size_t), ("jmem", ctypes.c_size_t),
                    ("ppeak", ctypes.c_size_t), ("jpeak", ctypes.c_size_t)]

    usr.CreateAppContainerProfile.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, V, w.DWORD, ctypes.POINTER(PSID)]
    usr.CreateAppContainerProfile.restype = ctypes.c_long
    usr.DeriveAppContainerSidFromAppContainerName.argtypes = [w.LPCWSTR, ctypes.POINTER(PSID)]
    usr.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    adv.ConvertSidToStringSidW.argtypes = [PSID, ctypes.POINTER(w.LPWSTR)]
    k32.CreatePipe.argtypes = [ctypes.POINTER(H), ctypes.POINTER(H), ctypes.POINTER(SA), w.DWORD]
    k32.SetHandleInformation.argtypes = [H, w.DWORD, w.DWORD]
    k32.InitializeProcThreadAttributeList.argtypes = [V, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    k32.UpdateProcThreadAttribute.argtypes = [V, w.DWORD, ctypes.c_size_t, V, ctypes.c_size_t, V, V]
    k32.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, V, V, w.BOOL, w.DWORD, V, w.LPCWSTR,
                                   ctypes.POINTER(SIEX), ctypes.POINTER(PI)]
    k32.CreateJobObjectW.argtypes = [V, w.LPCWSTR]
    k32.CreateJobObjectW.restype = H
    k32.SetInformationJobObject.argtypes = [H, ctypes.c_int, V, w.DWORD]
    k32.AssignProcessToJobObject.argtypes = [H, H]
    k32.ResumeThread.argtypes = [H]
    k32.WaitForSingleObject.argtypes = [H, w.DWORD]
    k32.TerminateProcess.argtypes = [H, w.UINT]
    k32.GetExitCodeProcess.argtypes = [H, ctypes.POINTER(w.DWORD)]
    k32.ReadFile.argtypes = [H, V, w.DWORD, ctypes.POINTER(w.DWORD), V]
    k32.WriteFile.argtypes = [H, V, w.DWORD, ctypes.POINTER(w.DWORD), V]
    k32.CloseHandle.argtypes = [H]
    return NS(ctypes=ctypes, w=w, k32=k32, adv=adv, usr=usr, H=H, V=V, SA=SA, SECCAP=SECCAP, SI=SI, SIEX=SIEX,
              PI=PI, EXT=EXT)


def _ac_sid(api):
    sid = api.V()
    hr = api.usr.CreateAppContainerProfile(_AC_NAME, _AC_NAME, "gt island scan", None, 0, api.ctypes.byref(sid))
    if hr & 0xFFFFFFFF == 0x800700B7:                        # already exists
        hr = api.usr.DeriveAppContainerSidFromAppContainerName(_AC_NAME, api.ctypes.byref(sid))
    if hr != 0:
        raise OSError("no AppContainer identity (%#x)" % (hr & 0xFFFFFFFF))
    text = api.w.LPWSTR()
    api.adv.ConvertSidToStringSidW(sid, api.ctypes.byref(text))
    return sid, text.value


def _stage(read_paths, sid_text):
    """Copy the top-level .py files of each read path an AppContainer cannot read in place to a staging folder that only
    the container's identity may read (never write); -> {original folder: staged folder}. Reused while the
    files are unchanged. The user's own files are not re-permissioned."""
    import hashlib
    base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "gt-island-stage")
    mapping = {}
    for rp in read_paths:
        if not rp or not os.path.isdir(rp) or not _needs_staging(rp):
            continue
        files = sorted(f for f in os.listdir(rp) if f.endswith(".py") and os.path.isfile(os.path.join(rp, f)))
        h = hashlib.sha256((sid_text + "|" + os.path.realpath(rp)).encode())
        for f in files:
            st = os.stat(os.path.join(rp, f))
            h.update(("%s|%d|%d;" % (f, st.st_size, st.st_mtime_ns)).encode())
        dest = os.path.join(base, h.hexdigest()[:20])
        if not os.path.isdir(dest):
            os.makedirs(base, exist_ok=True)
            tmp = dest + ".part-%d" % os.getpid()
            os.makedirs(tmp)
            try:
                subprocess.run(["icacls", tmp, "/grant", "*%s:(OI)(CI)RX" % sid_text], capture_output=True,
                               check=True, timeout=30)
                for f in files:
                    shutil.copyfile(os.path.join(rp, f), os.path.join(tmp, f))
                os.replace(tmp, dest)
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                if not os.path.isdir(dest):
                    raise
        mapping[rp] = dest
    return mapping


def _win_run(argv, stdin, timeout, max_out, read_paths):
    """One child in an AppContainer inside a Job Object. Raises on setup trouble (the caller falls back)."""
    import threading
    api = _win_api()
    ct, w, k = api.ctypes, api.w, api.k32
    sid, sid_text = _ac_sid(api)
    argv = _map_argv(list(argv), _stage(list(read_paths), sid_text))
    sa = api.SA(ct.sizeof(api.SA), None, True)
    pipes = [api.H() for _ in range(6)]
    in_r, in_w, out_r, out_w, err_r, err_w = pipes
    for a, b in ((in_r, in_w), (out_r, out_w), (err_r, err_w)):
        if not k.CreatePipe(ct.byref(a), ct.byref(b), ct.byref(sa), 0):
            raise OSError("pipe")
    for h in (in_w, out_r, err_r):
        k.SetHandleInformation(h, 1, 0)                      # the parent's ends are not inherited
    caps = api.SECCAP(sid, None, 0, 0)
    size = ct.c_size_t(0)
    k.InitializeProcThreadAttributeList(None, 1, 0, ct.byref(size))
    attrs = ct.create_string_buffer(size.value)
    k.InitializeProcThreadAttributeList(attrs, 1, 0, ct.byref(size))
    k.UpdateProcThreadAttribute(attrs, 0, 0x20009, ct.byref(caps), ct.sizeof(caps), None, None)
    si = api.SIEX()
    si.si.cb = ct.sizeof(api.SIEX)
    si.si.flags = 0x100                                      # STARTF_USESTDHANDLES
    si.si.hin, si.si.hout, si.si.herr = in_r, out_w, err_w
    si.attrs = ct.cast(attrs, api.V)
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    env = ct.create_string_buffer(("SystemRoot=%s\0LOCALAPPDATA=%s\0\0" % (os.environ.get("SystemRoot", "C:\\Windows"),
                                                                           local)).encode("utf-16-le"))
    pi = api.PI()
    ok = k.CreateProcessW(argv[0], ct.create_unicode_buffer(subprocess.list2cmdline(argv)), None, None, True,
                          0x00080000 | 0x4 | 0x400 | 0x08000000, env, os.environ.get("SystemRoot", "C:\\Windows"),
                          ct.byref(si), ct.byref(pi))
    if not ok:
        err = ct.get_last_error()
        for h in pipes:
            k.CloseHandle(h)
        raise OSError("CreateProcess failed (%d)" % err)
    job = k.CreateJobObjectW(None, None)
    info = api.EXT()
    info.basic.flags = 0x8 | 0x100 | 0x2000                   # one process, memory cap, kill on close
    info.basic.active = 1
    info.pmem = 512 * 1024 * 1024
    k.SetInformationJobObject(job, 9, ct.byref(info), ct.sizeof(info))
    k.AssignProcessToJobObject(job, pi.hProcess)
    k.ResumeThread(pi.hThread)
    for h in (in_r, out_w, err_w, pi.hThread):
        k.CloseHandle(h)
    got = {"out": bytearray(), "err": bytearray()}

    def pump(handle, key, cap):
        buf = ct.create_string_buffer(65536)
        n = w.DWORD()
        while k.ReadFile(handle, buf, 65536, ct.byref(n), None) and n.value:
            if len(got[key]) <= cap:
                got[key] += buf.raw[:n.value]

    def feed():
        n = w.DWORD()
        if stdin:
            k.WriteFile(in_w, stdin, len(stdin), ct.byref(n), None)
        k.CloseHandle(in_w)

    threads = [threading.Thread(target=pump, args=(out_r, "out", max_out), daemon=True),
               threading.Thread(target=pump, args=(err_r, "err", 8192), daemon=True),
               threading.Thread(target=feed, daemon=True)]
    for t in threads:
        t.start()
    timed_out = k.WaitForSingleObject(pi.hProcess, int(timeout * 1000)) == 0x102
    if timed_out:
        k.TerminateProcess(pi.hProcess, 1)
    code = w.DWORD()
    k.GetExitCodeProcess(pi.hProcess, ct.byref(code))
    k.CloseHandle(job)                                       # kills anything still in the job
    for t in threads:
        t.join(5)
    for h in (out_r, err_r, pi.hProcess):
        k.CloseHandle(h)
    out = bytes(got["out"])
    return _result(code.value, out[:max_out], bytes(got["err"])[:8192], "appcontainer", timed_out, len(out) > max_out)


def _appcontainer_usable():
    """True when a child can start in an AppContainer here (checked once). An interpreter in a per-user
    folder cannot be read from the container, so this is not the same as the API being present."""
    if "ok" not in _AC_OK:
        try:
            r = _win_run([sys.executable, "-I", "-c", "print(1)"], b"", 20.0, 1024, [])
            _AC_OK["ok"] = r["rc"] == 0 and r["out"].strip() == b"1"
        except Exception:                                    # noqa: BLE001
            _AC_OK["ok"] = False
    return _AC_OK["ok"]


def available_backends():
    out = []
    if sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC):
        out.append("seatbelt")
    elif sys.platform.startswith("linux") and _bwrap_usable():
        out.append("bwrap")
    elif sys.platform == "win32" and _appcontainer_usable():
        out.append("appcontainer")
    return out + ["none"]


def _result(rc=-1, out=b"", err=b"", backend="none", timed_out=False, truncated=False):
    return {"rc": rc, "out": out, "err": err, "backend": backend, "isolated": backend != "none",
            "timed_out": timed_out, "truncated": truncated}


def _refused(backend, reason):
    """Write mode asked for something this launcher cannot fence: nothing was run."""
    r = _result(126, b"", ("not run: " + reason).encode(), backend)
    r["isolated"] = False
    r["reason"] = reason
    return r


def _base_env(extra=None):
    """The minimal safe environment for write mode: PATH and LANG, plus what the caller adds."""
    base = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8"}
    if os.name == "nt":
        base = {"PATH": os.environ.get("SystemRoot", "C:\\Windows") + "\\System32",
                "SystemRoot": os.environ.get("SystemRoot", "C:\\Windows")}
    base.update({str(k): str(v) for k, v in (extra or {}).items()})
    return base


def _user_process_count():
    """How many processes this user runs now (`ps -u UID -o pid=` lines); 1024 when that cannot be read. On Linux
    RLIMIT_NPROC counts every thread, not only processes, so threads are listed too (`ps -L`)."""
    try:
        argv = ["ps"] + (["-L"] if sys.platform.startswith("linux") else []) + ["-u", str(os.getuid()), "-o", "pid="]
        r = subprocess.run(argv, capture_output=True, timeout=10, env={})
        n = len([ln for ln in r.stdout.splitlines() if ln.strip()])
        return n if r.returncode == 0 and n > 0 else 1024
    except Exception:                                        # noqa: BLE001
        return 1024


FSIZE_LIMIT = 1 << 30            # write mode: no single file larger than 1 GB
NPROC_HEADROOM = 256             # write mode: at most this many more processes than the user runs now (floor 512)
CPU_MARGIN = 10                  # write mode: CPU seconds on top of the timeout


def _write_mode_limits(timeout):
    """-> a preexec_fn that caps the child tree (POSIX, write mode only), or None. The numbers are fixed in the parent,
    before the fork, so the child does no more than three setrlimit calls: RLIMIT_NPROC stops a fork bomb from filling the
    user's process table before the timeout fires; RLIMIT_CPU ends a spinning command; RLIMIT_FSIZE a runaway file."""
    if os.name == "nt":
        return None
    try:
        import resource
    except ImportError:
        return None
    nproc = max(_user_process_count() + NPROC_HEADROOM, 512)
    cpu = int(timeout) + CPU_MARGIN

    def pre():
        for what, want in ((resource.RLIMIT_NPROC, nproc), (resource.RLIMIT_CPU, cpu), (resource.RLIMIT_FSIZE, FSIZE_LIMIT)):
            try:
                hard = resource.getrlimit(what)[1]
                v = want if hard == resource.RLIM_INFINITY else min(want, hard)
                resource.setrlimit(what, (v, v))
            except (ValueError, OSError):
                pass
    return pre


STDERR_LIMIT = 8192             # the stderr a result keeps
KEEP_HEAD = 2048                # of a capped stream: this many bytes from the start, the rest from the END


def _head_tail(data, limit, head=KEEP_HEAD):
    """`data` unchanged when it fits in `limit` bytes; otherwise its first `head` bytes, a marker and its LAST bytes, the
    whole no longer than `limit`. The end is kept because a test runner prints its summary there; a cap that kept only the
    start dropped exactly the line the caller needed (2026-10-28)."""
    if len(data) <= limit:
        return data
    marker = ("\n[... %d bytes omitted ...]\n" % (len(data) - limit)).encode()
    room = limit - len(marker)
    if room < 8:
        return data[:limit]
    h = min(head, room // 2)
    return data[:h] + marker + data[len(data) - (room - h):]


def _kill_group(pid):
    """SIGKILL the child's whole process group and wait (briefly) until it is empty. The child runs in a session of its own
    (start_new_session), so this reaches a background loop it left behind (`( loop ) &`), not the launcher or the caller."""
    if os.name == "nt":
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        return
    for _ in range(100):
        try:
            os.killpg(pid, 0)
        except OSError:
            return
        time.sleep(0.01)


def _under(p, root):
    p, root = os.path.realpath(p), os.path.realpath(root)
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def _drop_home_paths(paths):
    """Write mode: no read path may lie under the user's home folder. The profile allows `paths` AFTER it denies the home
    folder, so an interpreter prefix there (a virtualenv, ~/bin/python's prefix) would reopen the whole of it. What stays
    is a venv's site-packages (purelib/platlib) when the running interpreter sits in one, so a home venv's packages
    still import, and the single file pyvenv.cfg (python will not start without reading it); bin/ and the rest of its
    root stay unreadable. A home interpreter that cannot run
    without those is broken on purpose (HOME_INTERPRETER_HINT tells the user what to do) rather than the home opened."""
    home = os.path.expanduser("~")
    kept = [p for p in paths if p and not _under(p, home)]
    if _under(sys.prefix, home) and sys.prefix != sys.base_prefix:
        cfg = os.path.join(sys.prefix, "pyvenv.cfg")             # python reads this one file at start-up
        if os.path.isfile(cfg):
            kept.append(cfg)
        try:
            import sysconfig
            for key in ("purelib", "platlib"):
                d = sysconfig.get_paths().get(key)
                if d and os.path.isdir(d) and _under(d, sys.prefix) and d not in kept:
                    kept.append(d)
        except Exception:                                    # noqa: BLE001
            pass
    return kept


def _spawn(cmd, stdin, timeout, max_out, backend, env=None, cwd=None, limits=None):
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=({} if env is None else env), cwd=cwd, start_new_session=(os.name != "nt"),
                                preexec_fn=limits)
    except OSError as exc:
        return _result(127, b"", ("could not start: %s" % exc.__class__.__name__).encode(), backend)
    timed_out = False
    try:
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name != "nt":
                _kill_group(proc.pid)
            else:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:                                # noqa: BLE001
                out, err = b"", b""
        # the command has finished (or been killed): nothing it started may outlive it and keep writing files
        _kill_group(proc.pid)
    finally:
        _kill_group(proc.pid)
    truncated = len(out) > max_out
    return _result(proc.returncode if proc.returncode is not None else -9, _head_tail(out, max_out),
                   _head_tail(err, STDERR_LIMIT), backend, timed_out, truncated)


def run(argv, stdin=b"", timeout=10.0, read_paths=(), max_out=1048576, backend=None,
        write_paths=(), env=None, shell=False, cwd=None):
    if write_paths or env is not None or shell or cwd:
        return _run_ext(argv, stdin, timeout, read_paths, max_out, backend, write_paths, env, shell, cwd)
    return _run_plain(argv, stdin, timeout, read_paths, max_out, backend)


def _run_ext(argv, stdin, timeout, read_paths, max_out, backend, write_paths, env, shell, cwd):
    """The write-mode / shell / env / cwd path. With write_paths it never runs unsandboxed."""
    try:
        argv = list(argv)
        write_paths = [p for p in write_paths if p]
        want = backend or available_backends()[0]
        benv = _base_env(env)
        wd = os.path.realpath(cwd) if cwd else None
        if want not in available_backends() or want == "none":
            if write_paths:
                return _refused("none", "no isolating sandbox backend is usable here, so write_paths cannot be enforced")
            cmd = ["/bin/sh", "-c", argv[0]] if shell else argv
            return _spawn(cmd, stdin, timeout, max_out, "none", benv, wd)
        if want == "appcontainer":
            if write_paths or shell:
                return _refused("appcontainer", "the AppContainer backend cannot honour write_paths or shell yet")
            return _run_plain(argv, stdin, timeout, read_paths, max_out, "appcontainer")
        exe_argv = ["/bin/sh", "-c", argv[0]] if shell else [os.path.realpath(argv[0])] + argv[1:]
        paths = list(read_paths) + [sys.base_prefix, sys.prefix]
        if not shell:
            paths.append(os.path.dirname(os.path.dirname(exe_argv[0])))
        if write_paths:
            # the interpreters a command can reach: the one running us, and whatever python3 the base PATH finds
            for exe in (sys.executable, shutil.which("python3", path=benv.get("PATH"))):
                if exe:
                    paths.append(os.path.dirname(os.path.dirname(os.path.realpath(exe))))
            paths = _drop_home_paths(paths)
        limits = _write_mode_limits(timeout) if write_paths else None
        if want == "seatbelt":
            cmd = [SANDBOX_EXEC, "-p", _profile(paths, write_paths)] + exe_argv
            r = _spawn(cmd, stdin, timeout, max_out, "seatbelt", benv, wd, limits)
            if r["rc"] == 71 and b"sandbox_apply" in r["err"]:
                if write_paths:
                    return _refused("seatbelt", "a nested sandbox refused the profile, so write_paths cannot be enforced")
                return _spawn(exe_argv, stdin, timeout, max_out, "none", benv, wd)
            return r
        if want == "bwrap":
            # Read side on Linux needs no allow-list: bwrap mounts the whole file system read-only with the home folder
            # (and /home, /root) replaced by an empty tmpfs, so there is no deny-by-default profile to tighten here.
            r = _spawn(_bwrap_cmd(shutil.which("bwrap"), exe_argv, paths, write_paths, benv, wd), stdin, timeout,
                       max_out, "bwrap", {}, None, limits)
            if r["rc"] == 1 and r["err"].startswith(b"bwrap:"):
                if write_paths:
                    return _refused("bwrap", "bwrap refused the namespaces, so write_paths cannot be enforced")
                return _spawn(exe_argv, stdin, timeout, max_out, "none", benv, wd)
            return r
        return _refused("none", "unknown backend %s" % want)
    except Exception as exc:                                 # noqa: BLE001
        return _result(-1, b"", ("launcher error: %s" % exc.__class__.__name__).encode())


def _run_plain(argv, stdin=b"", timeout=10.0, read_paths=(), max_out=1048576, backend=None):
    try:
        argv = list(argv)
        want = backend or available_backends()[0]
        if want == "seatbelt" and "seatbelt" in available_backends():
            real = os.path.realpath(argv[0])
            paths = list(read_paths) + [os.path.dirname(os.path.dirname(real)), sys.base_prefix]
            cmd = [SANDBOX_EXEC, "-p", _profile(paths), real] + argv[1:]
            r = _spawn(cmd, stdin, timeout, max_out, "seatbelt")
            if r["rc"] == 71 and b"sandbox_apply" in r["err"]:
                # a nested sandbox refused the profile: run clean and say it is not isolated
                return _spawn(argv, stdin, timeout, max_out, "none")
            return r
        if want == "bwrap" and "bwrap" in available_backends():
            real = os.path.realpath(argv[0])
            paths = list(read_paths) + [os.path.dirname(os.path.dirname(real)), sys.base_prefix]
            r = _spawn(_bwrap_cmd(shutil.which("bwrap"), [real] + argv[1:], paths), stdin, timeout, max_out, "bwrap")
            if r["rc"] == 1 and r["err"].startswith(b"bwrap:"):
                # the namespaces were refused after all: run clean and say it is not isolated
                return _spawn(argv, stdin, timeout, max_out, "none")
            return r
        if want == "appcontainer" and "appcontainer" in available_backends():
            try:
                return _win_run([os.path.realpath(argv[0])] + argv[1:], stdin, timeout, max_out, read_paths)
            except Exception:                                # noqa: BLE001
                # the container could not be set up: run clean and say it is not isolated
                return _spawn(argv, stdin, timeout, max_out, "none")
        return _spawn(argv, stdin, timeout, max_out, "none")
    except Exception as exc:                                 # noqa: BLE001
        return _result(-1, b"", ("launcher error: %s" % exc.__class__.__name__).encode())


if __name__ == "__main__":
    print("backends: " + ", ".join(available_backends()))
