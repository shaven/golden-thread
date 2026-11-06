#!/usr/bin/env python3
"""gt_skill_report.py -- which of the skills you wrote yourself have gone unused (0.22.0).

    gt_skill_report.py report [--days N] [--json]       (default 60 days)

A skill nobody calls still costs: its one-line description sits in the context of every session. This
lists the skills in ~/.claude/skills and the project's .claude/skills that no transcript shows being called
in the last N days -- or never, when the skill's folder is itself older than N days. A call is a typed
`/skill` command or a Skill tool call, found in Claude Code's transcripts (~/.claude/projects).

REPORT ONLY: it deletes, edits and moves nothing. Skills that belong to plugins (gt's and others') are
never listed. If the transcripts cannot be read, usage is UNKNOWN and nothing is called stale.
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
import time

COMMAND = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
READ_BUDGET_S = 8.0
DEFAULT_DAYS = 60


def home():
    return os.path.expanduser("~")


def _ts(raw):
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def local_skills(cwd=""):
    """-> {name: folder} for the skills the user keeps themselves."""
    out = {}
    for root in (os.path.join(home(), ".claude", "skills"),
                 os.path.join(cwd, ".claude", "skills") if cwd else ""):
        try:
            for name in sorted(os.listdir(root)) if root else []:
                d = os.path.join(root, name)
                if os.path.isdir(d) and not name.startswith("."):
                    out.setdefault(name, d)
        except OSError:
            pass
    return out


def last_used(names, now=None):
    """-> ({name: epoch of the latest call}, transcripts_read)."""
    now = time.time() if now is None else now
    root = os.path.join(home(), ".claude", "projects")
    used, read = {}, os.path.isdir(root)             # a missing folder is "usage unknown"; an empty one is "none used"
    files = glob.glob(os.path.join(root, "*", "*.jsonl"))
    deadline = time.time() + READ_BUDGET_S
    for path in files:
        if time.time() > deadline:
            break
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "command-name" not in line and '"Skill"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    ts = _ts(row.get("timestamp")) or 0
                    content = (row.get("message") or {}).get("content")
                    texts = [content] if isinstance(content, str) else []
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "text":
                                texts.append(str(b.get("text") or ""))
                            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                                nm = str((b.get("input") or {}).get("skill") or "").split(":")[-1]
                                if nm in names:
                                    used[nm] = max(used.get(nm, 0), ts)
                    for t in texts:
                        for m in COMMAND.finditer(t):
                            nm = m.group(1).split(":")[-1]
                            if nm in names:
                                used[nm] = max(used.get(nm, 0), ts)
        except OSError:
            continue
    return used, read


def report(days=DEFAULT_DAYS, cwd="", now=None):
    now = time.time() if now is None else now
    skills = local_skills(cwd)
    used, read = last_used(set(skills), now)
    stale = []
    if read:
        cutoff = now - days * 86400
        for name, folder in sorted(skills.items()):
            if used.get(name, 0) >= cutoff:
                continue
            try:
                born = os.path.getmtime(folder)
            except OSError:
                continue
            if name not in used and born > cutoff:
                continue                                     # new, and not yet given a chance
            stale.append({"name": name, "last_used": None if name not in used else
                          datetime.datetime.fromtimestamp(used[name]).strftime("%Y-%m-%d")})
    return {"days": days, "skills": len(skills), "transcripts_read": read, "stale": stale}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_skill_report.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--days", type=int, default=DEFAULT_DAYS)
    r.add_argument("--json", action="store_true")
    r.add_argument("--cwd", default="")
    a = ap.parse_args(argv)
    try:
        d = report(a.days, a.cwd)
    except Exception:                                        # noqa: BLE001
        d = {"days": a.days, "skills": 0, "transcripts_read": False, "stale": []}
    if a.json:
        print(json.dumps(d))
    elif not d["transcripts_read"]:
        print("usage unknown: no transcripts could be read")
    elif d["stale"]:
        print("%d of %d local skill(s) unused for %d days: %s" % (
            len(d["stale"]), d["skills"], d["days"], ", ".join(s["name"] for s in d["stale"])))
    else:
        print("every local skill has been used in the last %d days (or is new)" % d["days"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
