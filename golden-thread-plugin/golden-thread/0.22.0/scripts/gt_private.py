#!/usr/bin/env python3
"""gt_private.py -- owner-only files and folders for the things gt keeps OUT of the conversation.

Used by the "keep the main conversation small" mechanisms (0.21.4): gt_spill.py (a command's
full output), gt_intake.py (a big paste, a file waiting to be digested) and gt_rules_once.py
(a per-session marker). Each of them parks text that is NOT meant to be read back by the model
unless someone asks, and that text can hold anything the original held, a credential included,
so the rules are the same everywhere and live in one place:

  * the folder is created mode 0700 and the file is created mode 0600 BEFORE it receives a byte
    (os.open with the mode, not a chmod afterwards), so there is no window in which another
    user can read it;
  * on Windows, where those mode bits mean nothing, the folder gets an owner-only ACL instead
    (icacls: inheritance removed, the current user alone granted full control, inherited by
    every file made in it). If icacls cannot be run or refuses, the folder is left as the
    profile's own inherited ACL and `acl_problem()` says why; gt_doctor's `private-files` row
    reads the real ACL back (0.21.4);
  * writes are atomic (a sibling temp file, then os.replace) and LF-only on every OS
    (bytes, never text mode; mkstemp opens the file O_BINARY on Windows);
  * a name is a short safe token, never a caller-supplied path: `safe_token` keeps letters,
    digits, dot, dash and underscore and nothing else, so an id can not climb out of its folder;
  * `prune` removes by age and by total size, oldest first, and never raises: housekeeping must
    not be the reason a hook fails.

Nothing here prints file content. Stdlib only; Python 3.8+.
"""
import os
import re
import subprocess
import tempfile
import time

_TOKEN = re.compile(r"[^A-Za-z0-9._-]+")


def safe_token(value, limit=64):
    """-> a name made only of letters, digits, '.', '-' and '_' (at most `limit` characters), or ''.
    A leading dot is dropped so the result is never hidden or '..'."""
    t = _TOKEN.sub("-", str(value or "")).strip("-.")[:limit]
    return t


_ACL_DONE = set()          # folders locked (or tried) by this process: one icacls call each
_ACL_PROBLEMS = {}         # folder -> why the owner-only ACL could not be set


def is_windows():
    return os.name == "nt"


def _icacls(args):
    """Run icacls. -> (exit status, output). A seam for the tests; never raises."""
    try:
        p = subprocess.run(["icacls"] + list(args), capture_output=True, text=True, timeout=20)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, type(exc).__name__


def current_user():
    """-> DOMAIN\\user (or user) for the ACL, or ''."""
    name = os.environ.get("USERNAME") or ""
    dom = os.environ.get("USERDOMAIN") or ""
    return ("%s\\%s" % (dom, name) if dom and name else name)


def lock_windows_dir(path):
    """Owner-only ACL on a Windows folder: inheritance removed, the current user granted full control
    that files and subfolders inherit. -> True, or False with the reason in acl_problem(path)."""
    user = current_user()
    if not user:
        _ACL_PROBLEMS[path] = "the user name is not known (USERNAME is unset)"
        return False
    rc, out = _icacls([path, "/inheritance:r", "/grant:r", "%s:(OI)(CI)F" % user])
    if rc != 0:
        _ACL_PROBLEMS[path] = "icacls exited %s: %s" % (rc, (out or "").strip().splitlines()[-1:] or "no output")
        return False
    _ACL_PROBLEMS.pop(path, None)
    return True


def acl_problem(path=None):
    """-> why the owner-only ACL could not be set on `path` (any folder when None), or ''."""
    if path is not None:
        return _ACL_PROBLEMS.get(path, "")
    return next(iter(_ACL_PROBLEMS.values()), "")


def acl_state(path):
    """-> ("ok" | "open" | "unknown", detail) for a Windows folder: ok when icacls lists only the
    current user; open when anyone else holds an entry. Read-only."""
    rc, out = _icacls([path])
    user = current_user().lower()
    if rc != 0 or not user:
        return "unknown", "icacls could not read the ACL (%s)" % (out.strip().splitlines()[-1:] or rc)
    others = set()
    for line in out.splitlines():
        m = re.search(r"(?:^|\s)([^\s:()]+(?:\\[^\s:()]+)?):\(", line)
        if m and m.group(1).lower() not in (user, user.split("\\")[-1]):
            others.add(m.group(1))
    if others:
        return "open", "also readable by: " + ", ".join(sorted(others))
    return "ok", "owner only"


def ensure_private_dir(path):
    """Create `path` (and its parents) with 0700 on the last level; on Windows an owner-only ACL
    (see lock_windows_dir), failing soft."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        if not is_windows():
            os.chmod(path, 0o700)
        elif path not in _ACL_DONE:
            _ACL_DONE.add(path)
            lock_windows_dir(path)
    except OSError:
        pass
    return path


def lock_home(path):
    """Owner-only ACL on a secrets home (the unlock home, the LOTR home), once per path. Windows only: elsewhere
    the 0700 mode the caller set does this and nothing runs. -> True, or False with the reason in acl_problem."""
    if not is_windows():
        return True
    if path in _ACL_DONE:
        return not acl_problem(path)
    _ACL_DONE.add(path)
    return lock_windows_dir(path)


def write_private(path, data):
    """Write `data` (bytes or str) to `path` atomically, mode 0600, LF bytes. -> True on success.

    A str is encoded UTF-8 with 'replace' so a stray lone surrogate can not abort the write. Any
    OSError returns False; the caller decides whether that matters."""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    folder = os.path.dirname(path) or "."
    ensure_private_dir(folder)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=folder)      # mkstemp creates it 0600
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if not is_windows():
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def prune(folder, max_age_days=14, max_bytes=256 * 1024 * 1024, keep_suffixes=None, now=None):
    """Remove files in `folder` older than `max_age_days`, then the oldest until the total is at
    most `max_bytes`. -> the number removed. Never raises; only plain files directly inside
    `folder` are touched. Names ending in one of `keep_suffixes` are left alone."""
    removed = 0
    try:
        now = time.time() if now is None else now
        entries = []
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if keep_suffixes and name.endswith(tuple(keep_suffixes)):
                continue
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not os.path.isfile(p) or os.path.islink(p):
                continue
            entries.append((st.st_mtime, st.st_size, p))
        cutoff = now - max_age_days * 86400
        live = []
        for mtime, size, p in sorted(entries):
            if mtime < cutoff:
                try:
                    os.unlink(p)
                    removed += 1
                except OSError:
                    live.append((mtime, size, p))
            else:
                live.append((mtime, size, p))
        total = sum(size for _m, size, _p in live)
        for _mtime, size, p in live:                 # oldest first
            if total <= max_bytes:
                break
            try:
                os.unlink(p)
                removed += 1
                total -= size
            except OSError:
                pass
    except OSError:
        pass
    return removed


def mode_of(path):
    """-> the permission bits of `path` as an int (0o600 ...), or None."""
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return None
