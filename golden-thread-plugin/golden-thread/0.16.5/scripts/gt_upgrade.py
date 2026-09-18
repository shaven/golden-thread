#!/usr/bin/env python3
"""Bring an existing vault up to the installed release.

    gt_upgrade.py status --vault <path>          # what is pending, and why
    gt_upgrade.py run --vault <path> --dry-run   # rehearse: report, write nothing
    gt_upgrade.py run --vault <path>             # apply, after backing the vault up

## Why this exists

`install.sh` updates the PLUGIN. Nothing updated a VAULT. On 2026-09-11 adopting the
0.11.0 spool model meant finding the migrations by reading source, hand-merging two
documents, and running a migration across 43 projects by hand -- and the hand-run
rehearsal wrote the live vault, because one tool took no `--vault`. Every part of that
was avoidable and none of it was written down as a procedure.

## The stamp is what makes this possible

A vault records the release its files came from, in
`Projects/golden-thread/.vault-version.json`. Without it there is no way to know which
migrations have run, and -- worse -- a three-way merge of a document the owner has
edited has no BASE, so it degenerates into "overwrite" or "leave alone", both wrong.

A vault with no stamp is not assumed current: it is assumed OLD, and every migration
is offered. Migrations detect their own work (`pending()`), so re-running one that has
already happened is a no-op rather than a duplication.

## The base for a document merge travels with the vault

`Projects/golden-thread/.templates/` holds the exact template text this vault was last
seeded or upgraded from. That is the merge base, and it is a copy INSIDE the vault on
purpose: old release directories are pruned (only current and previous stay on disk),
so a base that lived in the plugin would vanish exactly when an old vault needed it.

## What it refuses to do

  * Run on a vault whose git tree is dirty -- the upgrade must be revertible with one
    `git checkout`, which it is not if it lands on top of someone's uncommitted work.
  * Report success over a migration that REFUSED. A refusal is a decision for a person
    (2026-09-11: a project with two ADR-6 entries): the step is named, the run exits 1,
    and re-running after resolving it picks up where it stopped. It does NOT stop the
    other migrations -- they are independent, and halting them would turn one
    person-sized decision into an upgrade nobody can make progress on. This bullet used
    to open "Continue past a migration that REFUSED", contradicting both the sentence
    after it and `cmd_run`, where every step in the plan runs (corrected 2026-09-18).
  * Apply, unasked, a merge that would remove lines from the owner's document (a base a
    release before 0.14.0 recorded from the owner's own file, or a release dropping a
    section the owner kept). It is held and reported as needing a person, never pending;
    `run --accept-merge <doc>` takes it, `run --record-base <doc>` keeps the document.
  * Merge a document that has no base gt recorded. It is left untouched and reported as
    needing a person, never as a pending step; `run --record-base <doc>` establishes a
    base after review, and install.sh never passes that flag.
  * Guess at a document merge that conflicts. It writes the conflict markers to a
    `.merge-conflict` file beside the document and leaves the document untouched.
  * Redo a conflict it has already written. While that `.merge-conflict` file exists
    for the same base, template and document (hashes in a sidecar under `.templates/`),
    the merge is not pending: it is reported as "conflict awaiting you" and nothing is
    written -- no merge, no stamp, no rewritten conflict file.
"""
import argparse
import datetime
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE.parent / "templates"
STAMP = Path("Projects") / "golden-thread" / ".vault-version.json"
BASE_DIR = Path("Projects") / "golden-thread" / ".templates"
# Documents a vault owns and an owner edits: merged, never overwritten.
MERGED_DOCS = {"PROTOCOL.md": Path("Projects") / "PROTOCOL.md",
               "CONVENTIONS.md": Path("Projects") / "CONVENTIONS.md"}


def release_version():
    """The version of the release this script ships in."""
    try:
        return json.loads((HERE.parent / ".claude-plugin" / "plugin.json")
                          .read_text(encoding="utf-8"))["version"]
    except Exception:
        return HERE.parent.name


