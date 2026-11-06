#!/usr/bin/env python3
"""gt_write_probe.py -- can this Python write where gt writes? Asked of each interpreter (0.20.1).

    gt_write_probe.py probe [--vault V] [--json]     every candidate interpreter, PASS/FAIL per
                                                     place, and which one gt uses
    gt_write_probe.py check <python> [--vault V]     exit 0 when <python> passes everywhere

WHY. On macOS a file carrying `com.apple.provenance` (and a folder under privacy protection) can
refuse writes from ONE interpreter and accept them from another: on 2026-10-03 Homebrew's
python3.9 -- the default `python3` on that Mac -- got `[Errno 1] Operation not permitted`
replacing the vault's log.md and rewriting files under a worktree, while /usr/bin/python3
succeeded. gt's hooks and tools ran plain `python3`, so vault writes could fail for a user
whose `python3` is Homebrew's, and the scheduled jobs had already failed the same way.

THE PROBE, run BY the interpreter being judged (a subprocess, never this process): in each
place, create a file, write it, os.replace it onto a second name, remove it; and, when the
place holds a file gt rewrites (the vault's log.md), open that file for append and close it
without writing a byte -- the open is what a provenance refusal fails, and nothing changes.

Places: the vault (when one is configured) and ~/.claude/golden-thread. Candidates: the one the
hook wrappers run (~/.claude/golden-thread/python), the interpreter gt recorded (gt_schedule.py
choose-interpreter), /usr/bin/python3 on macOS when the Command Line Tools are installed, the
`python3` on PATH (never the Windows Store stub), and the one running this.

Also gt's wording of an EPERM: `eperm_message(path, exc)` -- a single line naming the
interpreter and the two fixes, instead of a traceback (gt_spool.py and safe_write.py carry the
same wording, because they run from the vault without the hooks dir on their path).

Exit: 0 every candidate gt would use passes | 1 the one gt uses fails somewhere | 2 usage.
"""
import argparse
import errno
import json
import os
import shutil
import subprocess
import sys

OK, PROBLEM, USAGE = 0, 1, 2
SYSTEM_PYTHON = "/usr/bin/python3"
XCODE_SELECT = "/usr/bin/xcode-select"

# Run by the CANDIDATE interpreter: `py -c PROBE_CODE <dir> [<existing file>]`.
# Exit 0 ok, 13 refused (EPERM / EACCES), 3 any other OSError.
PROBE_CODE = (
    "import errno, os, sys\n"
    "d = sys.argv[1]\n"
    "a = os.path.join(d, '.gt-write-probe-%d' % os.getpid())\n"
    "b = a + '.moved'\n"
    "try:\n"
    "    with open(a, 'w') as fh:\n"
    "        fh.write('gt write probe\\n')\n"
    "    os.replace(a, b)\n"
    "    os.remove(b)\n"
    "    if len(sys.argv) > 2 and os.path.isfile(sys.argv[2]):\n"
    "        open(sys.argv[2], 'ab').close()\n"
    "except OSError as e:\n"
    "    for p in (a, b):\n"
    "        try:\n"
    "            os.remove(p)\n"
    "        except OSError:\n"
    "            pass\n"
    "    sys.exit(13 if e.errno in (errno.EPERM, errno.EACCES) else 3)\n")


def gt_home():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread")


def recorded_interpreter():
    """The interpreter gt_schedule.py recorded (interpreter.json), or None."""
    try:
        with open(os.path.join(gt_home(), "interpreter.json"), encoding="utf-8") as fh:
            p = json.load(fh)["python"]
    except Exception:
        return None
    return p if isinstance(p, str) and os.path.isfile(p) and os.access(p, os.X_OK) else None


def hooks_interpreter():
    """The interpreter the hook wrappers run (~/.claude/golden-thread/python: install.sh writes
    it on Windows and, since 0.20.1, on macOS), or None."""
    try:
        with open(os.path.join(gt_home(), "python"), encoding="utf-8") as fh:
            p = fh.readline().strip()
    except OSError:
        return None
    return p if p and os.path.isfile(p) else None


def used_interpreter():
    """The interpreter gt's hooks and tools run here: the hooks' record, else (macOS) the jobs'
    record, else on Windows the one running this, else `python3` on PATH."""
    p = hooks_interpreter()
    if p:
        return p
    if sys.platform == "darwin":
        p = recorded_interpreter()
        if p:
            return p
    if os.name == "nt":
        return sys.executable
    return shutil.which("python3") or sys.executable


def configured_vault():
    v = os.environ.get("GT_VAULT")
    if v:
        return v
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"),
                  encoding="utf-8") as fh:
            return json.load(fh).get("vault_path") or None
    except Exception:
        return None


