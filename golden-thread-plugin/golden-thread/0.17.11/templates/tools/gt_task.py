#!/usr/bin/env python3
"""gt_task.py -- create, list and work tasks through a tool instead of by hand.

    gt_task.py add TEXT --vault V (--project SLUG | --inbox) [--ref REF] [--p N]
                        [--waiting user|agent|external|parked] [--due YYYY-MM-DD] [--dry-run]
    gt_task.py list --vault V [FILTER ...] [--json]
    gt_task.py done  ID --vault V [--reason TEXT] [--dry-run]
    gt_task.py drop  ID --vault V --reason TEXT [--dry-run]
    gt_task.py defer ID --vault V --until YYYY-MM-DD --reason TEXT [--dry-run]
    gt_task.py count --vault V [--json]           # for gt_surface: p:: 1 waiting on the owner

FILTERS (any combination; a task must match all of them):
    <slug>        one project (sub-project as parent/child), or `inbox`
    p1 p2 p3      that priority
    mine          waiting:: user
    overdue       due:: before today
    stale         p:: 1 open for more than 7 days
    deferred      only tasks deferred to a later date (hidden otherwise)
    ref:<text>    whose ref:: contains <text>

WHY THIS EXISTS (owner, 2026-09-28). Tasks were hand-typed into `## Tasks` in a format only a
careful writer gets right, and read by nothing but the TASKS.md rollup when someone asked "what's
next". The owner called the task list a black hole. 0.17.2 gave handoffs a status and three verbs
-- surface, list, handle -- and this is the same shape for tasks: create one the way a developer
drops a TODO into code, see them without loading any project's context, and work through a
filtered set to get rid of them.

ONE STORE, ONE PARSER. Tasks stay in each project's README `## Tasks`; this is a writer and a
reader for it, not a second database, and it parses with gt_tasks.py's own `parse_tasks` rules
(imported from beside this file) so a task written here ranks in TASKS.md exactly as a
hand-written one does. A second parser is how the 2026-09-03 multi-line field bug would come back.

IDs are `slug:LINE:HASH` -- the README line number plus six hex of that line's text. Every write
re-reads the line and refuses if the hash no longer matches, so an ID from a list taken before
someone else edited the file can never close the wrong task.

NOTHING IS DELETED. done and drop check the box and append what settled it; drop requires a
reason. A deferral requires a date (`[defer:: D]`): the task is hidden from `list` until then
and comes back on its own. "Later, some time" is a drop with a reason, said out loud -- the same
rule as handoffs.

CORE RULE 1, QUEUE-FIRST (0.17.11). This tool never edits a README or INBOX.md itself. A write is
queued in <vault>/Projects/golden-thread/spool/queue/ (gt_write_queue's request format: `## Tasks`
as a replace-section, a new INBOX line as an append) and gt_broker.py -- found in
~/.claude/golden-thread/hooks/ -- drains it at once. The broker asks gt_session whether another LIVE
session has claimed the file; if one has, the write stays QUEUED for the next drain and this says
so (exit 1). A tool that edited a claimed file because it is not the Write tool would disarm the
rule by the side door. Closing a line of INBOX.md (no `## ` section to replace) is a replace-file
whose base is the hash of the bytes this tool read, so an edit made in between is escalated, never
overwritten. One write stays direct, after the same claim check: a #conflict task raised by the
broker itself ($GT_BROKER_APPLYING -- it holds the drain lock, so a queued task would wait a whole
drain).

Exit: 0 ok | 1 not written now (claimed by another session -- the write stays queued -- or a
stale ID, or the broker escalated a conflict, or no broker to drain with) | 2 usage | 3 could
not do it.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gt_tasks                                             # noqa: E402  the ONE parser

WAITING = ("user", "agent", "external", "parked")
STALE_DAYS = 7
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def today() -> datetime.date:
    pinned = os.environ.get("GT_TODAY")
    if pinned:
        try:
            return datetime.date.fromisoformat(pinned)
        except ValueError:
            pass
    return datetime.date.today()


def _date(s):
    try:
        return datetime.date.fromisoformat(s) if s and DATE.match(s) else None
    except ValueError:
        return None


# --------------------------------------------------------------------- where ----

def readmes(vault: Path):
    """-> [(slug, path)] for every project and sub-project README, plus the inbox."""
    out = []
    root = vault / "Projects"
    if root.is_dir():
        for p in sorted(root.iterdir()):
            if (p / "README.md").is_file():
                out.append((p.name, p / "README.md"))
            if p.is_dir():
                for sub in sorted(p.iterdir()):
                    if sub.is_dir() and (sub / "README.md").is_file():
                        out.append(("%s/%s" % (p.name, sub.name), sub / "README.md"))
    if (vault / "INBOX.md").is_file():
        out.append(("inbox", vault / "INBOX.md"))
    return out


def target(vault: Path, project: str | None, inbox: bool) -> Path | None:
    if inbox:
        return vault / "INBOX.md"
    if not project or ".." in project.split("/") or project.startswith("/"):
        return None
    p = vault / "Projects" / project / "README.md"
    return p if p.is_file() else None


def _h(line: str) -> str:
    return hashlib.sha1(line.strip().encode("utf-8")).hexdigest()[:6]


# ---------------------------------------------------------------------- read ----

def tasks_in(slug: str, path: Path):
    """Open task rows with their line numbers. The INBOX has no `## Tasks` heading, so every
    checkbox line there counts; a README's count only under `## Tasks`, as gt_tasks reads it."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    start, end = 0, len(lines)
    if slug != "inbox":
        hs = [i for i, l in enumerate(lines) if re.match(r"^## Tasks\s*$", l)]
        if not hs:
            return []
        start = hs[0] + 1
        nxt = [i for i in range(start, len(lines)) if lines[i].startswith("## ")]
        end = nxt[0] if nxt else len(lines)
    rows = []
    for i in range(start, end):
        line = lines[i]
        # parse through gt_tasks so the fields mean exactly what the rollup thinks they mean
        parsed = gt_tasks.parse_tasks("## Tasks\n" + line + "\n", where=slug)
        if not parsed or parsed[0]["done"]:
            continue
        t = parsed[0]
        fields = dict(gt_tasks.FIELD.findall(line))
        rows.append({"id": "%s:%d:%s" % (slug, i + 1, _h(line)), "project": slug,
                     "line": i + 1, "text": t["text"], "p": t["p"], "waiting": t["waiting"],
                     "due": t["due"], "since": t["since"], "ref": fields.get("ref"),
                     "defer": fields.get("defer")})
    return rows


