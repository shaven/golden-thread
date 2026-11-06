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
  - **Outside the vault:** the card itself is parked as one file per card in
    `~/.claude/golden-thread/notices/report-cards/` (newest 3 per folder kept), which the
    SessionStart run of a session in the SAME folder surfaces and then deletes.

## Several sessions at once (0.21.4)

The owner runs several sessions at once, often in one folder. Until 0.21.4 every card went into
one shared file, read-modified-written with no lock -- two SessionEnds at the same moment lost a
card -- and the next session to start in ANY folder took every card, so a project-X close-out
question appeared in a project-Y session. Now each card is its own file, created under a name no
other writer can take (O_EXCL), carrying the folder (`cwd`) its session ran in. A starting session
takes only the cards whose folder is its own, claiming each by an atomic rename so two sessions
starting together never both show it; the others wait for their folder. The old shared file
(`report-card.md`, from an earlier release) is still read once, by whichever session starts
first, then removed.

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
import datetime
import glob
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
NOTICE = os.path.join(NOTICE_DIR, "report-card.md")       # pre-0.21.4 shared file: read, never written
CARD_DIR = os.path.join(NOTICE_DIR, "report-cards")         # one file per card (0.21.4)
NOTICE_KEEP = 3                       # newest cards kept per folder when several stack up
CARD_MAX_AGE = 30 * 86400             # a card nobody started a session for in a month is dropped
CARD_META = re.compile(r"^<!-- gt-card (\{.*\}) -->\n?", re.S)
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


def compact():
    """gt's `startup_output` (0.21.4), registered in gt_settings: compact unless it is `full`.
    The card is delivered at SessionStart, so it follows the setting that governs that output."""
    return _registry_get("startup_output",
                         _cfg_choice("startup_output", ("compact", "full"), "compact")) != "full"


class F(tuple):
    """One finding: unpacks as (kind, text) exactly as before 0.21.4, and carries `short`, the
    count-only line the compact card prints (no id lists, no "why this matters" -- that lives in
    /gt:gt-help and `gt_settings.py explain report_card`)."""
    def __new__(cls, kind, text, short=None):
        t = tuple.__new__(cls, (kind, text))
        t.short = short or text
        return t


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
        findings.append(F(
            "hygiene", "Vault files were written by a session with no registered task "
            "(%s). Registration lapses silently mid-session -- `gt_session.py register` "
            "again after a `release`, or the writes are unattributed."
            % ", ".join(sorted(unclaimed_sessions)),
            "%d session(s) wrote the vault with no registered task -- `gt_session.py register`"
            % len(unclaimed_sessions)))
    if not sessions and wrote:
        findings.append(F(
            "hygiene", "%d file(s) written this session with NO session registered at "
            "all. Nothing could have warned another session off them." % len(wrote),
            "%d file(s) written with NO session registered" % len(wrote)))


def check_uncommitted(v, findings):
    out = _run(["git", "-C", v, "status", "--porcelain"])
    rows = [l for l in out.splitlines() if l.strip()]
    if len(rows) >= 8:
        findings.append(F(
            "hygiene", "%d uncommitted file(s) in the vault. Attribution lives in the "
            "commit message, so until these are committed there is no durable record "
            "of which session changed them." % len(rows),
            "%d uncommitted file(s) in the vault" % len(rows)))


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
        findings.append(F(
            "hygiene", "TASKS.md is older than %d project README(s) (%s). It is generated "
            "-- re-run `gt_tasks.py`, or 'what's next?' answers from stale priority."
            % (len(newer), ", ".join(sorted(newer)[:4])),
            "TASKS.md older than %d project README(s) -- re-run `gt_tasks.py`" % len(newer)))


# What `gt_adr.py allocate` writes besides the heading: an allocation comment and, when asked
# for, structured field lines. A slot holding only these has no decision in it yet. The same
# rule lives in gt_state.py (the PreCompact message) and gt_adr.py's `status`; none of them can
# import another, so the three copies are pinned by tests.
_ADR_SCAFFOLD = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?(supersedes|expires[ _]when|expires)"
                           r"(?:\*\*)?\s*:", re.I)


