#!/usr/bin/env python3
"""gt_procedure_drafts.py -- find the procedures you repeat (0.22.0; report only).

    gt_procedure_drafts.py find [--days N] [--min-sessions N] [--json]
    gt_procedure_drafts.py stage ID                  a skeleton draft skill, in gt's state folder only
    gt_procedure_drafts.py list [--json]
    gt_procedure_drafts.py approve SLUG [--yes]      the ONLY thing that copies a draft into ~/.claude/skills
    gt_procedure_drafts.py reject SLUG

Looks at the Bash tool calls in Claude Code's transcripts (~/.claude/projects) over the last N days (60),
reduces each command to its first two words (flags, paths and arguments are dropped, so no value can
appear), collapses consecutive duplicates, and reports every sequence of 5 steps that holds at least 3
distinct non-generic steps and appears in at least N sessions (3). Plain tool shapes such as
grep -> sed -> grep repeat everywhere and are not reported (measured 2026-10-18, research.md).

`find` WRITES NOTHING; each sequence it reports carries an id (8 hex of a hash of its steps). `stage ID`
turns one into a SKELETON draft skill under ~/.claude/golden-thread/state/drafts/skills/procedure-<id>/ --
the step command heads (the same two-word reduction) with a placeholder for the arguments, never a value and
never transcript text -- and never into a skills folder. The owner fills in the arguments and decides.
`approve SLUG` without --yes only says what it would do; with --yes it scans the draft again (hidden
characters, or a scan that cannot run, refuse it), refuses to overwrite a skill that exists, and copies it to
~/.claude/skills/<slug>/SKILL.md. The scan looks for hidden characters ONLY; it does not judge wording, so a
draft whose plain text reads like an instruction to Claude is approved if it carries no hidden character.
Reading the draft before `approve --yes` is the owner's review (a staged skeleton holds only command heads
and a placeholder, so plain wording gets there only if someone edits the draft).
`reject SLUG` deletes the draft. Nothing stages or approves by itself: no
hook calls this script.
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time

SAFE = re.compile(r"^[a-z0-9_.\-]{1,30}$")
GENERIC = {"grep", "sed", "ssh", "ls", "cat", "head", "tail", "find", "for h", "wc", "sort", "awk",
           "python -", "python -c", "python -m", "cd", "echo", "git status", "git diff", "git log",
           "git add", "rm", "mkdir", "cp", "mv", "test", "[", "bash -c", "which", "tee", "xargs", "jq",
           "open", "sleep", "for f", "for i", "while", "true", "export", "printf", "git show"}
N = 5
READ_BUDGET_S = 20.0


def normalize(cmd):
    cmd = cmd.strip().splitlines()[0] if cmd.strip() else ""
    cmd = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)+", "", cmd)
    toks = cmd.split()
    if not toks:
        return None
    t1, t2 = os.path.basename(toks[0]).lower(), ""
    if len(toks) > 1 and not toks[1].startswith(("-", "/", "~", ".", "$", "'", '"')):
        t2 = os.path.basename(toks[1]).lower()
    if t1.startswith("python") and len(toks) > 1:
        t1, t2 = "python", os.path.basename(toks[1]).lower()
    if not SAFE.match(t1):
        return None
    return t1 + (" " + t2 if t2 and SAFE.match(t2) else "")


def seq_id(steps):
    return hashlib.sha256("\n".join(steps).encode("utf-8")).hexdigest()[:8]


def drafts_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "state", "drafts", "skills")


def skills_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "skills")


SLUG = re.compile(r"^procedure-[0-9a-f]{8}$")


def _private_dir(path):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_private(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _scan_ok(text):
    """True only when the island scan ran and found no hidden characters. A scan that cannot run is not clean."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import gt_island_scan
        res = gt_island_scan.scan(text)
    except Exception:                                        # noqa: BLE001
        return False
    return res.get("status") == "scanned" and not any(f.get("hidden") for f in res.get("findings", []))


def _skeleton(sid, sessions, steps):
    lines = ["---", "name: procedure-%s" % sid,
             "description: Draft from a sequence repeated in %d sessions (%s). Fill in the arguments, then approve."
             % (sessions, ", ".join(steps)), "---", "",
             "# Procedure draft %s" % sid, "",
             "You ran these steps, in this order, in %d separate sessions. This is a skeleton: replace each "
             "placeholder with what you actually pass, say when to use it, then approve it." % sessions, ""]
    for i, step in enumerate(steps, 1):
        lines.append("%d. Run `%s` (add the arguments you need)." % (i, step))
    return "\n".join(lines) + "\n"


