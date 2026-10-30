"""Owner-only ACL on the LOTR home, Windows only (gt 0.22.0, phase 122).

POSIX gets this from mode 700. On Windows the home's protection was only the profile's inherited ACL, which
nothing verified, so a home on a shared volume or under a custom LOTR_HOME could be open to a group. lock_home
runs icacls once (inheritance removed, the current user alone); state() reads it back for the doctor. Both fail
soft: a missing icacls is "unknown", never a crash. The same recipe as gt_private in the main plugin, kept here
because the two plugins install separately."""
import os
import re
import subprocess

_DONE = set()
_PROBLEMS = {}


def _is_windows():
    return os.name == "nt"


def _icacls(args):
    """Run icacls -> (exit status, output). A seam for the tests; never raises."""
    try:
        p = subprocess.run(["icacls"] + list(args), capture_output=True, text=True, timeout=20)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, type(exc).__name__


def _user():
    name = os.environ.get("USERNAME") or ""
    dom = os.environ.get("USERDOMAIN") or ""
    return "%s\\%s" % (dom, name) if dom and name else name


def lock_home(path):
    """Owner-only ACL on `path`, once per path. No-op off Windows. -> True, or False with problem(path)."""
    path = str(path)
    if not _is_windows():
        return True
    if path in _DONE:
        return not _PROBLEMS.get(path)
    _DONE.add(path)
    user = _user()
    if not user:
        _PROBLEMS[path] = "USERNAME is unset"
        return False
    rc, out = _icacls([path, "/inheritance:r", "/grant:r", "%s:(OI)(CI)F" % user])
    if rc != 0:
        _PROBLEMS[path] = "icacls exited %s: %s" % (rc, (out or "").strip().splitlines()[-1:] or "no output")
        return False
    return True


def problem(path):
    return _PROBLEMS.get(str(path), "")


def state(path):
    """-> ("ok" | "open" | "unknown", detail): ok when icacls lists only the current user."""
    rc, out = _icacls([str(path)])
    user = _user().lower()
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
