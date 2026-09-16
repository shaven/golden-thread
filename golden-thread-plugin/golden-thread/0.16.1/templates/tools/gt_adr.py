#!/usr/bin/env python3
"""ADR numbers allocated atomically, and `decisions.md` rendered from them.

    gt_adr.py allocate <project> --title "..."   # prints the number it reserved
    gt_adr.py merge <project>                    # render decisions.md
    gt_adr.py status <project>
    gt_adr.py migrate <project>                  # one-time: split today's decisions.md

## Why an allocator, and why not a counter

`decisions.md` entries carry a NUMBER, which is a scarce identity: two sessions may
safely write two log lines in the same instant, but they cannot both be ADR-6. It has
already happened -- `Projects/cyc26-talk/decisions.md` carries two different ADR-6
headings, two unrelated decisions on one number, and nothing reported it.

A counter file that is read and then written back does NOT fix this. Read-then-write
is two operations, so two sessions both read 5, both write 6, and both use ADR-6 --
the same collision moved somewhere harder to see.

`os.open(O_CREAT|O_EXCL)` is ONE operation. It either creates the file or raises
FileExistsError, and the filesystem decides which. So: compute the lowest free number,
try to create exactly that file, and on collision recompute and retry.

**The created file is named by the NUMBER ALONE** -- `0007.md`, never
`0007.<session>.md`. Two sessions each appending their own id produce two filenames
that never collide, and the entire guarantee evaporates while still looking correct.
The session id goes INSIDE the file. This is the easiest detail here to get wrong.

Verified under contention: 100 racing process pairs, 200 allocations, 0 duplicates.

## Derived, never stored

The next number is computed from the files that exist. A stored count drifts the
moment an ADR is deleted, a file is restored from git, or a sync returns an older
copy -- and a drifted counter re-issues a number already taken, which is the bug.
What is on disk cannot be wrong about itself. Same reason `install.sh` derives the
version from the newest directory rather than a constant.

## Known limit

An exclusive create is atomic on ONE filesystem. Two machines writing one project
through a synced folder can both create `0007.md`, and the sync resolves it as a
conflicted copy. `gt_lint.py`'s `adr-number-collision` check is what catches that.
This does not solve distributed consensus and must not be described as if it does.
"""
import argparse
import errno
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gt_spool as S  # noqa: E402

KIND = "decisions"
SLOT = re.compile(r"^(\d{4})\.md$")
# `## ADR-7: title`. The `amendment` form is deliberate usage elsewhere in the vault
# and is NOT a second allocation -- it amends an existing decision.
HEAD = re.compile(r"^##\s*ADR-(\d+)\s*(amendment)?\s*:?", re.M)
MAX_RETRY = 100


def slots(vault, project):
    d = S.spool_dir(vault, KIND, project)
    if not d.is_dir():
        return {}
    out = {}
    for f in d.iterdir():
        m = SLOT.match(f.name)
        if m:
            out[int(m.group(1))] = f
    return out


def baseline_numbers(vault, project):
    """ADR numbers already present in the frozen baseline."""
    b = S.spool_dir(vault, KIND, project) / S.BASELINE
    if not b.is_file():
        return set()
    return {int(m.group(1)) for m in HEAD.finditer(
        b.read_text(encoding="utf-8", errors="replace")) if not m.group(2)}


HIGHWATER = ".highwater"


