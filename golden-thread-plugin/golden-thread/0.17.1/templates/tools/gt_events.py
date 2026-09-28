#!/usr/bin/env python3
"""A structured event stream: one JSON line per movement of knowledge in the vault.

    gt_events.py emit --kind promote --item Knowledge/x.md --level-from 3 --level-to 4
    gt_events.py merge                 # render events.jsonl from the spool
    gt_events.py validate [file]       # report bad lines; exit 1 if any
    gt_events.py list [--kind K] [--project P] [--since ISO] [--json]
    gt_events.py backfill [--dry-run] [--since DATE]   # derive history from git + log.md

## Emitters call `emit()` / `safe_emit()` -- they never write JSON themselves

`gt_log.py add --event`, `gt_adr.py allocate`, `gt_tasks.py` and `vault_init.py`
import this file and call `safe_emit()`. It validates and spools
exactly as the CLI does, and it NEVER raises: an event log that could fail the
operation it describes would be switched off the first time it did. A failure is one
line on stderr and the operation carries on.

## Backfill (0.15.0)

`backfill` derives `actor:"backfill"` events for the time before anything emitted:

  git history   Knowledge/ adds -> create (L4) or promote (L3->L4, commit says so);
                global-memory/ adds -> promote (->L5); renames between ladder levels ->
                promote/relocate; project folder renames -> rename; README checkbox
                lines added or flipped (`git log -p`) -> task.open / task.done
  log.md        dated lines carrying an arrow (-> or the arrow glyph) whose verb maps to a kind
  README now    every current `## Tasks` line with no task event yet -> task.open /
                task.done (seeds the snapshot `gt_tasks.py` diffs against)

Schema v1 has no confidence key and refuses unknown keys, so confidence and
provenance live in `note`: `backfill conf=0.8 src=git:<sha10> <text>`. `src` is what
makes backfill idempotent: the dedupe key is (kind, item, from, to, src), read back
from every spooled event, so a second run -- on any machine -- adds nothing. Only
this session's own spool file is appended to.

## Why

`log.md` is prose with no fixed grammar, so nothing can be computed from it: of 43
movement-verb lines only 21 carry an arrow (design-addons.md §3.1). This is the stream a
visualizer or an audit reads instead. It is emitted by a SCRIPT, never hand-written by
skills, because two emitters of one format drift.

## Same spool pattern as gt_log.py

Each session appends only to `Projects/golden-thread/spool/events/<session>.jsonl`, via
`gt_spool` (session id, vault resolution, safe_write). `Projects/golden-thread/events.jsonl`
is GENERATED from the spool. JSONL cannot carry a banner line, so "generated" is stated
here instead: hand edits to events.jsonl are lost at the next merge.

## Ordering is total and machine-independent

Merge sorts by (ts as a UTC instant, ts text, session, line index within the session's
file). The instant matters: `ts` carries an offset, and string-sorting two stamps from
different zones would order them wrongly. Lines are re-serialised canonically, so two
machines merging the same spool produce byte-identical output, and a second merge
changes nothing.

## Schema v1 -- validation is strict on purpose

Keys: v, ts, session, actor, kind, item, from, to, level_from, level_to, project, and
optional note. Unknown keys are refused, not ignored: a key one emitter invents and no
reader knows is the start of the drift the single emitter exists to prevent. Paths must
be vault-relative -- an absolute path or `..` names something outside the vault, or a
different place on another machine. A merge that meets an invalid spool line refuses
and writes nothing, so events.jsonl only ever contains valid events.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gt_spool as S  # noqa: E402

KIND = "events"
VERSION = 1
ACTORS = ("claude", "user", "cron", "backfill")
KINDS = ("capture", "file", "create", "promote", "relocate", "retire", "rename", "merge",
         "archive", "ingest", "task.open", "task.done", "adr", "source.supersede",
         "announce")
KEYS = ("v", "ts", "session", "actor", "kind", "item", "from", "to", "level_from",
        "level_to", "project", "note")
REQUIRED = KEYS[:-1]
NOTE_MAX = 120
SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class Invalid(ValueError):
    pass


def target(vault):
    return Path(vault) / "Projects" / "golden-thread" / "events.jsonl"


def spool_file(vault, sid):
    return S.spool_dir(vault, KIND) / ("%s.jsonl" % sid)


def parse_ts(value):
    """ISO-8601 with an offset -> aware datetime, or raise Invalid."""
    if not isinstance(value, str) or not value:
        raise Invalid("ts: must be an ISO-8601 string with an offset")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise Invalid("ts: %r is not ISO-8601" % value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Invalid("ts: %r has no UTC offset" % value)
    return dt


def _rel_path(key, value, nullable):
    if value is None:
        if nullable:
            return
        raise Invalid("%s: required" % key)
    if not isinstance(value, str) or not value.strip():
        raise Invalid("%s: must be a non-empty string" % key)
    if CONTROL_RE.search(value):
        raise Invalid("%s: contains a control character" % key)
    if value.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", value):
        raise Invalid("%s: %r is absolute; use a vault-relative path" % (key, value))
    if ".." in value:
        raise Invalid("%s: %r contains '..'; use a vault-relative path" % (key, value))


def _level(key, value):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise Invalid("%s: %r must be an integer 1-5 or null" % (key, value))


def validate_event(ev):
    """Raise Invalid with a one-line reason, or return None."""
    if not isinstance(ev, dict):
        raise Invalid("not a JSON object")
    unknown = sorted(set(ev) - set(KEYS))
    if unknown:
        raise Invalid("unknown key(s): %s" % ", ".join(unknown))
    missing = [k for k in REQUIRED if k not in ev]
    if missing:
        raise Invalid("missing key(s): %s" % ", ".join(missing))
    if ev["v"] != VERSION or isinstance(ev["v"], bool):
        raise Invalid("v: %r is not %d" % (ev["v"], VERSION))
    parse_ts(ev["ts"])
    if not isinstance(ev["session"], str) or not SESSION_RE.match(ev["session"]):
        raise Invalid("session: %r is not a session id" % (ev["session"],))
    if ev["actor"] not in ACTORS:
        raise Invalid("actor: %r is not one of %s" % (ev["actor"], "|".join(ACTORS)))
    if ev["kind"] not in KINDS:
        raise Invalid("kind: %r is not one of %s" % (ev["kind"], "|".join(KINDS)))
    _rel_path("item", ev["item"], nullable=False)
    _rel_path("from", ev["from"], nullable=True)
    _rel_path("to", ev["to"], nullable=True)
    _level("level_from", ev["level_from"])
    _level("level_to", ev["level_to"])
    p = ev["project"]
    if p is not None and (not isinstance(p, str) or not PROJECT_RE.match(p)):
        raise Invalid("project: %r is not a project slug" % (p,))
    if "note" in ev:
        n = ev["note"]
        if not isinstance(n, str):
            raise Invalid("note: must be a string")
        if len(n) > NOTE_MAX:
            raise Invalid("note: %d chars; the limit is %d" % (len(n), NOTE_MAX))
        if CONTROL_RE.search(n):
            raise Invalid("note: contains a control character")


def serialise(ev):
    """Canonical line: schema key order, no whitespace, UTF-8 kept readable."""
    return json.dumps({k: ev[k] for k in KEYS if k in ev}, ensure_ascii=False,
                      separators=(",", ":"))


def parse_line(line):
    try:
        ev = json.loads(line)
    except ValueError as exc:
        raise Invalid("not valid JSON (%s)" % exc.msg)
    validate_event(ev)
    return ev


def read_spool(vault):
    """-> (events in merge order, [(file, lineno, reason)] for bad lines)."""
    d = S.spool_dir(vault, KIND)
    rows, bad = [], []
    if not d.is_dir():
        return [], []
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.suffix != ".jsonl":
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            bad.append((f, 0, "unreadable (%s)" % exc))
            continue
        for i, ln in enumerate(body.splitlines()):
            if not ln.strip():
                continue
            try:
                ev = parse_line(ln)
            except Invalid as exc:
                bad.append((f, i + 1, str(exc)))
                continue
            instant = parse_ts(ev["ts"]).astimezone(timezone.utc)
            rows.append(((instant, ev["ts"], ev["session"], f.name, i), ev))
    rows.sort(key=lambda t: t[0])
    return [ev for _, ev in rows], bad


def render(events):
    return "".join(serialise(ev) + "\n" for ev in events)


# ------------------------------------------------------------ library API ----

def clip(text, limit=NOTE_MAX):
    """A note-safe string: control characters become spaces, clipped to `limit`."""
    text = re.sub(r"\s+", " ", CONTROL_RE.sub(" ", str(text or ""))).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def now_ts():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build(kind, item, frm=None, to=None, level_from=None, level_to=None, project=None,
          actor="claude", note=None, sid=None, ts=None):
    """-> a validated event dict, or raise Invalid."""
    ev = {"v": VERSION, "ts": ts or now_ts(), "session": S.session_id(sid),
          "actor": actor, "kind": kind, "item": item, "from": frm, "to": to,
          "level_from": level_from, "level_to": level_to, "project": project}
    if note is not None:
        ev["note"] = note
    validate_event(ev)
    return ev


def append_events(vault, events, sid=None):
    """Append serialised events to THIS session's spool file, in one write."""
    if not events:
        return None
    sp = spool_file(vault, S.session_id(sid))
    sp.parent.mkdir(parents=True, exist_ok=True)
    S._safe_write(sp, "".join(serialise(ev) + "\n" for ev in events))
    return sp


