#!/usr/bin/env python3
"""Golden Thread flow: draw knowledge moving up the ladder, over time, as one HTML file.

    gt_flow.py render --vault V [--out FILE|DIR] [--redact] [--tasks] [--project P ...]
                      [--since DATE]

## What it reads, what it writes

Reads `<vault>/Projects/golden-thread/events.jsonl` (the event stream, schema v1, written
by gt's `gt_events.py merge`) and, for lane labels only, the `stage` and `domain`
frontmatter of `<vault>/Projects/<slug>/README.md`. It writes ONE file: `--out`, or
`gt-flow-<stamp>.html` in the current directory -- or in a fresh temp directory when the
current directory is inside the vault. Nothing is ever written inside the vault; an
`--out` that points into it is refused. Bytecode is not written either, so importing the
vault's own validator leaves no `__pycache__` behind.

## The page

Projects are swimlanes, time runs left to right, and each lane is split into the ladder's
bands, bottom to top: none, 1 session, 2 memory, 3 research/decisions/design, 4 Knowledge,
5 global-memory, Core. An event is one mark at its time, in the band it arrived at; when
it names both a from-level and a to-level that differ, an arrow joins them, so a
promotion is an arrow climbing. An event whose `from` is where an earlier mark in the lane arrived is joined to that mark,
so one item's climb reads as a single thread. The kind is carried by colour AND shape (five families),
the level by vertical position, so colour is never the only encoding. The SVG is built
here, not in the browser: the page carries its count in `data-count`, which equals the
number of marks visible at load (every `class="mark"` element not also `off`). Vanilla JS
adds the project and kind filters and a details panel on hover, focus or click (Tab or arrow keys
walk the marks). No network at all -- no CDN, no web fonts -- so it opens offline.

Levels the event leaves null are inferred from its path where the path is unambiguous
(`Knowledge/` -> 4, `global-memory/` -> 5, `.../core-rules/` -> Core, `.../memory/` -> 2,
`research.md|decisions.md|design.md|spec.md` -> 3) and the details panel says so. Schema
v1 caps levels at 5, so Core is always inferred from a `core-rules/` path.

## Task events are hidden at first

On a real vault `task.open` / `task.done` are most of the stream (a backfill of one vault:
~1,219 of ~1,430 events) and bury the knowledge movement the page exists to show. So the
`task.*` kinds start unchecked in the Kinds filter: their marks are in the SVG and the data,
drawn `off`, one click away, and the header says "211 of 1430 events shown (task events
hidden -- toggle in Kinds)". `--tasks` renders with them shown.

## --redact

Every project, path, task id, domain and session becomes a short salted hash
(`p-3fa2`, `f-91c0`, `d-..`, `s-..`); notes are dropped. Levels, kinds, actors, stages,
colours and counts are kept. The salt is random per render and never written, so one
file is internally consistent and two renders cannot be joined. The redacted payload is
built from a whitelist -- nothing from an event reaches the page except through the hash
-- and then checked: if any original name still appears in what was generated, the
render is refused and nothing is written. Anything published or shared must be rendered
with --redact.

## Exit codes

0 written · 1 bad arguments, an invalid or unreadable event file, or an unknown event `v`
· 2 no events yet (missing or empty events.jsonl) · 3 the filters matched nothing.

Standard library only, Python 3.8+.
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.dont_write_bytecode = True   # an imported validator must leave no __pycache__ behind

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_REL = os.path.join("Projects", "golden-thread", "events.jsonl")
SCHEMA_VERSION = 1

# -- schema v1, mirrored from gt_events.py for when that file cannot be found ----------
ACTORS = ("claude", "user", "cron", "backfill")
KINDS = ("capture", "file", "create", "promote", "relocate", "retire", "rename", "merge",
         "archive", "ingest", "task.open", "task.done", "adr", "source.supersede",
         "announce")
KEYS = ("v", "ts", "session", "actor", "kind", "item", "from", "to", "level_from",
        "level_to", "project", "note")

# Kind families: one colour and one shape each, so kind never rests on colour alone.
FAMILIES = (
    ("arrive", "Arrive", "circle", ("capture", "file", "create", "ingest", "adr")),
    ("move", "Move", "triangle", ("promote", "relocate", "rename", "merge")),
    ("leave", "Leave", "square", ("retire", "archive", "source.supersede")),
    ("task", "Task", "diamond", ("task.open", "task.done")),
    ("say", "Announce", "star", ("announce",)),
)
FAMILY_OF = {k: f[0] for f in FAMILIES for k in f[3]}

CORE = 6       # display level for core-rules/, above schema v1's 5
NONE = 0       # display band for events with no level at all
BANDS = ((CORE, "Core", "core-rules/"), (5, "5", "global-memory/"), (4, "4", "Knowledge/"),
         (3, "3", "research · decisions · design"), (2, "2", "memory/"),
         (1, "1", "session"), (NONE, "–", "no level"))
BAND_H = 22
LABEL_W = 190
TOP = 34
LANE_GAP = 10
RECORD_KEYS = ("i", "ts", "kind", "family", "actor", "item", "from", "to", "project",
               "session", "level_from", "level_to", "inferred", "from_name", "to_name", "note",
               "events", "total_in_file", "count", "redacted")


class Refused(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code


# -- validator: gt's own when it can be found, else the mirror ---------------------------
def _newest(root, sub):
    try:
        vs = [d for d in os.listdir(root) if re.fullmatch(r"\d+\.\d+\.\d+", d)
              and os.path.isfile(os.path.join(root, d, sub, "gt_events.py"))]
    except OSError:
        return None
    if not vs:
        return None
    return os.path.join(root, max(vs, key=lambda s: tuple(int(x) for x in s.split("."))), sub)


def _validator_dirs(vault):
    """Where gt_events.py may live, first hit wins: the gt release beside this module
    (plugin cache or source checkout), $GT_CORE_SCRIPTS/../templates/tools, the vault's
    own tools, then the installed gt in the home plugin cache."""
    parent = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    sub = os.path.join("templates", "tools")
    out = [_newest(os.path.join(parent, "gt"), sub),
           _newest(os.path.join(parent, "golden-thread"), sub)]
    env = os.environ.get("GT_CORE_SCRIPTS")
    if env:
        out.append(os.path.join(os.path.dirname(env), sub))
    out.append(os.path.join(vault, "Projects", "golden-thread", "tools"))
    out.append(_newest(os.path.expanduser("~/.claude/plugins/cache/golden-thread-plugin/gt"),
                       sub))
    return [d for d in out if d and os.path.isfile(os.path.join(d, "gt_events.py"))]


def load_validator(vault):
    """-> (validate(ev) raising ValueError, source label)."""
    import importlib.util
    for d in _validator_dirs(vault):
        added = d not in sys.path
        if added:
            sys.path.insert(0, d)
        try:
            spec = importlib.util.spec_from_file_location("gt_events_for_flow",
                                                          os.path.join(d, "gt_events.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if getattr(mod, "VERSION", None) == SCHEMA_VERSION and \
                    callable(getattr(mod, "validate_event", None)):
                return mod.validate_event, os.path.join(d, "gt_events.py")
        except Exception:
            pass
        finally:
            if added and d in sys.path:
                sys.path.remove(d)
    return validate_event, "built-in mirror of schema v1"


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SESSION = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")


def parse_ts(value):
    if not isinstance(value, str) or not value:
        raise ValueError("ts: must be an ISO-8601 string with an offset")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("ts: %r is not ISO-8601" % value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("ts: %r has no UTC offset" % value)
    return dt


def validate_event(ev):
    """The same checks as gt_events.validate_event, for when that file is not found."""
    if not isinstance(ev, dict):
        raise ValueError("not a JSON object")
    unknown = sorted(set(ev) - set(KEYS))
    if unknown:
        raise ValueError("unknown key(s): %s" % ", ".join(unknown))
    missing = [k for k in KEYS[:-1] if k not in ev]
    if missing:
        raise ValueError("missing key(s): %s" % ", ".join(missing))
    parse_ts(ev["ts"])
    if not isinstance(ev["session"], str) or not _SESSION.match(ev["session"]):
        raise ValueError("session: %r is not a session id" % (ev["session"],))
    if ev["actor"] not in ACTORS:
        raise ValueError("actor: %r is not one of %s" % (ev["actor"], "|".join(ACTORS)))
    if ev["kind"] not in KINDS:
        raise ValueError("kind: %r is not one of %s" % (ev["kind"], "|".join(KINDS)))
    for key, nullable in (("item", False), ("from", True), ("to", True)):
        v = ev[key]
        if v is None and nullable:
            continue
        if not isinstance(v, str) or not v.strip():
            raise ValueError("%s: must be a non-empty string" % key)
        if _CONTROL.search(v) or v.startswith(("/", "\\", "~")) or \
                re.match(r"^[A-Za-z]:", v) or ".." in v:
            raise ValueError("%s: %r is not a vault-relative path" % (key, v))
    for key in ("level_from", "level_to"):
        v = ev[key]
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)
                              or not 1 <= v <= 5):
            raise ValueError("%s: %r must be an integer 1-5 or null" % (key, v))
    p = ev["project"]
    if p is not None and (not isinstance(p, str) or not _PROJECT.match(p)):
        raise ValueError("project: %r is not a project slug" % (p,))
    if "note" in ev and (not isinstance(ev["note"], str) or len(ev["note"]) > 120
                         or _CONTROL.search(ev["note"])):
        raise ValueError("note: must be a string of at most 120 plain characters")


def read_events(vault):
    path = os.path.join(vault, EVENTS_REL)
    no_events = (
        "no events yet: %s %s.\n"
        "The flow view draws the event stream, and a vault has one only once Golden Thread\n"
        "tools have emitted events (gt-promote, gt-review, gt-create, gt_adr.py, ... through\n"
        "gt_events.py) and `gt_events.py merge` has written events.jsonl. For history from\n"
        "before events existed, preview a backfill from git first, then run it for real:\n"
        "  python3 %s/Projects/golden-thread/tools/gt_events.py backfill --vault %s --dry-run\n"
        "Nothing was written.")
    if not os.path.isfile(path):
        raise Refused(2, no_events % (path, "does not exist", vault, vault))
    try:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        raise Refused(1, "cannot read %s (%s)" % (path, e))
    lines = [(i + 1, ln) for i, ln in enumerate(body.splitlines()) if ln.strip()]
    if not lines:
        raise Refused(2, no_events % (path, "is empty", vault, vault))
    validate, _source = load_validator(vault)
    events = []
    for n, ln in lines:
        try:
            ev = json.loads(ln)
        except ValueError as e:
            raise Refused(1, "%s:%d: not valid JSON (%s); nothing was written" % (path, n, e))
        if isinstance(ev, dict) and (ev.get("v") != SCHEMA_VERSION or isinstance(ev.get("v"), bool)):
            raise Refused(1, "%s:%d: unknown event schema version v=%s; this gt-flow reads "
                             "schema v%d only. Install the gt-flow release that matches your gt "
                             "(bash install.sh). Nothing was written."
                          % (path, n, json.dumps(ev.get("v")), SCHEMA_VERSION))
        try:
            validate(ev)
        except ValueError as e:
            raise Refused(1, "%s:%d: invalid event (%s); run `gt_events.py validate` to see "
                             "every bad line. Nothing was written." % (path, n, e))
        events.append(ev)
    return events


# -- labels from README frontmatter ------------------------------------------------------
def frontmatter(path):
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read(20000)
    except (OSError, UnicodeDecodeError):
        return {}
    m = re.match(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", text, re.S)
    if not m:
        return {}
    out = {}
    for ln in m.group(1).splitlines():
        mm = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*$", ln)
        if mm:
            val = re.sub(r"\s+#.*$", "", mm.group(2)).strip().strip("\"'")
            out[mm.group(1)] = val
    return out


# -- levels ------------------------------------------------------------------------------
def infer_level(path):
    if not path:
        return None
    p = path.replace("\\", "/")
    if re.search(r"(^|/)core-rules(/|$)", p):
        return CORE
    if p.startswith("global-memory/"):
        return 5
    if p.startswith("Knowledge/"):
        return 4
    if re.match(r"^Projects/.+/(research|decisions|design|spec)\.md$", p):
        return 3
    if re.match(r"^Projects/.+/memory(/|$)", p):
        return 2
    return None


def levels(ev):
    """-> (from, to, inferred?) display levels; core-rules/ paths win as Core."""
    inferred = False
    lf, lt = ev["level_from"], ev["level_to"]
    if infer_level(ev["from"]) == CORE:
        lf = CORE
    elif lf is None:
        lf = infer_level(ev["from"])
        inferred = inferred or lf is not None
    tgt = ev["to"] or (ev["item"] if ev["from"] is None else None)
    if infer_level(tgt) == CORE:
        lt = CORE
    elif lt is None:
        lt = infer_level(tgt)
        inferred = inferred or lt is not None
    return lf, lt, inferred


# -- redaction ---------------------------------------------------------------------------
class Namer:
    """Real names, or salted short hashes that are stable within one render."""

    def __init__(self, redact):
        self.redact = redact
        self.salt = os.urandom(16) if redact else b""
        self.maps = {}
        self.used = set()

    def __call__(self, prefix, value):
        if value is None:
            return None
        if not self.redact:
            return value
        m = self.maps.setdefault(prefix, {})
        if value not in m:
            digest = hashlib.sha256(self.salt + prefix.encode() + b"\0"
                                    + value.encode("utf-8")).hexdigest()
            n = 4
            while "%s-%s" % (prefix, digest[:n]) in self.used:
                n += 1
            m[value] = "%s-%s" % (prefix, digest[:n])
            self.used.add(m[value])
        return m[value]


GENERIC_NAMES = {"README.md", "research.md", "decisions.md", "design.md", "spec.md",
                 "MEMORY.md", "index.md", "log.md", "INBOX.md", "idea.md", "source.md",
                 "Projects", "Knowledge", "global-memory", "memory", "core-rules",
                 "golden-thread", "Sources"}


def sensitive_strings(events, labels, vault):
    """Every original name a redacted page must not contain."""
    out = set()
    for ev in events:
        for key in ("item", "from", "to"):
            v = ev[key]
            if v:
                out.add(v)
                for seg in v.split("/"):
                    stem = seg[:-3] if seg.endswith(".md") else seg
                    for s in (seg, stem):
                        if len(s) >= 4 and s not in GENERIC_NAMES:
                            out.add(s)
        if ev["project"]:
            out.add(ev["project"])
            out.update(s for s in ev["project"].split("/") if len(s) >= 4)
        if ev.get("note") and len(ev["note"]) >= 4:
            out.add(ev["note"])
        out.add(ev["session"])
    for fm in labels.values():
        if fm.get("domain") and len(fm["domain"]) >= 4:
            out.add(fm["domain"])
    out.add(os.path.abspath(vault))
    return {s for s in out if s and s not in GENERIC_NAMES}


# -- filtering ---------------------------------------------------------------------------
def select(events, projects, since):
    out = events
    if projects:
        want = set(projects)
        out = [e for e in out if e["project"] is not None and
               any(e["project"] == p or e["project"].startswith(p + "/") for p in want)]
    if since:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
            out = [e for e in out if e["ts"][:10] >= since]
        else:
            try:
                cut = parse_ts(since)
            except ValueError as e:
                raise Refused(1, "--since: %s (use YYYY-MM-DD or ISO-8601 with an offset)" % e)
            out = [e for e in out if parse_ts(e["ts"]) >= cut]
    return out


# -- output location ---------------------------------------------------------------------
def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def resolve_out(out, vault, now):
    name = "gt-flow-%s.html" % now.strftime("%Y%m%d-%H%M%S")
    if out is None:
        base = os.getcwd()
        if _inside(base, vault):
            base = tempfile.mkdtemp(prefix="gt-flow-")
        path = os.path.join(base, name)
    elif os.path.isdir(out):
        path = os.path.join(out, name)
    else:
        path = out
    if _inside(os.path.dirname(os.path.abspath(path)) or ".", vault) or _inside(path, vault):
        raise Refused(1, "--out %s is inside the vault; the flow view never writes into the "
                         "vault. Choose a path outside it (or omit --out)." % path)
    if not os.path.isdir(os.path.dirname(os.path.abspath(path))):
        raise Refused(1, "--out %s: the directory does not exist" % path)
    return path


# -- page --------------------------------------------------------------------------------
def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def shape(kind_shape, x, y, r=5.5):
    if kind_shape == "circle":
        return '<circle class="glyph" cx="%.1f" cy="%.1f" r="%.1f"/>' % (x, y, r)
    if kind_shape == "square":
        return '<rect class="glyph" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>' % (
            x - r * .85, y - r * .85, r * 1.7, r * 1.7)
    if kind_shape == "triangle":
        return '<path class="glyph" d="M%.1f %.1fL%.1f %.1fL%.1f %.1fZ"/>' % (
            x, y - r * 1.1, x + r, y + r * .8, x - r, y + r * .8)
    if kind_shape == "diamond":
        return '<path class="glyph" d="M%.1f %.1fL%.1f %.1fL%.1f %.1fL%.1f %.1fZ"/>' % (
            x, y - r * 1.15, x + r * 1.15, y, x, y + r * 1.15, x - r * 1.15, y)
    pts = []
    import math
    for i in range(10):
        rr = r * 1.2 if i % 2 == 0 else r * .5
        a = -math.pi / 2 + i * math.pi / 5
        pts.append("%.1f %.1f" % (x + rr * math.cos(a), y + rr * math.sin(a)))
    return '<path class="glyph" d="M%sZ"/>' % "L".join(pts)


def arrow(x0, y0, x1, y1):
    """A line from (x0, y0) that stops short of the glyph at (x1, y1), with a head."""
    import math
    dx, dy = x1 - x0, y1 - y0
    d = math.hypot(dx, dy)
    if d < 12:
        return ""
    ux, uy = dx / d, dy / d
    tx, ty = x1 - ux * 7.5, y1 - uy * 7.5          # tip
    bx, by = tx - ux * 5.5, ty - uy * 5.5          # base of the head
    return ('<line class="arrow" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
            '<path class="arrowhead" d="M%.1f %.1fL%.1f %.1fL%.1f %.1fZ"/>'
            '<circle class="origin" cx="%.1f" cy="%.1f" r="2.2"/>'
            % (x0, y0, bx, by, tx, ty, bx - uy * 3.5, by + ux * 3.5, bx + uy * 3.5,
               by - ux * 3.5, x0, y0))


def band_y(lane_top, level):
    idx = [b[0] for b in BANDS].index(level if level is not None else NONE)
    return lane_top + idx * BAND_H + BAND_H / 2


def level_name(level):
    for lv, short, long_ in BANDS:
        if lv == level:
            return "%s (%s)" % (short, long_)
    return "none"


TASK_KINDS = tuple(k for k in KINDS if k.startswith("task."))


def build(events, total, vault, redact, now, filters_note, show_tasks=False):
    name = Namer(redact)
    projects = sorted({e["project"] for e in events}, key=lambda p: (p is None, p or ""))
    labels = {}
    for p in projects:
        if p is not None:
            labels[p] = frontmatter(os.path.join(vault, "Projects", *p.split("/"), "README.md"))

    times = [parse_ts(e["ts"]) for e in events]
    t0, t1 = min(times), max(times)
    if (t1 - t0) < timedelta(hours=12):
        t0, t1 = t0 - timedelta(hours=12), t1 + timedelta(hours=12)
    span_days = (t1 - t0).total_seconds() / 86400.0
    plot_w = int(min(8000, max(640, span_days * 28)))
    pad = 16
    width = LABEL_W + plot_w + pad * 2
    lane_h = len(BANDS) * BAND_H
    height = TOP + len(projects) * (lane_h + LANE_GAP) + 8

    def xof(dt):
        return LABEL_W + pad + (dt - t0).total_seconds() / (t1 - t0).total_seconds() * plot_w

    svg = []
    # time ticks
    step = 1
    for s in (1, 2, 7, 14, 30, 61, 91, 182, 365):
        step = s
        if span_days / s <= plot_w / 90.0:
            break
    tz = t0.tzinfo
    d = datetime(t0.year, t0.month, t0.day, tzinfo=tz)
    ticks = []
    while d <= t1:
        if d >= t0:
            ticks.append(d)
        d += timedelta(days=step)
    for d in ticks:
        x = xof(d)
        svg.append('<line class="tick" x1="%.1f" y1="%d" x2="%.1f" y2="%d"/>'
                   % (x, TOP - 6, x, height - 8))
        svg.append('<text class="ticklabel" x="%.1f" y="%d">%s</text>'
                   % (x, TOP - 12, d.strftime("%Y-%m-%d")))

    lanes = {}
    for i, p in enumerate(projects):
        top = TOP + i * (lane_h + LANE_GAP)
        lanes[p] = top
        fm = labels.get(p, {})
        pid = name("p", p) if p is not None else "(no project)"
        sub = " · ".join(x for x in (fm.get("stage"), name("d", fm.get("domain"))
                                     if fm.get("domain") else None) if x)
        svg.append('<g class="lane" data-project="%s">' % esc(pid))
        svg.append('<rect class="laneBg" x="0" y="%d" width="%d" height="%d"/>'
                   % (top, width, lane_h))
        for j, (lv, short, _long) in enumerate(BANDS):
            y = top + j * BAND_H
            if j % 2 == 1:
                svg.append('<rect class="band" x="%d" y="%d" width="%d" height="%d"/>'
                           % (LABEL_W, y, plot_w + pad * 2, BAND_H))
            svg.append('<text class="bandlabel" x="%d" y="%.1f">%s</text>'
                       % (LABEL_W - 6, y + BAND_H / 2 + 4, esc(short)))
        svg.append('<text class="lanelabel" x="8" y="%d">%s</text>' % (top + 16, esc(pid)))
        if sub:
            svg.append('<text class="lanesub" x="8" y="%d">%s</text>' % (top + 32, esc(sub)))
        svg.append('</g>')

    fam_shape = {f[0]: f[2] for f in FAMILIES}
    data = []
    occupied = {}
    last_at = {}
    order = sorted(range(len(events)), key=lambda i: times[i])
    for n, i in enumerate(order):
        ev = events[i]
        lf, lt, inferred = levels(ev)
        at = lt if lt is not None else lf
        top = lanes[ev["project"]]
        x = xof(times[i])
        slot = (ev["project"], at, int(x // 9))
        k = occupied.get(slot, 0)
        occupied[slot] = k + 1
        x += k * 9
        y = band_y(top, at)
        fam = FAMILY_OF[ev["kind"]]
        pid = name("p", ev["project"]) if ev["project"] is not None else None
        rec = {"i": n, "ts": ev["ts"], "kind": ev["kind"], "family": fam, "actor": ev["actor"],
               "item": name("f", ev["item"]), "from": name("f", ev["from"]),
               "to": name("f", ev["to"]), "project": pid,
               "session": name("s", ev["session"]),
               "level_from": lf, "level_to": lt, "inferred": inferred,
               "from_name": level_name(lf) if lf is not None else None,
               "to_name": level_name(lt) if lt is not None else None}
        if not redact and ev.get("note"):
            rec["note"] = ev["note"]
        data.append(rec)
        hidden = not show_tasks and ev["kind"] in TASK_KINDS
        parts = ['<g class="mark fam-%s%s" tabindex="%d" role="button" data-i="%d" '
                 'data-kind="%s" data-project="%s">' % (fam, " off" if hidden else "",
                                                          -1 if hidden else 0, n,
                                                          esc(ev["kind"]),
                                                          esc(pid or "(no project)"))]
        prev = last_at.get((ev["project"], ev["from"])) if ev["from"] else None
        if prev is not None and (lf is None or lt is None or lf != lt or prev[1] != y):
            # the thread: an arrow from the mark where this item last arrived
            parts.append(arrow(prev[0], prev[1], x, y))
        elif lf is not None and lt is not None and lf != lt:
            parts.append(arrow(x, band_y(top, lf), x, y))
        parts.append(shape(fam_shape[fam], x, y))
        parts.append('<title>%s %s</title>' % (esc(ev["kind"]), esc(rec["item"])))
        parts.append('</g>')
        svg.append("".join(parts))
        arrived = ev["to"] or (ev["item"] if ev["from"] is None else None)
        if arrived:
            last_at[(ev["project"], arrived)] = (x, y)

    n_tasks = 0 if show_tasks else sum(1 for e in events if e["kind"] in TASK_KINDS)
    visible = len(events) - n_tasks
    svg_text = ('<svg id="chart" width="%d" height="%d" '
                'viewBox="0 0 %d %d" role="group" aria-label="Knowledge flow: %d events" '
                'data-count="%d">%s</svg>'
                % (width, height, width, height, len(events), visible, "".join(svg)))

    proj_opts = [(name("p", p) if p is not None else "(no project)",
                  sum(1 for e in events if e["project"] == p)) for p in projects]
    kinds_present = [k for k in KINDS if any(e["kind"] == k for e in events)]
    payload = {"events": data, "total_in_file": total, "count": len(events),
               "redacted": redact}
    payload_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) \
        .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    stamp = now.strftime("%Y-%m-%d %H:%M:%S %z")
    dynamic = [svg_text, payload_text, filters_note or ""]
    proj_html = "".join(
        '<label><input type="checkbox" name="project" value="%s" checked> %s '
        '<span class="n">%d</span></label>' % (esc(p), esc(p), c) for p, c in proj_opts)
    kind_html = "".join(
        '<label><input type="checkbox" name="kind" value="%s"%s> '
        '<svg class="sw fam-%s" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">'
        '%s</svg> %s <span class="n">%d</span></label>'
        % (esc(k), "" if (k in TASK_KINDS and not show_tasks) else " checked", FAMILY_OF[k],
           shape(fam_shape[FAMILY_OF[k]], 7, 7.5, 4.5), esc(k),
           sum(1 for e in events if e["kind"] == k)) for k in kinds_present)
    fam_html = "".join(
        '<li><svg class="sw fam-%s" width="14" height="14" viewBox="0 0 14 14" '
        'aria-hidden="true">%s</svg> <b>%s</b> %s</li>'
        % (f[0], shape(f[2], 7, 7.5, 4.5), esc(f[1]), esc(", ".join(f[3]))) for f in FAMILIES)
    band_html = "".join('<li><b>%s</b> %s</li>' % (esc(s), esc(l)) for _lv, s, l in BANDS)
    dynamic += [proj_html]

    page = PAGE
    for key, val in (("STAMP", esc(stamp)), ("SHOWN", str(visible)),
                     ("COUNT", str(len(events))),
                     ("TASKHIDDEN", "" if n_tasks else " hidden"),
                     ("TOTAL", str(total)),
                     ("REDACTED", "redacted: names are salted hashes, notes dropped"
                      if redact else "NOT redacted: real names; run with --redact before sharing"),
                     ("RCLASS", "ok" if redact else "warn"),
                     ("FILTERS", esc(filters_note or "all events")),
                     ("PROJECTS", proj_html), ("KINDS", kind_html), ("FAMILIES", fam_html),
                     ("BANDS", band_html), ("SVG", svg_text), ("DATA", payload_text)):
        page = page.replace("{{%s}}" % key, val)
    if redact:
        # Vocabulary the page carries anyway (template, kinds, actors, level and family
        # names, JSON keys, stages): a name that is one of these words cannot be told apart
        # from it, so it is not a leak. Every other original name must be absent.
        vocab = "\n".join([PAGE, " ".join(KINDS + ACTORS + KEYS),
                           " ".join(b[2] for b in BANDS), " ".join(f[1] + " " + f[0]
                                                                  for f in FAMILIES),
                           " ".join(RECORD_KEYS),
                           " ".join(fm.get("stage", "") for fm in labels.values())])
        blob = "\n".join(dynamic)
        leaks = sorted(x for x in sensitive_strings(events, labels, vault)
                       if x not in vocab and (x in blob or esc(x) in blob))
        if leaks:
            raise Refused(1, "redaction self-check failed: %d original name(s) would appear "
                             "in the page; nothing was written" % len(leaks))
    return page


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Golden Thread flow</title>
<style>
:root{--bg:#fbfaf7;--fg:#1d1f23;--muted:#62666d;--line:#d9d6cf;--lane:#ffffff;--band:#f1efe9;
--panel:#ffffff;--focus:#0a58ca;--warn:#9a3412;--ok:#166534;
--arrive:#1f7a4d;--move:#1d5fb8;--leave:#a23b3b;--task:#9a6a00;--say:#7b3fa6}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#15171a;--fg:#e8e6e1;
--muted:#a3a7ad;--line:#33373d;--lane:#1c1f23;--band:#23272c;--panel:#1c1f23;--focus:#7fb0ff;
--warn:#fdba74;--ok:#86efac;--arrive:#5cc98f;--move:#6aa7ff;--leave:#f08a8a;--task:#e0b340;
--say:#c79bf0}}
:root[data-theme="dark"]{--bg:#15171a;--fg:#e8e6e1;--muted:#a3a7ad;--line:#33373d;--lane:#1c1f23;
--band:#23272c;--panel:#1c1f23;--focus:#7fb0ff;--warn:#fdba74;--ok:#86efac;--arrive:#5cc98f;
--move:#6aa7ff;--leave:#f08a8a;--task:#e0b340;--say:#c79bf0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding-block:20px 40px;padding-inline:16px}
h1{font-size:1.35rem;margin:0 0 4px}
.meta{color:var(--muted);margin:0 0 4px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;border:1px solid currentColor;font-size:12px}
.badge.warn{color:var(--warn)}.badge.ok{color:var(--ok)}
.row{display:flex;flex-wrap:wrap;gap:12px;margin:14px 0}
fieldset{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:0;flex:1 1 280px;min-width:0}
legend{font-weight:600;padding:0 4px}
fieldset label{display:inline-flex;align-items:center;gap:4px;margin:2px 10px 2px 0;max-width:100%;overflow-wrap:anywhere}
.n{color:var(--muted);font-size:12px}
button{font:inherit;font-size:12px;background:transparent;color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:1px 8px;margin-left:4px;cursor:pointer}
.legend ul{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:4px 14px}
.legend li{display:inline-flex;align-items:center;gap:5px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--lane);max-width:100%}
#chart{display:block}
#chart text{fill:var(--fg);font-size:11px}
#chart .ticklabel{fill:var(--muted);text-anchor:middle;font-size:10px}
#chart .tick{stroke:var(--line);stroke-dasharray:2 3}
#chart .laneBg{fill:var(--lane);stroke:var(--line)}
#chart .band{fill:var(--band)}
#chart .bandlabel{fill:var(--muted);text-anchor:end;font-size:10px}
#chart .lanelabel{font-weight:600;font-size:12px}
#chart .lanesub{fill:var(--muted);font-size:10px}
.fam-arrive{--c:var(--arrive)}.fam-move{--c:var(--move)}.fam-leave{--c:var(--leave)}
.fam-task{--c:var(--task)}.fam-say{--c:var(--say)}
.glyph{fill:var(--c);stroke:var(--lane);stroke-width:1}
.sw .glyph{stroke:none}
.mark .arrow{stroke:var(--c);stroke-width:1.6}
.mark .arrowhead{fill:var(--c)}
.mark .origin{fill:none;stroke:var(--c);stroke-width:1.2}
.mark{cursor:pointer;outline:none}
.mark:focus .glyph,.mark.sel .glyph{stroke:var(--focus);stroke-width:2.5}
.mark.off{display:none}
[hidden]{display:none!important}
#details{margin-top:12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:10px 12px;min-height:3.2em}
#details dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:0}
#details dt{color:var(--muted)}#details dd{margin:0;overflow-wrap:anywhere}
.hint{color:var(--muted);font-size:12px}
</style>
</head>
<body>
<main>
<h1>Golden Thread flow</h1>
<p class="meta">Generated {{STAMP}} · <span id="shown" data-count="{{SHOWN}}">{{SHOWN}}</span> of {{COUNT}} events shown<span id="taskhint"{{TASKHIDDEN}}> (task events hidden — toggle in Kinds)</span> ({{TOTAL}} in events.jsonl; {{FILTERS}}) · <span class="badge {{RCLASS}}">{{REDACTED}}</span></p>
<p class="hint">Time runs left to right; each project is a lane; height within a lane is the ladder level. An arrow joins the level an item left to the level it reached.</p>
<div class="row">
<fieldset id="fp"><legend>Projects <button type="button" data-all="project">all</button><button type="button" data-none="project">none</button></legend>{{PROJECTS}}</fieldset>
<fieldset id="fk"><legend>Kinds <button type="button" data-all="kind">all</button><button type="button" data-none="kind">none</button></legend>{{KINDS}}</fieldset>
</div>
<div class="row legend">
<fieldset><legend>Kind families (colour and shape)</legend><ul>{{FAMILIES}}</ul></fieldset>
<fieldset><legend>Levels (height in a lane)</legend><ul>{{BANDS}}</ul></fieldset>
</div>
<div class="scroll" id="scroll">{{SVG}}</div>
<section id="details" aria-live="polite"><span class="hint">Hover, focus (Tab, then arrow keys) or click a mark for its details.</span></section>
</main>
<script type="application/json" id="flow-data">{{DATA}}</script>
<script>
(function(){
var D=JSON.parse(document.getElementById('flow-data').textContent);
var svg=document.getElementById('chart'),shown=document.getElementById('shown');
var marks=Array.prototype.slice.call(svg.querySelectorAll('.mark'));
var det=document.getElementById('details'),sel=null,hint=document.getElementById('taskhint');
function checked(n){var o={};document.querySelectorAll('input[name="'+n+'"]').forEach(function(i){if(i.checked)o[i.value]=1});return o}
function apply(){var p=checked('project'),k=checked('kind'),c=0;
 marks.forEach(function(m){var on=p[m.getAttribute('data-project')]&&k[m.getAttribute('data-kind')];
  m.classList.toggle('off',!on);m.setAttribute('tabindex',on?'0':'-1');if(on)c++});
 svg.setAttribute('data-count',c);shown.setAttribute('data-count',c);shown.textContent=c;
 hint.hidden=!marks.some(function(m){return /^task[.]/.test(m.getAttribute('data-kind'))&&!k[m.getAttribute('data-kind')]});
 document.querySelectorAll('.lane').forEach(function(l){l.style.opacity=p[l.getAttribute('data-project')]?1:.35})}
function row(k,v){var dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=v;return [dt,dd]}
function show(m){var e=D.events[+m.getAttribute('data-i')];if(!e)return;
 if(sel)sel.classList.remove('sel');sel=m;m.classList.add('sel');
 var ch='';if(e.level_from!=null&&e.level_to!=null){var d=e.level_to-e.level_from;ch=d>0?'up '+d:d<0?'down '+(-d):'same level'}
 var dl=document.createElement('dl');
 [['when',e.ts],['kind',e.kind+' ('+e.family+')'],['item',e.item],['project',e.project||'(no project)'],
  ['from → to',(e.from||'—')+' → '+(e.to||'—')],
  ['level',(e.from_name||'—')+' → '+(e.to_name||'—')+(e.inferred?' (inferred from path)':'')],
  ['level change',ch||'—'],['actor',e.actor],['session',e.session],
  ['note',D.redacted?'(redacted)':(e.note||'—')]].forEach(function(r){row(r[0],r[1]).forEach(function(x){dl.appendChild(x)})});
 det.textContent='';det.appendChild(dl)}
marks.forEach(function(m,i){
 m.addEventListener('mouseenter',function(){show(m)});
 m.addEventListener('focus',function(){show(m)});
 m.addEventListener('click',function(){show(m);m.focus()});
 m.addEventListener('keydown',function(ev){
  var vis=marks.filter(function(x){return !x.classList.contains('off')}),j=vis.indexOf(m);
  if(ev.key==='ArrowRight'||ev.key==='ArrowDown'){if(j<vis.length-1)vis[j+1].focus();ev.preventDefault()}
  else if(ev.key==='ArrowLeft'||ev.key==='ArrowUp'){if(j>0)vis[j-1].focus();ev.preventDefault()}
  else if(ev.key==='Enter'||ev.key===' '){show(m);ev.preventDefault()}});
});
document.querySelectorAll('input[type=checkbox]').forEach(function(i){i.addEventListener('change',apply)});
document.querySelectorAll('button[data-all],button[data-none]').forEach(function(b){b.addEventListener('click',function(){
 var n=b.getAttribute('data-all')||b.getAttribute('data-none'),v=b.hasAttribute('data-all');
 document.querySelectorAll('input[name="'+n+'"]').forEach(function(i){i.checked=v});apply()})});
})();
</script>
</body>
</html>
"""