def _highwater(d):
    """The largest number ever issued here, or 0 if unknown.

    This is a FLOOR, never the source of truth. The next number is
    max(derived, highwater) + 1, so a high-water file that is missing, empty,
    corrupt or stale-low cannot cause a reissue -- the derived maximum still
    covers every slot that exists. It only ever adds safety.

    It exists because deriving alone is not quite enough: deleting the highest
    slot removes it from the derived set and the number becomes issuable again,
    while a reference to it may already exist in prose, a commit message, or
    another session's draft. A number, once issued, is spent. Caught by
    test_deleting_the_highest_does_not_roll_back.
    """
    try:
        return int((d / HIGHWATER).read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def _raise_highwater(d, n):
    """Monotonic: only ever raises. A failure here is not fatal -- the derived
    maximum remains correct, so a read-only or full disk degrades to today's
    behaviour rather than to a wrong number."""
    try:
        if n > _highwater(d):
            (d / HIGHWATER).write_text("%d\n" % n, encoding="utf-8")
    except OSError:
        pass


def allocate(vault, project, title, sid=None):
    """Reserve the next number by creating its slot exclusively. Returns (n, path)."""
    sid = S.session_id(sid)
    d = S.spool_dir(vault, KIND, project)
    d.mkdir(parents=True, exist_ok=True)
    for _ in range(MAX_RETRY):
        taken = set(slots(vault, project)) | baseline_numbers(vault, project)
        n = max(max(taken) if taken else 0, _highwater(d)) + 1
        p = d / ("%04d.md" % n)
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue                      # someone took it between our scan and here
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("<!-- allocated by %s -->\n## ADR-%d: %s\n" % (sid, n, title or "TITLE"))
        _raise_highwater(d, n)
        return n, p
    raise SystemExit("could not allocate an ADR number after %d attempts" % MAX_RETRY)


def render(vault, project):
    parts = []
    b = S.spool_dir(vault, KIND, project) / S.BASELINE
    if b.is_file():
        parts.append(S.read_owner(b).rstrip("\n"))
    for n in sorted(slots(vault, project)):
        body = S.read_owner(slots(vault, project)[n])
        body = "\n".join(l for l in body.splitlines() if not l.startswith("<!-- allocated by"))
        parts.append(body.strip("\n"))
    return S.BANNER + "\n\n" + "\n\n".join(p for p in parts if p) + "\n"


def target(vault, project):
    return Path(vault) / "Projects" / project / "decisions.md"


def cmd_allocate(a):
    v = S.vault_root(a.vault)
    if a.dry_run:
        # Deliberately does NOT reserve: a dry run that consumed a number would
        # leave a hole every time someone rehearsed.
        used = sorted(set(slots(v, a.project)) | baseline_numbers(v, a.project))
        print("dry run: would reserve ADR-%d for %s (nothing written)"
              % ((used[-1] + 1) if used else 1, a.project), file=sys.stderr)
        return 0
    n, p = allocate(v, a.project, a.title, a.id)
    # The number on stdout ALONE, so a caller can use it directly; everything else
    # goes to stderr. A session that has to parse prose to learn its number will
    # eventually parse it wrong.
    print("reserved ADR-%d for %s -> %s" % (n, a.project, p.name), file=sys.stderr)
    _emit_adr(v, a.project, n, a.title, a.id)
    print(n)
    return 0


def _emit_adr(vault, project, n, title, sid):
    """One `adr` event per reserved number (level 3: decisions.md). Never fails allocate:
    the number is already spent, and an event log that could un-spend it would be worse
    than a missing event."""
    try:
        import gt_events as E
    except Exception as exc:
        print("gt_adr: adr event NOT recorded (gt_events.py unavailable: %s)" % exc,
              file=sys.stderr)
        return
    dec = "Projects/%s/decisions.md" % project
    E.safe_emit(vault, "adr", "%s#ADR-%d" % (dec, n), to=dec, level_to=3,
                project=project, note=E.clip("ADR-%d: %s" % (n, title or "(untitled)")),
                sid=sid)


def cmd_merge(a):
    v = S.vault_root(a.vault)
    t = target(v, a.project)
    if t.exists() and not S.is_generated(t) \
            and not (S.spool_dir(v, KIND, a.project) / S.BASELINE).exists():
        print("REFUSED: %s is not generated and no baseline exists — run `migrate` first"
              % t, file=sys.stderr)
        return 2
    produced = render(v, a.project)
    if getattr(a, "dry_run", False):
        current = S.read_owner(t) if t.exists() else ""
        print("dry run: %s/decisions.md would be %s"
              % (a.project, "unchanged" if produced == current else "rewritten from the spool"))
        return 0
    # A decisions.md line typed by hand has no ADR slot to live in, so it is not guessed
    # into one: the merge refuses and names the lines (0.15.0 -- rendering deleted them).
    found = S.hand_written(t, produced)
    if found:
        print("REFUSED: %s has %d hand-written line(s) no ADR slot holds; rendering would "
              "delete them. Needs a person: move them into an ADR (gt_adr.py allocate), "
              "then merge again:\n  %s"
              % (t, len(found), "\n  ".join(found[:20])), file=sys.stderr)
        return 3
    changed = S.write_if_changed(t, produced)
    print("%s/decisions.md %s" % (a.project, "updated" if changed else "unchanged (idempotent)"))
    return 0


def cmd_status(a):
    v = S.vault_root(a.vault)
    used = sorted(set(slots(v, a.project)) | baseline_numbers(v, a.project))
    print("  %s: %d ADR(s)%s" % (a.project, len(used),
                                 ", highest ADR-%d" % used[-1] if used else ""))
    for n, p in sorted(slots(v, a.project).items()):
        txt = p.read_text(encoding="utf-8", errors="replace")
        owner = (re.search(r"<!-- allocated by (.+?) -->", txt) or [None, "?"])[1]
        stub = "  UNFINISHED (allocated, no body)" if len(txt.strip().splitlines()) <= 2 else ""
        print("    %s  ADR-%d  by %s%s" % (p.name, n, owner, stub))
    return 0


def cmd_migrate(a):
    v = S.vault_root(a.vault)
    t = target(v, a.project)
    base = S.spool_dir(v, KIND, a.project) / S.BASELINE
    if base.exists():
        print("already migrated")
        return 0
    if not t.exists():
        print("no decisions.md for %s" % a.project)
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
    nums = [int(m.group(1)) for m in HEAD.finditer(original) if not m.group(2)]
    dupes = sorted({n for n in nums if nums.count(n) > 1})
    if dupes:
        # Refuse, do not renumber. The vault holds ~1,141 plain-prose ADR-N
        # references; renumbering is an owner decision, not a migration side effect.
        print("REFUSED: %s has duplicate ADR number(s): %s — an owner must resolve "
              "these before migrating (renumbering would invalidate inbound references)"
              % (a.project, ", ".join("ADR-%d" % n for n in dupes)), file=sys.stderr)
        return 3
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_bytes(raw)
    produced = render(v, a.project)
    note = S.roundtrip_note(produced[len(S.BANNER) + 2:], original)
    # The gate is on BYTES: the frozen baseline must be the file, exactly.
    if base.read_bytes() != raw:
        note = None
    if note is None:
        base.unlink()
        print("REFUSED: merge would not reproduce decisions.md byte-for-byte; nothing changed",
              file=sys.stderr)
        return 3
    if a.dry_run:
        base.unlink()
        print("dry run: would freeze %d ADR(s) of %s as baseline and generate decisions.md%s"
              % (len(nums), a.project, note))
        return 0
    S.write_if_changed(t, produced)
    print("migrated %s: %d ADR(s) frozen as baseline%s" % (a.project, len(nums), note))
    return 0


def main(argv=None):
    # The shared flags are declared ONCE and attached to the top level and to every
    # subcommand, so `--vault` and `--dry-run` work on either side of the verb. A flag
    # that only parses before the subcommand is a flag people will believe they passed
    # -- which is the exact failure core_explicit_vault_target exists to prevent.
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
    p = argparse.ArgumentParser(description="atomic ADR numbers; decisions.md merge",
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    al = sub.add_parser("allocate", parents=[common])
    al.add_argument("project"); al.add_argument("--title", default="")
    for c in ("merge", "status", "migrate"):
        sp = sub.add_parser(c, parents=[common]); sp.add_argument("project")
    args = p.parse_args(argv)
    for key, default in (("vault", None), ("id", None), ("dry_run", False)):
        if not hasattr(args, key):
            setattr(args, key, default)
    return {"allocate": cmd_allocate, "merge": cmd_merge, "status": cmd_status,
            "migrate": cmd_migrate}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