def stage(sid, days=60, min_sessions=3):
    """-> (exit code, message). Writes only under the state folder."""
    if not re.match(r"^[0-9a-f]{8}$", sid or ""):
        return 2, "not a sequence id (8 hex characters from `find`)"
    hit = next((s for s in find(days, min_sessions)["sequences"] if s["id"] == sid), None)
    if hit is None:
        return 2, "no such sequence in the last %d days (run `find` for the current ids)" % days
    slug = "procedure-" + sid
    d = os.path.join(drafts_dir(), slug)
    if os.path.isdir(d):
        return 0, "%s is already staged" % slug
    text = _skeleton(sid, hit["sessions"], hit["steps"])
    if not _scan_ok(text):
        return 2, "the draft could not be scanned clean, so nothing was staged"
    for part in (os.path.dirname(os.path.dirname(drafts_dir())), os.path.dirname(drafts_dir()), drafts_dir(), d):
        _private_dir(part)
    _write_private(os.path.join(d, "SKILL.md"), text)
    _write_private(os.path.join(d, "meta.json"), json.dumps({
        "id": sid, "slug": slug, "state": "draft", "sessions": hit["sessions"],
        "created": datetime.datetime.now().isoformat(timespec="seconds")}))
    return 0, "staged %s (%s); nothing is in your skills folder" % (slug, d)


def _meta(slug):
    try:
        with open(os.path.join(drafts_dir(), slug, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def list_drafts():
    out = []
    try:
        names = sorted(os.listdir(drafts_dir()))
    except OSError:
        return out
    for n in names:
        m = _meta(n) if SLUG.match(n) else None
        if m:
            out.append({"slug": n, "state": m.get("state", "draft"), "sessions": m.get("sessions", 0)})
    return out


def approve(slug, yes):
    if not SLUG.match(slug or ""):
        return 2, "not a draft name"
    m = _meta(slug)
    if m is None:
        return 2, "no such draft"
    target = os.path.join(skills_dir(), slug, "SKILL.md")
    if not yes:
        return 0, "would copy %s to %s -- run again with --yes to approve it" % (slug, target)
    if m.get("state") != "draft":
        return 2, "%s is already %s" % (slug, m.get("state"))
    if os.path.exists(os.path.dirname(target)):
        return 2, "a skill named %s already exists; it was not touched" % slug
    try:
        with open(os.path.join(drafts_dir(), slug, "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return 2, "the draft could not be read"
    if not _scan_ok(text):
        return 2, "the draft did not scan clean (hidden characters, or the scan could not run); nothing was copied"
    os.makedirs(os.path.dirname(target))
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    m["state"] = "approved"
    _write_private(os.path.join(drafts_dir(), slug, "meta.json"), json.dumps(m))
    return 0, "approved: copied to %s" % target


def reject(slug):
    if not SLUG.match(slug or "") or _meta(slug) is None:
        return 2, "no such draft"
    shutil.rmtree(os.path.join(drafts_dir(), slug), ignore_errors=True)
    return 0, "rejected %s (deleted)" % slug


def find(days=60, min_sessions=3):
    cutoff = time.time() - days * 86400
    deadline = time.time() + READ_BUDGET_S
    seqs, read = {}, False
    for path in glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", "*.jsonl")):
        if time.time() > deadline:
            break
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        read = True
        cmds = []
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"Bash"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    for b in (row.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash":
                            n = normalize(str((b.get("input") or {}).get("command") or ""))
                            if n and (not cmds or cmds[-1] != n):
                                cmds.append(n)
        except OSError:
            continue
        for i in range(len(cmds) - N + 1):
            g = tuple(cmds[i:i + N])
            specific = {t for t in g if t not in GENERIC and t.split()[0] not in GENERIC}
            if len(specific) >= 3:
                seqs.setdefault(g, set()).add(path)
    out = sorted(({"id": seq_id(g), "sessions": len(v), "steps": list(g)}
                  for g, v in seqs.items() if len(v) >= min_sessions),
                 key=lambda d: (-d["sessions"], d["steps"]))
    return {"days": days, "min_sessions": min_sessions, "transcripts_read": read, "sequences": out[:50]}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_procedure_drafts.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("--days", type=int, default=60)
    f.add_argument("--min-sessions", type=int, default=3)
    f.add_argument("--json", action="store_true")
    st = sub.add_parser("stage")
    st.add_argument("id")
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    ap_ = sub.add_parser("approve")
    ap_.add_argument("slug")
    ap_.add_argument("--yes", action="store_true")
    rj = sub.add_parser("reject")
    rj.add_argument("slug")
    a = ap.parse_args(argv)
    if a.cmd in ("stage", "approve", "reject", "list"):
        if a.cmd == "list":
            rows = list_drafts()
            print(json.dumps(rows) if a.json else
                  ("\n".join("%s  %s  (%d sessions)" % (r["slug"], r["state"], r["sessions"]) for r in rows)
                   or "no drafts"))
            return 0
        code, msg = (stage(a.id) if a.cmd == "stage" else approve(a.slug, a.yes) if a.cmd == "approve"
                     else reject(a.slug))
        print(msg)
        return code
    try:
        res = find(a.days, a.min_sessions)
    except Exception:                                        # noqa: BLE001
        res = {"days": a.days, "min_sessions": a.min_sessions, "transcripts_read": False, "sequences": []}
    if a.json:
        print(json.dumps(res))
    elif not res["transcripts_read"]:
        print("usage unknown: no transcripts could be read")
    else:
        print("%d repeated sequence(s) of %d steps in the last %d days (shown: first two words of each step)"
              % (len(res["sequences"]), N, res["days"]))
        for s in res["sequences"][:15]:
            print("  %s  %d sessions: %s" % (s["id"], s["sessions"], " -> ".join(s["steps"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