# -- cli ---------------------------------------------------------------------------------
def cmd_render(a):
    vault = os.path.abspath(os.path.expanduser(a.vault))
    if not os.path.isdir(vault):
        raise Refused(1, "--vault %s is not a directory" % a.vault)
    now = datetime.now().astimezone()
    if a.out is not None:
        resolve_out(a.out, vault, now)      # refuse a bad --out before any work
    events = read_events(vault)
    chosen = select(events, a.project, a.since)
    if not chosen:
        raise Refused(3, "no events match the filters (%d in events.jsonl); nothing was written"
                      % len(events))
    out = resolve_out(a.out, vault, now)
    bits = []
    if a.project:
        bits.append("%d project filter(s)" % len(a.project) if a.redact
                    else "project: " + ", ".join(a.project))
    if a.since:
        bits.append("since " + a.since)
    page = build(chosen, len(events), vault, a.redact, now, "; ".join(bits), a.tasks)
    tmp = out + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(page)
    os.replace(tmp, out)
    n_tasks = 0 if a.tasks else sum(1 for e in chosen if e["kind"] in TASK_KINDS)
    print("wrote %s (%d of %d events%s%s)" % (
        out, len(chosen), len(events),
        "; %d task event(s) hidden at load, --tasks shows them" % n_tasks if n_tasks else "",
        ", redacted" if a.redact else ", real names -- render with --redact before sharing"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_flow.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("render", help="render the flow view to one self-contained HTML file")
    r.add_argument("--vault", required=True)
    r.add_argument("--out", help="file or existing directory, outside the vault")
    r.add_argument("--redact", action="store_true",
                   help="hash file, project, domain and session names; drop notes")
    r.add_argument("--tasks", action="store_true",
                   help="show task.open/task.done marks at load (hidden by default)")
    r.add_argument("--project", action="append", default=[],
                   help="only this project (and its sub-projects); repeatable")
    r.add_argument("--since", help="only events on/after YYYY-MM-DD or an ISO-8601 instant")
    a = ap.parse_args(argv)
    if a.cmd != "render":
        ap.print_help(sys.stderr)
        return 1
    try:
        return cmd_render(a)
    except Refused as e:
        print("gt_flow: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
