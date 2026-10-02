#!/usr/bin/env python3
"""Session report card: how well did this session actually use Golden Thread?

Runs at `SessionEnd` and `PreCompact` to COMPUTE the card, and at `SessionStart`
(`gt_report_card.py surface --hook`) to DELIVER it. Advisory only -- it never blocks
anything.

## What it writes (corrected 2026-09-13)

An earlier docstring said this "never writes to the vault". That was false:

  - **In the vault:** when `closeout_check` is not `off`, each close-out candidate is
    recorded by running the vault's `gt_closeout.py --vault <vault> ask <slug>
    report-card`, which appends `Projects/golden-thread/closeout-signals.jsonl`.
    The vault is passed explicitly so the write names its target.
  - **Outside the vault:** the card itself is written to the pending-notice file
    `~/.claude/golden-thread/notices/report-card.md` (newest 3 cards kept), which the
    SessionStart run surfaces and then deletes.

## Why a notice file, not stdout (hooks reference, checked 2026-09-13)

Per Claude Code's hooks reference, plain stdout from `PreCompact` and `SessionEnd`
reaches NEITHER the model nor the user: only UserPromptSubmit, UserPromptExpansion,
SessionStart and PostModelSwitch add stdout to context, and `systemMessage` is not
documented for PreCompact/SessionEnd. So every card printed here -- including its
"ASK THE USER" close-out question -- was never seen by anyone. The card is now parked
in a file at compact/end and handed over at the next SessionStart as a JSON
`systemMessage` (terminal) plus `additionalContext` (model). stdout is still printed
for debug logs and manual runs.

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

`~/.claude/vault-config.json` -> `"report_card"` (declared by this module's module.json
since 0.15.0, when the card moved out of gt core into the `report-card` module):

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
import tempfile
import time

CONFIG = os.path.expanduser("~/.claude/vault-config.json")
# The stable hooks dir, where install.sh copies this script (a hookdir script) and gt's
# own gt_settings.py / gt_paths.py beside it.
HOOKS_DIR = os.path.expanduser("~/.claude/golden-thread/hooks")
# expanduser reads HOME at call time, so a test's fixture HOME is honoured.
NOTICE_DIR = os.path.expanduser("~/.claude/golden-thread/notices")
NOTICE = os.path.join(NOTICE_DIR, "report-card.md")
NOTICE_KEEP = 3                       # newest cards kept when several stack up
CARD_SEP = "\n<!-- gt-report-card -->\n"
MAX_CONTEXT = 8000                    # SessionStart output must stay well under 10,000
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

    Since 0.15.0 this script ships in the gt-report-card module, not beside gt's
    scripts, so gt_settings is looked for in the stable hooks dir as well as next to
    this file (which IS the hooks dir when a hook runs it). A copy run from the
    module's own tree finds the installed gt_settings there, or none, and falls back.
    """
    try:
        if HOOKS_DIR not in sys.path and os.path.isdir(HOOKS_DIR):
            sys.path.append(HOOKS_DIR)
        import gt_settings
        v = gt_settings.get(name)
        if v:
            return v
    except Exception:
        pass
    return fallback


def _cfg_choice(name, values, default):
    """The config value when it is one of `values`, else `default` -- the fallback used
    when no gt_settings registry is reachable. Before 0.15.0 the registry always sat
    beside this script, so `closeout_check` had no fallback of its own and would have
    ignored a configured `off`."""
    v = _cfg().get(name)
    v = v.strip().lower() if isinstance(v, str) else ""
    return v if v in values else default


def mode():
    return _registry_get("report_card", _cfg_choice("report_card", VALID, DEFAULT_MODE))


def closeout_mode():
    return _registry_get("closeout_check", _cfg_choice("closeout_check", ("off", "ask"), "ask"))


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


def closeout_candidates(v):
    """Projects the vault's own probe thinks may be finished. Runs the VAULT's copy of
    gt_closeout.py (seeded from templates/tools) so the rules the rollup uses and
    the rules this hook uses are one file. Records each prompt as `asked` with the
    signals at that moment, which is the data the thresholds get tuned against.

    That `ask` is a VAULT WRITE (it appends closeout-signals.jsonl), so `--vault` is
    always passed: the write names its target instead of the tool inferring one from
    its own location."""
    tool = os.path.join(v, "Projects", "golden-thread", "tools", "gt_closeout.py")
    if not os.path.exists(tool):
        return []
    out = _run([sys.executable, tool, "--vault", v, "candidates", "--json"])
    try:
        cands = json.loads(out) if out.strip() else []
    except Exception:
        return []
    for c in cands:
        _run([sys.executable, tool, "--vault", v, "ask", c["slug"], "report-card"])
    return cands


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


