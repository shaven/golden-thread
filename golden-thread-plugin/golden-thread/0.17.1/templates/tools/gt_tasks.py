#!/usr/bin/env python3
"""Roll every project's tasks into /TASKS.md.

Reads `## Tasks` checkbox lines from each Projects/<slug>/README.md, combines the
task's own `p::` with its project's `pp:` frontmatter, applies the four escalation
rules, and writes the rollup. Two more sources feed the top of the file:

  INBOX.md (vault root)   thoughts captured from anywhere, not yet filed to a project.
                          Unchecked lines render under "Inbox"; /gt:gt-review routes them.
  gt_closeout.py          projects whose signals say they may be finished render under
                          "Review", with the reasons, so closing a project is asked
                          about rather than forgotten.

Tasks at `p:: 7` or higher are SHELVED: kept in the README as a record, excluded from
every section and every escalation rule, counted in Project standing only. That is
how a finished project's leftover tasks stop outranking live work without being
deleted.

Each project's README.md is the only source of truth FOR ITS TASKS; the Inbox and
Review sections above them come from INBOX.md and gt_closeout.py as described above.
TASKS.md itself is a projection of all three — it is overwritten on every run and must
never be hand-edited.

Task events: a real write also emits gt_events `task.open` / `task.done` for checkbox
lines whose state differs from the event stream's last record of them. The stream is
the snapshot, so an unchanged vault emits nothing; nothing at all is emitted until
`gt_events.py backfill` has seeded the stream with task events.

Situational priority is computed HERE, at run time, against the real clock. It is
deliberately never stored: a written-down "current priority" is stale the moment it
lands on disk, while a stored rule stays correct forever.

Usage:  python3 Projects/golden-thread/tools/gt_tasks.py [--vault PATH] [--dry-run] [--json]

  --json   the ranked rollup as JSON on stdout; never writes TASKS.md
  GT_NOW   (env) pin the clock, ISO 8601 -- for tests
"""
import argparse
import hashlib
import os, datetime as dt, pathlib, re, shutil, sys
from zoneinfo import ZoneInfo

# Where the digest of the LAST generated TASKS.md is kept. A generation receipt: it answers
# "did I write what is on disk right now?", which is the only question that distinguishes a
# file this tool may replace from one a person has touched.
DIGEST_FILE = "Projects/golden-thread/.tasks-digest"
BACKUP_DIR = "Projects/golden-thread/backups"

# How many copies of a hand-edited TASKS.md to keep, and for how long. BOTH, because
# each bound alone fails a real case seen in this vault:
#
#   * count alone   a vault that backs up twice a year keeps copies for ever;
#   * age alone     a broken receipt (see write_rollup) makes EVERY run copy the file
#                   aside, so thirty days of hourly runs is hundreds of files.
#
# So: keep the newest KEEP_BACKUPS, then drop any of those older than KEEP_BACKUP_DAYS,
# except that the newest copy is never pruned -- the run that just made it must not
# find it gone, however old the clock says the vault is. Only `TASKS.md.*` is ever
# touched: gt_lint keeps its review-queue backups in this same directory.
KEEP_BACKUPS = 10
KEEP_BACKUP_DAYS = 30


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _backup_stamp():
    """The second-resolution timestamp in a backup's name.

    A seam as much as a helper: a test pins it to prove that two runs landing in the
    same second keep two copies rather than one.
    """
    return dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")


def _reserve_backup(bdir, base):
    """A path under `bdir` that did not exist a moment ago, created empty and exclusively.

    The stamp in `base` has one-second resolution and `shutil.copy2` overwrites without
    a word, so two runs inside the same second used to leave ONE file where two edits
    had been backed up -- silently destroying the very copy this code exists to keep.
    O_EXCL settles the race in the kernel; the suffix only has to be unique, not pretty.
    """
    for n in range(1000):
        p = bdir / (base if n == 0 else "%s.%d" % (base, n))
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return p
    raise OSError("cannot find an unused backup name under %s" % bdir)