def bodyless_adrs(v):
    """-> sorted ADR numbers whose spool slot has a heading and no decision text (0.21.0).

    Reads only Projects/golden-thread/spool/decisions/ -- never the rest of the vault -- and
    returns [] when it is not there or not readable: the card never fails a session close."""
    root = os.path.join(v, "Projects", "golden-thread", "spool", "decisions")
    found = set()
    try:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                m = re.match(r"^(\d{4})\.md$", name)
                if not m:
                    continue
                try:
                    with open(os.path.join(dirpath, name), encoding="utf-8", errors="replace") as fh:
                        lines = fh.read().strip().splitlines()
                except OSError:
                    continue
                body = [l for l in lines[2:] if l.strip() and not _ADR_SCAFFOLD.match(l)]
                if not body:
                    found.add(int(m.group(1)))
    except OSError:
        return []
    return sorted(found)


def check_unsaved_adrs(v, findings):
    """An ADR number was allocated and its slot never got a body: compaction drops the decision
    that was only ever said aloud (2026-09-24-precompact-unsaved-decisions-check)."""
    nums = bodyless_adrs(v)
    if nums:
        findings.append(F(
            "hygiene", "%s allocated with no body in the spool: the decision exists only in this "
            "conversation and compaction will drop it. Write it (`/gt:gt-work`) before compacting."
            % ", ".join("ADR-%d" % n for n in nums[:6]),
            "%d ADR(s) allocated with no body (%s) -- write them with /gt:gt-work"
            % (len(nums), ", ".join("ADR-%d" % n for n in nums[:3]))))


def check_queue_backlog(v, findings):
    """Queued vault writes nobody has drained for more than a day (0.22.0, review C2). With
    `queue_autodrain` on these are claimed, held or escalated requests; with it off, plain neglect."""
    q = os.path.join(v, "Projects", "golden-thread", "spool", "queue")
    try:
        names = [n for n in os.listdir(q) if n.endswith(".json") and not n.startswith(".")]
    except OSError:
        return
    now = time.time()
    n = 0
    for name in names:
        try:
            if now - os.stat(os.path.join(q, name)).st_mtime > 86400:
                n += 1
        except OSError:
            pass
    if n:
        findings.append(F(
            "hygiene", "%d queued vault write(s) have waited more than a day: `gt_broker.py drain "
            "--vault <vault>` applies them, or read why each is held in spool/broker." % n,
            "%d queued vault write(s) have waited more than a day" % n))


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
        findings.append(F(
            "hygiene", "%d safe_write entr(ies) never landed at their target and are "
            "still pending. Content exists but is not where anything will read it." % n,
            "%d safe_write entr(ies) never landed at their target" % n))


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
        findings.append(F(
            "feature", "No Knowledge page has changed in %d days, while project "
            "research.md files keep growing. `/gt-promote` exists to graduate a finding "
            "that has now applied in a second context -- otherwise cross-project "
            "knowledge stays buried in one project's log." % int(newest),
            "No Knowledge page changed in %d days -- /gt:gt-promote" % int(newest)))


def check_validation_used(v, findings):
    """/gt-validate re-derives a claim from primary sources with fresh context."""
    hits = _run(["grep", "-rl", "--include=research.md", "-e", "independently verified",
                 os.path.join(v, "Projects")])
    if not hits.strip():
        findings.append(F(
            "feature", "Nothing in the vault is labelled `independently verified`. "
            "`/gt-validate` re-derives a claim from primary sources in a fresh context, "
            "which is the only check that catches a wrong PREMISE -- reviewing your own "
            "reasoning inherits its blind spot.",
            "Nothing labelled `independently verified` -- /gt:gt-validate"))


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
        findings.append(F("feature", "%d item(s) waiting in review-queue.md." % n,
                          "%d item(s) waiting in review-queue.md" % n))


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
        findings.append(F(
            "feature", "%d Knowledge page(s) marked `status: stale` (%s). Stale pages are "
            "read as fact by any session that does not check the frontmatter."
            % (len(stale), ", ".join(stale[:3])),
            "%d Knowledge page(s) marked `status: stale`" % len(stale)))


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
        findings.append(F(
            "feature", "Memory files not listed in their MEMORY.md index: %s. The index is "
            "what a session loads, so an unlisted file is effectively invisible."
            % ", ".join(bad[:4]),
            "%d project(s) with memory files missing from MEMORY.md" % len(bad)))