def render(m, findings, cands):
    """The card as text -- identical to what was printed before 0.12.9."""
    if not findings and not cands:
        return "GOLDEN THREAD report card (%s): clean." % m
    hy = [f for k, f in findings if k == "hygiene"]
    fe = [f for k, f in findings if k == "feature"]
    lines = ["GOLDEN THREAD report card (%s)" % m]
    if hy:
        lines.append("  This session:")
        lines += ["   - %s" % f for f in hy]
    if fe:
        lines.append("  Available and unused:")
        lines += ["   - %s" % f for f in fe]
    if cands:
        lines.append("  Projects that look finished -- ASK THE USER whether to close each one:")
        lines += ["   - %s (%s): %s" % (c["slug"], c["stage"], "; ".join(c["reasons"]))
                  for c in cands]
        lines.append("    Record the answer: python3 Projects/golden-thread/tools/gt_closeout.py "
                     "answer <slug> yes|no|later \"<why>\"")
        lines.append("    Closing means: stage: complete in README.md, leftover tasks to p:: 7, "
                     "then gt-work.")
    return "\n".join(lines)


def _hook_input(deadline=0.5):
    """The hook's stdin JSON, or {}. Never blocks: a manual run or a test runner may
    hand us a terminal or a pipe that is never closed."""
    try:
        import select
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        fd = sys.stdin.fileno()
        buf = b""
        end = time.time() + deadline
        while True:
            left = end - time.time()
            if left <= 0:
                break
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
        d = json.loads(buf.decode("utf-8", "replace")) if buf.strip() else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".report-card.", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def write_notice(card, hook):
    """Park the card for the next SessionStart. Stdout from PreCompact/SessionEnd is
    seen by no one (hooks reference, 2026-09-13), so this file is the delivery path."""
    header = "## Report card -- %s -- session %s -- %s" % (
        time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        hook.get("session_id") or "unknown",
        hook.get("hook_event_name") or "manual")
    cards = []
    try:
        with open(NOTICE, encoding="utf-8") as fh:
            cards = [c.strip("\n") for c in fh.read().split(CARD_SEP) if c.strip()]
    except Exception:
        cards = []
    cards.append(header + "\n" + card)
    _atomic_write(NOTICE, CARD_SEP.join(cards[-NOTICE_KEEP:]) + "\n")


def surface():
    """SessionStart: hand a parked card to the user (systemMessage) and the model
    (additionalContext), then delete it. Fails open and silent on any error."""
    try:
        if not os.path.isfile(NOTICE):
            return 0
        with open(NOTICE, encoding="utf-8") as fh:
            raw = fh.read()
        cards = [c.strip("\n") for c in raw.split(CARD_SEP) if c.strip()]
        if not cards:
            return 0
        n = sum(1 for c in cards for l in c.splitlines() if l.startswith("   - "))
        body = "\n\n".join(cards)
        if len(body) > MAX_CONTEXT:
            body = body[:MAX_CONTEXT] + ("\n[... truncated; %d more characters in %s]"
                                         % (len(body) - MAX_CONTEXT, NOTICE))
        ctx = ("Golden Thread report card(s) from the previous session, computed at "
               "PreCompact/SessionEnd where hook output is not shown to anyone. If it "
               "contains a close-out question (\"ASK THE USER\"), put that question to "
               "the user in your first reply.\n\n" + body)
        where = "your last session" if len(cards) == 1 else "your last %d sessions" % len(cards)
        msg = "Golden Thread report card from %s: %d finding%s -- see below" % (
            where, n, "" if n == 1 else "s")
        out = json.dumps({"systemMessage": msg,
                          "hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": ctx}})
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    except Exception:
        return 0
    try:
        os.unlink(NOTICE)
    except Exception:
        pass
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "surface" in argv:
        return surface()
    m = mode()
    if m == "off":
        return 0
    v = vault()
    if not v:
        return 0
    hook = _hook_input()
    try:
        findings = build(v, m)
    except Exception:
        return 0                      # advisory only: never fail a session close
    cands = closeout_candidates(v) if closeout_mode() != "off" else []
    card = render(m, findings, cands)
    try:
        write_notice(card, hook)
    except Exception:
        pass                          # the notice is best-effort; stdout still gets it
    print(card)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