def merge_quiet(vault):
    """Regenerate events.jsonl; returns the merge exit code, never raises."""
    try:
        return cmd_merge(argparse.Namespace(vault=str(vault), dry_run=False, quiet=True))
    except Exception as exc:
        print("gt_events: events.jsonl NOT regenerated (%s: %s); spooled events are kept"
              % (type(exc).__name__, exc), file=sys.stderr)
        return 1


def emit(vault, kind, item, frm=None, to=None, level_from=None, level_to=None,
         project=None, actor="claude", note=None, sid=None, merge=True, dry_run=False):
    """Validate and spool one event; regenerate events.jsonl unless merge=False.

    Raises Invalid on a bad event, OSError on a spool it cannot write. Callers that
    describe another operation use safe_emit() instead.
    """
    ev = build(kind, item, frm, to, level_from, level_to, project, actor, note, sid)
    if dry_run:
        return ev
    append_events(Path(vault), [ev], sid)
    if merge and merge_quiet(Path(vault)):
        print("gt_events: event spooled; events.jsonl NOT regenerated", file=sys.stderr)
    return ev


def safe_emit(vault, kind, item, **kw):
    """emit() that cannot fail its caller: any failure is one stderr line, then None."""
    try:
        return emit(vault, kind, item, **kw)
    except BaseException as exc:          # SystemExit from vault resolution included
        if isinstance(exc, KeyboardInterrupt):
            raise
        print("gt_events: %s event for %s NOT recorded (%s: %s); the operation itself "
              "is unaffected" % (kind, item, type(exc).__name__, exc), file=sys.stderr)
        return None