CLOSEOUT_LIMIT = 3


def closeout_limit():
    """-> how many projects one close may ask about, or None for all (`closeout_limit`)."""
    raw = _registry_get("closeout_limit", _cfg().get("closeout_limit") or str(CLOSEOUT_LIMIT))
    raw = str(raw).strip().lower()
    if raw == "all":
        return None
    return int(raw) if raw.isdigit() and 1 <= int(raw) <= 100 else CLOSEOUT_LIMIT


def session_projects(v, sid):
    """-> the projects session `sid` wrote to, in first-write order, from the event stream."""
    if not sid:
        return []
    out = []
    try:
        with open(os.path.join(v, "Projects", "golden-thread", "events.jsonl"),
                  encoding="utf-8") as fh:
            for line in fh:
                if sid not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                p = e.get("project") if e.get("session") == sid else None
                if isinstance(p, str) and p and p not in out:
                    out.append(p)
    except OSError:
        pass
    return out


def closeout_candidates(v, sid=None):
    """Projects the vault's own probe thinks may be finished. Runs the VAULT's copy of
    gt_closeout.py (seeded from templates/tools) so the rules the rollup uses and
    the rules this hook uses are one file. Records each prompt as `asked` with the
    signals at that moment, which is the data the thresholds get tuned against.

    That `ask` is a VAULT WRITE (it appends closeout-signals.jsonl), so `--vault` is
    always passed: the write names its target instead of the tool inferring one from
    its own location."""
    tool = os.path.join(v, "Projects", "golden-thread", "tools", "gt_closeout.py")
    if not os.path.exists(tool):
        return [], 0
    out = _run([sys.executable, tool, "--vault", v, "candidates", "--json"])
    try:
        cands = json.loads(out) if out.strip() else []
    except Exception:
        return [], 0
    # the session's own projects first, then the probe's order; at most closeout_limit of
    # them, and only those are recorded as asked -- the rest come up at a later close
    mine = session_projects(v, sid)
    cands.sort(key=lambda c: mine.index(c["slug"]) if c.get("slug") in mine else len(mine))
    limit = closeout_limit()
    shown, rest = (cands, []) if limit is None else (cands[:limit], cands[limit:])
    for c in shown:
        _run([sys.executable, tool, "--vault", v, "ask", c["slug"], "report-card"])
    return shown, len(rest)


def closeout_deferred(v):
    """The one summary line for close-out questions the owner deferred with `later` (0.21.0,
    item #30), or "". A count and a date, never the projects: being told must cost no context.
    Reads the vault's own gt_closeout.py `deferred`, so the rule for "still in force" is one file."""
    tool = os.path.join(v, "Projects", "golden-thread", "tools", "gt_closeout.py")
    if not os.path.exists(tool):
        return ""
    out = _run([sys.executable, tool, "--vault", v, "deferred", "--json"])
    try:
        rep = json.loads(out) if out.strip() else {}
        if not rep.get("count"):
            return ""
        until = rep["first"] if rep["first"] == rep["last"] else "%s to %s" % (rep["first"], rep["last"])
        return "Close-out: %d project(s) deferred until %s (gt_closeout.py history)" % (
            rep["count"], until)
    except Exception:
        return ""


def _models_signature(vault):
    """The intent -> model mapping in effect (the same string gt_model_calibrate stores), or None
    when gt_model cannot be imported here."""
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [HOOKS_DIR] + sorted(glob.glob(os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(here))), "golden-thread", "*", "scripts")), reverse=True)
    for d in dirs:
        if os.path.isfile(os.path.join(d, "gt_model.py")):
            try:
                if d not in sys.path:
                    sys.path.append(d)
                import gt_model
                mapping = gt_model.mapping(vault)[0]
                return "|".join("%s=%s/%s" % (k, x.get("model"), x.get("effort") or "-")
                                for k, x in sorted(mapping.items()))
            except Exception:
                return None
    return None


