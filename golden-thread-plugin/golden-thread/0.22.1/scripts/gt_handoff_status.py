#!/usr/bin/env python3
"""gt_handoff_status.py -- is a handoff still waiting on someone, and until when?

    gt_handoff_status.py list --vault V [--project SLUG] [--all] [--json]
    gt_handoff_status.py mark FILE --vault V --status handled|open|deferred
                              [--until YYYY-MM-DD] [--reason TEXT] [--dry-run]
    gt_handoff_status.py items FILE --vault V [--json]
    gt_handoff_status.py resolve FILE --vault V (--item ID|N|TEXT | --all)
                              --as closed|task|dropped|farmed [--reason TEXT] [--task ID] [--dry-run]
                              [--engine ROW --packet ID [--provider P --model M --connection C --kind K]
                               [--outcome pending|accepted|rejected] [--validation REF]]   (farmed)
    gt_handoff_status.py farmed --vault V [--json]      # one line: items farmed and waiting

A handoff exists because a session could NOT capture everything. Until 0.17.2 nothing recorded
whether anyone had dealt with it: `gt_surface` showed a new one once and then went quiet, so a
handoff scrolled past on a busy morning was as good as never written. This gives every handoff
a STATUS, and the status -- not a "shown once" ledger -- decides whether it keeps coming back.

THE FOUR STATES, and what decides each:

  open      written by gt_handoff, or re-opened; keeps surfacing until handled or deferred
  deferred  put off TO A DATE (`until:`). Hidden until then; on that date it is open again.
            There is no deferral without a date -- "later, some time" is a decision to drop it,
            and that is `handled` with a reason, said out loud.
  handled   marked so by a person, OR every task citing it has been checked off. The tasks
            gt-work raises are the record; when the last one closes, the handoff is done
            without anyone having to remember a second step.
  history   a handoff written before 0.17.2 (no `status:`) that is over a week old with no open
            task citing it. The upgrade must not resurface every old handoff in the vault.

A handoff with no `status:` field is treated as `open` while it is under a week old or any
open task cites it -- the same rule the first 0.17.2 surfacer used, so upgrading changes
nothing about what is shown on day one.

EACH OPEN ITEM MUST BE RESOLVED BEFORE THE HANDOFF IS HANDLED (0.21.0, owner 2026-10-08). An
item is an unchecked `- [ ]` line in the handoff itself (outside its `## Status log` and `## Item
resolutions`). `mark --status handled` -- and so `gt_close.py handoff` -- is refused while any item
has no SETTLED resolution, and the refusal names each one. `resolve` records how an item was dealt
with, as an append to the handoff's own `## Item resolutions` section, through the write queue:

  closed    done; the reason is optional
  task      converted to a tracked task; `--task ID` records an existing one, otherwise gt_task.py
            creates it, and the handoff records the task ID so the item can be followed
  dropped   not going to be done; a reason is REQUIRED and is recorded
  farmed    handed to an external engine (phase 78); NOT settled until its answer is accepted, and
            acceptance needs a validation reference (gt-validate / gt:verify ran on it). The record
            is key=value fields: engine (the farm/LOTR row), provider, model, connection, kind,
            packet, outcome (pending | accepted | rejected), validation. The label an accepted
            answer carries comes from the verification path its KIND takes (KINDS, VERIFY_PATHS),
            never from the engine: `self-verified (external engine, weaker)`, never "independently
            verified".

The set is the RESOLUTIONS table: a resolution is a name, what it needs, and whether an entry of it
is SETTLED. 0.22 adds a fourth, "farmed", whose `settled` test is false until the answer is
accepted. An unknown word in the file never counts as settled. gt_task.py (the vault's tool, which
cannot import this module) carries the same OUTCOMES, KINDS, VERIFY_PATHS and FARM_FIELDS tables;
tests/test_review_farm_handoff.py fails when the two drift.

This never loads a handoff's BODY into anything. `list` reads the frontmatter and counts the
task lines that cite the file, which is the whole point: surfacing a handoff must not cost the
context of handling it (owner, 2026-09-28).

THROUGH THE WRITE QUEUE (0.17.11). `mark` does not write the handoff: `status:` and `until:` go
to gt_write_queue.py as set-property requests and the status-log line as an append, and the queue
is drained at once by gt_broker.py -- so another LIVE session's claim on the file leaves the mark
QUEUED (exit 3, said on stderr) instead of being written over. The queue cannot delete a key, so a
deferral that ends leaves `until: none`, which reads as no date. A hand-written handoff with no
frontmatter at all is marked with ONE replace-file (new frontmatter block, the original body, the
status-log line) whose base is the hash of the bytes read, so an edit made in between is escalated
by the broker, never overwritten. Nothing here writes the handoff directly.

Exit: 0 ok | 2 usage | 3 could not do it (file missing, outside the vault, bad date, claimed by
another live session -- the mark is then queued, not lost).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

LEGACY_DAYS = 7
NO_DATE = "none"     # what `until:` becomes when a deferral ends: the queue cannot delete a key
STATES = ("open", "deferred", "handled")
TASK = re.compile(r"^\s*- \[( |x|X)\]\s+(.*)$")
FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)

RES_SECTION = "Item resolutions"
# Sections whose checkboxes are record, not open items.
NOT_ITEM_SECTIONS = ("status log", RES_SECTION.lower())
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
RES_LINE = re.compile(r"^- \[([^\]\s]+)\] (\S+) \((\d{4}-\d{2}-\d{2})\)(?:: ?(.*))?$")
BLOCK_ID = re.compile(r"\s\^(h\d+)\s*$")
HID_RE = re.compile(r"^handoff_id:\s*(H-\d{8}-[a-z2-7]{4})\s*$", re.M)
B32 = "abcdefghijklmnopqrstuvwxyz234567"

# The resolution set. name -> {"needs": None | "reason" | "task",
#                              "settled": callable(entry) -> bool  (default: always settled)}
# A resolution that is NOT settled leaves its item counting as unresolved (0.22's "farmed", until
# its answer is accepted). entry = {"id", "resolution", "date", "detail"}.
def _farmed_settled(entry) -> bool:
    d = parse_farmed(entry.get("detail") or "")
    return d.get("outcome") == "accepted" and bool(d.get("validation"))


RESOLUTIONS = {
    "closed": {"needs": None},
    "task": {"needs": "task"},
    "dropped": {"needs": "reason"},
    "farmed": {"needs": "farm", "settled": _farmed_settled},
}

# What a farmed item records (phase 78). Data, so a new engine, kind or verification path is a row.
# The engine is named by its farm/LOTR row; provider, model and connection are separate fields, never
# parsed out of one string. gt_task.py carries identical copies (it runs from the vault and cannot
# import this): tests/test_review_farm_handoff.py fails when they differ.
FARM_FIELDS = ("engine", "provider", "model", "connection", "kind", "packet", "outcome", "validation")
OUTCOMES = ("pending", "accepted", "rejected")
# How an answer is verified, and the label that path earns. The LABEL belongs to the PATH, never to an
# engine's name or reputation: an external engine's answer, checked by gt-validate's fresh agent over
# the engine's own claims, is still the engine's work.
VERIFY_PATHS = {"external-engine": "self-verified (external engine, weaker)"}
# What can be handed off. verify_path names the VERIFY_PATHS row its answers take.
KINDS = {
    "task": {"verify_path": "external-engine", "summary": "a task: bulk or mechanical work"},
    "finding": {"verify_path": "external-engine", "summary": "a finding: a second opinion on a claim"},
}
FARM_DEFAULT_KIND = "task"
FARM_TOKEN = re.compile(r"(?:^|\s)(" + "|".join(FARM_FIELDS) + r")=(\S+)")


def valid_kind(kind) -> bool:
    return kind in KINDS


def parse_farmed(detail: str) -> dict:
    """The key=value fields of a farmed entry's detail; anything else in it (a wikilink) is ignored."""
    return {k: v for k, v in FARM_TOKEN.findall(detail or "")}