def load_sibling(name):
    """Import a tool that sits beside this file (gt_tasks), or None."""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        return __import__(name)
    except Exception:
        return None


# ------------------------------------------------------------------- tasks ----

def task_item(readme_rel, label):
    """The task id: `<README path>#t-<sha1(label)[:10]>`.

    The label is the task text with its `[field:: value]`s removed, exactly as
    gt_tasks.parse_tasks() computes it, so changing a task's priority or due date
    does not make it a different task -- but rewording it does.
    """
    digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:10]
    return "%s#t-%s" % (str(readme_rel).replace("\\", "/"), digest)


def task_state(events):
    """{item: last task.* kind} in merge order."""
    state = {}
    for ev in events:
        if ev["kind"] in ("task.open", "task.done"):
            state[ev["item"]] = ev["kind"]
    return state


def _project_or_none(slug):
    return slug if isinstance(slug, str) and PROJECT_RE.match(slug) else None


def sync_tasks(vault, observed, sid=None, dry_run=False):
    """Emit task.open / task.done for README checkboxes that differ from the stream.

    `observed` is [(readme_rel, project_slug, label, done)]. The stream itself is the
    snapshot: the last task.* event per item is the state last seen, so there is no
    second state file to drift or conflict, and two machines agree. Nothing is emitted
    until the stream holds at least one task event (run `backfill` once to seed it):
    otherwise the first rollup in an existing vault would announce every task ever
    written as newly opened. Returns the number of events emitted; never raises.
    """
    try:
        vault = Path(vault)
        events, _ = read_spool(vault)
        state = task_state(events)
        if not state:
            return 0
        out, seen = [], set()
        for readme_rel, slug, label, done in observed:
            item = task_item(readme_rel, label)
            if item in seen:
                continue
            seen.add(item)
            want = "task.done" if done else "task.open"
            if state.get(item) == want:
                continue
            try:
                out.append(build(want, item, project=_project_or_none(slug),
                                 note=clip(label), sid=sid))
            except Invalid as exc:
                print("gt_events: task event skipped (%s)" % exc, file=sys.stderr)
        if out and not dry_run:
            append_events(vault, out, sid)
            merge_quiet(vault)
        return len(out)
    except Exception as exc:
        print("gt_events: task events NOT recorded (%s: %s); the rollup is unaffected"
              % (type(exc).__name__, exc), file=sys.stderr)
        return 0


# ----------------------------------------------------------------- backfill ----

ARROW = re.compile(r"(\S+)\s*(?:→|(?<!-)->)\s*(\S+)")
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2})(?::\d{2})?)?"
                      r"(?:\s+([A-Z]{2,5}|[+-]\d{2}:?\d{2}))?\s+\[([\w.-]+)\]\s+(.*)$")