def _prune_backups(bdir, now=None):
    """Apply the KEEP_BACKUPS / KEEP_BACKUP_DAYS bound. -> number of files removed.

    Best effort: pruning old copies must never be able to fail the rollup, and a file
    that cannot be removed simply stays.
    """
    now = now or dt.datetime.now()
    try:
        kept = sorted((p for p in bdir.glob("TASKS.md.*") if p.is_file()),
                      key=lambda p: p.name, reverse=True)      # names sort chronologically
    except OSError:
        return 0
    doomed = list(kept[KEEP_BACKUPS:])
    cutoff = (now - dt.timedelta(days=KEEP_BACKUP_DAYS)).timestamp()
    for p in kept[1:KEEP_BACKUPS]:            # [0] is the newest: never pruned
        try:
            if p.stat().st_mtime < cutoff:
                doomed.append(p)
        except OSError:
            continue
    removed = 0
    for p in doomed:
        try:
            p.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def write_rollup(vault, out):
    """Replace TASKS.md, but never silently over someone's edit. -> [note, ...]

    TASKS.md is a PROJECTION -- its own header says every task lives in a project's README.md
    and this file is generated from those. So a tick made here was never going to survive, by
    design. The defect was never that the edit is discarded; it is that it was discarded
    SILENTLY, by a bare truncating write with no record of what this tool had produced.

    Four things, in this order:
      1. compare what is on disk against the digest of what we generated last time;
      2. if they differ, copy the file aside BEFORE touching it and say so, naming where the
         edit actually belongs -- a backup nobody is told about is not a backup. The copy
         gets an exclusively-created name (two runs in one second must not overwrite each
         other's copy) and the directory is then pruned to KEEP_BACKUPS / KEEP_BACKUP_DAYS;
      3. write atomically (tmp + fsync + os.replace), so a crash or a Dropbox sync mid-write
         cannot leave a torn or empty TASKS.md at the vault root. Prevention beats recovery:
         with an atomic replace the half-written state never exists to recover from;
      4. record the generation receipt -- INSIDE a try. It is written AFTER the replace, so
         an exception there used to abandon the whole reporting path: TASKS.md was already
         gone, the backup was already made, and `return notes` never ran, so the one message
         telling the person where their edit had been kept was replaced by a traceback.
         Reproduced with the receipt path made a directory (IsADirectoryError). A receipt
         is bookkeeping about a write that has already happened; it must not be able to
         destroy the account of that write.

    Every note is RETURNED, not printed, so the caller decides where they go -- and so a
    failure late in this function cannot swallow the notes earned earlier in it.
    """
    target = vault / "TASKS.md"
    digest_path = vault / DIGEST_FILE
    notes = []

    if target.exists():
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None          # unreadable: back it up rather than reason about it
        try:
            recorded = digest_path.read_text(encoding="utf-8").strip()
        except OSError:
            recorded = ""
        # An absent receipt is UNKNOWN, not permission. But on the first run after this landed
        # there genuinely is none, and refusing would wedge every vault -- so back up and
        # continue, which is neither refusing nor overwriting in silence.
        if current is None or _sha(current) != recorded:
            stamp = _backup_stamp()
            bdir = vault / BACKUP_DIR
            bdir.mkdir(parents=True, exist_ok=True)
            kept = _reserve_backup(bdir, "TASKS.md.%s" % stamp)
            shutil.copy2(target, kept)
            notes.append("TASKS.md on disk is not what this tool last generated")
            notes.append("  kept a copy: %s" % kept.relative_to(vault))
            # Said on EVERY backup, not only on the second and later ones. This line used
            # to sit in the `else` of `if not recorded`, so the very first run -- the one
            # most likely to be swallowing a hand edit made before the check existed --
            # was the one run that never said where the edit belonged.
            notes.append("  Edits here do not survive: TASKS.md is generated from each "
                         "project's README.md `## Tasks`. Make the change there.")
            if not recorded:
                notes.append("  no previous digest recorded -- first run since this check "
                             "existed, so this is probably just the pre-existing file")
            pruned = _prune_backups(bdir)
            if pruned:
                notes.append("  pruned %d older backup(s): keeping the newest %d, none "
                             "older than %d days" % (pruned, KEEP_BACKUPS, KEEP_BACKUP_DAYS))

    tmp = target.with_name("TASKS.md.gt-tmp-%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(out)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)

    # The receipt comes last and cannot be allowed to matter more than the write it
    # describes. Without this guard an unwritable receipt raised AFTER TASKS.md had been
    # replaced, so the notes above -- including "kept a copy: ..." -- were never returned
    # and the person saw a traceback where the location of their backup should have been.
    try:
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        digest_path.write_text(_sha(out) + "\n", encoding="utf-8")
    except OSError as exc:
        notes.append("could not record the generation receipt %s (%s: %s)"
                     % (DIGEST_FILE, type(exc).__name__, exc))
        notes.append("  TASKS.md was written; but with no receipt the next run cannot tell "
                     "its own output from a hand edit, so it will copy TASKS.md aside and "
                     "say this again. Fix the path to stop the backups.")
    return notes


STALE_P1_DAYS = 7      # a p::1 older than this escalates its project one level
DEADLINE_DAYS = 3      # a due date within this many days escalates one level
SHELVED_P = 7          # p at or above this is shelved: kept, never ranked, never escalates
CLOSED_STAGES = ("complete", "archived")   # no soft escalation for a closed project

DAYS = {d: i for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])}
FIELD = re.compile(r"\[([a-z_]+)::\s*([^\]]*)\]")
TASK = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")


def frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        m = re.match(r'^([a-z_]+):\s*(.*)$', line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def as_int(raw, default, what):
    """A typo such as `[p:: high]` is reported and read as the default -- one bad
    README must not stop TASKS.md being written for every other project."""
    try:
        return int(raw or default)
    except ValueError:
        print(f"warning: {what}: {raw!r} is not a number, using {default}", file=sys.stderr)
        return default


def parse_tasks(text, where="README"):
    """Checkbox lines under a `## Tasks` heading, until the next `## ` heading."""
    m = re.search(r'^## Tasks\s*$', text, re.M)
    if not m:
        return []
    rest = text[m.end():]
    nxt = re.search(r'^## ', rest, re.M)
    block = rest[: nxt.start()] if nxt else rest
    tasks = []
    for line in block.splitlines():
        t = TASK.match(line)
        if not t:
            continue
        fields = dict(FIELD.findall(t.group(2)))
        label = FIELD.sub("", t.group(2)).strip().rstrip("—-").strip()
        tasks.append({
            "done": t.group(1).lower() == "x",
            "text": label,
            "p": as_int(fields.get("p"), 3, f"{where}: p:: on '{label}'"),
            "waiting": fields.get("waiting", "agent"),
            "due": fields.get("due"),
            "since": fields.get("since"),
            "blocks": fields.get("blocks"),
        })
    return tasks


def parse_inbox(path):
    """Unchecked checkbox lines in INBOX.md. `[project:: slug]` is a hint, not a filing:
    the line stays in the inbox until gt-review moves it and checks it off."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        t = TASK.match(line)
        if not t or t.group(1).lower() == "x":
            continue
        fields = dict(FIELD.findall(t.group(2)))
        out.append({"text": FIELD.sub("", t.group(2)).strip().rstrip("—-").strip(),
                    "project": fields.get("project"), "since": fields.get("since")})
    return out


def closeout_candidates(vault):
    """Projects gt_closeout.py thinks may be finished. Imported from beside this file;
    absent or broken, the section is simply empty -- the rollup must never fail
    because an advisory probe did."""
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import gt_closeout
        return gt_closeout.candidates(vault)
    except Exception:
        return []


def task_events(vault, observed):
    """task.open / task.done for checkboxes that changed since the event stream last saw
    them (gt_events.sync_tasks). Only on a real write -- never --dry-run or --json --
    and only once the stream has been seeded by `gt_events.py backfill`, so an ordinary
    rollup with nothing changed emits nothing. Absent or broken, the rollup is
    unaffected: an event log must never stop TASKS.md being written."""
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import gt_events
        return gt_events.sync_tasks(vault, observed)
    except Exception as exc:
        print(f"gt_tasks: task events NOT recorded ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 0


# -- time windows --------------------------------------------------------------
# Pure functions: no I/O, no config, no gt_tasks state. The time-window guard module
# imports parse_window / window_contains from here, so keep them that way.
#
# Two forms, both ending `<IANA zone> -> <target>`:
#
#   daily   `Mon-Fri 07:00-15:15 America/Chicago -> 0`
#           each day Mon..Fri, 07:00 to 15:15 that day. Unchanged since it shipped:
#           a reversed day range (Fri-Mon) or time range (22:00-02:00) never opens.
#   span    `Fri 16:00 → Sun 16:44 America/Chicago -> 0`   (`-` or `–` also accepted)
#           one continuous stretch of the week from Fri 16:00 to Sun 16:44. A span
#           whose end is earlier in the week than its start wraps past Sunday
#           midnight: `Sun 17:00 → Fri 16:00` is open all week except Fri 16:00-Sun 17:00.
#
# The zone always comes from the rule, never the host. The end is inclusive to the
# minute exactly as the daily form always was: 15:15:00 is inside, 15:15:01 is not.
_DAILY = re.compile(
    r'\s*(\w{3})-(\w{3})\s+(\d{2}):(\d{2})-(\d{2}):(\d{2})\s+(\S+)\s*->\s*(\d)\s*$')
_SPAN = re.compile(
    r'\s*(\w{3})\s+(\d{2}):(\d{2})\s*(?:→|–|-)\s*(\w{3})\s+(\d{2}):(\d{2})\s+(\S+)\s*->\s*(\d)\s*$')
_WEEK = 7 * 86400


def _hm(h, m):
    h, m = int(h), int(m)
    if h > 23 or m > 59:
        raise ValueError
    return h * 3600 + m * 60


def parse_window(rule):
    """Parse a window rule. Returns a dict, or None when the rule is empty, malformed,
    names an unknown day, or names a zone zoneinfo cannot load."""
    if not rule:
        return None
    try:
        m = _DAILY.match(rule)
        if m:
            d1, d2, h1, m1, h2, m2, tz, target = m.groups()
            out = {"kind": "daily", "days": (DAYS[d1], DAYS[d2]),
                   "start": _hm(h1, m1), "end": _hm(h2, m2)}
        else:
            m = _SPAN.match(rule)
            if not m:
                return None
            d1, h1, m1, d2, h2, m2, tz, target = m.groups()
            out = {"kind": "span",
                   "start": DAYS[d1] * 86400 + _hm(h1, m1),
                   "end": DAYS[d2] * 86400 + _hm(h2, m2)}
        out.update(zone=ZoneInfo(tz), tz=tz, target=int(target))
        return out
    except Exception:
        return None


def window_contains(window, now):
    """Is `now` (an aware datetime; naive is read as UTC, never host-local) inside a
    parsed window?"""
    if not window:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    local = now.astimezone(window["zone"])
    secs = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
    if window["kind"] == "daily":
        d1, d2 = window["days"]
        return d1 <= local.weekday() <= d2 and window["start"] <= secs <= window["end"]
    t = local.weekday() * 86400 + secs
    s, e = window["start"], window["end"]
    return s <= t <= e if s <= e else (t >= s or t <= e)


def window_open(rule, now):
    """`Mon-Fri 07:00-15:15 America/Chicago -> 0` — the target PP if open now, else None."""
    w = parse_window(rule)
    return w["target"] if w and window_contains(w, now) else None


def days_since(datestr, today):
    if not datestr:
        return None
    try:
        return (today - dt.date.fromisoformat(datestr)).days
    except ValueError:
        return None


def effective(pp, escalate, tasks, now, today, stage="active"):
    """Lower is more urgent.

    PP0 means active harm accruing right now. Only an explicitly declared window
    (`pp_escalate`) can reach it — that is a deliberate statement that this period
    IS the harm window. The softer rules below are evidence of neglect, not of
    active harm, so they escalate but floor at PP1. Without that floor, three
    projects sat at PP0 simultaneously on the first run and the level stopped
    meaning anything.
    """
    eff, why = pp, []
    tgt = window_open(escalate, now)
    if tgt is not None and tgt < eff:
        eff = tgt
        why.append(f"window open -> PP{tgt}")
    # Shelved tasks are a record, not work: they never escalate. Nor does a closed
    # project -- its leftover dated tasks used to hold it at PP1 for weeks.
    if stage in CLOSED_STAGES:
        return max(eff, 0), why + [f"stage {stage}: no escalation"]
    open_tasks = [t for t in tasks if not t["done"] and t["p"] < SHELVED_P]
    soft = eff  # soft rules may not push below PP1

    stale = [t for t in open_tasks
             if t["p"] == 1 and (days_since(t["since"], today) or 0) > STALE_P1_DAYS]
    if stale:
        oldest = max(days_since(t["since"], today) for t in stale)
        soft = min(soft, pp - 1)
        why.append(f"{len(stale)} stale P1 (oldest {oldest}d)")

    # days_since is 0 on the due date itself, so test for None: `or -99` dropped it.
    due_in = [days_since(t["due"], today) for t in open_tasks]
    soon = [t for t, d in zip(open_tasks, due_in) if d is not None and d >= -DEADLINE_DAYS]
    if soon:
        soft = min(soft, pp - 1)
        why.append(f"{len(soon)} due within {DEADLINE_DAYS}d")

    blocking = [t for t in open_tasks if t["blocks"]]
    if blocking:
        soft = min(soft, pp - 1)
        why.append("blocks " + ", ".join(sorted({t["blocks"] for t in blocking})))

    eff = min(eff, max(soft, 1))
    return max(eff, 0), why


def main():
    ap = argparse.ArgumentParser()
    # The default is the vault this copy of the tool was installed into (tools live at
    # <vault>/Projects/golden-thread/tools/), which is why a bare run works. $GT_VAULT
    # still wins, so a session opened against one vault cannot regenerate another's
    # TASKS.md by accident -- core_explicit_vault_target.
    ap.add_argument("--vault", default=os.environ.get("GT_VAULT")
                    or str(pathlib.Path(__file__).resolve().parents[3]))
    ap.add_argument("--dry-run", "-n", action="store_true",
                    help="print the rollup to stdout; do not write TASKS.md")
    ap.add_argument("--json", action="store_true",
                    help="print the ranked rollup as JSON to stdout; never writes TASKS.md")
    args = ap.parse_args()
    vault = pathlib.Path(args.vault)
    projects_dir = vault / "Projects"
    if not projects_dir.is_dir():
        sys.exit(f"no Projects/ under {vault}")

    now = dt.datetime.now(dt.timezone.utc)
    if os.environ.get("GT_NOW"):   # fixed clock for tests; ISO 8601, naive = UTC
        try:
            raw = os.environ["GT_NOW"].strip()
            now = dt.datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        except ValueError:
            sys.exit(f"GT_NOW: {os.environ['GT_NOW']!r} is not an ISO 8601 datetime")
        now = (now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc)
    today = now.astimezone().date()
    stamp = now.astimezone().strftime("%Y-%m-%d %H:%M %Z")

    rows, projects, observed = [], [], []
    # Sub-projects live one level deeper (parent: in frontmatter). A single-level
    # glob silently drops them -- the same class of bug CONVENTIONS.md warns about
    # for gt-lint's memory-unlisted check. Scan both levels.
    readmes = sorted(set(projects_dir.glob("*/README.md")) | set(projects_dir.glob("*/*/README.md")))
    for readme in readmes:
        text = readme.read_text()
        fm = frontmatter(text)
        if fm.get("type") != "project":
            continue
        slug = fm.get("slug", readme.parent.name)
        if fm.get("parent"):
            slug = fm["parent"] + "/" + slug
        where = readme.relative_to(vault)
        pp = as_int(fm.get("pp"), 3, f"{where}: pp")
        tasks = parse_tasks(text, where)
        stage = fm.get("stage", "?")
        eff, why = effective(pp, fm.get("pp_escalate"), tasks, now, today, stage)
        win = window_open(fm.get("pp_escalate"), now) is not None
        projects.append({"slug": slug, "domain": fm.get("domain", "?"),
                         "stage": stage, "pp": pp, "eff": eff, "why": why, "window_open": win,
                         "n": len([t for t in tasks if not t["done"] and t["p"] < SHELVED_P]),
                         "shelved": len([t for t in tasks if not t["done"] and t["p"] >= SHELVED_P])})
        observed += [(where.as_posix(), slug, t["text"], t["done"]) for t in tasks]
        for t in tasks:
            if not t["done"] and t["p"] < SHELVED_P:
                rows.append({**t, "slug": slug, "pp": pp, "eff": eff, "window_open": win})

    inbox = parse_inbox(vault / "INBOX.md")
    review = closeout_candidates(vault)

    rows.sort(key=lambda r: (r["eff"], r["p"], r["due"] or "9999", r["slug"]))
    projects.sort(key=lambda p: (p["eff"], p["pp"], p["slug"]))

    if args.json:
        # Read-only by construction: returns before TASKS.md is even rendered.
        def stale(r):
            age = days_since(r["since"], today)
            return r["p"] == 1 and age is not None and age > STALE_P1_DAYS
        import json
        print(json.dumps({
            "version": 1,
            "generated_at": now.isoformat(),
            "sections": {"inbox": inbox, "review": review},
            "tasks": [{"rank": i, "project": r["slug"], "priority": f"PP{r['eff']}-P{r['p']}",
                       "text": r["text"], "waiting": r["waiting"], "due": r["due"],
                       "since": r["since"], "window_open": r["window_open"], "stale": stale(r)}
                      for i, r in enumerate(rows, 1)],
        }, indent=2, ensure_ascii=False, default=str))
        return

    def fmt(r):
        bits = []
        if r["due"]:
            d = days_since(r["due"], today)
            bits.append(f"due {r['due']}" + (f" (**{abs(d)}d overdue**)" if d and d > 0 else ""))
        if r["since"]:
            age = days_since(r["since"], today)
            if age is not None and r["p"] == 1 and age > STALE_P1_DAYS:
                bits.append(f"**stale {age}d**")
            elif age is not None:
                bits.append(f"{age}d old")
        if r["blocks"]:
            bits.append(f"blocks `{r['blocks']}`")
        return " · ".join(bits)

    L = []
    L.append("# Tasks — all projects\n")
    L.append(f"> **Generated {stamp}** by `Projects/golden-thread/tools/gt_tasks.py`.")
    L.append("> Do not edit this file. Every task lives in its project's `README.md`")
    L.append("> under `## Tasks`; this is a projection of those files.\n")
    L.append("Sort key is `PP<effective>-P<task>`. **PP is the project's priority at this")
    L.append("moment** — baseline from frontmatter, raised by any escalation rule that is")
    L.append("currently firing. Re-run the script to re-evaluate; the answer changes with")
    L.append("the clock even when no file has changed.\n")
    L.append(f"See [[CONVENTIONS]] > Priority. Thresholds: stale P1 > {STALE_P1_DAYS}d, deadline within {DEADLINE_DAYS}d; p >= {SHELVED_P} is shelved.\n")
    L.append("---\n")

    L.append(f"## Inbox — not yet filed ({len(inbox)})\n")
    L.append("Captured from anywhere, in `INBOX.md`. `/gt:gt-review` routes each one to a project;")
    L.append("until then it is here so it is not lost, and nowhere else so it does not rank.\n")
    if inbox:
        L.append("| Captured | Thought | Hint |")
        L.append("|---|---|---|")
        for i in inbox:
            L.append(f"| {i['since'] or '—'} | {i['text']} | {('`' + i['project'] + '`') if i['project'] else '—'} |")
    else:
        L.append("*Empty. Add a checkbox line to `INBOX.md` from any session.*")

    L.append(f"\n## Review — projects that may be ready to close ({len(review)})\n")
    L.append("Computed by `gt_closeout.py` from task state and write-back history. A row here is a")
    L.append("question, not a verdict: answer it with `gt_closeout.py answer <slug> yes|no|later`.\n")
    if review:
        L.append("| Project | Stage | Why it looks finished | Open / shelved |")
        L.append("|---|---|---|---|")
        for s in review:
            L.append(f"| `{s['slug']}` | {s['stage']} | {'; '.join(s['reasons'])} | {s['open']} / {s['shelved']} |")
    else:
        L.append("*Nothing looks finished right now.*")
    L.append("\n---\n")

    yours = [r for r in rows if r["waiting"] == "user"]
    mine = [r for r in rows if r["waiting"] == "agent"]

    L.append(f"## Waiting on you ({len(yours)})\n")
    L.append("Decisions and actions nobody else can take.\n")
    L.append("| | Task | Project | Notes |")
    L.append("|---|---|---|---|")
    for r in yours:
        L.append(f"| `PP{r['eff']}-P{r['p']}` | {r['text']} | `{r['slug']}` | {fmt(r)} |")

    L.append(f"\n## Ready to work ({len(mine)})\n")
    L.append("Nothing blocking these — say the word.\n")
    L.append("| | Task | Project | Notes |")
    L.append("|---|---|---|---|")
    for r in mine:
        L.append(f"| `PP{r['eff']}-P{r['p']}` | {r['text']} | `{r['slug']}` | {fmt(r)} |")

    other = [r for r in rows if r["waiting"] not in ("user", "agent")]
    if other:
        L.append(f"\n## External and parked ({len(other)})\n")
        L.append("| | Task | Project | State |")
        L.append("|---|---|---|---|")
        for r in other:
            L.append(f"| `PP{r['eff']}-P{r['p']}` | {r['text']} | `{r['slug']}` | `{r['waiting']}` |")

    L.append("\n---\n")
    L.append("## Project standing\n")
    L.append("| Project | Domain | Stage | Baseline | Now | Why it moved | Open | Shelved |")
    L.append("|---|---|---|---|---|---|---|---|")
    for p in projects:
        moved = f"**PP{p['eff']}**" if p["eff"] != p["pp"] else f"PP{p['eff']}"
        L.append(f"| `{p['slug']}` | {p['domain']} | {p['stage']} | PP{p['pp']} | {moved} | "
                 f"{'; '.join(p['why']) or '—'} | {p['n']} | {p['shelved'] or '—'} |")

    L.append("\n---\n")
    L.append("## Live views (Obsidian only)\n")
    L.append("Dataview renders these; they are inert as raw markdown, which is why the")
    L.append("static tables above exist. Same dual-reader pattern as `Projects/README.md`.\n")
    L.append("### Everything waiting on you\n")
    L.append("```dataview\nTASK\nFROM \"Projects\"\nWHERE !completed AND waiting = \"user\"\nSORT p ASC\n```\n")
    L.append("### Every open P1\n")
    L.append("```dataview\nTASK\nFROM \"Projects\"\nWHERE !completed AND p = 1\nSORT file.folder ASC\n```\n")
    L.append("### Projects by baseline priority\n")
    L.append("```dataview\nTABLE WITHOUT ID link(file.folder, slug) AS Project, pp AS PP, stage AS Stage, domain AS Domain\n"
             "FROM \"Projects\"\nWHERE type = \"project\" AND file.name = \"README\"\nSORT pp ASC, slug ASC\n```\n")

    out = "\n".join(L) + "\n"
    if args.dry_run:
        print(out)
        # `len(L)` counted APPEND CALLS, several of which push a multi-line chunk (the
        # dataview blocks alone are five lines each), so the number was always short of
        # the file it described. Count the lines actually rendered.
        print("dry run: TASKS.md not written (%d line(s) rendered)" % len(out.splitlines()),
              file=sys.stderr)
        return
    for note in write_rollup(vault, out):
        print(note, file=sys.stderr)
    print(f"TASKS.md written — {len(rows)} open tasks across {len(projects)} projects")
    emitted = task_events(vault, observed)
    if emitted:
        print(f"  events: {emitted} task.open/task.done")
    print(f"  waiting on you: {len(yours)}   ready to work: {len(mine)}   other: {len(other)}   "
          f"inbox: {len(inbox)}   review: {len(review)}   shelved: {sum(p['shelved'] for p in projects)}")
    for s in review:
        print(f"  REVIEW {s['slug']}: {'; '.join(s['reasons'])}")
    for p in projects:
        if p["eff"] != p["pp"]:
            print(f"  ESCALATED {p['slug']}: PP{p['pp']} -> PP{p['eff']} ({'; '.join(p['why'])})")


if __name__ == "__main__":
    main()
