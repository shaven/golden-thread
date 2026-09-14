#!/usr/bin/env python3
"""A structured event stream: one JSON line per movement of knowledge in the vault.

    gt_events.py emit --kind promote --item Knowledge/x.md --level-from 3 --level-to 4
    gt_events.py merge                 # render events.jsonl from the spool
    gt_events.py validate [file]       # report bad lines; exit 1 if any
    gt_events.py list [--kind K] [--project P] [--since ISO] [--json]

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
import json
import re
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
    args = p.parse_args(argv)
    for key, default in (("vault", None), ("id", None), ("dry_run", False)):
        if not hasattr(args, key):
            setattr(args, key, default)
    return {"emit": cmd_emit, "merge": cmd_merge, "validate": cmd_validate,
            "list": cmd_list}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