ZONES = {"UTC": "+00:00", "GMT": "+00:00", "Z": "+00:00",
         "CDT": "-05:00", "CST": "-06:00", "EDT": "-04:00", "EST": "-05:00",
         "MDT": "-06:00", "MST": "-07:00", "PDT": "-07:00", "PST": "-08:00",
         "BST": "+01:00", "CET": "+01:00", "CEST": "+02:00"}
TAG_KINDS = {"graduate": "promote", "promote": "promote", "relocate": "relocate",
             "retire": "retire", "rename": "rename", "merge": "merge",
             "archive": "archive", "ingest": "ingest", "supersede": "source.supersede",
             "create": "create", "file": "file", "capture": "capture"}
VERB_KINDS = ((r"promot|graduat", "promote"), (r"renam", "rename"), (r"merg", "merge"),
              (r"supersed", "source.supersede"), (r"relocat|\bmoved\b", "relocate"),
              (r"archiv", "archive"), (r"retir", "retire"), (r"ingest", "ingest"),
              (r"\bfiled\b", "file"))
PROMOTE_MSG = re.compile(r"promot|graduat", re.I)
NOT_PROJECT_DIRS = {"memory", "tools", "spool", "core-rules", "lint", ".templates",
                    "Sources", "Knowledge"}


def level_of(path):
    """The promotion-ladder level a vault path sits at, or None."""
    p = str(path or "")
    if p.startswith("global-memory/") and p.endswith(".md"):
        return 5
    if p.startswith("Knowledge/") and p.endswith(".md"):
        return 4
    if p.startswith("Projects/"):
        if "/memory/" in p and p.endswith(".md"):
            return 2
        if re.search(r"/(decisions|research|design)\.md$", p) \
                or re.match(r"^Projects/golden-thread/spool/decisions/.+/\d{4}\.md$", p):
            return 3
    return None


def project_of(vault, path):
    """`Projects/<slug>/...` -> slug (`parent/child` when that folder is a project)."""
    parts = str(path or "").split("/")
    if len(parts) < 2 or parts[0] != "Projects":
        return None
    if parts[1] == "golden-thread" and parts[2:4] == ["spool", "decisions"] and len(parts) > 5:
        return _project_or_none("/".join(parts[4:-1]))
    if len(parts) > 3 and parts[2] not in NOT_PROJECT_DIRS \
            and (Path(vault) / "Projects" / parts[1] / parts[2] / "README.md").is_file():
        return _project_or_none(parts[1] + "/" + parts[2])
    return _project_or_none(parts[1]) if len(parts) > 2 or "." not in parts[1] else None


def _bnote(conf, src, text):
    return clip("backfill conf=%.1f src=%s %s" % (conf, src, text))


def note_fields(note):
    """`backfill conf=0.8 src=git:abc` -> {"conf": 0.8, "src": "git:abc"} (readers' helper)."""
    out = {}
    for key, val in re.findall(r"\b(conf|src)=(\S+)", note or ""):
        out[key] = float(val) if key == "conf" else val
    return out


def _git(vault, *args):
    try:
        p = subprocess.run(["git", "-c", "core.quotepath=off", "-C", str(vault), *args],
                           capture_output=True, text=True, errors="replace")
    except OSError:
        return None
    return p.stdout if p.returncode == 0 else None


def _vault_path(token):
    """A cleaned token if it is a usable vault-relative path, else None."""
    t = token.strip("`*_()[],;:.\"'<>")
    if "|" in t:
        t = t.split("|", 1)[0]
    if not t or ("/" not in t and not t.endswith(".md")):
        return None
    if t.startswith(("/", "\\", "~")) or ".." in t or re.match(r"^[A-Za-z]:", t) \
            or "://" in t or CONTROL_RE.search(t):
        return None
    return t


def _endpoint(vault, token):
    """An arrow endpoint as a vault path: a path token, or a `[[Page]]` that exists."""
    path = _vault_path(token)
    if path:
        return path
    wl = re.match(r"^\W*\[\[([^\]|#/]+)", token)
    if wl:
        cand = "Knowledge/%s.md" % wl.group(1).strip()
        if (Path(vault) / cand).is_file() and _vault_path(cand):
            return cand
    return None