def format_farmed(d: dict) -> str:
    return " ".join("%s=%s" % (k, d[k]) for k in FARM_FIELDS if d.get(k))


def farm_label(d: dict) -> str:
    """The label for an answer, from its KIND's verification path. Never from the engine."""
    kind = KINDS.get(d.get("kind") or FARM_DEFAULT_KIND) or KINDS[FARM_DEFAULT_KIND]
    return VERIFY_PATHS[kind["verify_path"]]


def item_id(text: str) -> str:
    """The LEGACY id of an item without a block id: a hash of its text. Kept so a resolution
    recorded before 0.21.0 ids still counts."""
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()[:6]


def _body(text: str) -> str:
    m = FM.match(text)
    return text[m.end():] if m else text


def handoff_id_of(text: str):
    m = FM.match(text)
    h = HID_RE.search(m.group(1) + "\n") if m else None
    return h.group(1) if h else None


def existing_handoff_ids(vault):
    out = set()
    root = Path(vault) / "Projects"
    if root.is_dir():
        for f in root.rglob("*.md"):
            if "handoff" not in f.as_posix().lower():
                continue
            try:
                h = handoff_id_of(f.read_text(encoding="utf-8")[:4000])
            except (OSError, UnicodeDecodeError):
                continue
            if h:
                out.add(h)
    return out


def new_handoff_id(vault=None, day=None, pick=None):
    """H-YYYYMMDD-xxxx: the date and 4 lowercase base32 characters from the secrets module,
    unique among the vault's existing handoffs (retry on a collision). Generated by CODE, never
    by the model. `pick` is a test seam for the random choice."""
    pick = pick or secrets.choice
    day = day or today()
    taken = existing_handoff_ids(vault) if vault else set()
    for _ in range(1000):
        cand = "H-%s-%s" % (day.strftime("%Y%m%d"), "".join(pick(B32) for _i in range(4)))
        if cand not in taken:
            return cand
    raise RuntimeError("no free handoff id after 1000 tries")