def check_calibration_remind(v, findings):
    """One gentle line (0.21.0) when the last model-calibration pass is older than
    model_calibration_remind_days, or the intent -> model mapping changed since it (a newer model
    shipped). 0 turns it off; no pass ever, no line. It never runs a pass."""
    try:
        days = int(_registry_get("model_calibration_remind_days", _cfg_choice(
            "model_calibration_remind_days", ("0", "30", "60", "90"), "60")))
    except (TypeError, ValueError):
        days = 60
    if days <= 0:
        return
    try:
        with open(os.path.expanduser("~/.claude/golden-thread/model-calibration/latest.json"),
                  encoding="utf-8") as fh:
            last = json.load(fh)
        when = datetime.datetime.fromisoformat(str(last.get("updated")))
    except (OSError, ValueError, AttributeError, TypeError):
        return
    age = (datetime.datetime.now() - when).days
    why = []
    if age > days:
        why.append("the last pass was %d days ago (more than the %d-day reminder)" % (age, days))
    stored = last.get("models_signature")
    if stored and _models_signature(v) not in (None, stored):
        why.append("the models behind the fast, balanced and deep tiers have changed since")
    if why:
        findings.append(F("reminder", "Model calibration (%s): run /gt:gt-calibrate to re-check "
                          "which model each agent needs, or `gt_settings.py set "
                          "model_calibration_remind_days 0` to stop this line. It never runs by "
                          "itself." % "; ".join(why),
                          "Model calibration due (%s) -- /gt:gt-calibrate"
                          % "; ".join(("last pass %dd ago" % age) if w.startswith("the last pass")
                                      else "models changed" for w in why)))


def build(v, m):
    findings = []
    check_calibration_remind(v, findings)
    check_session_registered(v, findings)
    check_uncommitted(v, findings)
    check_tasks_fresh(v, findings)
    check_unsaved_adrs(v, findings)
    check_safe_write_backlog(findings)
    check_queue_backlog(v, findings)
    if m == "full":
        check_promotion(v, findings)
        check_validation_used(v, findings)
        check_review_queue(v, findings)
        check_stale_pages(v, findings)
        check_memory_index(v, findings)
    return findings


def _closeout_lines(cands, more):
    """The close-out question, the same words in the long and the compact card."""
    lines = ["  Projects that look finished -- ASK THE USER whether to complete each one:"]
    lines += ["   - %s (%s): %s" % (c["slug"], c["stage"], "; ".join(c["reasons"]))
              for c in cands]
    lines.append("    Record the answer: python3 Projects/golden-thread/tools/gt_closeout.py "
                 "answer <slug> yes|no|later [--until YYYY-MM-DD] \"<why>\"")
    lines.append("    Completing means (/gt:gt-complete <slug>): stage: complete in README.md, "
                 "leftover tasks to p:: 7, then gt-work.")
    if more:
        lines.append("    %d more look finished -- not asked this close (closeout_limit; "
                     "gt_closeout.py candidates lists them)." % more)
    return lines


def _short(f):
    return getattr(f, "short", None) or f[1]


def render(m, findings, cands, deferred="", more=0, short=False):
    """The card as text -- identical to what was printed before 0.12.9.

    `short` (startup_output compact, 0.21.4): one line per finding, counts not lists, no section
    headings and no reasons; the close-out question below is kept word for word."""
    if not findings and not cands and not deferred:
        return "GOLDEN THREAD report card (%s): clean." % m
    if short:
        order = {"hygiene": 0, "reminder": 1, "feature": 2}
        lines = ["GOLDEN THREAD report card (%s):" % m]
        lines += ["  - %s" % _short(f) for f in sorted(findings, key=lambda f: order.get(f[0], 3))]
        if cands:
            lines += _closeout_lines(cands, more)
        if deferred:
            lines.append("  %s" % deferred.replace(" (gt_closeout.py history)", ""))
        return "\n".join(lines)
    hy = [f for k, f in findings if k == "hygiene"]
    rm = [f for k, f in findings if k == "reminder"]
    fe = [f for k, f in findings if k == "feature"]
    lines = ["GOLDEN THREAD report card (%s)" % m]
    if hy:
        lines.append("  This session:")
        lines += ["   - %s" % f for f in hy]
    if rm:
        lines.append("  Reminder:")
        lines += ["   - %s" % f for f in rm]
    if fe:
        lines.append("  Available and unused:")
        lines += ["   - %s" % f for f in fe]
    if cands:
        lines += _closeout_lines(cands, more)
    if deferred:
        lines.append("  %s" % deferred)
    return "\n".join(lines)