def _from_git_names(vault, since):
    """Knowledge / global-memory adds, ladder renames, project folder renames."""
    fmt = "%x1e%H%x1f%aI%x1f%s"
    args = ["log", "--relative", "--no-color", "-M", "--name-status", "--format=" + fmt]
    if since:
        args.append("--since=" + since)
    out = _git(vault, *args)
    if out is None:
        return []
    found = []
    for block in out.split("\x1e")[1:]:
        head, _, body = block.partition("\n")
        try:
            sha, ts, subject = head.split("\x1f", 2)
        except ValueError:
            continue
        src = "git:" + sha[:10]
        promoted = bool(PROMOTE_MSG.search(subject))
        dirs = {}
        for line in body.splitlines():
            cols = line.split("\t")
            if len(cols) < 2 or any(c.startswith('"') for c in cols[1:]):
                continue
            status = cols[0]
            if status == "A":
                path = cols[1]
                name = path.rsplit("/", 1)[-1]
                if path.startswith("Knowledge/") and path.endswith(".md") \
                        and not name.startswith("_") and name != "index.md":
                    if promoted:
                        found.append(dict(kind="promote", item=path, to=path, level_from=3,
                                          level_to=4, ts=ts, note=_bnote(0.7, src, subject)))
                    else:
                        found.append(dict(kind="create", item=path, to=path, level_to=4,
                                          ts=ts, note=_bnote(0.8, src, subject)))
                elif path.startswith("global-memory/") and path.endswith(".md") \
                        and name != "MEMORY.md":
                    found.append(dict(kind="promote", item=path, to=path,
                                      level_from=4 if promoted else None, level_to=5, ts=ts,
                                      note=_bnote(0.7 if promoted else 0.5, src, subject)))
            elif status.startswith("R") and len(cols) >= 3:
                old, new = cols[1], cols[2]
                pair = _project_dir_pair(old, new)
                if pair:
                    entry = dirs.setdefault(pair, {"moves": [], "readme": False})
                    entry["moves"].append((old, new))
                    entry["readme"] = entry["readme"] or old == pair[0] + "/README.md"
                    continue
                ev = _ladder_move(vault, old, new, ts, src, subject)
                if ev:
                    found.append(ev)
        for (old_dir, new_dir), entry in sorted(dirs.items()):
            if entry["readme"]:
                # The folder's own README moved with it: the project was renamed.
                found.append(dict(kind="rename", item=new_dir, frm=old_dir, to=new_dir,
                                  project=_project_or_none(new_dir[len("Projects/"):]),
                                  ts=ts, note=_bnote(0.9, src, "%d file(s): %s"
                                                     % (len(entry["moves"]), subject))))
                continue
            # Files left a project that kept its README (merge-project does this):
            # not a rename -- judge each move on the ladder instead.
            for old, new in entry["moves"]:
                ev = _ladder_move(vault, old, new, ts, src, subject)
                if ev:
                    found.append(ev)
    return found


def _project_dir_pair(old, new):
    """(`Projects/a`, `Projects/b`) when the move differs only in one project folder."""
    po, pn = old.split("/"), new.split("/")
    if not (po[0] == pn[0] == "Projects") or "spool" in po or "spool" in pn:
        return None
    k = 0
    while k < min(len(po), len(pn)) - 1 and po[-1 - k] == pn[-1 - k]:
        k += 1
    ho, hn = po[:len(po) - k], pn[:len(pn) - k]
    if k and len(ho) == len(hn) and len(ho) in (2, 3) and ho[:-1] == hn[:-1] \
            and ho[-1] != hn[-1] and ho[-1] not in NOT_PROJECT_DIRS \
            and hn[-1] not in NOT_PROJECT_DIRS:
        return "/".join(ho), "/".join(hn)
    return None


def _ladder_move(vault, old, new, ts, src, subject):
    """A file rename judged by ladder level: up = promote, down or across = relocate."""
    lf, lt = level_of(old), level_of(new)
    if not (lf and lt):
        return None
    base = dict(item=new, frm=old, to=new, level_from=lf, level_to=lt, ts=ts,
                project=project_of(vault, new) or project_of(vault, old))
    if lt > lf:
        return dict(base, kind="promote", note=_bnote(0.9, src, subject))
    if lt < lf:
        return dict(base, kind="relocate", note=_bnote(0.8, src, subject))
    if lf >= 4:
        return dict(base, kind="rename", note=_bnote(0.8, src, subject))
    if project_of(vault, old) != project_of(vault, new):
        return dict(base, kind="relocate", note=_bnote(0.7, src, subject))
    return None