def all_tasks(vault: Path):
    out = []
    for slug, path in readmes(vault):
        out += tasks_in(slug, path)
    return out


def matches(r: dict, filters: list[str], now: datetime.date) -> bool:
    d = _date(r["defer"])
    deferred = d is not None and d > now
    if deferred and "deferred" not in filters:
        return False
    if r["p"] >= gt_tasks.SHELVED_P and not any(f.startswith("p") and f[1:].isdigit()
                                                  for f in filters):
        return False                       # shelved: kept as a record, never listed by default
    for f in filters:
        if f == "deferred":
            if not deferred:
                return False
        elif f == "mine":
            if r["waiting"] != "user":
                return False
        elif f == "overdue":
            due = _date(r["due"])
            if not due or due >= now:
                return False
        elif f == "stale":
            s = _date(r["since"])
            if r["p"] != 1 or not s or (now - s).days <= STALE_DAYS:
                return False
        elif re.fullmatch(r"p\d+", f):
            if r["p"] != int(f[1:]):
                return False
        elif f.startswith("ref:"):
            if f[4:].lower() not in (r["ref"] or "").lower():
                return False
        elif r["project"] != f and not r["project"].startswith(f + "/"):
            return False
    return True


def listed(vault: Path, filters: list[str]):
    now = today()
    rows = [r for r in all_tasks(vault) if matches(r, filters, now)]
    rows.sort(key=lambda r: (r["p"], r["since"] or "9999", r["project"], r["line"]))
    return rows


