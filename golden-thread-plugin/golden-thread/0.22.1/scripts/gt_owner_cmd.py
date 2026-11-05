#!/usr/bin/env python3
"""gt_owner_cmd.py -- hand a command the owner must run to the owner, ready to paste (0.21.4).

    gt_owner_cmd.py hand [--script] [--json] -- CMD [ARG ...]

WHY. Some gt actions are gated on a person at a terminal on purpose: `gt_broker.py accept` refuses
with `needs_owner` when no person is driving it, because applying a reviewed write to a review target
(design.md, global-memory/) is the owner's sign-off. That gate is right and stays where it is. The
hand-off was the clumsy part: the refusal named a short form of the command, the owner retyped or
copied a long line (absolute path, conflict id, `--version N`, `--vault`), and every mis-copy was a
fresh refusal. In one session the same accept was retried four times, none of them a decision
problem (owner, 2026-10-16: "populate the paste so I don't even need to copy").

WHAT IT DOES. `hand(argv)` quotes the command once, for the shell it will be pasted into (sh/zsh;
on Windows PowerShell, the default shell of Windows Terminal: `& 'C:\\...\\python.exe' ...`), and:
  * copies it to the system clipboard -- pbcopy (macOS), clip (Windows), wl-copy, xclip or xsel
    (Linux) -- and says so; with no clipboard writer, or when the write fails, it prints the
    command for the owner to copy, with no error and the caller's exit status unchanged;
  * with `--script` (or the `owner_command_script` setting on), first writes the command to a
    throwaway script under ~/.claude/golden-thread/owner-cmd/ (0700 folder, 0600 file) and hands
    over the one short line `zsh <path>` (`sh <path>` off macOS) instead. The script runs the
    command; on exit 0 it deletes itself, on any other exit it stays so the owner can see what ran.
    Scripts older than 7 days are pruned. On Windows it is a .ps1 and the line is
    `powershell -NoProfile -ExecutionPolicy Bypass -File '<path>'` (the bypass covers that one
    process; a default policy refuses an unsigned script), with the same delete-only-on-0 rule.

WHAT IT NEVER DOES. It never reads the clipboard, only writes it. It never runs the command: the
person still performs the gated action at their own terminal, so nothing in the gate is bypassed.
The script is written outside the vault (a vault write goes only through the write queue).

TESTS. GT_CLIPBOARD_CMD names the clipboard writer (a command line that reads stdin) in place of
the detected one; `none` means no clipboard. In a test sandbox (GT_TEST_SANDBOX=1) the real
clipboard is never touched unless GT_CLIPBOARD_CMD says otherwise. GT_OWNER_CMD_DIR moves the
script folder.

Exit (CLI): 0 handed over (clipboard or printed) | 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gt_private                                                # noqa: E402

KEEP_DAYS = 7
CLIP_TIMEOUT = 5


def is_windows():
    return os.name == "nt"


_PS_BARE = re.compile(r"^[A-Za-z0-9_./:=+\\-]+$")     # nothing PowerShell reads specially


def ps_quote(arg):
    """One PowerShell argument: bare when it is plain, else single-quoted ('' for a ')."""
    arg = str(arg)
    return arg if _PS_BARE.match(arg) else "'" + arg.replace("'", "''") + "'"


def command_line(argv):
    """-> `argv` as one line for the shell it is pasted into: POSIX sh/zsh quoting, or on Windows a
    PowerShell line (the default shell of Windows Terminal) -- `& 'C:\\...\\python.exe' ...`, since a
    quoted path at the start of a PowerShell line is a string, not a command."""
    argv = [str(a) for a in argv]
    if is_windows():
        return "& " + " ".join(["'" + argv[0].replace("'", "''") + "'"] + [ps_quote(a) for a in argv[1:]])
    return shlex.join(argv)


def clipboard_writer():
    """-> (argv of a command that copies its stdin to the clipboard, name) or (None, why).
    GT_CLIPBOARD_CMD is a command line, or a JSON list of arguments (the tests use that form)."""
    env = os.environ.get("GT_CLIPBOARD_CMD")
    if env is not None:
        if env.strip().lower() in ("", "none", "off"):
            return None, "no clipboard (GT_CLIPBOARD_CMD=none)"
        if env.lstrip().startswith("["):
            try:
                return [str(x) for x in json.loads(env)], "GT_CLIPBOARD_CMD"
            except ValueError:
                return None, "GT_CLIPBOARD_CMD is not a valid JSON list"
        return shlex.split(env), "GT_CLIPBOARD_CMD"
    if os.environ.get("GT_TEST_SANDBOX"):
        return None, "test sandbox: the real clipboard is not touched"
    if sys.platform == "darwin":
        tool = shutil.which("pbcopy")
        return ([tool], "pbcopy") if tool else (None, "pbcopy not found")
    if is_windows():
        tool = shutil.which("clip")
        return ([tool], "clip") if tool else (None, "clip not found")
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return [shutil.which("wl-copy")], "wl-copy"
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            return [shutil.which("xclip"), "-selection", "clipboard"], "xclip"
        if shutil.which("xsel"):
            return [shutil.which("xsel"), "--clipboard", "--input"], "xsel"
    return None, "no clipboard tool (no display, or none of wl-copy, xclip, xsel)"


def copy(text):
    """Write `text` to the clipboard; never reads it. -> (True, tool) or (False, why)."""
    argv, why = clipboard_writer()
    if not argv:
        return False, why
    # clip.exe reads its input in the console code page unless it starts with a UTF-16 BOM, so a
    # path with a non-ASCII character would arrive mangled: hand it UTF-16LE with the BOM
    data = (("﻿" + text).encode("utf-16-le") if why == "clip" else text.encode("utf-8"))
    try:
        p = subprocess.run(argv, input=data, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=CLIP_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return False, "%s could not run (%s)" % (why, type(e).__name__)
    if p.returncode != 0:
        return False, "%s exited %d" % (why, p.returncode)
    return True, why


def script_dir():
    return os.environ.get("GT_OWNER_CMD_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "golden-thread", "owner-cmd")


def script_line(path):
    """-> the one line that runs the wrapper script at `path`."""
    if is_windows():
        # -ExecutionPolicy Bypass for this one process only: a default policy refuses unsigned .ps1
        return "powershell -NoProfile -ExecutionPolicy Bypass -File '%s'" % path.replace("'", "''")
    shell = "zsh" if sys.platform == "darwin" and shutil.which("zsh") else "sh"
    return shlex.join([shell, path])


SCRIPT = """#!/bin/sh
# gt: a command for you to run at your own terminal (gt_owner_cmd.py, %(when)s).
# It deletes itself only when the command exits 0, and stays when it fails so you can see what ran.
%(command)s
rc=$?
if [ "$rc" -eq 0 ]; then rm -f -- "$0"; fi
exit "$rc"
"""

# $? is read first: a command that could not start leaves $LASTEXITCODE unset
SCRIPT_PS1 = """# gt: a command for you to run at your own terminal (gt_owner_cmd.py, %(when)s).
# It deletes itself only when the command exits 0, and stays when it fails so you can see what ran.
%(command)s
$started = $?
$rc = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } elseif ($started) { 0 } else { 1 }
if ($rc -eq 0) { Remove-Item -LiteralPath $PSCommandPath -Force }
exit $rc
"""


def write_script(line, now=None):
    """-> (path, None), or (None, why) when no script can be written here. A .sh for sh/zsh, or on
    Windows a .ps1 (UTF-8 with a BOM, which Windows PowerShell 5.1 needs to read non-ASCII paths)."""
    d = gt_private.ensure_private_dir(script_dir())
    gt_private.prune(d, max_age_days=KEEP_DAYS, now=now)
    now = time.time() if now is None else now
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    ext, tmpl = (".ps1", SCRIPT_PS1) if is_windows() else (".sh", SCRIPT)
    path = os.path.join(d, "run-%s-%s%s" % (stamp, os.urandom(3).hex(), ext))
    body = tmpl % {"when": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), "command": line}
    data = (b"\xef\xbb\xbf" if is_windows() else b"") + body.encode("utf-8")
    if not gt_private.write_private(path, data):
        return None, "could not write %s" % path
    return path, None


def script_wanted():
    try:
        import gt_settings
        return (gt_settings.get("owner_command_script") or "off") == "on"
    except Exception:                                            # noqa: BLE001
        return False


def hand(argv, script=None):
    """Hand `argv` to the owner. -> {command, paste, clipboard, clipboard_why, script, lines}:
    `lines` are what to show (the caller prints them). Never runs the command."""
    command = command_line(argv)
    paste, path, note = command, None, None
    if script is None:
        script = script_wanted()
    if script:
        path, note = write_script(command)
        if path:
            paste = script_line(path)
    ok, why = copy(paste)
    lines = []
    if ok:
        lines.append("the command to run is on your clipboard -- paste it into a terminal "
                     "and press return:")
    else:
        lines.append("run this yourself, in a terminal (%s, so it is not on your clipboard):" % why)
    lines.append("    " + paste)
    if path:
        lines.append("(%s runs: %s -- and deletes itself if that succeeds)" % (path, command))
    elif note:
        lines.append("(no wrapper script: %s)" % note)
    return {"command": command, "paste": paste, "clipboard": ok, "clipboard_why": why,
            "script": path, "lines": lines}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_owner_cmd.py", description="hand a command to the owner "
                                 "to run at their own terminal: clipboard, else printed")
    sub = ap.add_subparsers(dest="cmd")
    h = sub.add_parser("hand", help="copy CMD to the clipboard (never runs it)")
    h.add_argument("--script", action="store_true", default=None,
                   help="wrap it in a self-deleting script and hand over `zsh <path>`")
    h.add_argument("--json", action="store_true")
    h.add_argument("command", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.cmd != "hand":
        ap.print_usage(sys.stderr)
        return 2
    cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not cmd:
        print("gt_owner_cmd.py hand: no command given (hand [--script] -- CMD ARG...)", file=sys.stderr)
        return 2
    res = hand(cmd, script=a.script)
    if a.json:
        print(json.dumps({k: v for k, v in res.items() if k != "lines"}, indent=1))
    else:
        print("\n".join(res["lines"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