def _from_git_tasks(vault, since):
    """README checkbox lines added (task.open/task.done) or flipped, from `git log -p`."""
    gt_tasks = load_sibling("gt_tasks")
    if gt_tasks is None:
        return []
    args = ["log", "--relative", "--no-color", "--no-renames", "-p", "-U0",
            "--format=%x1e%H%x1f%aI", "--", "Projects/*/README.md"]
    if since:
        args.insert(1, "--since=" + since)
    out = _git(vault, *args)
    if out is None:
        return []
    found = []

    def label_of(raw):
        return gt_tasks.FIELD.sub("", raw).strip().rstrip("—-").strip()

    for block in out.split("\x1e")[1:]:
        head, _, body = block.partition("\n")
        try:
            sha, ts = head.split("\x1f", 1)
        except ValueError:
            continue
        src = "git:" + sha[:10]
        path, removed, added = None, {}, []

        def flush():
            if not path or not path.endswith("README.md"):
                return
            for label, done in added:
                was = removed.get(label)
                if was:
                    prev = was.pop(0)
                    if prev == done:
                        continue
                kind = "task.done" if done else "task.open"
                found.append(dict(kind=kind, item=task_item(path, label),
                                  project=project_of(vault, path), ts=ts,
                                  note=_bnote(0.7, src, label)))

        for line in body.splitlines():
            if line.startswith("diff --git "):
                flush()
                path, removed, added = None, {}, []
            elif line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else None
            elif line.startswith("--- "):
                continue
            elif line[:1] in ("+", "-") and path:
                t = gt_tasks.TASK.match(line[1:])
                if not t:
                    continue
                label = label_of(t.group(2))
                if not label:
                    continue
                done = t.group(1).lower() == "x"
                if line[0] == "-":
                    removed.setdefault(label, []).append(done)
                else:
                    added.append((label, done))
        flush()
    return found


def _from_log(vault, since):
    """Dated log.md lines with an arrow whose verb maps to an event kind."""
    log = Path(vault) / "log.md"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        m = LOG_LINE.match(line)
        if not m or not ARROW.search(m.group(5)):
            continue
        date, hm, zone, tag, rest = m.groups()
        if since and date < since[:10]:
            continue
        kind = TAG_KINDS.get(tag.lower())
        conf = 0.7
        if kind is None:
            conf = 0.5
            kind = next((k for pat, k in VERB_KINDS if re.search(pat, rest, re.I)), None)
            if kind is None:
                continue
        a = ARROW.search(rest)
        frm, to = (_endpoint(vault, a.group(1)), _endpoint(vault, a.group(2)))
        if not (frm or to):
            conf -= 0.2
        offset = zone if zone and zone[0] in "+-" else ZONES.get(zone or "UTC", "+00:00")
        if len(offset) == 5:
            offset = offset[:3] + ":" + offset[3:]
        ts = "%sT%s:00%s" % (date, hm or "00:00", offset)
        digest = hashlib.sha1(line.encode("utf-8")).hexdigest()[:10]
        target_path = to or frm
        found.append(dict(kind=kind, item=target_path or "log.md", frm=frm, to=to,
                          level_from=level_of(frm), level_to=level_of(to),
                          project=project_of(vault, target_path), ts=ts,
                          note=_bnote(conf, "log:" + digest, rest)))
    return found


def _from_readmes(vault, covered):
    """Current `## Tasks` lines no task event covers yet (seeds gt_tasks' diff)."""
    gt_tasks = load_sibling("gt_tasks")
    if gt_tasks is None:
        return []
    found, seen = [], set()
    ts = now_ts()
    pdir = Path(vault) / "Projects"
    for readme in sorted(set(pdir.glob("*/README.md")) | set(pdir.glob("*/*/README.md"))):
        try:
            text = readme.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = gt_tasks.frontmatter(text)
        if fm.get("type") != "project":
            continue
        rel = readme.relative_to(vault).as_posix()
        for t in gt_tasks.parse_tasks(text, rel):
            item = task_item(rel, t["text"])
            if item in covered or item in seen or not t["text"]:
                continue
            seen.add(item)
            found.append(dict(kind="task.done" if t["done"] else "task.open", item=item,
                              project=project_of(vault, rel), ts=ts,
                              note=_bnote(0.5, "readme", t["text"])))
    return found


def _key(ev):
    return (ev["kind"], ev["item"], ev.get("from"), ev.get("to"),
            note_fields(ev.get("note")).get("src"))


def backfill(vault, since=None, sid=None):
    """-> (new events, number already present, number refused by validation)."""
    vault = Path(vault)
    existing, _ = read_spool(vault)
    keys = {_key(ev) for ev in existing if ev["actor"] == "backfill"}
    raw = []
    if _git(vault, "rev-parse", "--is-inside-work-tree") is not None:
        raw += _from_git_names(vault, since)
        raw += _from_git_tasks(vault, since)
    raw += _from_log(vault, since)
    new, dup, bad = [], 0, 0
    for r in raw:
        try:
            ev = build(r["kind"], r["item"], r.get("frm"), r.get("to"), r.get("level_from"),
                       r.get("level_to"), r.get("project"), "backfill", r.get("note"), sid,
                       r.get("ts"))
        except Invalid:
            bad += 1
            continue
        k = _key(ev)
        if k in keys:
            dup += 1
            continue
        keys.add(k)
        new.append(ev)
    covered = set(task_state(existing)) | {ev["item"] for ev in new
                                           if ev["kind"] in ("task.open", "task.done")}
    for r in _from_readmes(vault, covered):
        k = (r["kind"], r["item"], None, None, "readme")
        if k in keys:
            dup += 1
            continue
        try:
            ev = build(r["kind"], r["item"], project=r.get("project"), actor="backfill",
                       note=r["note"], sid=sid, ts=r["ts"])
        except Invalid:
            bad += 1
            continue
        keys.add(k)
        new.append(ev)
    return new, dup, bad