def _fd_ready(fd, timeout):
    """True when `fd` has data (or EOF) within `timeout` seconds -- select() on POSIX.

    Native Windows (0.20.1): select() takes sockets only, so on a pipe it raised, the caller
    caught it, and every hook payload read as {} -- every card said "session unknown".
    There PeekNamedPipe answers the same question; a stdin that is not a pipe (a file, NUL)
    makes it fail, and os.read() will not block on those."""
    if os.name != "nt":
        import select
        return bool(select.select([fd], [], [], timeout)[0])
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.PeekNamedPipe.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    handle = msvcrt.get_osfhandle(fd)
    avail, end = wintypes.DWORD(), time.time() + timeout
    while True:
        if not k32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None):
            return True                     # not a pipe, or the writer closed it: EOF
        if avail.value or time.time() >= end:
            return bool(avail.value)
        time.sleep(0.01)


def _hook_input(deadline=0.5):
    """The hook's stdin JSON, or {}. Never blocks: a manual run or a test runner may
    hand us a terminal or a pipe that is never closed."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        fd = sys.stdin.fileno()
        buf = b""
        end = time.time() + deadline
        while True:
            left = end - time.time()
            if left <= 0:
                break
            if not _fd_ready(fd, left):
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
        d = json.loads(buf.decode("utf-8", "replace")) if buf.strip() else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _same_dir(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


def _card_meta(path):
    """-> the card's {cwd, session, event, at}, or None when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as fh:
            m = CARD_META.match(fh.readline())
        d = json.loads(m.group(1)) if m else None
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _card_files():
    try:
        names = os.listdir(CARD_DIR)
    except OSError:
        return []
    return sorted(os.path.join(CARD_DIR, n) for n in names if n.startswith("card-") and n.endswith(".md"))


