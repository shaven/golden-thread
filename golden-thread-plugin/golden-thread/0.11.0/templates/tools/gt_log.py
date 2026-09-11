#!/usr/bin/env python3
"""`log.md` as a generated file: sessions spool, the merge renders.

    gt_log.py add "2026-09-11 13:40 CDT [work] slug — what happened"
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


def cmd_add(a):
    v = S.vault_root(a.vault)
    p = S.append(v, KIND, a.text, sid=a.id)
    if not a.no_merge:
        cmd_merge(argparse.Namespace(vault=str(v), quiet=True))
    print("spooled -> %s" % p.relative_to(v))
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
    changed = S.write_if_changed(t, render(v))
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
    original = t.read_text(encoding="utf-8", errors="replace")
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(original, encoding="utf-8")
    produced = render(v)
    # Compare the CONTENT, ignoring the banner we are adding on purpose.
    if produced[len(S.BANNER) + 2:].rstrip("\n") != original.rstrip("\n"):
        base.unlink()
        print("REFUSED: merge would not reproduce log.md byte-for-byte; nothing changed",
              file=sys.stderr)
        return 3
    S.write_if_changed(t, produced)
    print("migrated: %d line(s) frozen as %s; log.md is now generated"
          % (len(original.splitlines()), base.name))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="log.md spool and merge")
    p.add_argument("--vault")
    p.add_argument("--id", help="session id override (testing)")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("text"); a.add_argument("--no-merge", action="store_true")
    sub.add_parser("merge")
    sub.add_parser("status")
    sub.add_parser("migrate")
    args = p.parse_args(argv)
    return {"add": cmd_add, "merge": cmd_merge, "status": cmd_status,
            "migrate": cmd_migrate}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
