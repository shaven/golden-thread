#!/usr/bin/env python3
"""gt_errors -- ONE line, not a traceback, when the OS refuses a gt vault tool (0.20.1).

Shipped twice, byte for byte: in gt's scripts/ (the hooks dir) and in templates/tools/ (the
vault's Projects/golden-thread/tools/), because a vault tool runs from the vault without the
hooks dir on its path. tests/test_gt_errors.py fails the build when the two copies differ.

WHY. The 0.20 usability run (2026-10-04, finding B4) ran the vault tools from Claude's shell
under gt sandbox mode: gt_tasks, gt_lint, gt_adr, vault_init, gt_closeout, gt_broker drain and
gt_log each died in a Python traceback, and gt_log advised Full Disk Access -- advice for a
different refusal entirely. Python raises the same PermissionError (EPERM) for two causes:

  sandbox     gt sandbox mode is on and this process runs inside Claude Code's sandbox. The
              vault is written only through the write queue / the gt-vault MCP. The fix is a
              ROUTE: the MCP tool, or the same command run from a terminal.
  provenance  macOS refused this interpreter a file (com.apple.provenance, a privacy-protected
              folder). The fix is another interpreter, or Full Disk Access.

They are told apart by where the process runs, never by the error: sandbox_mode is on in
~/.claude/vault-config.json, this process is Claude's (CLAUDECODE or SANDBOX_RUNTIME in the
environment), and the platform has a Claude Code sandbox (not native Windows). Full Disk
Access is never advised for a sandbox refusal. Bubblewrap's read-only binds give EROFS, so
EROFS counts as a refusal too.

    run(main, "gt_tasks")                  main()'s exit code; on a refusal one line, exit 5
    run(main, "gt_broker drain", mcp="vault_queue_drain")   ... naming the MCP alternative
    refusal_line(path, exc, tool=None)     the line itself (sandbox- or provenance-worded)

Stdlib only; never raises from its own code.
"""
import errno
import json
import os
import re
import shlex
import sys

SANDBOX_WHY = "sandbox mode: the vault is written only through the queue / gt-vault MCP"
READS_WHY = "and read only through the gt-vault MCP (sandbox_vault_reads deny)"
REFUSED_RC = 5


def _config():
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"),
                  encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                   # noqa: BLE001
        return {}


def _setting(d, name, default):
    v = d.get(name)
    return v.strip().lower() if isinstance(v, str) and v.strip() else default


def sandbox_mode_on():
    return _setting(_config(), "sandbox_mode", "off") == "on"


def sandbox_reads_denied():
    d = _config()
    return (_setting(d, "sandbox_mode", "off") == "on"
            and _setting(d, "sandbox_vault_reads", "deny") != "allow")


def in_sandbox():
    """Is this process inside Claude Code's sandbox with gt sandbox mode on? Native Windows has
    no Claude Code sandbox (permission rules on the file tools only), so a refusal there is
    never the sandbox's."""
    if os.name == "nt":
        return False
    if not (os.environ.get("SANDBOX_RUNTIME") or os.environ.get("CLAUDECODE")):
        return False
    return sandbox_mode_on()


def is_denial(exc):
    """EPERM / EACCES, and EROFS (bubblewrap's read-only bind) -- the OS said no."""
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in (
        errno.EPERM, errno.EACCES, errno.EROFS)


