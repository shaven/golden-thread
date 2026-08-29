#!/usr/bin/env python3
"""Session report card: how well did this session actually use Golden Thread?

Runs at `SessionEnd` and `PreCompact`. Advisory only -- it never blocks anything
and never writes to the vault.

## Why

Every part of this system is opt-in at the moment of use: registering a session,
claiming a file, labelling a figure's verification state, promoting a finding up
the ladder, regenerating the rollup. Each is cheap; each is also easy to skip when
attention is on the actual work. Nothing has ever looked back at a finished session
and said "here is what you skipped."

The 2026-08-29 session is the worked example. It used `safe_write` throughout and
still let its registration lapse twice mid-session, writing to the vault unclaimed
both times. Nothing noticed, because nothing was looking.

## Two tiers

`~/.claude/vault-config.json` -> `"report_card"`:

  | value     | what it does                                                     |
  |-----------|------------------------------------------------------------------|
  | `off`     | nothing                                                          |
  | `minimal` | HYGIENE only: what went wrong in THIS session (**default**)      |
  | `full`    | hygiene, plus features available and unused, with the reason why |

`minimal` is the default because hygiene findings are always actionable and
always about work just done. `full` additionally looks at the vault as a whole
and suggests capability the user is not drawing on -- valuable, but noisier, and
worth opting into rather than being handed unasked at the end of every session.
"""
import json
import os
import re
import subprocess
import sys
import time

CONFIG = os.path.expanduser("~/.claude/vault-config.json")
DEFAULT_MODE = "minimal"
VALID = ("off", "minimal", "full")


def _cfg():
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except Exception:
        return {}



def _registry_get(name, fallback):
    """Prefer the shared settings registry so defaults live in ONE place.

    gt_settings.py is the single source of truth for what a setting means and what
    it defaults to; reading the config key directly here would create a second
    default that drifts from it -- the same duplication the plugin forbids for rule
    text in hooks. Falls back to reading the file only if the registry is absent,
    so a partial install still behaves.
    """
    try:
        import gt_settings
        v = gt_settings.get(name)
        if v:
            return v
    except Exception:
        pass
    return fallback


def mode():
    v = (_cfg().get("report_card") or "").strip().lower()
    return _registry_get("report_card", v if v in VALID else DEFAULT_MODE)


def vault():
    v = _cfg().get("vault_path")
    return v if v and os.path.isdir(v) else None