def vkey(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except (ValueError, AttributeError):
        return (0,)


def read_stamp(vault):
    try:
        return json.loads((vault / STAMP).read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_stamp(vault, version, applied):
    data = read_stamp(vault)
    hist = data.get("history", [])
    hist.append({"to": version, "at": datetime.datetime.now().astimezone().isoformat(),
                 "applied": applied})
    out = {"gt": version, "updated": hist[-1]["at"], "history": hist[-12:]}
    p = vault / STAMP
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")


def git(vault, *args):
    try:
        p = subprocess.run(["git", "-C", str(vault), *args],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)


def tool(vault, name):
    return vault / "Projects" / "golden-thread" / "tools" / name


# ---- migrations -------------------------------------------------------------
#
# Each is (version, id, what, pending(vault) -> reason or "", run(vault, dry) -> note).
# `pending` reads the vault, never the stamp: what is on disk cannot be wrong about
# itself, and a stamp can be restored from a backup or arrive from another machine.

def _spool_baseline(vault, kind, project=None):
    d = vault / "Projects" / "golden-thread" / "spool" / kind
    if project:
        d = d / project
    return d / "0000-baseline.md"


def pending_log_spool(vault):
    if not (vault / "log.md").is_file():
        return ""
    return "" if _spool_baseline(vault, "log").is_file() else "log.md is not generated"


def run_log_spool(vault, dry):
    t = tool(vault, "gt_log.py")
    if not t.is_file():
        return "SKIPPED: gt_log.py is not in this vault (run install.sh first)"
    cmd = [sys.executable, str(t), "--vault", str(vault), "migrate"]
    if dry:
        cmd.append("--dry-run")
    rc, out = _run(cmd)
    line = out.strip().splitlines()[-1] if out.strip() else ("rc=%s" % rc)
    if rc == 0 and not dry and line.startswith("migrated"):
        # Say what changed for the owner, not just that something did (0.15.0): the file
        # is generated from here on, so a hand edit to it is lost at the next merge.
        line = ("log.md — %s\n    log.md is now GENERATED from spool/log/: add entries with "
                "gt_log.py; a hand edit is overwritten at the next merge" % line)
    return line


def _projects(vault):
    base = vault / "Projects"
    return sorted(set(list(base.glob("*/decisions.md")) + list(base.glob("*/*/decisions.md"))))


def pending_decisions_spool(vault):
    todo = [str(d.parent.relative_to(vault / "Projects")) for d in _projects(vault)
            if not _spool_baseline(vault, "decisions",
                                   str(d.parent.relative_to(vault / "Projects"))).is_file()]
    return ("%d project(s) with an unmigrated decisions.md: %s"
            % (len(todo), ", ".join(todo[:6]) + (" …" if len(todo) > 6 else ""))) if todo else ""


def run_decisions_spool(vault, dry):
    t = tool(vault, "gt_adr.py")
    if not t.is_file():
        return "SKIPPED: gt_adr.py is not in this vault (run install.sh first)"
    done, refused, files = 0, [], []
    for dec in _projects(vault):
        slug = str(dec.parent.relative_to(vault / "Projects"))
        if _spool_baseline(vault, "decisions", slug).is_file():
            continue
        cmd = [sys.executable, str(t), "--vault", str(vault), "migrate", slug]
        if dry:
            cmd.append("--dry-run")
        rc, out = _run(cmd)
        if rc == 0:
            done += 1
            # Every converted file by name, with the tool's own note (e.g. trailing blank
            # lines dropped) -- a bare count hid what changed in which file (0.15.0).
            files.append("Projects/%s/decisions.md — %s"
                         % (slug, out.strip().splitlines()[-1] if out.strip() else "ok"))
        else:
            # A refusal is a decision for a person, not a failure to retry.
            refused.append("%s: %s" % (slug, out.strip().splitlines()[-1] if out.strip() else rc))
    note = "%s %d project(s)" % ("would migrate" if dry else "migrated", done)
    if files:
        note += ":\n    " + "\n    ".join(files)
        if not dry:
            note += ("\n    these decisions.md files are now GENERATED from spool/decisions/: "
                     "add ADRs with gt_adr.py; a hand edit is overwritten at the next merge")
    if refused:
        note += "; %d REFUSED (an owner must resolve these):\n    " % len(refused)
        note += "\n    ".join(refused)
    return note


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _conflict_files(vault, name, rel):
    """-> (the .merge-conflict file beside the document, its hash sidecar)."""
    return (vault / (str(rel) + ".merge-conflict"),
            vault / BASE_DIR / (name + ".merge-conflict.json"))


def _conflict_key(vault, name, rel):
    return {"base": _sha(vault / BASE_DIR / name), "template": _sha(TEMPLATES / name),
            "document": _sha(vault / rel)}


def conflict_awaiting(vault, name, rel):
    """-> the .merge-conflict path when gt already wrote a conflict for EXACTLY this base,
    template and document, else None.

    Without this a conflicting merge stayed pending, so every install re-ran it, re-stamped
    the vault and rewrote the conflict file -- a clean vault made dirty on every install,
    for a conflict that only a person can resolve. Any change to the three inputs (the
    owner resolved it into the document, a new release, a new base) makes it pending again.
    """
    conflict, sidecar = _conflict_files(vault, name, rel)
    if not (conflict.is_file() and sidecar.is_file() and (vault / BASE_DIR / name).is_file()):
        return None
    try:
        recorded = json.loads(sidecar.read_text(encoding="utf-8"))
        return conflict if recorded == _conflict_key(vault, name, rel) else None
    except (OSError, ValueError):
        return None


def merge_text(target, base, shipped):
    """-> (rc, merged text) from git merge-file, written nowhere near the vault.

    rc 0 clean, 1 conflicts (merged text carries the markers), 2 could not run."""
    with tempfile.TemporaryDirectory(prefix="gt-merge-") as d:
        ours = Path(d) / target.name
        shutil.copyfile(target, ours)
        try:
            # Bytes, not text=True: universal newlines turned a CRLF document into LF.
            p = subprocess.run(["git", "merge-file", "-p", str(ours), str(base), str(shipped)],
                               capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return 2, str(exc)
        return ((0 if p.returncode == 0 else 1 if p.returncode > 0 else 2),
                p.stdout.decode("utf-8", errors="surrogateescape"))


def line_changes(cur, merged):
    """-> (lines added, [lines removed]) going from the owner's document to the merge."""
    a, b = cur.splitlines(), merged.splitlines()
    added, removed = 0, []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed.extend(a[i1:i2])
        if op in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def held_merge(vault, name, rel):
    """-> (removed lines, base == ours) when a CLEAN merge of this document would remove
    lines from the owner's file, else None.

    Held, never applied by `run` on its own, and not a pending step (2026-09-14). A release
    before 0.14.0 recorded the owner's own file as the base; with base == ours a clean
    3-way merge IS the template, so the owner's sections and wikilinks went, reported as
    "would merge cleanly (-16 lines)" -- and install.sh applies pending steps unattended.
    A release removing a section the owner still has is the same decision. A person makes
    it: `run --accept-merge <doc>` takes the merge, `run --record-base <doc>` keeps the
    document and makes this release its base."""
    shipped, target, base = TEMPLATES / name, vault / rel, vault / BASE_DIR / name
    if not (shipped.is_file() and target.is_file() and base.is_file()):
        return None
    cur, new, old = (p.read_bytes().decode("utf-8", errors="surrogateescape")
                     for p in (target, shipped, base))
    if cur == new or conflict_awaiting(vault, name, rel):
        return None
    # A base identical to the owner's document (byte for byte) is one a release before
    # 0.14.0 recorded FROM that document. It says nothing about what the owner changed, so
    # no merge against it is trustworthy -- one that only ADDS lines re-inserted a section
    # the owner had deleted. Always held, whatever the merge would add or remove.
    if base.read_bytes() == target.read_bytes():
        rc, merged = merge_text(target, base, shipped)
        return ((line_changes(cur, merged)[1] if rc == 0 else []), True)
    if old == new:
        return None
    rc, merged = merge_text(target, base, shipped)
    if rc != 0:
        return None
    _, removed = line_changes(cur, merged)
    return (removed, old == cur) if removed else None


def pending_doc_merges(vault):
    out = []
    for name, rel in MERGED_DOCS.items():
        shipped = TEMPLATES / name
        target = vault / rel
        if not shipped.is_file() or not target.is_file():
            continue
        new = shipped.read_text(encoding="utf-8")
        if new == target.read_text(encoding="utf-8"):
            continue
        base = vault / BASE_DIR / name
        # Base == shipped: the release has nothing this vault has not already taken, and
        # the remaining difference is the owner's own edit. Reporting that as pending made
        # an edited PROTOCOL.md pending FOREVER -- and since 0.14.0 install.sh applies
        # pending steps, every install would re-stamp a clean vault and leave it dirty.
        if not base.is_file():
            continue            # needs a person, never auto-appliable: see docs_without_base
        if base.read_text(encoding="utf-8") == new:
            continue
        if conflict_awaiting(vault, name, rel):
            continue            # a person's turn: see attention()
        if held_merge(vault, name, rel):
            continue            # would remove the owner's lines: a person's turn, see held_merge
        out.append("%s differs from the shipped template (3-way merge)" % rel)
    return "; ".join(out)


def docs_without_base(vault):
    """-> [(name, rel)] for an owner document that differs from the shipped template and
    has NO merge base gt recorded.

    Such a document is NOT a pending step. Until 0.14.0 `run` recorded the owner's
    CURRENT file as the base, and the next run's 3-way merge (base == ours) then took the
    release's side of every difference -- the owner's edits, silently gone, one install
    later. Since install.sh applies pending steps unattended that would lose work with
    nobody watching, so only a base gt itself wrote (seeded, or after its own clean merge)
    is merged. A base is established only by `run --record-base <doc>`, after a person
    has reviewed the document against the shipped template; the installer never passes it.
    """
    out = []
    for name, rel in MERGED_DOCS.items():
        shipped = TEMPLATES / name
        target = vault / rel
        if not shipped.is_file() or not target.is_file():
            continue
        if (vault / BASE_DIR / name).is_file():
            continue
        if shipped.read_text(encoding="utf-8") != target.read_text(encoding="utf-8"):
            out.append((name, rel))
    return out


def attention(vault):
    """-> [str] items that need a person and are never applied automatically."""
    items = ["no merge base — review %s against the shipped template, then run "
             "/gt:gt-upgrade (template: %s; once reviewed: gt_upgrade.py run --record-base %s)"
             % (vault / rel, TEMPLATES / name, name)
             for name, rel in docs_without_base(vault)]
    for name, rel in MERGED_DOCS.items():
        if (TEMPLATES / name).is_file() and (vault / rel).is_file():
            conflict = conflict_awaiting(vault, name, rel)
            if conflict:
                items.append("conflict awaiting you: %s — resolve, then /gt:gt-upgrade" % conflict)
            held = held_merge(vault, name, rel)
            if held:
                removed, base_is_ours = held
                if base_is_ours:
                    items.append("merge held — the merge base of %s is identical to your "
                                 "document: a release before 0.14.0 recorded it from your own "
                                 "file, so no merge against it can tell your edits from the "
                                 "release's. Review it against %s, then keep your document: "
                                 "gt_upgrade.py run --record-base %s (or take the merge: "
                                 "gt_upgrade.py run --accept-merge %s)"
                                 % (vault / rel, TEMPLATES / name, name, name))
                    continue
                why = "the release removes lines you still have"
                shown = "; ".join(repr(l.strip()[:60]) for l in removed if l.strip())[:300]
                items.append("merge held — merging %s would remove %d line(s) from it: %s (%s). "
                             "Review it against %s, then either keep your document: "
                             "gt_upgrade.py run --record-base %s, or take the merge: "
                             "gt_upgrade.py run --accept-merge %s"
                             % (vault / rel, len(removed), why, shown, TEMPLATES / name,
                                name, name))
    return items


def print_attention(vault):
    items = attention(vault)
    if items:
        print("\nneeds a person (%d, never applied automatically):" % len(items))
        for item in items:
            print("  needs a person: %s" % item)
    return items


def record_bases(vault, names, dry):
    """Explicit, after review: the shipped template becomes the base of each named doc.

    The SHIPPED text, not the owner's file: "reviewed against this release" means the
    release has nothing more to give, so a later release merges only its own changes."""
    for name in names:
        rel, shipped = MERGED_DOCS[name], TEMPLATES / name
        base = vault / BASE_DIR / name
        conflict, sidecar = _conflict_files(vault, name, rel)
        # An existing base is kept -- except after a conflict gt wrote (its sidecar is
        # there): once the owner has resolved it into the document, that document IS
        # reviewed against this release. Re-merging it against the old base would conflict
        # again wherever they kept their own text, on every run.
        # And except a held merge (held_merge): the person has reviewed a merge that would
        # remove their lines and is keeping their document -- most often a base a release
        # before 0.14.0 recorded from the owner's own file. The old base is backed up.
        held = held_merge(vault, name, rel) if base.is_file() else None
        if base.is_file() and not sidecar.is_file() and not held:
            print("record-base: %s already has a merge base — left as it is" % rel)
            continue
        if conflict.is_file():
            print("record-base: %s still exists — resolve it into %s and delete it first; "
                  "nothing recorded" % (conflict, rel))
            continue
        if not shipped.is_file() or not (vault / rel).is_file():
            print("record-base: %s or its shipped template is missing — nothing recorded" % rel)
            continue
        if not dry:
            if held:
                saved = (Path.home() / ".claude" / "golden-thread" / "backups" /
                         ("%s.base.%s" % (name, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))))
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(base, saved)
                print("record-base: the base it replaces is saved at %s" % saved)
            base.parent.mkdir(parents=True, exist_ok=True)
            base.write_text(shipped.read_text(encoding="utf-8"), encoding="utf-8")
            sidecar.unlink(missing_ok=True)
        print("record-base: %s %s the %s template as its merge base"
              % (rel, "would record" if dry else "recorded", release_version()))


def run_doc_merges(vault, dry, accept=()):
    """`accept`: documents a person has told to take a held merge (--accept-merge)."""
    notes = []
    for name, rel in MERGED_DOCS.items():
        shipped = TEMPLATES / name
        target = vault / rel
        if not shipped.is_file() or not target.is_file():
            continue
        new = shipped.read_text(encoding="utf-8")
        cur = target.read_text(encoding="utf-8")
        if new == cur:
            continue
        base_path = vault / BASE_DIR / name
        if not base_path.is_file():
            # No base gt recorded: left untouched, and NO base captured -- see
            # docs_without_base for why capturing the owner's file lost their edits.
            notes.append("%s: no merge base — left untouched (needs a person)" % rel)
            continue
        if conflict_awaiting(vault, name, rel):
            continue            # already written for these exact inputs: reported, not redone
        if name not in accept and held_merge(vault, name, rel):
            notes.append("%s: merge held — it would remove lines from your document; "
                         "left untouched (needs a person)" % rel)
            continue
        key = _conflict_key(vault, name, rel)
        rc, out = _merge3(target, base_path, shipped, dry)
        notes.append("%s: %s" % (rel, out))
        conflict, sidecar = _conflict_files(vault, name, rel)
        if not dry and rc == 1:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
        if not dry and rc == 0:
            base_path.write_text(new, encoding="utf-8")
            # A conflict from an earlier run is settled by this merge: drop its leftovers.
            conflict.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
    return "\n    ".join(notes) if notes else "documents already match the release"


def _merge3(target, base, shipped, dry):
    """git merge-file: keep the vault's edits, take the release's changes."""
    # OUTSIDE THE VAULT, always. This working copy used to be `<doc>.merging` beside the
    # document, so `run --dry-run` -- whose entire promise is "rehearse: report, write nothing"
    # -- created a file inside the vault, and a crash or a kill between the copy and the
    # `finally` left it there for someone to find (found 2026-09-18). git merge-file only reads
    # it, so where it lives is arbitrary; a temp directory makes the dry run honest and stops
    # the non-dry path from littering a vault it may be about to refuse to touch.
    tmpdir = tempfile.mkdtemp(prefix="gt-upgrade-merge-")
    tmp = Path(tmpdir) / (target.name + ".merging")
    shutil.copy2(target, tmp)
    try:
        # Bytes end to end: text=True plus write_text turned CRLF documents into LF.
        p = subprocess.run(["git", "merge-file", "-p", str(tmp), str(base), str(shipped)],
                           capture_output=True, timeout=60)
        merged_bytes = p.stdout
        merged = merged_bytes.decode("utf-8", errors="surrogateescape")
        if p.returncode != 0:
            # Conflicts: never guess. Write them beside the document and leave it.
            if not dry:
                conflict = target.with_suffix(target.suffix + ".merge-conflict")
                conflict.write_bytes(merged_bytes)
                return 1, ("CONFLICT — document untouched; the merged text with markers "
                           "is at %s for you to resolve" % conflict.name)
            return 1, "CONFLICT — would need a hand merge"
        if dry:
            # Added AND removed, never a net count: "+0 lines" hid six overwritten lines.
            added, removed = line_changes(
                target.read_bytes().decode("utf-8", errors="surrogateescape"), merged)
            return 0, ("would merge cleanly (+%d added, -%d removed from your document)"
                       % (added, len(removed)))
        target.write_bytes(merged_bytes)
        return 0, "merged cleanly"
    except (OSError, subprocess.SubprocessError) as exc:
        return 2, "could not merge: %s" % exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _removed_rules(dest):
    """Rule files the owner listed in .gt-removed beside core-rules/ (vault_init's reader:
    one definition of the file, so the two places that seed rules cannot disagree)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from vault_init import read_removed
        return read_removed(dest.parent)
    except Exception:
        return set()


def pending_core_rules(vault):
    src = TEMPLATES / "core-rules"
    if not src.is_dir():
        return ""
    dest = vault / "Projects" / "golden-thread" / "core-rules"
    if not dest.is_dir():
        return "core-rules/ is not established in this vault"
    removed = _removed_rules(dest)
    missing = [f.name for f in sorted(src.glob("core_*.md"))
               if not (dest / f.name).is_file() and f.name not in removed]
    return ("%d new Core rule(s): %s" % (len(missing), ", ".join(missing))) if missing else ""


def run_core_rules(vault, dry):
    """Seeded, never overwritten: a rule the owner has edited stays edited."""
    src = TEMPLATES / "core-rules"
    dest = vault / "Projects" / "golden-thread" / "core-rules"
    added, removed = [], _removed_rules(dest)
    for f in sorted(src.glob("core_*.md")):
        if (dest / f.name).is_file() or f.name in removed:
            continue
        added.append(f.name)
        if not dry:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest / f.name)
    return ("%s %d rule(s): %s" % ("would add" if dry else "added", len(added),
                                   ", ".join(added))) if added else "no new Core rules"


MIGRATIONS = (
    ("0.11.0", "log-spool", "log.md becomes generated from per-session spool files",
     pending_log_spool, run_log_spool),
    ("0.11.0", "decisions-spool", "each project's decisions.md becomes generated",
     pending_decisions_spool, run_decisions_spool),
    ("0.12.0", "core-rules", "Core rules shipped since this vault was seeded",
     pending_core_rules, run_core_rules),
    ("0.12.0", "doc-merge", "PROTOCOL.md and CONVENTIONS.md take the release's changes",
     pending_doc_merges, run_doc_merges),
)


def _run(cmd):
    # PYTHONDONTWRITEBYTECODE: the vault's tools import each other, so running them
    # drops __pycache__ into Projects/golden-thread/tools/. Harmless, but a REHEARSAL
    # that leaves files behind is not a rehearsal, and the test that proves "--dry-run
    # writes nothing" should not have to make an exception for it.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)


# ---- commands ---------------------------------------------------------------

def plan(vault):
    """-> [(version, id, what, reason)] for everything still pending."""
    out = []
    for version, mid, what, pending, _run_fn in MIGRATIONS:
        try:
            reason = pending(vault)
        except Exception as exc:                      # a broken check is not "clean"
            reason = "could not determine: %s" % exc
        if reason:
            out.append((version, mid, what, reason))
    return out


def cmd_status(a):
    vault = Path(a.vault)
    # `cmd_run` has always refused a vault that is not there; `status` did not, and
    # reported on a nonexistent path as though it were an un-upgraded vault -- "never
    # stamped, every migration is offered" is an alarming thing to print about a typo.
    # It matters more now that a pending status exits 0: without this, every exit code
    # from `status` would be 0 and the command could no longer fail at all. Found
    # 2026-09-12 by the test written for the request that changed the pending code.
    if not vault.is_dir():
        print("no such vault: %s" % vault, file=sys.stderr)
        return 2
    stamp = read_stamp(vault)
    here = release_version()
    print("vault:   %s" % vault)
    print("stamped: %s" % (stamp.get("gt") or "never stamped — treated as OLD, "
                                             "every migration is offered"))
    print("release: %s" % here)
    todo = plan(vault)
    if not todo:
        print("\nup to date — nothing pending")
        print_attention(vault)
        return 0
    print("\n%d pending step(s):" % len(todo))
    for version, mid, what, reason in todo:
        print("  [%s] %-16s %s" % (version, mid, what))
        print("      %s" % reason)
    print_attention(vault)
    print("\nRehearse:  gt_upgrade.py run --vault %s --dry-run" % vault)
    # 0, not 1. `status` SUCCEEDED -- it looked and found pending steps, which is the
    # normal answer for a vault that has not been upgraded yet. Returning 1 made every
    # interactive run of /gt:gt-upgrade render as a tool error, training the reader to
    # ignore a check whose whole job is to be read. Non-zero stays for genuine failures:
    # no such vault (2), an unreadable stamp, an unexpected exception. Nothing branches
    # on this code -- gt_doctor computes pending itself and the skill only reads the
    # printed output -- so callers were not relying on it (verified 2026-09-12).
    return 0


def cmd_run(a):
    vault = Path(a.vault)
    if not vault.is_dir():
        print("no such vault: %s" % vault, file=sys.stderr)
        return 2
    dry = a.dry_run

    # One `git checkout` must be able to undo this, which it cannot if the upgrade
    # lands on top of work someone has not committed.
    rc, out = git(vault, "status", "--porcelain")
    if rc == 0 and out.strip() and not dry and not a.allow_dirty:
        print("REFUSED: the vault has uncommitted changes — commit them first, so this "
              "upgrade is one commit and one `git checkout` away from undone.\n",
              file=sys.stderr)
        print(out.strip()[:800], file=sys.stderr)
        print("\n(--allow-dirty overrides, --dry-run always works)", file=sys.stderr)
        return 2

    if a.record_base:
        record_bases(vault, a.record_base, dry)
    if a.accept_merge:
        held = [n for n in a.accept_merge if held_merge(vault, n, MERGED_DOCS[n])]
        if held:
            print("accept-merge: %s" % run_doc_merges(vault, dry, accept=set(held)))
        else:
            print("accept-merge: no held merge for %s — nothing to accept"
                  % ", ".join(a.accept_merge))

    todo = plan(vault)
    if not todo:
        print("up to date — nothing pending")
        # Stamp only when the stamp would change. Re-stamping a vault already at this
        # release adds a history line and nothing else -- and dirties a clean vault, e.g.
        # every re-run while a conflict awaits its owner.
        if not dry and read_stamp(vault).get("gt") != release_version():
            write_stamp(vault, release_version(), [])
        print_attention(vault)
        return 0

    backup = ""
    if not dry:
        backup = _backup(vault)
        print("backup: %s\n" % backup)

    applied, failed = [], []
    for version, mid, what, reason in todo:
        run_fn = next(m[4] for m in MIGRATIONS if m[1] == mid)
        print("[%s] %s — %s" % (version, mid, what))
        try:
            note = run_fn(vault, dry)
        except Exception as exc:
            note = "ERROR: %s" % exc
        print("    %s" % note)
        (failed if ("REFUSED" in note or "ERROR" in note or "CONFLICT" in note)
         else applied).append(mid)

    if dry:
        print("\ndry run — nothing written. Drop --dry-run to apply.")
        return 0

    write_stamp(vault, release_version(), applied)
    print("\nstamped %s" % release_version())
    if failed:
        print("%d step(s) need a person: %s" % (len(failed), ", ".join(failed)))
        print("Nothing else was skipped; re-run after resolving and it continues.")
    print_attention(vault)
    _receipt(vault, applied, failed)
    return 1 if failed else 0


def _backup(vault):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path.home() / ".claude" / "golden-thread" / "backups" / ("vault-%s.tar.gz" % stamp)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The vault's own history is not the backup: this must survive a bad merge in a
    # tree that may not have been pushed anywhere.
    with tarfile.open(out, "w:gz") as tf:
        for item in sorted(vault.iterdir()):
            if item.name in (".git", "node_modules", ".obsidian"):
                continue
            tf.add(item, arcname=item.name)
    return str(out)


def _receipt(vault, applied, failed):
    """A line in log.md, through the tool that owns it."""
    t = tool(vault, "gt_log.py")
    if not t.is_file():
        return
    when = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    line = ("%s [work] golden-thread — vault upgraded to %s: applied %s%s"
            % (when, release_version(), ", ".join(applied) or "nothing",
               ("; NEEDS A PERSON: " + ", ".join(failed)) if failed else ""))
    _run([sys.executable, str(t), "--vault", str(vault), "add", line])


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=argparse.SUPPRESS,
                        help="the vault to upgrade (default: $GT_VAULT, then config)")
    common.add_argument("--dry-run", "-n", action="store_true", default=argparse.SUPPRESS,
                        help="report what would change; write nothing")
    p = argparse.ArgumentParser(description="bring a vault up to the installed release",
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common])
    r = sub.add_parser("run", parents=[common])
    r.add_argument("--allow-dirty", action="store_true",
                   help="upgrade even with uncommitted changes in the vault")
    r.add_argument("--record-base", action="append", choices=sorted(MERGED_DOCS),
                   default=[], metavar="DOC",
                   help="after reviewing DOC (%s) against the shipped template, record that "
                        "template as its merge base so later releases merge into it. "
                        "Explicit only: install.sh never passes it" % ", ".join(sorted(MERGED_DOCS)))
    r.add_argument("--accept-merge", action="append", choices=sorted(MERGED_DOCS),
                   default=[], metavar="DOC",
                   help="apply a held merge of DOC -- one that removes lines from your document "
                        "-- after reviewing it. Explicit only: install.sh never passes it")
    a = p.parse_args(argv)
    for key, default in (("vault", None), ("dry_run", False), ("allow_dirty", False),
                         ("record_base", []), ("accept_merge", [])):
        if not hasattr(a, key):
            setattr(a, key, default)
    if not a.vault:
        a.vault = os.environ.get("GT_VAULT")
    if not a.vault:
        try:
            a.vault = json.loads((Path.home() / ".claude" / "vault-config.json")
                                 .read_text(encoding="utf-8"))["vault_path"]
        except Exception:
            print("no vault: pass --vault, set GT_VAULT, or run /gt:gt-init",
                  file=sys.stderr)
            return 2
    return {"status": cmd_status, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