def _item_lines(text: str):
    """-> [(line index in the BODY lines, match)] for every open item (see items())."""
    lines = _body(text).splitlines()
    out, skip = [], False
    for i, line in enumerate(lines):
        h = HEADING.match(line)
        if h:
            skip = h.group(1).strip().lower() in NOT_ITEM_SECTIONS
            continue
        m = TASK.match(line)
        if m and m.group(1) == " " and not skip:
            out.append((i, m))
    return lines, out


def items(text: str):
    """Every open item the handoff raised: its unchecked checkboxes, in order, outside the status
    log and the resolutions record. -> [{"id", "text", "block"}]; id is the block id (`h2`) when
    the line carries one, else the legacy text hash."""
    out = []
    _lines, found = _item_lines(text)
    for _i, m in found:
        t = " ".join(m.group(2).split())
        b = BLOCK_ID.search(" " + t)
        block = b.group(1) if b else None
        if block:
            t = BLOCK_ID.sub("", " " + t).strip()
        if t:
            out.append({"id": block or item_id(t), "block": block, "text": t})
    return out


def assign_ids(text: str, hid: str):
    """-> (new text, [block ids assigned], handoff id assigned or None). Adds the `handoff_id`
    frontmatter key if missing and ` ^hN` to each open item line without one, numbered after the
    highest existing; changes no other text and never renumbers."""
    lines, found = _item_lines(text)
    used = [int(m.group(1)[1:]) for line in lines for m in [BLOCK_ID.search(line)] if m]
    nxt, assigned = (max(used) + 1 if used else 1), []
    for i, m in found:
        if not BLOCK_ID.search(lines[i]):
            lines[i] = lines[i].rstrip() + " ^h%d" % nxt
            assigned.append("h%d" % nxt)
            nxt += 1
    body_changed = bool(assigned)
    new = text
    if body_changed:
        head = text[:len(text) - len(_body(text))]
        tail = "\n" if text.endswith("\n") else ""
        new = head + "\n".join(lines) + tail
    added = None
    if not handoff_id_of(new):
        new = set_frontmatter(new, {"handoff_id": hid})
        added = hid
    return new, assigned, added


def resolutions(text: str) -> dict:
    """key -> the latest resolution entry recorded under it (a full id, a block id or a legacy
    text hash)."""
    out, on = {}, False
    for line in _body(text).splitlines():
        h = HEADING.match(line)
        if h:
            on = h.group(1).strip().lower() == RES_SECTION.lower()
            continue
        m = RES_LINE.match(line.rstrip()) if on else None
        if m:
            out[m.group(1)] = {"id": m.group(1), "resolution": m.group(2), "date": m.group(3),
                               "detail": (m.group(4) or "").strip()}
    return out


def is_settled(entry) -> bool:
    spec = RESOLUTIONS.get(entry["resolution"]) if entry else None
    if spec is None:
        return False
    test = spec.get("settled")
    return True if test is None else bool(test(entry))


def item_states(text: str, stem: str = ""):
    rec = resolutions(text)
    hid = handoff_id_of(text)
    rows = []
    for it in items(text):
        full = "%s#%s" % (hid, it["id"]) if (hid and it["block"]) else it["id"]
        e = rec.get(full) or rec.get(it["id"]) or rec.get(item_id(it["text"]))
        rows.append({"n": len(rows) + 1, "id": it["id"], "full_id": full, "text": it["text"],
                     "link": ("[[%s#^%s]]" % (stem, it["id"])) if (stem and it["block"]) else None,
                     "resolution": e["resolution"] if e else None,
                     "detail": e["detail"] if e else None, "settled": is_settled(e)})
        if e and e["resolution"] == "farmed":
            f = parse_farmed(e["detail"])
            f["label"] = farm_label(f)
            rows[-1]["farmed"] = f
    return rows


def unresolved_items(text: str, stem: str = ""):
    return [r for r in item_states(text, stem) if not r["settled"]]


def refusal(rel: str, left) -> str:
    return ("refused: %d item(s) in %s have no settled resolution -- resolve each as closed, a "
            "task, or dropped with a reason (gt_handoff_status.py resolve):\n%s"
            % (len(left), rel, "\n".join("  [%s] %s%s" % (
                r["full_id"], r["text"], (" (%s: not settled yet)" % r["resolution"])
                if r["resolution"] else "") for r in left)))


def today() -> datetime.date:
    pinned = os.environ.get("GT_TODAY")               # tests pin the clock
    if pinned:
        try:
            return datetime.date.fromisoformat(pinned)
        except ValueError:
            pass
    return datetime.date.today()


def read_frontmatter(text: str) -> dict:
    m = FM.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _projects(vault: Path):
    root = vault / "Projects"
    if not root.is_dir():
        return
    for p in sorted(root.iterdir()):
        if p.is_dir():
            yield p
            for sub in sorted(p.iterdir()):
                if sub.is_dir() and (sub / "README.md").is_file():
                    yield sub


def handoff_files(proj: Path):
    """gt_handoff writes `handoff/*.md`; people wrote `handoff*.md` beside the README before the
    tool was trusted, and both are real handoffs."""
    for f in sorted(list(proj.glob("handoff/*.md")) + list(proj.glob("handoff*.md"))):
        if f.is_file() and f.name.lower() != "readme.md":
            yield f