def _run(args, cwd=None):
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=15)
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def _age_days(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 86400.0
    except Exception:
        return None


# ---------------------------------------------------------------- hygiene ----

def check_session_registered(v, findings):
    """Did a session file exist, and does the edit ledger agree with its claims?"""
    sdir = os.path.join(v, "Projects", "golden-thread", "sessions")
    sessions = []
    if os.path.isdir(sdir):
        sessions = [f for f in os.listdir(sdir) if f.endswith(".md") and f != "README.md"]
    gitdir = os.path.join(v, ".git")
    ledger = os.path.join(gitdir, "gt-edits.jsonl")
    wrote = set()
    unclaimed_sessions = set()
    if os.path.exists(ledger):
        try:
            with open(ledger) as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("path"):
                        wrote.add(r["path"])
                    if not r.get("task"):
                        unclaimed_sessions.add(r.get("session", "?"))
        except Exception:
            pass
    if wrote and unclaimed_sessions:
        findings.append(
            ("hygiene", "Vault files were written by a session with no registered task "
             "(%s). Registration lapses silently mid-session -- `gt_session.py register` "
             "again after a `release`, or the writes are unattributed."
             % ", ".join(sorted(unclaimed_sessions))))
    if not sessions and wrote:
        findings.append(
            ("hygiene", "%d file(s) written this session with NO session registered at "
             "all. Nothing could have warned another session off them." % len(wrote)))


def check_uncommitted(v, findings):
    out = _run(["git", "-C", v, "status", "--porcelain"])
    rows = [l for l in out.splitlines() if l.strip()]
    if len(rows) >= 8:
        findings.append(
            ("hygiene", "%d uncommitted file(s) in the vault. Attribution lives in the "
             "commit message, so until these are committed there is no durable record "
             "of which session changed them." % len(rows)))


def check_tasks_fresh(v, findings):
    tasks = os.path.join(v, "TASKS.md")
    if not os.path.exists(tasks):
        return
    t_age = _age_days(tasks)
    newer = []
    proj = os.path.join(v, "Projects")
    if os.path.isdir(proj):
        for name in os.listdir(proj):
            r = os.path.join(proj, name, "README.md")
            if os.path.exists(r):
                a = _age_days(r)
                if a is not None and t_age is not None and a < t_age - 0.01:
                    newer.append(name)
    if newer:
        findings.append(
            ("hygiene", "TASKS.md is older than %d project README(s) (%s). It is generated "
             "-- re-run `gt_tasks.py`, or 'what's next?' answers from stale priority."
             % (len(newer), ", ".join(sorted(newer)[:4]))))


def check_safe_write_backlog(findings):
    """Pending writes that are still RECOVERABLE.

    Deliberately mirrors safe_write.outstanding(): an entry counts only when the
    file it was diverted to still exists. A `done: false` entry whose pending file
    was since deleted has nothing left to recover, and counting it produces a
    finding no one can act on -- caught on this check's first run, where a 2026-08-28
    test against an unwritable path reported as a real backlog. A report card that
    raises unactionable findings gets ignored, which costs more than the check gains.
    """
    ledger = os.path.expanduser("~/.claude/safe_write_ledger.jsonl")
    if not os.path.exists(ledger):
        return
    seen = {}
    try:
        with open(ledger) as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                seen[(e.get("target"), e.get("wrote_to"))] = e
    except Exception:
        return
    n = sum(1 for e in seen.values()
            if not e.get("done") and os.path.exists(e.get("wrote_to") or ""))
    if n:
        findings.append(
            ("hygiene", "%d safe_write entr(ies) never landed at their target and are "
             "still pending. Content exists but is not where anything will read it." % n))


# ------------------------------------------------------------ feature use ----

def check_promotion(v, findings):
    """Findings accumulating with nothing graduating up the ladder."""
    kn = os.path.join(v, "Knowledge")
    if not os.path.isdir(kn):
        return
    ages = [a for a in (_age_days(os.path.join(kn, f)) for f in os.listdir(kn)
                        if f.endswith(".md")) if a is not None]
    if not ages:
        return
    newest = min(ages)
    if newest > 30:
        findings.append(
            ("feature", "No Knowledge page has changed in %d days, while project "
             "research.md files keep growing. `/gt-promote` exists to graduate a finding "
             "that has now applied in a second context -- otherwise cross-project "
             "knowledge stays buried in one project's log." % int(newest)))


def check_validation_used(v, findings):
    """/gt-validate re-derives a claim from primary sources with fresh context."""
    hits = _run(["grep", "-rl", "--include=research.md", "-e", "independently verified",
                 os.path.join(v, "Projects")])
    if not hits.strip():
        findings.append(
            ("feature", "Nothing in the vault is labelled `independently verified`. "
             "`/gt-validate` re-derives a claim from primary sources in a fresh context, "
             "which is the only check that catches a wrong PREMISE -- reviewing your own "
             "reasoning inherits its blind spot."))


def check_review_queue(v, findings):
    q = os.path.join(v, "review-queue.md")
    if not os.path.exists(q):
        return
    try:
        with open(q) as fh:
            n = len([l for l in fh if l.strip().startswith("- [ ]")])
    except Exception:
        return
    if n:
        findings.append(("feature", "%d item(s) waiting in review-queue.md." % n))


def check_stale_pages(v, findings):
    kn = os.path.join(v, "Knowledge")
    if not os.path.isdir(kn):
        return
    stale = []
    for f in sorted(os.listdir(kn)):
        if not f.endswith(".md"):
            continue
        try:
            with open(os.path.join(kn, f)) as fh:
                head = fh.read(600)
        except Exception:
            continue
        if re.search(r"^status:\s*stale", head, re.M):
            stale.append(f)
    if stale:
        findings.append(
            ("feature", "%d Knowledge page(s) marked `status: stale` (%s). Stale pages are "
             "read as fact by any session that does not check the frontmatter."
             % (len(stale), ", ".join(stale[:3]))))


def check_memory_index(v, findings):
    """MEMORY.md is the index sessions actually load; orphans are invisible."""
    bad = []
    proj = os.path.join(v, "Projects")
    if not os.path.isdir(proj):
        return
    for name in sorted(os.listdir(proj)):
        mdir = os.path.join(proj, name, "memory")
        idx = os.path.join(mdir, "MEMORY.md")
        if not (os.path.isdir(mdir) and os.path.exists(idx)):
            continue
        try:
            body = open(idx).read()
        except Exception:
            continue
        orphans = [f for f in os.listdir(mdir)
                   if f.endswith(".md") and f != "MEMORY.md" and f[:-3] not in body]
        if orphans:
            bad.append("%s (%d)" % (name, len(orphans)))
    if bad:
        findings.append(
            ("feature", "Memory files not listed in their MEMORY.md index: %s. The index is "
             "what a session loads, so an unlisted file is effectively invisible."
             % ", ".join(bad[:4])))


def build(v, m):
    findings = []
    check_session_registered(v, findings)
    check_uncommitted(v, findings)
    check_tasks_fresh(v, findings)
    check_safe_write_backlog(findings)
    if m == "full":
        check_promotion(v, findings)
        check_validation_used(v, findings)
        check_review_queue(v, findings)
        check_stale_pages(v, findings)
        check_memory_index(v, findings)
    return findings


def main():
    m = mode()
    if m == "off":
        return 0
    v = vault()
    if not v:
        return 0
    try:
        findings = build(v, m)
    except Exception:
        return 0                      # advisory only: never fail a session close
    if not findings:
        print("GOLDEN THREAD report card (%s): clean." % m)
        return 0
    hy = [f for k, f in findings if k == "hygiene"]
    fe = [f for k, f in findings if k == "feature"]
    print("GOLDEN THREAD report card (%s)" % m)
    if hy:
        print("  This session:")
        for f in hy:
            print("   - %s" % f)
    if fe:
        print("  Available and unused:")
        for f in fe:
            print("   - %s" % f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