def _create_exclusive(text, sid):
    """Write `text` as a new card file no other writer can take: a private temp file, then a hard
    link to the final name (O_EXCL semantics -- it fails if the name exists, and the next name is
    tried). Where hard links are not supported, O_CREAT|O_EXCL on the final name instead."""
    os.makedirs(CARD_DIR, exist_ok=True)
    tag = re.sub(r"[^A-Za-z0-9]", "_", str(sid or "unknown")[:8])
    fd, tmp = tempfile.mkstemp(prefix=".card.", dir=CARD_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        for i in range(100):
            final = os.path.join(CARD_DIR, "card-%019d-%s-%d-%d.md" % (time.time_ns(), tag, os.getpid(), i))
            try:
                os.link(tmp, final)
                return final
            except FileExistsError:
                continue
            except (OSError, NotImplementedError):
                try:
                    xfd = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                except FileExistsError:
                    continue
                with os.fdopen(xfd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                return final
        raise OSError("no free card name")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _prune(cwd, now):
    """Keep the newest NOTICE_KEEP cards of this folder; drop any card older than CARD_MAX_AGE."""
    mine = []
    for f in _card_files():
        try:
            age = now - os.path.getmtime(f)
        except OSError:
            continue
        if age > CARD_MAX_AGE:
            _unlink(f)
            continue
        meta = _card_meta(f)
        if meta is not None and _same_dir(meta.get("cwd") or "", cwd):
            mine.append(f)
    for f in mine[:-NOTICE_KEEP]:
        _unlink(f)


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _hook_cwd(hook):
    return str(hook.get("cwd") or "") or os.getcwd()


def write_notice(card, hook):
    """Park the card for the next SessionStart in the same folder. Stdout from
    PreCompact/SessionEnd is seen by no one (hooks reference, 2026-09-13), so this file is the
    delivery path. One file per card (0.21.4): concurrent closes never lose one."""
    cwd = _hook_cwd(hook)
    meta = {"cwd": cwd, "session": hook.get("session_id") or "",
            "event": hook.get("hook_event_name") or "manual", "at": time.time()}
    header = "## Report card -- %s -- session %s -- %s" % (
        time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        hook.get("session_id") or "unknown",
        hook.get("hook_event_name") or "manual")
    _create_exclusive("<!-- gt-card %s -->\n%s\n%s\n" % (json.dumps(meta), header, card),
                      hook.get("session_id"))
    _prune(cwd, time.time())


def take_cards(cwd):
    """-> this folder's parked cards, oldest first, each claimed by an atomic rename so a session
    starting at the same moment cannot show it too; plus the old shared file, once."""
    out = []
    for f in _card_files():
        meta = _card_meta(f)
        if meta is None or not _same_dir(meta.get("cwd") or "", cwd):
            continue
        claimed = "%s.taken-%d" % (f, os.getpid())
        try:
            os.rename(f, claimed)
        except OSError:
            continue                                  # another session took it first
        try:
            with open(claimed, encoding="utf-8") as fh:
                text = CARD_META.sub("", fh.read(), count=1).strip("\n")
            if text:
                out.append(text)
        except Exception:
            pass
        _unlink(claimed)
    try:
        if os.path.isfile(NOTICE):
            claimed = "%s.taken-%d" % (NOTICE, os.getpid())
            os.rename(NOTICE, claimed)
            try:
                with open(claimed, encoding="utf-8") as fh:
                    raw = fh.read()
            except Exception:
                os.rename(claimed, NOTICE)            # unreadable: left where it was, as before
                raise
            _unlink(claimed)
            out = [c.strip("\n") for c in raw.split(CARD_SEP) if c.strip()] + out
    except Exception:
        pass
    return out


HEADER = re.compile(r"^## Report card -- .*$", re.M)


def _compact_surface(cards):
    """startup_output compact (0.21.4): the parked card(s) without their timestamp/session-id
    headers, the same text to the terminal and the model, and the one instruction the model
    needs -- only when a card asks a close-out question."""
    bodies = [HEADER.sub("", c).strip("\n") for c in cards]
    body = "\n".join(b for b in bodies if b)
    if not body:
        return ""
    if len(body) > MAX_CONTEXT:
        body = body[:MAX_CONTEXT // 2] + "\n[... truncated]"
    if len(cards) > 1:
        body = "From your last %d sessions:\n%s" % (len(cards), body)
    ctx = body
    if "ASK THE USER" in body:
        ctx = "Put the ASK THE USER question below to the user in your first reply.\n" + body
    return json.dumps({"systemMessage": body,
                       "hookSpecificOutput": {"hookEventName": "SessionStart",
                                              "additionalContext": ctx}})


def surface():
    """SessionStart: hand this folder's parked cards to the user (systemMessage) and the model
    (additionalContext), and delete them. Cards of other folders stay for their own sessions.
    Fails open and silent on any error."""
    try:
        cards = take_cards(_hook_cwd(_hook_input()))
        if not cards:
            return 0
        if compact():
            out = _compact_surface(cards)
            if out:
                sys.stdout.write(out + "\n")
                sys.stdout.flush()
            return 0
        n = sum(1 for c in cards for l in c.splitlines() if l.startswith("   - "))
        body = "\n\n".join(cards)
        if len(body) > MAX_CONTEXT:
            body = body[:MAX_CONTEXT] + ("\n[... truncated; %d more characters not shown]"
                                         % (len(body) - MAX_CONTEXT))
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
    cands, more = closeout_candidates(v, hook.get("session_id")) \
        if closeout_mode() != "off" else ([], 0)
    deferred = closeout_deferred(v) if closeout_mode() != "off" else ""
    card = render(m, findings, cands, deferred, more, short=compact())
    # Parked for the next SessionStart only when a SESSION ended or compacted (0.20.1). A run
    # with no hook payload is a person or a script -- selftest.sh runs this to prove it works
    # -- and parking that card made the first real session after an install open with a report
    # on the installer's own smoke run ("session unknown -- manual"). It still prints.
    if hook.get("session_id") or hook.get("hook_event_name"):
        try:
            write_notice(card, hook)
        except Exception:
            pass                      # the notice is best-effort; stdout still gets it
    print(card)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