def recorded_interpreter():
    """The interpreter install.sh recorded as able to write where gt writes, or None."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "golden-thread",
                               "interpreter.json"), encoding="utf-8") as fh:
            p = json.load(fh).get("python")
        return p if isinstance(p, str) and p else None
    except Exception:                                   # noqa: BLE001
        return None


def terminal_command(argv=None, python=None):
    """The command that was run, spelled to paste into a terminal: an absolute interpreter, the
    script's absolute path, every argument as given -- with a relative --vault made absolute."""
    argv = list(sys.argv if argv is None else argv)
    py = python or recorded_interpreter() or sys.executable or "python3"
    out = [py]
    if argv:
        out.append(os.path.abspath(argv[0]))
    rest = argv[1:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--vault" and i + 1 < len(rest):
            out += [a, os.path.abspath(os.path.expanduser(rest[i + 1]))]
            i += 2
            continue
        if a.startswith("--vault="):
            a = "--vault=" + os.path.abspath(os.path.expanduser(a[len("--vault="):]))
        out.append(a)
        i += 1
    if os.name == "nt":
        import subprocess
        return subprocess.list2cmdline(out)
    return " ".join(shlex.quote(x) for x in out)


def _why_os(exc):
    code = getattr(exc, "errno", None) or 0
    return "%s, %s" % (os.strerror(code), errno.errorcode.get(code, "error")) if code \
        else str(exc)


def sandbox_line(path, exc=None, tool=None, mcp=None, command=None):
    """One line: what was refused, why (sandbox mode), and the exact next step."""
    # A temp sibling (TASKS.md.gt-tmp-123) reads as the file it stands for.
    path = re.sub(r"\.gt-tmp-\d+$", "", str(path))
    what = "%s%s" % (path, " (%s)" % _why_os(exc) if exc is not None else "")
    why = SANDBOX_WHY + (", " + READS_WHY if sandbox_reads_denied() else "")
    cmd = command or terminal_command()
    step = ("use the gt-vault MCP tool %s, or run this from a terminal: %s" % (mcp, cmd)
            if mcp else "run this from a terminal: %s" % cmd)
    if not tool:
        base = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""
        tool = base[:-3] if base.endswith(".py") else (base or "gt")
    return "%s: refused: %s -- %s. Next: %s" % (tool, what, why, step)


def provenance_line(path, exc, python=None):
    """The macOS-interpreter wording (0.20.0, unchanged): which interpreter, and the fix."""
    python = python or sys.executable or "python3"
    rec = recorded_interpreter()
    code = getattr(exc, "errno", None) or 0
    fix = ("run gt with the interpreter it recorded (%s)" % rec if rec and rec != python
           else "run gt with /usr/bin/python3" if sys.platform == "darwin"
           and python != "/usr/bin/python3" else "check the file's permissions")
    tail = (" -- on macOS a file carrying com.apple.provenance or a privacy-protected folder "
            "refuses some interpreters" if sys.platform == "darwin" else "")
    return ("gt: cannot write %s: %s (%s), running %s%s. Fix: %s, or give %s Full Disk Access "
            "(System Settings > Privacy & Security)."
            % (path, os.strerror(code) if code else exc, errno.errorcode.get(code, "error"),
               python, tail, fix, python))


def refusal_line(path, exc, python=None, tool=None, mcp=None):
    """The one line for a refused write: sandbox-worded inside Claude's sandbox, else the
    interpreter wording."""
    if in_sandbox():
        return sandbox_line(path, exc, tool=tool, mcp=mcp)
    return provenance_line(path, exc, python)


def run(main, tool, mcp=None, rc=REFUSED_RC):
    """Call main(); a refusal from the OS becomes one line on stderr and exit `rc`. Anything
    else propagates unchanged. An exception carrying `gt_line` (a tool already worded it)
    prints that line."""
    try:
        return main()
    except OSError as exc:
        line = getattr(exc, "gt_line", None)
        if not line and not is_denial(exc):
            raise
        if not line:
            path = getattr(exc, "filename", None) or "a file"
            line = refusal_line(path, exc, tool=tool, mcp=mcp)
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:                               # noqa: BLE001
            pass
        return rc


def refused(path, exc, tool=None):
    """-> a PermissionError carrying the one line as `gt_line` (raise it; run() prints it)."""
    line = refusal_line(path, exc, tool=tool)
    err = PermissionError(getattr(exc, "errno", None) or errno.EPERM, line, str(path))
    err.gt_line = line
    return err