def fmt(r: dict) -> str:
    bits = ["P%d" % r["p"], r["waiting"]]
    if r["due"]:
        bits.append("due " + r["due"])
    if r["defer"]:
        bits.append("deferred to " + r["defer"])
    if r["since"]:
        bits.append("since " + r["since"])
    text = r["text"] if len(r["text"]) <= 110 else r["text"][:107] + "..."
    ref = ("  -> " + r["ref"]) if r["ref"] else ""
    return "%s  [%s]  %s%s" % (r["id"], ", ".join(bits), text, ref)


# ------------------------------------------------------------------ write ----

def claimed_elsewhere(vault: Path, path: Path):
    """-> the holder line if another LIVE session claims this file, else None. gt_session
    missing or failing is not a claim: the write proceeds, as the guard hook fails open."""
    tool = HERE / "gt_session.py"
    if not tool.is_file():
        return None
    try:
        p = subprocess.run([sys.executable, str(tool), "--vault", str(vault), "check",
                            str(path.relative_to(vault))],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 1 else None


def write_lines(path: Path, lines: list[str]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".gt_task.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------- the write queue ----
#
# 0.17.11 (Core rule 1, queue-first): vault Markdown is written only through the write queue,
# and a write made inside a script is invisible to the hooks. So this tool does not edit a README
# or INBOX.md itself: it deposits ONE request in <vault>/Projects/golden-thread/spool/queue/ --
# the exact schema gt_write_queue.build() produces -- and runs the broker's drain at once, so the
# task lands immediately unless another live session holds the file. This file lives in the
# vault and cannot import the plugin, so the few lines of the request format are copied here;
# tests/test_gt_task.py checks every request it writes against gt_write_queue.validate().

Q_SCHEMA = 1
Q_REL = Path("Projects") / "golden-thread" / "spool" / "queue"
Q_HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$")
Q_ANY_TOP = re.compile(r"^#{1,2}\s")
Q_FENCE = re.compile(r"^\s*(```|~~~)")
# Set by gt_broker.py when it raises a #conflict task through this tool: the broker is the
# writer of record and holds the drain lock, so a request queued from inside its drain would
# wait for the NEXT drain. In that one case the task is written here, after the claim check.
BROKER_APPLYING = "GT_BROKER_APPLYING"


def _q_session() -> str:
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
        if os.environ.get(var):
            return os.environ[var].strip()
    return "anonymous"


def _q_slug(text: str, n: int) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:n].strip("-") or "x"


def q_find_section(lines: list, section: str):
    """gt_write_queue.find_section, verbatim: the broker hashes the section by these rules, so a
    second reading of where a section ends would turn every replace into a false conflict."""
    want = section.strip().lower()
    fenced, start = False, None
    for i, line in enumerate(lines):
        if Q_FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if start is not None and Q_ANY_TOP.match(line):
            return start, i
        m = Q_HEADING.match(line)
        if start is None and m and m.group(1).strip().lower() == want:
            start = i
    return (start, len(lines)) if start is not None else None


def q_section_body(lines: list, section: str):
    rng = q_find_section(lines, section)
    if rng is None:
        return None
    return "\n".join(lines[rng[0] + 1:rng[1]]).strip("\n")


def _q_sha(text):
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def queue_request(vault: Path, rel: str, op: str, content: str, section=None,
                  base_raw=None) -> dict:
    """Build and deposit one request (gt_write_queue schema 1), atomically. -> the request.
    replace-file: `base_raw` is the file's raw bytes as this tool READ them, so an edit made
    after that read -- not just after the queueing -- is escalated rather than overwritten."""
    now = datetime.datetime.now(datetime.timezone.utc)
    sid = _q_session()
    base = None
    if op == "replace-file":
        base = None if base_raw is None else hashlib.sha256(base_raw).hexdigest()
    elif op == "replace-section":
        try:
            # raw bytes, no newline translation: exactly what gt_write_queue and the broker hash
            text = (vault / rel).read_bytes().decode("utf-8")
            base = _q_sha(q_section_body(text.replace("\r\n", "\n").split("\n") if text else [],
                                         section))
        except (OSError, UnicodeDecodeError):
            base = None
    req = {"schema": Q_SCHEMA,
           "id": "%s-%s-%s" % (now.strftime("%Y%m%dT%H%M%S.%fZ"), _q_slug(sid, 8),
                               _q_slug(rel, 60)),
           "submitted": now.isoformat(timespec="microseconds"),
           "session": sid, "origin": "session", "path": rel, "op": op, "section": section,
           "content": content, "key": None, "target_existed": (vault / rel).is_file(),
           "base_sha256": base, "hint": "gt_task.py"}
    q = vault / Q_REL
    q.mkdir(parents=True, exist_ok=True)
    stem, n = req["id"], 1
    while (q / (req["id"] + ".json")).exists():
        n += 1
        req["id"] = "%s-%d" % (stem, n)
    fd, tmp = tempfile.mkstemp(prefix=".req.", suffix=".tmp", dir=str(q))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(req, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, q / (req["id"] + ".json"))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return req


def find_broker():
    """-> the installed gt_broker.py, or None. install.sh puts it in the stable hooks dir
    (gt_components.HOOK_DIR_SCRIPTS); failing that, the newest gt release in the plugin cache."""
    hooks = Path.home() / ".claude" / "golden-thread" / "hooks" / "gt_broker.py"
    if hooks.is_file():
        return hooks
    def ver(p):
        try:
            return tuple(int(x) for x in p.parent.parent.name.split("."))
        except ValueError:
            return ()
    cands = list((Path.home() / ".claude" / "plugins" / "cache").glob(
        "*/gt/*/scripts/gt_broker.py"))
    return max(cands, key=ver) if cands else None


def drain(vault: Path, broker):
    """-> ({request id: broker log row}, None) or ({}, why the drain did not run)."""
    if broker is None:
        return {}, "no gt_broker.py found (looked in ~/.claude/golden-thread/hooks/)"
    try:
        p = subprocess.run([sys.executable, str(broker), "drain", "--vault", str(vault), "--json"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, "the drain could not run (%s)" % exc.__class__.__name__
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return {}, "the drain did not run: %s" % (
            (p.stderr.strip().splitlines() or ["exit %d" % p.returncode])[-1])
    return {r.get("request"): r for r in data.get("results", [])}, None


def enclosing_section(lines: list, i: int):
    """-> the `## ` heading whose section (by the broker's rules) holds line i, or None."""
    for j in range(i - 1, -1, -1):
        m = Q_HEADING.match(lines[j])
        if m:
            rng = q_find_section(lines, m.group(1))
            return m.group(1).strip() if rng and rng[0] < i < rng[1] else None
    return None


def through_queue(vault: Path, rel: str, op: str, content: str, section=None, base_raw=None):
    """Queue one write and drain. -> (exit code, None) when it landed, else (exit code, the one
    line to print on stderr). Landed = applied, or already there (deduplicated)."""
    try:
        req = queue_request(vault, rel, op, content, section, base_raw)
    except OSError as exc:
        return 3, "could not queue the write to %s: %s" % (rel, exc)
    broker = find_broker()
    rows, note = drain(vault, broker)
    row = rows.get(req["id"])
    how = "python3 %s drain --vault %s" % (broker or "~/.claude/golden-thread/hooks/gt_broker.py",
                                          vault)
    if row is None:
        return 1, "queued, not written yet: %s -- %s; apply it with `%s`" % (
            rel, note or "the drain did not decide it", how)
    d, why = row.get("decision"), row.get("reason", "")
    if d in ("apply", "deduplicate"):
        return 0, None
    if d == "held":
        if "claimed by live session" in why:
            return 1, ("queued, not written -- another live session holds %s (%s); it lands on "
                       "the next drain: `%s`" % (rel, why, how))
        return 1, "queued, not written -- %s: %s; it is retried by `%s`" % (rel, why, how)
    if d == "escalate":
        return 1, ("not written -- %s changed under this request, so the broker escalated it as "
                   "a #conflict task (%s)" % (rel, row.get("conflict", why)))
    return 3, "the write broker rejected the write to %s: %s" % (rel, why)


def resolve_ref(vault: Path, ref: str) -> str | None:
    """-> None if the ref resolves, else what was looked for. `[[Page]]` resolves against any
    markdown file of that name (as Obsidian does); anything else is a vault-relative path."""
    m = re.fullmatch(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]", ref.strip())
    if m:
        name = m.group(1).strip()
        stem = Path(name).name
        if (vault / (name + ".md")).is_file() or any(vault.rglob(stem + ".md")):
            return None
        return "no page named %r anywhere in the vault" % name
    p = (vault / ref.strip()).resolve()
    if str(p).startswith(str(vault.resolve())) and p.exists():
        return None
    return "no file at %s (paths are relative to the vault)" % ref


def cmd_add(a, vault: Path) -> int:
    text = " ".join(a.text).strip()
    if not text or "\n" in text or gt_tasks.FIELD.search(text):
        print("the task text must be one line with no [field:: value] in it -- pass fields as "
              "flags", file=sys.stderr)
        return 2
    path = target(vault, a.project, a.inbox)
    if path is None:
        print("no such project %r (pass --project <slug>, or --inbox)" % a.project,
              file=sys.stderr)
        return 3
    if a.ref:
        why = resolve_ref(vault, a.ref)
        if why:
            print("refusing the ref: %s" % why, file=sys.stderr)
            return 3
    if a.due and not _date(a.due):
        print("--due must be YYYY-MM-DD", file=sys.stderr)
        return 3
    fields = []
    if a.ref:
        fields.append("[ref:: %s]" % a.ref.strip())
    if not a.inbox:
        fields += ["[p:: %d]" % a.p, "[waiting:: %s]" % a.waiting]
        if a.due:
            fields.append("[due:: %s]" % a.due)
    else:
        fields.append("[project:: %s]" % a.project) if a.project else None
    fields.append("[since:: %s]" % today().isoformat())
    line = "- [ ] %s %s" % (text, " ".join(fields))
    lines = path.read_text(encoding="utf-8").splitlines()
    if a.inbox:
        at = len(lines)
    else:
        hs = [i for i, l in enumerate(lines) if re.match(r"^## Tasks\s*$", l)]
        if not hs:
            lines += ["", "## Tasks", ""]
            hs = [len(lines) - 2]
        at = hs[0] + 1
        while at < len(lines) and not lines[at].strip():
            at += 1                          # newest first, directly under the heading
    rel = path.relative_to(vault)
    if a.dry_run:
        print("would add to %s:\n%s" % (rel, line))
        return 0
    slug = "inbox" if a.inbox else a.project
    if os.environ.get(BROKER_APPLYING) == "1":
        holder = claimed_elsewhere(vault, path)
        if holder:
            print("refused -- another live session holds %s:\n  %s" % (rel, holder),
                  file=sys.stderr)
            return 1
        lines.insert(at, line)
        try:
            write_lines(path, lines)
        except OSError as exc:
            print("could not write %s: %s" % (rel, exc), file=sys.stderr)
            return 3
        print("added %s:%d:%s\n%s" % (slug, at + 1, _h(line), line))
        return 0
    relp = str(rel).replace(os.sep, "/")
    if a.inbox:
        rc, msg = through_queue(vault, relp, "append", line)
    else:
        # replace-section, not append: newest first, directly under the heading
        lines.insert(at, line)
        body = q_section_body(lines, "Tasks")
        if body is not None and line in body.split("\n"):
            rc, msg = through_queue(vault, relp, "replace-section", body, "Tasks")
        else:      # the broker's `## Tasks` is another heading (case differs): add to that one
            rc, msg = through_queue(vault, relp, "append", line, "Tasks")
    if msg:
        print(msg, file=sys.stderr)
        return rc
    # The broker owns the layout of what it writes, so the line number comes from the file.
    now = path.read_text(encoding="utf-8").splitlines()
    if line not in now:
        print("already there -- the broker found a near-identical line in %s and added nothing"
              % rel)
        return 0
    n = now.index(line) + 1
    print("added %s:%d:%s\n%s" % (slug, n, _h(line), line))
    return 0


def locate(vault: Path, tid: str):
    """-> (path, index, line) or (None, error)."""
    m = re.fullmatch(r"(.+):(\d+):([0-9a-f]{6})", tid or "")
    if not m:
        return None, "an ID looks like slug:LINE:HASH -- take it from `list`"
    slug, n, h = m.group(1), int(m.group(2)), m.group(3)
    path = (vault / "INBOX.md") if slug == "inbox" else target(vault, slug, False)
    if path is None:
        return None, "no project %r" % slug
    lines = path.read_text(encoding="utf-8").splitlines()
    if n < 1 or n > len(lines) or _h(lines[n - 1]) != h:
        return None, ("the task at %s changed since it was listed (someone edited the file) -- "
                      "list again and use the new ID" % tid)
    if not gt_tasks.TASK.match(lines[n - 1]) or re.match(r"^\s*-\s*\[[xX]\]", lines[n - 1]):
        return None, "%s is not an open task" % tid
    return (path, n - 1, lines), None


def _settle(vault: Path, a, verb: str) -> int:
    found, err = locate(vault, a.id)
    if err:
        print(err, file=sys.stderr)
        return 1
    path, i, lines = found
    try:
        raw = path.read_bytes()       # the base a replace-file is checked against
        if raw.decode("utf-8").splitlines() != lines:
            raw = None
    except (OSError, UnicodeDecodeError):
        raw = None
    if raw is None:
        print("%s changed while it was being read -- list again and retry" % a.id, file=sys.stderr)
        return 1
    old = lines[i]
    stamp = today().isoformat()
    if verb == "defer":
        until = _date(a.until)
        if not until:
            print("--until must be YYYY-MM-DD", file=sys.stderr)
            return 3
        if until <= today():
            print("--until %s is not in the future; a deferral to today is no deferral" % a.until,
                  file=sys.stderr)
            return 3
        new = re.sub(r"\s*\[defer::[^\]]*\]", "", old)
        new = new.rstrip() + " [defer:: %s] — deferred %s: %s" % (a.until, stamp, a.reason)
    else:
        new = re.sub(r"^(\s*-\s*)\[ \]", r"\1[x]", old, count=1)
        word = "done" if verb == "done" else "dropped"
        new = new.rstrip() + " — %s %s%s" % (word, stamp, (": " + a.reason) if a.reason else "")
    rel = path.relative_to(vault)
    if a.dry_run:
        print("would change %s line %d:\n- %s\n+ %s" % (rel, i + 1, old.strip(), new.strip()))
        return 0
    done = {"done": "closed", "drop": "dropped", "defer": "deferred"}[verb]
    lines[i] = new
    heading = enclosing_section(lines, i)
    if os.environ.get(BROKER_APPLYING) == "1":
        # The broker itself is asking (a #conflict task): written directly, after the claim check.
        holder = claimed_elsewhere(vault, path)
        if holder:
            print("refused -- another live session holds %s:\n  %s" % (rel, holder),
                  file=sys.stderr)
            return 1
        try:
            write_lines(path, lines)
        except OSError as exc:
            print("could not write %s: %s" % (rel, exc), file=sys.stderr)
            return 3
        print("%s %s" % (done, a.id))
        return 0
    if heading is None:
        # Not inside a `## ` section (INBOX.md has none): the whole file, as replace-file, with
        # the hash of the bytes this tool read -- an edit made since then is escalated.
        rc, msg = through_queue(vault, str(rel).replace(os.sep, "/"), "replace-file",
                                "\n".join(lines) + "\n", base_raw=raw)
        if msg:
            print(msg, file=sys.stderr)
            return rc
        print("%s %s" % (done, a.id))
        return 0
    rc, msg = through_queue(vault, str(rel).replace(os.sep, "/"), "replace-section",
                            q_section_body(lines, heading), heading)
    if msg:
        print(msg, file=sys.stderr)
        return rc
    print("%s %s" % (done, a.id))
    return 0


def cmd_count(a, vault: Path) -> int:
    now = today()
    mine = [r for r in all_tasks(vault) if matches(r, ["p1", "mine"], now)]
    overdue = [r for r in mine if _date(r["due"]) and _date(r["due"]) < now]
    stale = [r for r in mine if matches(r, ["stale"], now)]
    d = {"p1_waiting_on_user": len(mine), "overdue": len(overdue), "stale": len(stale)}
    print(json.dumps(d) if a.json else
          "%d p:: 1 task(s) wait on you (%d overdue, %d open over %dd)"
          % (len(mine), len(overdue), len(stale), STALE_DAYS))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_task.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ad = sub.add_parser("add", help="create a task")
    ad.add_argument("text", nargs="+")
    ad.add_argument("--vault", required=True)
    g = ad.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", help="project slug (sub-project as parent/child)")
    g.add_argument("--inbox", action="store_true", help="no project yet: INBOX.md")
    ad.add_argument("--ref", help="[[wiki page]], or a vault path (Sources/..., Projects/...)")
    ad.add_argument("--p", type=int, default=2, choices=(1, 2, 3), help="priority (default 2)")
    ad.add_argument("--waiting", default="user", choices=WAITING)
    ad.add_argument("--due", help="YYYY-MM-DD")
    ad.add_argument("--dry-run", action="store_true")
    ls = sub.add_parser("list", help="open tasks matching every FILTER (read-only)")
    ls.add_argument("filters", nargs="*")
    ls.add_argument("--vault", required=True)
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--dry-run", action="store_true", help="accepted for symmetry; list writes nothing")
    for verb in ("done", "drop", "defer"):
        s = sub.add_parser(verb, help="%s a task by ID" % verb)
        s.add_argument("id")
        s.add_argument("--vault", required=True)
        s.add_argument("--reason", required=(verb != "done"))
        if verb == "defer":
            s.add_argument("--until", required=True, help="YYYY-MM-DD, in the future")
        s.add_argument("--dry-run", action="store_true")
    ct = sub.add_parser("count", help="p:: 1 tasks waiting on the owner (for gt_surface)")
    ct.add_argument("--vault", required=True)
    ct.add_argument("--json", action="store_true")
    ct.add_argument("--dry-run", action="store_true", help="accepted for symmetry; count writes nothing")
    a = ap.parse_args(argv)
    vault = Path(a.vault).expanduser().resolve()
    if not vault.is_dir():
        print("no vault at %s" % vault, file=sys.stderr)
        return 3
    if a.cmd == "add":
        return cmd_add(a, vault)
    if a.cmd == "list":
        rows = listed(vault, a.filters)
        if a.json:
            print(json.dumps(rows, indent=1))
        elif not rows:
            print("no open task matches %s" % (" ".join(a.filters) or "(all)"))
        else:
            for r in rows:
                print(fmt(r))
            print("\n%d task(s)" % len(rows))
        return 0
    if a.cmd == "count":
        return cmd_count(a, vault)
    return _settle(vault, a, a.cmd)


if __name__ == "__main__":
    sys.exit(main())
