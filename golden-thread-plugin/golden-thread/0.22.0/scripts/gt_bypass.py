#!/usr/bin/env python3
"""gt_bypass.py -- say so when Claude Code's permission prompts are off (0.22.0, phase 89e).

    gt_bypass.py [--cwd DIR]        print the one-line warning, or nothing

Two things switch the prompts off, and either one means gt's "asks first" promises and a lockdown
level's prompts no longer stand between a command and the machine:

  * the session was LAUNCHED with `--dangerously-skip-permissions` (or `--permission-mode
    bypassPermissions`): found by walking this process's ancestors' arguments with `ps`, the way the
    unlock code finds the session root. POSIX only; on Windows, or when `ps` fails, the launch is
    unknown and nothing is claimed;
  * a Claude Code settings layer (user, user-local, project, project-local) sets
    `permissions.defaultMode` to `bypassPermissions`.

gt_surface prints the line at session start (silent when neither holds), and gt_io_policy's
preallowed_tool counts a bypass launch as pre-allowed, so disk_write's unseen-write check is not
waved through by a flag given at launch. Read-only: it never writes and never changes a setting.
GT_BYPASS_ARGS stands in for the process walk (tests); GT_TEST_SANDBOX (every test process) means
the real tree is not walked. Stdlib only; never raises.
"""
import argparse
import json
import os
import re
import subprocess
import sys

FLAG = re.compile(r"(?:^|\s)(--dangerously-skip-permissions)(?=\s|$)|"
                  r"(?:^|\s)--permission-mode(?:=|\s+)(bypassPermissions)(?=\s|$)")
MAX_DEPTH = 16


def flag_in(args):
    """-> the bypass launch flag found in one process's argument text, else None."""
    m = FLAG.search(args or "")
    if not m:
        return None
    return m.group(1) or "--permission-mode bypassPermissions"


def _ps(pid):
    """-> (parent pid, argument text) for pid, or None when ps cannot say."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(int(pid))], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        ppid, _, args = out.partition(" ")
        return int(ppid), args.strip()
    except Exception:                                    # noqa: BLE001 - unknown, not a claim
        return None


def walk(pid, ps=_ps):
    """-> the bypass flag in an ancestor of pid (pid included), else None. Bounded, cycle-safe."""
    seen = set()
    for _ in range(MAX_DEPTH):
        if pid in seen or pid <= 1:
            return None
        seen.add(pid)
        row = ps(pid)
        if not row:
            return None
        hit = flag_in(row[1])
        if hit:
            return hit
        pid = row[0]
    return None


def launch_flag(env=None):
    """-> the bypass flag this session was launched with, else None."""
    env = os.environ if env is None else env
    if env.get("GT_BYPASS_ARGS") is not None:
        return flag_in(env["GT_BYPASS_ARGS"])
    if env.get("GT_TEST_SANDBOX") or os.name == "nt":
        return None
    return walk(os.getpid())


def settings_mode(cwd=None, home=None):
    """-> (what, file) for the first settings layer that sets bypassPermissions, else None. A file that
    is missing or unreadable is skipped here (preallowed_tool, which fails closed, handles the rest)."""
    home = home or os.path.expanduser("~")
    layers = [os.path.join(home, ".claude", "settings.json"),
              os.path.join(home, ".claude", "settings.local.json")]
    if cwd:
        layers += [os.path.join(cwd, ".claude", "settings.json"),
                   os.path.join(cwd, ".claude", "settings.local.json")]
    for path in dict.fromkeys(layers):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            if d["permissions"]["defaultMode"] == "bypassPermissions":
                return "permissions.defaultMode: bypassPermissions", path
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def line(cwd=None, home=None, env=None):
    """-> the one-line warning, or "" when the prompts are on."""
    flag = launch_flag(env)
    mode = settings_mode(cwd, home)
    if not flag and not mode:
        return ""
    why = []
    if flag:
        why.append("launch flag %s" % flag)
    if mode:
        why.append("settings %s (%s)" % mode)
    return ("BYPASS PERMISSIONS: %s -- Claude Code's own permission prompts are off, so gt's "
            "\"asks first\" promises and the lockdown level's prompts do not apply this session; "
            "restart without it to get them back." % "; ".join(why))


def main(argv=None):
    ap = argparse.ArgumentParser(description="warn when permission prompts are off")
    ap.add_argument("--cwd", default=os.getcwd())
    a = ap.parse_args(argv)
    out = line(a.cwd)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