def cmd_backfill(a):
    v = S.vault_root(a.vault)
    since = a.since
    if since and not re.match(r"^\d{4}-\d{2}-\d{2}", since):
        print("--since: %r is not a YYYY-MM-DD date" % since, file=sys.stderr)
        return 2
    new, dup, bad = backfill(v, since, a.id)
    counts = {}
    for ev in new:
        counts[ev["kind"]] = counts.get(ev["kind"], 0) + 1
    summary = ", ".join("%s=%d" % kv for kv in sorted(counts.items())) or "none"
    if a.dry_run:
        print("dry run: backfill would add %d event(s) [%s]; %d already present, "
              "%d refused by validation; nothing written" % (len(new), summary, dup, bad))
        for ev in new[:a.sample]:
            print("  " + serialise(ev))
        return 0
    if not new:
        print("backfill: nothing new (%d already present, %d refused)" % (dup, bad))
        return 0
    sp = append_events(v, new, a.id)
    print("backfill: added %d event(s) [%s] -> %s; %d already present, %d refused"
          % (len(new), summary, sp.relative_to(v), dup, bad))
    if not a.no_merge:
        return cmd_merge(argparse.Namespace(vault=str(v), dry_run=False, quiet=False))
    return 0


def _report_bad(bad, vault):
    for f, n, why in bad:
        try:
            name = f.relative_to(vault)
        except ValueError:
            name = f
        print("  %s:%d: %s" % (name, n, why), file=sys.stderr)


def cmd_emit(a):
    v = S.vault_root(a.vault)
    sid = S.session_id(a.id)
    ev = {
        "v": VERSION,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "session": sid,
        "actor": a.actor,
        "kind": a.kind,
        "item": a.item,
        "from": a.from_,
        "to": a.to,
        "level_from": None,
        "level_to": None,
        "project": a.project,
    }
    if a.note is not None:
        ev["note"] = a.note
    try:
        for key, raw in (("level_from", a.level_from), ("level_to", a.level_to)):
            if raw is not None:
                try:
                    ev[key] = int(raw)
                except ValueError:
                    raise Invalid("%s: %r must be an integer 1-5" % (key, raw))
        validate_event(ev)
    except Invalid as exc:
        print("REFUSED: invalid event, nothing written -- %s" % exc, file=sys.stderr)
        return 2
    line = serialise(ev)
    sp = spool_file(v, sid)
    if a.dry_run:
        print("dry run: would append to %s%s\n  %s"
              % (sp.relative_to(v), "" if a.no_merge else " and regenerate events.jsonl",
                 line))
        return 0
    sp.parent.mkdir(parents=True, exist_ok=True)
    S._safe_write(sp, line + "\n")
    print("spooled -> %s" % sp.relative_to(v))
    if not a.no_merge:
        rc = cmd_merge(argparse.Namespace(vault=str(v), dry_run=False, quiet=True))
        if rc:
            print("the event is spooled; events.jsonl was NOT regenerated", file=sys.stderr)
            return rc
    return 0


def cmd_merge(a):
    v = S.vault_root(a.vault)
    t = target(v)
    events, bad = read_spool(v)
    if bad:
        print("REFUSED: %d invalid spool line(s); events.jsonl not written" % len(bad),
              file=sys.stderr)
        _report_bad(bad, v)
        return 2
    produced = render(events)
    if not events and not t.exists():
        if not getattr(a, "quiet", False):
            print("no events spooled; nothing to merge")
        return 0
    if getattr(a, "dry_run", False):
        try:
            current = t.read_text(encoding="utf-8")
        except OSError:
            current = None
        print("dry run: events.jsonl would be %s (%d event(s))"
              % ("unchanged" if produced == current else "rewritten from the spool",
                 len(events)))
        return 0
    changed = S.write_if_changed(t, produced)
    if not getattr(a, "quiet", False):
        print("events.jsonl %s (%d event(s))"
              % ("updated" if changed else "unchanged (idempotent)", len(events)))
    return 0


