#!/usr/bin/env python3
"""gt_terminal.py -- open a terminal window that holds a command for the user to run (0.22.0).

    gt_terminal.py open --command "CMD" [--cwd DIR] [--dry-run]

Claude shows the user the exact command and asks one yes/no question; only on a yes does it run this.
The script never runs the command. It writes it into a private file (mode 700) under
~/.claude/golden-thread/terminal/ and asks the operating system to open that file in a terminal window,
where the user watches it run, reads the output (the window waits for a key) and may close it.

Refused with exit 2: an empty command, a newline in it, one over 2000 characters, one that looks like a
credential (the value is never printed or written). Exit 6: the credential scanner could not load (it
fails closed). Exit 3: no terminal program was found (the command is printed to paste instead).
macOS: iTerm2 if installed, else Terminal. Windows: a new cmd window. Linux: the first of
x-terminal-emulator, gnome-terminal, konsole, xterm.

Test hooks: GT_TERMINAL_PLATFORM, GT_TERMINAL_HAVE (space-separated programs present), GT_TERMINAL_NOOPEN.
"""
import argparse
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_CHARS = 2000
KEEP = 20
LINUX_TERMINALS = ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm")


def folder():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "terminal")


def platform():
    p = os.environ.get("GT_TERMINAL_PLATFORM") or sys.platform
    return "win32" if p.startswith(("win", "cygwin", "msys")) else ("darwin" if p == "darwin" else "linux")


def have(name, path=None):
    forced = os.environ.get("GT_TERMINAL_HAVE")
    if forced is not None:
        return name in forced.split()
    return os.path.isdir(path) if path else shutil.which(name) is not None


def check(command):
    """-> (exit code, message); 0 when the command may be placed in a window."""
    if not command.strip():
        return 2, "refused: the command is empty"
    if "\n" in command or "\r" in command:
        return 2, "refused: the command has a line break; give one command (join steps with &&)"
    if len(command) > MAX_CHARS:
        return 2, "refused: the command is over %d characters; put it in a script and open that" % MAX_CHARS
    try:
        if HERE not in sys.path:
            sys.path.append(HERE)
        import gt_secrets
        vault = None
        try:
            import json
            with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"), encoding="utf-8") as fh:
                vault = json.load(fh).get("vault_path")
        except Exception:                                # noqa: BLE001
            pass
        rules, _problems, _retracted = gt_secrets.rules_from_slot(vault)
        if not rules:
            raise RuntimeError("no detectors")
        hits = gt_secrets.scan_text(command, rules)
    except Exception as exc:                             # noqa: BLE001 -- a scanner that cannot run is a refusal
        return 6, "refused: the credential scan could not run (%s), so nothing was opened" % type(exc).__name__
    if hits:
        return 2, ("refused: the command looks like it holds a credential (rule %s); put the value in the "
                   "key store and name it by reference. Nothing was written or opened." % hits[0][1])
    return 0, ""


def write_script(command, cwd, plat):
    d = folder()
    os.makedirs(d, mode=0o700, exist_ok=True)
    ext = {"win32": ".cmd", "darwin": ".command", "linux": ".sh"}[plat]
    path = os.path.join(d, "run-%d-%s%s" % (int(time.time()), secrets.token_hex(3), ext))
    if plat == "win32":
        body = "@echo off\r\n%s\r\n@echo on\r\n%s\r\necho.\r\npause\r\n" % (
            ('cd /d "%s"' % cwd) if cwd else "rem", command)
    else:
        body = "#!/bin/bash\n%sset -x\n%s\nset +x\necho\nread -n 1 -r -s -p 'Done. Press any key to close this window.'\n" % (
            ("cd %s || exit 1\n" % shlex.quote(cwd)) if cwd else "", command)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    old = sorted((os.path.join(d, f) for f in os.listdir(d) if f.startswith("run-")), key=os.path.getmtime)
    for f in old[:-KEEP]:
        try:
            os.remove(f)
        except OSError:
            pass
    return path


def launcher(plat, path):
    """-> argv, or None when no terminal program was found."""
    if plat == "darwin":
        return ["open", "-a", "iTerm" if have("iTerm", "/Applications/iTerm.app") else "Terminal", path]
    if plat == "win32":
        return ["cmd", "/c", "start", "gt", "cmd", "/k", path]
    for t in LINUX_TERMINALS:
        if have(t):
            return [t, "-e", "bash", path] if t != "gnome-terminal" else [t, "--", "bash", path]
    return None


def cmd_open(a):
    command = a.command.strip()
    code, why = check(a.command)
    if code:
        print(why, file=sys.stderr)
        return code
    plat = platform()
    argv = launcher(plat, "<script file>" if a.dry_run else write_script(command, a.cwd, plat))
    if argv is None:
        print("no terminal program found on this machine; run this yourself:\n  %s" % command)
        return 3
    if a.dry_run:
        print("platform: %s\nwould open: %s\ncommand: %s" % (plat, " ".join(argv), command))
        return 0
    path = argv[-1]
    print("script: %s\nplatform: %s\ncommand: %s" % (path, plat, command))
    if os.environ.get("GT_TERMINAL_NOOPEN"):
        print("not opened (GT_TERMINAL_NOOPEN)")
        return 0
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        print("could not open a terminal (%s); run this yourself:\n  %s" % (type(exc).__name__, command))
        return 3
    print("opened a terminal window with the command")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open")
    o.add_argument("--command", required=True)
    o.add_argument("--cwd")
    o.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    return {"open": cmd_open}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