def citing_tasks(proj: Path, name: str):
    """-> (open, closed) counts of task lines in the project README naming this handoff file."""
    try:
        text = (proj / "README.md").read_text(encoding="utf-8")
    except OSError:
        return 0, 0
    o = c = 0
    for line in text.splitlines():
        m = TASK.match(line)
        if m and name in m.group(2):
            if m.group(1) == " ":
                o += 1
            else:
                c += 1
    return o, c


def slug_of(vault: Path, proj: Path) -> str:
    return proj.relative_to(vault / "Projects").as_posix()


def record(vault: Path, proj: Path, f: Path) -> dict:
    try:
        head = f.read_text(encoding="utf-8")[:2000]
    except OSError:
        head = ""
    fm = read_frontmatter(head)
    status = fm.get("status", "").lower()
    until = fm.get("until", "")
    if until.lower() == NO_DATE:
        until = ""
    open_n, closed_n = citing_tasks(proj, f.name)
    age = int((datetime.datetime.now().timestamp() - f.stat().st_mtime) // 86400)
    now = today()
    note = ""
    if status == "handled":
        eff = "handled"
    elif status == "deferred":
        try:
            due = datetime.date.fromisoformat(until)
        except ValueError:
            due = None
        if due is None:
            eff, note = "open", "deferred with no readable date -- treated as open"
        elif due > now:
            eff, note = "deferred", "until %s" % until
        else:
            eff, note = "open", "deferral ended %s" % until
    elif open_n == 0 and closed_n > 0:
        eff, note = "handled", "every task citing it is closed"
    elif status == "open":
        eff = "open"
    elif age <= LEGACY_DAYS or open_n:
        eff, note = "open", "no status recorded (written before 0.17.2)"
    else:
        eff = "history"
    return {"path": f.relative_to(vault).as_posix(), "project": slug_of(vault, proj),
            "status": eff, "recorded": status or None, "until": until or None,
            "open_items": open_n, "closed_items": closed_n, "age_days": age, "note": note}


def all_handoffs(vault: Path, project: str | None = None):
    out = []
    for proj in _projects(vault):
        slug = slug_of(vault, proj)
        if project and slug != project:
            continue
        for f in handoff_files(proj):
            try:
                out.append(record(vault, proj, f))
            except OSError:
                continue
    return out


def waiting(vault: Path, project: str | None = None):
    """Handoffs that want attention now, oldest first."""
    return sorted((r for r in all_handoffs(vault, project) if r["status"] == "open"),
                  key=lambda r: -r["age_days"])


def line(r: dict) -> str:
    items = ("%d open item%s" % (r["open_items"], "" if r["open_items"] == 1 else "s")
             if r["open_items"] else "no task cites it yet")
    extra = ("; " + r["note"]) if r["note"] else ""
    return "%s  (%s, %dd old, %s%s)" % (r["path"], r["project"], r["age_days"], items, extra)



# ------------------------------------------------------------------------ mark ----

def set_frontmatter(text: str, updates: dict) -> str:
    m = FM.match(text)
    lines = m.group(1).splitlines() if m else []
    body = text[m.end():] if m else text
    keys = {l.split(":", 1)[0].strip(): i for i, l in enumerate(lines) if ":" in l}
    for k, v in updates.items():
        if v is None:
            if k in keys:
                lines[keys[k]] = None
            continue
        if k in keys:
            lines[keys[k]] = "%s: %s" % (k, v)
        else:
            lines.append("%s: %s" % (k, v))
    lines = [l for l in lines if l is not None]
    return "---\n%s\n---\n%s" % ("\n".join(lines), body)


def do_mark(a) -> int:
    vault = Path(a.vault).expanduser().resolve()
    f = Path(a.file)
    f = (f if f.is_absolute() else vault / f).resolve()
    if not str(f).startswith(str(vault) + os.sep):
        print("refusing: %s is not inside the vault %s" % (f, vault), file=sys.stderr)
        return 3
    if not f.is_file():
        print("no such handoff: %s" % f, file=sys.stderr)
        return 3
    until = None
    if a.status == "deferred":
        if not a.until:
            print("a deferral needs --until YYYY-MM-DD. 'Later, some time' is a decision to "
                  "drop it: use --status handled --reason '...'", file=sys.stderr)
            return 2
        try:
            d = datetime.date.fromisoformat(a.until)
        except ValueError:
            print("--until must be YYYY-MM-DD, got %r" % a.until, file=sys.stderr)
            return 3
        if d <= today():
            print("--until %s is not in the future; a deferral to today is no deferral"
                  % a.until, file=sys.stderr)
            return 3
        until = a.until
    elif a.until:
        print("--until applies only to --status deferred", file=sys.stderr)
        return 2
    raw = f.read_bytes()
    text = raw.decode("utf-8")
    if a.status == "handled":
        left = unresolved_items(text, f.stem)
        if left:
            print(refusal(f.relative_to(vault).as_posix(), left), file=sys.stderr)
            return 3
    new = set_frontmatter(text, {"status": a.status, "until": until})
    stamp = today().isoformat()
    entry = "- %s %s%s%s" % (stamp, a.status, (" until " + until) if until else "",
                             (": " + a.reason) if a.reason else "")
    if "\n## Status log\n" not in new:
        new = new.rstrip("\n") + "\n\n## Status log\n\n"
    new = new.rstrip("\n") + "\n" + entry + "\n"
    rel = f.relative_to(vault).as_posix()
    if a.dry_run:
        print("would mark %s %s%s" % (rel, a.status, (" until " + until) if until else ""))
        return 0
    if FM.match(text):
        rc = _mark_via_queue(vault, rel, text, a.status, until, entry)
    else:
        # No frontmatter at all (a handoff written by hand before 0.17.2): set-property needs a
        # block to set a key in, so the whole new file goes as ONE replace-file, based on the
        # bytes read.
        rc = _mark_whole_file(vault, rel, raw, new, a.status, until)
    if rc == 0 and a.status == "handled":
        for r in item_states(text, f.stem):
            if r["resolution"]:
                print("  settled %s %s -- %s" % (r["full_id"], r["resolution"], r["text"]))
    return rc


def _wq():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import gt_write_queue                                         # noqa: PLC0415
    return gt_write_queue


def _mark_whole_file(vault: Path, rel, raw: bytes, new: str, status: str, until) -> int:
    """A handoff with no frontmatter: the whole new file (frontmatter block + original body +
    status-log line) as one replace-file, its base the sha256 of the raw bytes this tool READ --
    so an edit made after that read is escalated by the broker, never overwritten. Built from
    gt_write_queue's own build/validate/deposit because submit() records the base at queue time,
    not at read time."""
    import hashlib                                                # noqa: PLC0415
    wq = _wq()
    relp = str(rel).replace(os.sep, "/")
    req = wq.build(vault, relp, "replace-file", new, None, wq.session_id(None), "session", None)
    req["base_sha256"] = hashlib.sha256(raw).hexdigest()
    req["target_existed"] = True
    why = wq.validate(req, vault)
    if why:
        return _report([{"decision": "refused", "reason": why}], None, rel, status, until, wq,
                       vault)
    try:
        wq.deposit(vault, req)
    except OSError as exc:
        print("could not queue %s: %s" % (rel, exc), file=sys.stderr)
        return 3
    rows, note = wq.drain_now(vault)
    row = rows.get(req["id"]) or {"decision": "queued", "reason": "queued; not yet applied"}
    return _report([row], note, rel, status, until, wq, vault)


def _report(results, note, rel, status, until, wq, vault) -> int:
    """0 when every write was applied or already there; else one line on stderr, exit 3."""
    bad = [r for r in results if r["decision"] not in ("apply", "deduplicate")]
    if not bad:
        print("marked %s %s%s" % (rel, status, (" until " + until) if until else ""))
        return 0
    r = bad[0]
    if r["decision"] == "refused":
        print("refusing to mark %s: %s" % (rel, r["reason"]), file=sys.stderr)
    elif r["decision"] == "escalate":
        print("NOT marked: %s changed under the request; escalated to the owner as a #conflict "
              "task (%s)" % (rel, r.get("conflict", "spool/broker/conflicts/")), file=sys.stderr)
    else:
        print("queued, NOT marked yet: %s -- %s; %s" % (
            rel, note or r.get("reason") or r["decision"], wq.drain_hint(vault)), file=sys.stderr)
    return 3


def _mark_via_queue(vault: Path, rel, text: str, status: str, until, entry: str) -> int:
    """status (and until) as set-property, the status-log line as an append -- queued with
    gt_write_queue and drained at once by gt_broker (0.17.11), never written here: a script's
    write is invisible to the hooks, so writing directly would bypass Core rule 1."""
    wq = _wq()
    relp = str(rel).replace(os.sep, "/")
    writes = [{"path": relp, "op": "set-property", "key": "status", "content": status}]
    _has, cur_until = wq.frontmatter_get(text, "until")
    if until:
        writes.append({"path": relp, "op": "set-property", "key": "until", "content": until})
    elif cur_until is not None and cur_until.strip() not in ("", NO_DATE):
        writes.append({"path": relp, "op": "set-property", "key": "until", "content": NO_DATE})
    writes.append({"path": relp, "op": "append", "section": "Status log", "content": entry})
    try:
        results, note = wq.submit(vault, writes)
    except OSError as exc:
        print("could not queue %s: %s" % (rel, exc), file=sys.stderr)
        return 3
    return _report(results, note, rel, status, until, wq, vault)


def _spool():
    """The vault tools' gt_spool (templates/tools): the ONE slug -> Projects/<path>
    resolver (0.18.1), shared with gt_adr and vault_init."""
    tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import gt_spool
    return gt_spool


def do_list(a) -> int:
    vault = Path(a.vault).expanduser()
    if not vault.is_dir():
        print("no vault at %s" % vault, file=sys.stderr)
        return 3
    if a.project:
        # a bare sub-project slug filters on its parent/child path (0.18.1); unknown or
        # ambiguous is said, not shown as an empty list
        S = _spool()
        try:
            a.project = S.resolve_project(vault, a.project,
            allow_unregistered=True)  # an existing folder, as before; never creates
        except S.ProjectNotFound as exc:
            print("gt_handoff_status: %s" % exc, file=sys.stderr)
            return 3
    rows = all_handoffs(vault, a.project) if a.all else waiting(vault, a.project)
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print("no handoff waiting%s" % ((" in " + a.project) if a.project else ""))
        return 0
    for r in rows:
        print(("[%s] " % r["status"] if a.all else "") + line(r))
    return 0


def _find_handoff(a):
    vault = Path(a.vault).expanduser().resolve()
    f = Path(a.file)
    f = (f if f.is_absolute() else vault / f).resolve()
    if not str(f).startswith(str(vault) + os.sep):
        print("refusing: %s is not inside the vault %s" % (f, vault), file=sys.stderr)
        return vault, None
    if not f.is_file():
        print("no such handoff: %s" % f, file=sys.stderr)
        return vault, None
    return vault, f


def ensure_ids(vault: Path, f: Path, dry_run=False):
    """Back-fill a handoff written before 0.21.0: the `handoff_id` key and a block id on each open
    item, as ONE replace-file through the write queue (base = the bytes read, so a concurrent edit
    is escalated, not overwritten). Idempotent: nothing to assign, nothing written. Never
    renumbers. -> (text after, message or None, rc)."""
    raw = f.read_bytes()
    text = raw.decode("utf-8")
    new, assigned, added = assign_ids(text, new_handoff_id(vault))
    if new == text:
        return text, None, 0
    rel = f.relative_to(vault).as_posix()
    msg = "assigned ids to %s: %s%s" % (
        rel, ("handoff_id %s" % added) if added else "no new handoff_id",
        ("; block ids %s" % ", ".join("^" + b for b in assigned)) if assigned else "")
    if dry_run:
        return text, "would have " + msg, 0
    rc = _mark_whole_file_quiet(vault, rel, raw, new)
    if rc:
        return text, "could not assign ids to %s (queued, not applied)" % rel, rc
    return f.read_text(encoding="utf-8"), msg, 0


def _mark_whole_file_quiet(vault: Path, rel, raw: bytes, new: str) -> int:
    import hashlib as _h                                          # noqa: PLC0415
    wq = _wq()
    req = wq.build(vault, rel, "replace-file", new, None, wq.session_id(None), "session", None)
    req["base_sha256"] = _h.sha256(raw).hexdigest()
    req["target_existed"] = True
    why = wq.validate(req, vault)
    if why:
        print("refusing to assign ids: %s" % why, file=sys.stderr)
        return 3
    try:
        wq.deposit(vault, req)
    except OSError as exc:
        print("could not queue %s: %s" % (rel, exc), file=sys.stderr)
        return 3
    rows, _note = wq.drain_now(vault)
    row = rows.get(req["id"]) or {"decision": "queued"}
    return 0 if row["decision"] in ("apply", "deduplicate") else 3


def do_items(a) -> int:
    vault, f = _find_handoff(a)
    if f is None:
        return 3
    text, msg, rc = ensure_ids(vault, f, dry_run=getattr(a, "dry_run", False))
    if msg:
        print(msg, file=sys.stderr)
    if rc:
        return rc
    rows = item_states(text, f.stem)
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print("no open items in %s" % f.relative_to(vault).as_posix())
    for r in rows:
        if r.get("farmed") and r["settled"]:
            r["detail"] = "%s (%s)" % (r["detail"], r["farmed"]["label"])
        print("%d. [%s] %s -- %s" % (r["n"], r["full_id"], r["text"], (
            "%s%s%s" % (r["resolution"], "" if r["settled"] else " (not settled)",
                        (": " + r["detail"]) if r["detail"] else "")) if r["resolution"]
            else "UNRESOLVED"))
    return 0


def _pick(rows, key: str):
    key = key.strip()
    link = re.match(r"^\[\[.*#\^?(h\d+)\]\]$", key)
    if link:
        key = link.group(1)
    for r in rows:
        if key in (r["id"], "^" + r["id"], r["full_id"], "#" + r["id"]):
            return [r]
        if "#" in key and key.endswith("#" + r["id"]) and r["full_id"] == key:
            return [r]
    if key.isdigit():
        return [r for r in rows if r["n"] == int(key)]
    exact = [r for r in rows if r["text"] == " ".join(key.split())]
    return exact or [r for r in rows if key.lower() in r["text"].lower()]


def _tools_task(vault: Path):
    return vault / "Projects" / "golden-thread" / "tools" / "gt_task.py"


def _create_task(vault: Path, slug: str, handoff: Path, row: dict):
    """The existing task creation (gt_task.py add), so the item becomes a first-class task. The
    text cites the item's full id and its wikilink, and names the handoff by STEM, not file name:
    a task that names the file counts as an open task citing it and would keep the close refused,
    when the point is that the task now carries the item. -> (task id | None, error)."""
    tool = _tools_task(vault)
    if not tool.is_file():
        return None, "no gt_task.py in the vault's tools (%s)" % tool
    cite = row["full_id"] + ((" " + row["link"]) if row.get("link") else "")
    body = "%s -- from handoff %s (%s)" % (row["text"], handoff.stem, cite)
    p = subprocess.run([sys.executable, str(tool), "add", body, "--vault", str(vault),
                        "--project", slug, "--p", "1", "--waiting", "user"],
                       capture_output=True, text=True)
    m = re.search(r"^added (\S+)", p.stdout, re.M)
    if p.returncode != 0 or not m:
        return None, (p.stderr.strip().splitlines() or [p.stdout.strip() or "gt_task add failed"])[-1]
    return m.group(1), None


def _farm_merge(a, prev: dict):
    """-> (fields, error): the flags given, over what the item's previous farmed entry recorded (so
    `--outcome accepted --validation V` is enough once the engine and packet are on file). The engine
    is a farm/LOTR row NAME; provider, model and connection are separate fields."""
    d = dict(prev)
    for k in FARM_FIELDS:
        v = getattr(a, k, None)
        if v:
            d[k] = v
    d.setdefault("kind", FARM_DEFAULT_KIND)
    d.setdefault("outcome", "pending")
    for flag in ("engine", "packet"):
        if not d.get(flag):
            return d, "--as farmed needs --%s (the farm/LOTR row it went to, and the packet id)" % flag
    if not valid_kind(d["kind"]):
        return d, "--kind %r is not one of %s" % (d["kind"], ", ".join(sorted(KINDS)))
    if d["outcome"] not in OUTCOMES:
        return d, "--outcome %r is not one of %s" % (d["outcome"], ", ".join(OUTCOMES))
    if d["outcome"] == "accepted" and not d.get("validation"):
        return d, ("--outcome accepted needs --validation: the gt-validate / gt:verify record that "
                   "checked the answer (a farmed answer is never accepted unchecked)")
    bad = [k for k in FARM_FIELDS if d.get(k) and re.search(r"\s", d[k])]
    if bad:
        return d, "%s must be one word (no spaces): a row name, an id" % ", ".join("--" + k for k in bad)
    return d, None


def farmed_waiting(vault: Path, project: str | None = None):
    """-> {engine: n}: farmed items still waiting for their answer (outcome pending) in any handoff
    that is not handled. A rejected answer is not waiting; it needs the person."""
    out: dict = {}
    for r in all_handoffs(vault, project):
        if r["status"] in ("handled", "history"):
            continue
        try:
            text = (vault / r["path"]).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for row in item_states(text):
            f = row.get("farmed")
            if f and not row["settled"] and f.get("outcome") == "pending":
                out[f.get("engine", "?")] = out.get(f.get("engine", "?"), 0) + 1
    return out


def farmed_text(waiting: dict) -> str:
    """The ONE short line for a farmed-waiting count: `2 items farmed to gemini-row, waiting`."""
    n = sum(waiting.values())
    if not n:
        return ""
    to = next(iter(waiting)) if len(waiting) == 1 else "%d engines" % len(waiting)
    return "%d item%s farmed to %s, waiting" % (n, "" if n == 1 else "s", to)


def farmed_line(vault: Path) -> str:
    return farmed_text(farmed_waiting(Path(vault)))


def do_farmed(a) -> int:
    w = farmed_waiting(Path(a.vault).expanduser().resolve())
    if a.json:
        print(json.dumps(w))
    elif w:
        print(farmed_text(w))
    return 0


def do_resolve(a) -> int:
    vault, f = _find_handoff(a)
    if f is None:
        return 3
    spec = RESOLUTIONS.get(a.resolution)
    if spec is None:
        print("unknown resolution %r (one of %s)" % (a.resolution, ", ".join(sorted(RESOLUTIONS))),
              file=sys.stderr)
        return 2
    reason = " ".join((a.reason or "").split())
    if spec["needs"] == "reason" and not reason:
        print("--as %s needs --reason: a drop is a decision and its reason is recorded"
              % a.resolution, file=sys.stderr)
        return 2
    if bool(a.item) == bool(a.all):
        print("name the item with --item (id, number or text), or --all", file=sys.stderr)
        return 2
    text, msg, rc = ensure_ids(vault, f, dry_run=a.dry_run)
    if msg:
        print(msg, file=sys.stderr)
    if rc:
        return rc
    rows = item_states(text, f.stem)
    chosen = [r for r in rows if not r["settled"]] if a.all else _pick(rows, a.item)
    if len(chosen) != 1 and not a.all:
        print("%s item matches %r -- use the id from `items`" % (
            "no" if not chosen else "more than one", a.item), file=sys.stderr)
        return 3
    if not chosen:
        print("nothing to resolve in %s" % f.relative_to(vault).as_posix())
        return 0
    rel = f.relative_to(vault).as_posix()
    parts = rel.split("/")
    slug = "/".join(parts[1:parts.index("handoff")]) if "handoff" in parts else (
        parts[1] if rel.startswith("Projects/") else "")
    stamp = today().isoformat()
    writes, labels = [], {}
    for r in chosen:
        detail = reason
        if spec["needs"] == "farm":
            prev = parse_farmed(r["detail"]) if r["resolution"] == "farmed" else {}
            merged, err = _farm_merge(a, prev)
            if err:
                print("[%s] %s" % (r["full_id"], err), file=sys.stderr)
                return 2
            detail = format_farmed(merged)
            if merged["outcome"] == "accepted":
                labels[r["full_id"]] = farm_label(merged)
        if spec["needs"] == "task":
            if a.dry_run:
                tid = a.task or "(new task)"
            elif a.task:
                tid = a.task
            else:
                tid, err = _create_task(vault, slug, f, r)
                if err:
                    print("could not create the task for [%s]: %s" % (r["full_id"], err),
                          file=sys.stderr)
                    return 3
            detail = ("%s %s" % (tid, reason)).strip()
        if r.get("link"):
            detail = ("%s %s" % (detail, r["link"])).strip()
        line = "- [%s] %s (%s)%s" % (r["full_id"], a.resolution, stamp,
                                     (": " + detail) if detail else "")
        writes.append({"path": rel, "op": "append", "section": RES_SECTION, "content": line})
        if a.dry_run:
            print("would record: %s  (%s)" % (line, r["text"]))
    if a.dry_run:
        return 0
    try:
        results, note = _wq().submit(vault, writes)
    except OSError as exc:
        print("could not queue %s: %s" % (rel, exc), file=sys.stderr)
        return 3
    bad = [x for x in results if x["decision"] not in ("apply", "deduplicate")]
    if bad:
        print("NOT recorded: %s -- %s" % (rel, note or bad[0].get("reason") or bad[0]["decision"]),
              file=sys.stderr)
        return 3
    for r in chosen:
        print("resolved [%s] %s as %s%s" % (r["full_id"], r["text"], a.resolution,
                                            (" -- " + labels[r["full_id"]]) if r["full_id"] in labels else ""))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_handoff_status.py",
                                 description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("list", help="handoffs waiting on someone (open, or deferral ended)")
    l.add_argument("--vault", required=True)
    l.add_argument("--project", help="one project slug (sub-projects as parent/child)")
    l.add_argument("--all", action="store_true", help="every handoff, whatever its state")
    l.add_argument("--json", action="store_true")
    l.add_argument("--dry-run", action="store_true", help="accepted for symmetry; list writes nothing")
    m = sub.add_parser("mark", help="set a handoff's status")
    m.add_argument("file", help="the handoff, relative to the vault or absolute")
    m.add_argument("--vault", required=True)
    m.add_argument("--status", required=True, choices=STATES)
    m.add_argument("--until", help="YYYY-MM-DD, required with --status deferred")
    m.add_argument("--reason", help="one line, recorded in the handoff's status log")
    m.add_argument("--dry-run", action="store_true")
    it = sub.add_parser("items", help="a handoff's open items and how each was resolved")
    it.add_argument("file")
    it.add_argument("--vault", required=True)
    it.add_argument("--json", action="store_true")
    it.add_argument("--dry-run", action="store_true", help="accepted for symmetry; items writes nothing")
    r = sub.add_parser("resolve", help="record how an open item was dealt with")
    r.add_argument("file", help="the handoff, relative to the vault or absolute")
    r.add_argument("--vault", required=True)
    r.add_argument("--item", help="the item's id, its number in `items`, or its text")
    r.add_argument("--all", action="store_true", help="every item not yet settled")
    r.add_argument("--as", dest="resolution", required=True, choices=sorted(RESOLUTIONS))
    r.add_argument("--reason", help="why (required for dropped); recorded in the handoff")
    r.add_argument("--task", help="with --as task: an existing task's ID (default: create one)")
    for flag, help_ in (("engine", "with --as farmed: the farm/LOTR row it went to"),
                        ("provider", "with --as farmed: the provider (a separate field)"),
                        ("model", "with --as farmed: the model (a separate field)"),
                        ("connection", "with --as farmed: the LOTR connection, if any"),
                        ("kind", "with --as farmed: what was handed off (one of %s)" % ", ".join(KINDS)),
                        ("packet", "with --as farmed: the packet id"),
                        ("outcome", "with --as farmed: %s (default pending)" % " | ".join(OUTCOMES)),
                        ("validation", "with --outcome accepted: the gt-validate record that checked it")):
        r.add_argument("--" + flag, help=help_)
    r.add_argument("--dry-run", action="store_true")
    fm = sub.add_parser("farmed", help="one line: items farmed to an engine and waiting (nothing if none)")
    fm.add_argument("--vault", required=True)
    fm.add_argument("--json", action="store_true")
    fm.add_argument("--dry-run", action="store_true", help="accepted for symmetry; farmed writes nothing")
    a = ap.parse_args(argv)
    return {"list": do_list, "mark": do_mark, "items": do_items,
            "resolve": do_resolve, "farmed": do_farmed}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