def cmd_validate(a):
    v = None
    if a.file:
        files = [Path(a.file).expanduser()]
    else:
        v = S.vault_root(a.vault)
        d = S.spool_dir(v, KIND)
        files = sorted(d.glob("*.jsonl")) if d.is_dir() else []
        if target(v).exists():
            files.append(target(v))
    bad, good = [], 0
    for f in files:
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            bad.append((f, 0, "unreadable (%s)" % exc))
            continue
        for i, ln in enumerate(body.splitlines()):
            if not ln.strip():
                continue
            try:
                parse_line(ln)
                good += 1
            except Invalid as exc:
                bad.append((f, i + 1, str(exc)))
    for f, n, why in bad:
        name = f
        if v is not None:
            try:
                name = f.relative_to(v)
            except ValueError:
                pass
        print("  %s:%d: %s" % (name, n, why))
    print("validate: %d valid, %d invalid line(s) in %d file(s)"
          % (good, len(bad), len(files)))
    return 1 if bad else 0


def cmd_list(a):
    v = S.vault_root(a.vault)
    events, bad = read_spool(v)
    if bad:
        print("warning: skipped %d invalid spool line(s); run `validate`" % len(bad),
              file=sys.stderr)
    since = None
    if a.since:
        try:
            since = parse_ts(a.since)
        except Invalid:
            try:
                since = datetime.fromisoformat(a.since).astimezone()
            except ValueError:
                print("--since: %r is not ISO-8601" % a.since, file=sys.stderr)
                return 2
    out = []
    for ev in events:
        if a.kind and ev["kind"] != a.kind:
            continue
        if a.project and ev["project"] != a.project:
            continue
        if since is not None and parse_ts(ev["ts"]) < since:
            continue
        out.append(ev)
    for ev in out:
        if a.json:
            print(serialise(ev))
        else:
            move = ""
            if ev["from"] or ev["to"]:
                move = " %s -> %s" % (ev["from"] or "-", ev["to"] or "-")
            if ev["level_from"] or ev["level_to"]:
                move += " [L%s->L%s]" % (ev["level_from"] or "-", ev["level_to"] or "-")
            print("%s  %-16s %s%s%s%s" % (
                ev["ts"], ev["kind"], ev["item"], move,
                "  (%s)" % ev["project"] if ev["project"] else "",
                "  -- " + ev["note"] if ev.get("note") else ""))
    if not a.json:
        print("%d event(s)" % len(out))
    return 0


def main(argv=None):
    # --vault/--id/--dry-run on the top level AND every subcommand, SUPPRESS defaults:
    # see gt_log.py main() for why both halves matter.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=argparse.SUPPRESS)
    common.add_argument("--id", default=argparse.SUPPRESS,
                        help="session id override (testing)")
    common.add_argument("--dry-run", "-n", action="store_true",
                        default=argparse.SUPPRESS,
                        help="say what would change; write nothing")
    p = argparse.ArgumentParser(description="structured vault event stream",
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit", parents=[common], help="validate and spool one event")
    e.add_argument("--kind", required=True, help="|".join(KINDS))
    e.add_argument("--item", required=True, help="vault-relative path or task id")
    e.add_argument("--from", dest="from_", default=None)
    e.add_argument("--to", default=None)
    e.add_argument("--level-from", default=None)
    e.add_argument("--level-to", default=None)
    e.add_argument("--project", default=None)
    e.add_argument("--actor", default="claude", help="|".join(ACTORS))
    e.add_argument("--note", default=None, help="at most %d chars" % NOTE_MAX)
    e.add_argument("--no-merge", action="store_true")
    sub.add_parser("merge", parents=[common], help="render events.jsonl from the spool")
    val = sub.add_parser("validate", parents=[common], help="report invalid lines")
    val.add_argument("file", nargs="?", default=None)
    ls = sub.add_parser("list", parents=[common], help="read-only listing")
    ls.add_argument("--kind", default=None)
    ls.add_argument("--project", default=None)
    ls.add_argument("--since", default=None)
    ls.add_argument("--json", action="store_true")
    bf = sub.add_parser("backfill", parents=[common],
                        help="derive actor=backfill events from git history and log.md")
    bf.add_argument("--since", default=None, help="YYYY-MM-DD: ignore anything older")
    bf.add_argument("--sample", type=int, default=5, help="dry run: lines to show")
    bf.add_argument("--no-merge", action="store_true")
    args = p.parse_args(argv)
    for key, default in (("vault", None), ("id", None), ("dry_run", False)):
        if not hasattr(args, key):
            setattr(args, key, default)
    return {"emit": cmd_emit, "merge": cmd_merge, "validate": cmd_validate,
            "list": cmd_list, "backfill": cmd_backfill}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