def _system_python_usable():
    """/usr/bin/python3 without the Command Line Tools is a stub that opens an install dialog."""
    if sys.platform != "darwin" or not os.path.isfile(SYSTEM_PYTHON):
        return False
    try:
        return subprocess.run([XCODE_SELECT, "-p"], capture_output=True,
                              timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def candidates():
    """-> [(label, path)], deduplicated, in the order gt prefers them."""
    out = []
    hk = hooks_interpreter()
    if hk:
        out.append(("hooks", hk))
    rec = recorded_interpreter()
    if rec:
        out.append(("recorded", rec))
    if _system_python_usable():
        out.append(("system", SYSTEM_PYTHON))
    on_path = shutil.which("python3")
    # Windows: the Microsoft Store stub (...\WindowsApps\python3.exe) is not a Python.
    if on_path and "windowsapps" in on_path.replace("\\", "/").lower():
        on_path = None
    if on_path:
        out.append(("python3 on PATH", on_path))
    if sys.executable:
        out.append(("running this", sys.executable))
    seen, uniq = set(), []
    for label, p in out:
        key = os.path.realpath(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((label, p))
    return uniq


def places(vault=None):
    """-> [(name, dir, existing file or None)] gt writes into."""
    out = []
    vault = vault or configured_vault()
    if vault and os.path.isdir(vault):
        out.append(("vault", vault, os.path.join(vault, "log.md")))
    gh = gt_home()
    if os.path.isdir(gh):
        out.append(("gt home", gh, None))
    return out


def probe_one(py, where, existing=None, timeout=30):
    """-> "pass" | "refused" (EPERM/EACCES) | "error" | "cannot-run"."""
    args = [py, "-c", PROBE_CODE, where] + ([existing] if existing else [])
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return "cannot-run"
    return {0: "pass", 13: "refused", 3: "error"}.get(r.returncode, "cannot-run")


def probe(vault=None):
    """-> {"uses": path or None, "places": [...], "rows": [{label, python, results, ok}]}."""
    pl = places(vault)
    rows = []
    for label, py in candidates():
        res = {name: probe_one(py, d, f) for name, d, f in pl}
        rows.append({"label": label, "python": py, "results": res,
                     "ok": all(v == "pass" for v in res.values())})
    return {"uses": used_interpreter(),
            "places": [{"name": n, "dir": d} for n, d, _f in pl], "rows": rows}


def eperm_message(path, exc, python=None):
    """One line for a write the OS refused: which interpreter, and the fix. Never a
    traceback. `exc` is the OSError (EPERM or EACCES)."""
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        import gt_errors as _gte
        if _gte.in_sandbox():
            # gt sandbox mode (0.20.1): a route, not a permission -- never Full Disk Access.
            return _gte.sandbox_line(path, exc)
    except ImportError:
        pass
    python = python or sys.executable or "python3"
    rec = recorded_interpreter()
    why = os.strerror(exc.errno) if getattr(exc, "errno", None) else str(exc)
    fix = ("run gt with the interpreter it recorded (%s)" % rec if rec and rec != python
           else "run gt with /usr/bin/python3" if sys.platform == "darwin"
           and python != SYSTEM_PYTHON else "check the file's permissions")
    tail = (" -- on macOS a file carrying com.apple.provenance or a privacy-protected folder "
            "refuses some interpreters" if sys.platform == "darwin" else "")
    return ("gt: cannot write %s: %s (%s), running %s%s. Fix: %s, or give %s Full Disk Access "
            "(System Settings > Privacy & Security)."
            % (path, why, errno.errorcode.get(getattr(exc, "errno", 0) or 0, "error"), python,
               tail, fix, python))


def is_refusal(exc):
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in (errno.EPERM,
                                                                        errno.EACCES)


def main(argv=None):
    ap = argparse.ArgumentParser(description="can each Python write where gt writes?")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--vault")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("python")
    p.add_argument("--vault")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        pl = places(a.vault)
        bad = [(n, r) for n, d, f in pl for r in [probe_one(a.python, d, f)] if r != "pass"]
        for n, r in bad:
            print("FAIL %s: %s writing the %s" % (a.python, r, n))
        if not bad:
            print("PASS %s writes %s" % (a.python, ", ".join(n for n, _d, _f in pl) or
                                         "nothing to check (no vault, no gt home)"))
        return PROBLEM if bad else OK
    rep = probe(a.vault)
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        for r in rep["rows"]:
            print("%s %-16s %s  %s" % ("PASS" if r["ok"] else "FAIL", r["label"], r["python"],
                                       ", ".join("%s %s" % kv for kv in r["results"].items())))
        print("gt uses: %s" % rep["uses"])
    used = next((r for r in rep["rows"]
                 if os.path.realpath(r["python"]) == os.path.realpath(rep["uses"] or "")), None)
    return PROBLEM if used is not None and not used["ok"] else OK


if __name__ == "__main__":
    sys.exit(main())
