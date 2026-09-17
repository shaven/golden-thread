#!/usr/bin/env python3
"""`log.md` as a generated file: sessions spool, the merge renders.

    gt_log.py add "2026-09-11 13:40 CDT [work] slug — what happened"
    gt_log.py add "<line>" --event promote --item Knowledge/x.md --from P --to Knowledge/x.md
              --level-from 3 --level-to 4 --project slug     # also one structured event
    gt_log.py merge            # render log.md from the spool
    gt_log.py status           # what is spooled, and by whom
    gt_log.py migrate          # one-time: freeze today's log.md as the baseline

Why: see gt_spool.py. `add` writes only this session's own file, so two sessions can
never overwrite each other and `git add` scopes to your own work by construction
rather than by discipline.

## The format is load-bearing -- do not reflow it

`gt_closeout.py` PARSES `log.md` to compute how many days since each project was last
worked:

    ^(\\d{4}-\\d{2}-\\d{2})(?:\\s+[^\\s\\[]+){0,2}\\s+\\[work\\]\\s+(.+?)(?:\\s+(?:—|–|--?)\\s|\\s*$)

It matches the exact slug, comma-separated when work spanned several projects. So the
merge renders lines VERBATIM: it orders them and writes the banner, and changes not a
character of what a session wrote. A merge that tidied whitespace would silently break
project closeout, which is the kind of breakage nobody attributes to a log merge.
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gt_spool as S  # noqa: E402

KIND = "log"


def target(vault):
    return Path(vault) / "log.md"


def render(vault):
    body = "\n".join(S.lines(vault, KIND))
    return S.BANNER + "\n\n" + body + ("\n" if body else "")


STAMP_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?(?:\s+[A-Z]{2,5})?\s+")


def _event(a, v):
    """The `--event` half of `add`: one structured event through gt_events.safe_emit.

    Runs AFTER the log line is spooled and can never fail `add` -- the log line is the
    operation; the event describes it. The note defaults to the log line minus its
    date stamp, so the two records say the same thing.
    """
    try:
        import gt_events as E
    except Exception as exc:
        print("gt_log: --event NOT recorded (gt_events.py unavailable: %s)" % exc,
              file=sys.stderr)
        return
    note = a.note if a.note is not None else E.clip(STAMP_PREFIX.sub("", a.text, count=1))
    levels = {}
    for key in ("level_from", "level_to"):
        raw = getattr(a, key)
        if raw is not None:
            try:
                levels[key] = int(raw)
            except ValueError:
                levels[key] = raw          # validation refuses it, on stderr
    ev = E.safe_emit(v, a.event, a.item, frm=a.from_, to=a.to, project=a.project,
                     actor=a.actor, note=note, sid=a.id, merge=not a.no_merge,
                     dry_run=a.dry_run, **levels)
    if ev is not None and a.dry_run:
        print("dry run: would also spool the event\n  %s" % E.serialise(ev))


def cmd_add(a):
    v = S.vault_root(a.vault)
    if a.event and not a.item:
        print("REFUSED: --event needs --item (a vault-relative path or task id); "
              "nothing written", file=sys.stderr)
        return 2
    if a.dry_run:
        print("dry run: would append to %s and regenerate log.md\n  %s"
              % ((S.spool_dir(v, KIND) / ("%s.md" % S.session_id(a.id))).relative_to(v),
                 a.text))
        if a.event:
            _event(a, v)
        return 0
    p = S.append(v, KIND, a.text, sid=a.id)
    if not a.no_merge:
        cmd_merge(argparse.Namespace(vault=str(v), quiet=True, dry_run=False))
    print("spooled -> %s" % p.relative_to(v))
    if a.event:
        _event(a, v)
    return 0


def cmd_merge(a):
    v = S.vault_root(a.vault)
    t = target(v)
    # Refuse rather than clobber: a log.md with no baseline and no banner is the
    # pre-migration file, and rendering over it would destroy every historical line.
    if t.exists() and not S.is_generated(t) \
            and not (S.spool_dir(v, KIND) / S.BASELINE).exists():
        print("REFUSED: %s is not generated and no baseline exists — run `migrate` first"
              % t.name, file=sys.stderr)
        return 2
    if getattr(a, "dry_run", False):
        produced = render(v)
        current = S.read_owner(t) if t.exists() else ""
        print("dry run: log.md would be %s"
              % ("unchanged" if produced == current else "rewritten from the spool"))
        found = S.hand_written(t, produced)
        if found:
            print("dry run: %d hand-written line(s) would be kept in the spool first"
                  % len(found))
        return 0
    # THE RESCUE IS A SNAPSHOT, SO THE WRITE MUST BE A PRECONDITION.
    #
    # hand_written() reads log.md and decides which lines to rescue; write_if_changed() then
    # read it AGAIN and replaced it. A human save landing between those two reads was lost --
    # not because the rescue failed, but because it had already been computed against bytes
    # that no longer existed. The vault is on Dropbox with several sessions open, so "between
    # two reads" is an ordinary amount of time, not a theoretical one.
    #
    # gt 0.16.4 gave write_if_changed an `expect` precondition; passing it is what turns that
    # from an available mechanism into an applied one. Digest BEFORE hand_written(), so the
    # precondition covers the decision and not merely the write. capture_hand_written() writes
    # to the SPOOL, never to the target, so the digest stays valid across it.
    for _attempt in range(3):
        before = S.current_digest(t)
        produced = render(v)
        found = S.hand_written(t, produced)
        if found:
            kept = S.capture_hand_written(v, KIND, found)
            print("log.md: %d hand-written line(s) kept -> %s (they were not in the spool, "
                  "and rendering would have deleted them)" % (len(found), kept.relative_to(v)))
            produced = render(v)
        changed = S.write_if_changed(t, produced, expect=before)
        if changed is not S.STALE:
            break
    else:
        # Bounded, and it fails LOUDLY. Retrying forever against a file someone is actively
        # editing would hang a merge with no output; reporting success would lose their work.
        print("REFUSED: %s changed underneath this merge three times — nothing was written.\n"
              "  Something else is writing it right now. Re-run when it settles."
              % t.name, file=sys.stderr)
        return 3
    if not getattr(a, "quiet", False):
        print("log.md %s" % ("updated" if changed else "unchanged (idempotent)"))
    return 0


def cmd_status(a):
    v = S.vault_root(a.vault)
    d = S.spool_dir(v, KIND)
    if not d.is_dir():
        print("no spool yet")
        return 0
    me = S.session_id(a.id)
    for f in sorted(d.iterdir()):
        if f.suffix != ".md":
            continue
        n = len([x for x in f.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()])
        tag = "  <- this session" if f.stem == me else ""
        print("  %-44s %3d entr%s%s" % (f.name, n, "y" if n == 1 else "ies", tag))
    return 0


def cmd_migrate(a):
    """Freeze the current log.md as the baseline, then prove the merge reproduces it.

    The byte-identical round trip is the whole gate. If rendering the baseline does
    not return exactly the file we started from, the ordering or the banner is wrong,
    and the right outcome is to leave the vault untouched and say so.
    """
    v = S.vault_root(a.vault)
    t = target(v)
    base = S.spool_dir(v, KIND) / S.BASELINE
    if base.exists():
        print("already migrated: %s exists" % base.relative_to(v))
        return 0
    if not t.exists():
        print("no log.md to migrate")
        return 0
    # Refuse on a file we already generated. Freezing generated output as a new
    # baseline duplicates every line -- found in smoke testing, where deleting the
    # spool and re-migrating silently doubled the log. `migrate` is a ONE-TIME step;
    # the safe response to "already generated" is to stop, not to guess.
    if S.is_generated(t):
        print("REFUSED: %s is already generated — migrate is one-time. Restore the "
              "pre-migration file first if you are rebuilding the spool." % t.name,
              file=sys.stderr)
        return 2
    raw = t.read_bytes()
    original = S.read_owner(t)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_bytes(raw)
    produced = render(v)
    # Compare the CONTENT, ignoring the banner we are adding on purpose.
    note = S.roundtrip_note(produced[len(S.BANNER) + 2:], original)
    # The gate is on BYTES: the frozen baseline must be the file, exactly.
    if base.read_bytes() != raw:
        note = None
    if note is None:
        base.unlink()
        print("REFUSED: merge would not reproduce log.md byte-for-byte; nothing changed",
              file=sys.stderr)
        return 3
    if a.dry_run:
        base.unlink()
        print("dry run: would freeze %d line(s) as %s and generate log.md%s"
              % (len(original.splitlines()), base.name, note))
        return 0
    S.write_if_changed(t, produced)
    print("migrated: %d line(s) frozen as %s; log.md is now generated%s"
          % (len(original.splitlines()), base.name, note))
    return 0


def main(argv=None):
    # Declared once, attached to the top level AND to every subcommand, so --vault and
    # --dry-run work on either side of the verb. A flag that parses only before the
    # subcommand is one people will believe they passed -- the exact failure
    # core_explicit_vault_target exists to prevent.
    common = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS matters: without it, a subparser built from `parents` RESETS
    # these to their defaults when the flag appears before the verb, so
    # `--id alpha status` silently became id=None. Found by test_gt_log 2026-09-11.
    common.add_argument("--vault", default=argparse.SUPPRESS)
    common.add_argument("--id", default=argparse.SUPPRESS,
                        help="session id override (testing)")
    common.add_argument("--dry-run", "-n", action="store_true",
                        default=argparse.SUPPRESS,
                        help="say what would change; write nothing")
    p = argparse.ArgumentParser(description="log.md spool and merge", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", parents=[common])
    a.add_argument("text"); a.add_argument("--no-merge", action="store_true")
    # Optional structured event (gt_events.py schema v1). Plain `add` is unchanged.
    a.add_argument("--event", default=None, metavar="KIND",
                   help="also emit one gt_events.py event of this kind")
    a.add_argument("--item", default=None, help="event item: vault-relative path or task id")
    a.add_argument("--from", dest="from_", default=None)
    a.add_argument("--to", default=None)
    a.add_argument("--level-from", default=None)
    a.add_argument("--level-to", default=None)
    a.add_argument("--project", default=None)
    a.add_argument("--actor", default="claude")
    a.add_argument("--note", default=None, help="event note (default: the log line)")
    sub.add_parser("merge", parents=[common])
    sub.add_parser("status", parents=[common])
    sub.add_parser("migrate", parents=[common])
    args = p.parse_args(argv)
    for key, default in (("vault", None), ("id", None), ("dry_run", False)):
        if not hasattr(args, key):
            setattr(args, key, default)
    return {"add": cmd_add, "merge": cmd_merge, "status": cmd_status,
            "migrate": cmd_migrate}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
