#!/usr/bin/env python3
"""gt_cc_version.py -- is this machine's Claude Code new enough for everything in this gt? (0.21.4)

    gt_cc_version.py check [--min 2.1.300] [--offer] [--json]

WHY. gt supports Claude Code 2.1.292 and newer, but several 0.21.4 pieces work only from 2.1.300
(the auto_compact busy check needs `background_tasks`; `/gt:gt-minimize compact` needs 2.1.296).
Below that they do nothing and say so. The owner (2026-10-16): "lets just check if it is 300 or
higher. If not then we help them install the new version." So install.sh asks this once.

WHAT IT DOES. Reads `claude --version` (e.g. "2.1.296 (Claude Code)") and compares it with --min.
  * new enough: one line, exit 0;
  * older, with --offer, at a real terminal (stdin and stdout): asks "Update Claude Code now?
    [Y/n]"; a yes runs `claude update` (Claude Code's own updater, whichever way it was installed)
    and checks again. It never updates without that yes;
  * older, anywhere else (no terminal, --accept-defaults, the install run from inside Claude):
    prints the command to run, exit 0 -- the install is never failed or stopped by this;
  * no `claude` on PATH: says how to install Claude Code, exit 0.
Exit: 0 always for `check` (it advises; nothing depends on it) | 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

MIN = "2.1.300"
INSTALL_URL = "https://docs.claude.com/en/docs/claude-code/setup"


def parse(v):
    """'2.1.296 (Claude Code)' -> (2, 1, 296), or None."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def installed(claude=None):
    """-> (version tuple or None, the text it printed, the claude path or None)."""
    claude = claude or os.environ.get("GT_CLAUDE_BIN") or shutil.which("claude")   # GT_CLAUDE_BIN: tests
    if not claude:
        return None, "", None
    try:
        p = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None, "", claude
    out = (p.stdout or p.stderr or "").strip()
    return parse(out), out, claude


def at_terminal():
    """A person at a real terminal (gt_tty: on Windows NUL passes a plain isatty test)."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import gt_tty
        return gt_tty.has_terminal(sys.stdin) and gt_tty.has_terminal(sys.stdout)
    except Exception:                                            # noqa: BLE001
        return False


def off_path_install():
    """-> the native installer's claude in ~/.local/bin when PATH does not reach it, or None. The
    Mac and Windows installers both put it there and both can leave that folder off PATH."""
    base = os.path.join(os.path.expanduser("~"), ".local", "bin")
    for name in ("claude.exe", "claude") if os.name == "nt" else ("claude",):
        p = os.path.join(base, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def text(t):
    return ".".join(str(x) for x in t)


def check(minimum=MIN, offer=False, ask=input, run=subprocess.run, claude=None):
    """-> a result dict; prints what it found (and asks, with offer at a terminal)."""
    want = parse(minimum)
    have, raw, path = installed(claude)
    res = {"min": minimum, "found": text(have) if have else None, "ok": False, "updated": False,
           "claude": path}
    if not path:
        off = None if claude else off_path_install()
        if off:
            res["off_path"] = off
            print("Claude Code is installed at %s, but that folder is not on your PATH, so `claude`"
                  " does not run in a new terminal." % off)
            print("  Add %s to PATH (the install guide's PATH step), open a new terminal, and run"
                  " the install again." % os.path.dirname(off))
            return res
        print("Claude Code was not found on PATH. gt needs it: install it from %s" % INSTALL_URL)
        return res
    if not have:
        print("Claude Code's version could not be read (`claude --version` said: %s)." % (raw or "nothing"))
        return res
    if have >= want:
        res["ok"] = True
        print("✓ Claude Code %s (%s or newer)" % (text(have), minimum))
        return res
    print("⚠ Claude Code %s is older than %s. gt works from 2.1.292, but some of this release needs %s"
          % (text(have), minimum, minimum))
    print("  (auto_compact's busy check, /gt:gt-minimize compact); below it they do nothing and say so.")
    if offer and at_terminal():
        try:
            yes = ask("  Update Claude Code now? It runs `claude update`. [Y/n] ").strip().lower() in ("", "y", "yes")
        except EOFError:
            yes = False
        if yes:
            try:
                p = run([path, "update"])
                rc = p.returncode
            except (OSError, subprocess.SubprocessError) as e:
                rc = None
                print("  `claude update` could not run (%s)." % type(e).__name__)
            again, _raw, _p = installed(path)
            res["found"] = text(again) if again else res["found"]
            if again and again >= want:
                res["ok"] = res["updated"] = True
                print("✓ Claude Code is now %s. Restart Claude Code to use it." % text(again))
                return res
            print("  Claude Code is still %s after `claude update`%s. Update it by hand: %s"
                  % (res["found"], "" if rc in (0, None) else " (exit %s)" % rc, INSTALL_URL))
            return res
        print("  Not updated. To update later, run:  claude update")
        return res
    print("  To update, run this in a terminal:  claude update")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_cc_version.py", description="check Claude Code's version")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check")
    c.add_argument("--min", default=MIN)
    c.add_argument("--offer", action="store_true", help="at a terminal, offer to run `claude update`")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "check" or not parse(a.min):
        ap.print_usage(sys.stderr)
        return 2
    res = check(a.min, a.offer)
    if a.json:
        print(json.dumps(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
