#!/usr/bin/env python3
"""ADR numbers allocated atomically, and `decisions.md` rendered from them.

    gt_adr.py allocate <project> --title "..." [--supersedes N] [--expires-when "..."] [--expires YYYY-MM-DD]
    gt_adr.py merge <project>                    # render decisions.md
    gt_adr.py status <project>
    gt_adr.py migrate <project>                  # one-time: split today's decisions.md
    gt_adr.py lineage <project> <topic>          # read-only: the supersession chain(s)

`<project>` is a slug or a path under Projects/: a sub-project's bare slug resolves to
`Projects/<parent>/<slug>/` (gt_spool.resolve_project), and a name that matches no
project is refused, naming it, with nothing created (0.18.1).

## Structured fields (0.18.1)

An ADR body may carry these field lines, written by `allocate` when its flags are given
and parsed by `gt_lint.py` (`adr-expires`), `gt_brief.py` and `lineage`:

    - **Supersedes**: ADR-4            the decision(s) this one replaces
    - **Expires when**: <prose>        the condition that ends its truth (any age)
    - **Expires**: YYYY-MM-DD          a date after which it must be re-checked

The key may also be spelled `supersedes:` / `expires_when:` / `expires:` (the Knowledge
page frontmatter spelling), with or without the bullet and bold. `Decision`, `Context`
and `Rejected alternatives` are read the same way.

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
conflicted copy. `gt_lint.py`'s `adr-collision` check is what catches that.
This does not solve distributed consensus and must not be described as if it does.

The check's NAME is quoted here because it is what a `--only` or a suppression entry
has to match. It read `adr-number-collision` until 0.16.5 -- a name gt_lint has never
emitted -- so anyone who copied it out of this docstring narrowed their lint run to
nothing and got a clean report from a check that never ran.

## Exit codes

    0  done -- allocated, rendered, migrated, or already correct
    1  could not allocate a number after MAX_RETRY attempts
    2  usage -- including a project name that matches no project, or more than one --
       or the wrong step in the sequence: `merge` on a decisions.md that is
       neither generated nor migrated (run `migrate` first), `migrate` on one that
       is already generated (it is one-time)
    3  refused, and a PERSON has to decide: duplicate ADR numbers, hand-written
       lines no ADR slot holds, a round trip that would not reproduce the file, or
       a target being rewritten underneath the merge
    4  decisions.md EXISTS but cannot be read; nothing was written (gt_spool.Unreadable)
    5  the OS refused the write (EPERM/EACCES); one line names the interpreter and the fix

2 and 3 are deliberately different. 2 says run the other command; 3 says no command
will do it, because renumbering or discarding someone's lines is not a step a script
may take on its own.
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

# A structured field line inside an ADR body (see the module docstring). Lenient on
# purpose: the bullet, the bold, the colon inside or outside the bold, and the
# frontmatter spelling (`expires_when:`) all read the same.
FIELD = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?(supersedes|expires[ _]when|expires|date|decision|context|"
    r"rejected(?:[ _](?:alternatives|options))?)(?:\*\*)?\s*:\s*(?:\*\*\s*)?(.*)$", re.I)
ADR_REF = re.compile(r"ADR-?(\d+)", re.I)
ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


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
            (d / HIGHWATER).write_bytes(("%d\n" % n).encode("utf-8"))
    except OSError:
        pass


def used_numbers(vault, project):
    """The numbers this project can still SEE: slots on disk, plus the baseline.

    Not the same as the numbers it has SPENT -- a deleted slot leaves this set and
    the high-water floor is what remembers it. Anything answering "what comes next"
    must go through next_number(), never through this.
    """
    return set(slots(vault, project)) | baseline_numbers(vault, project)


def next_number(vault, project):
    """The number the next allocation will take. ONE definition, used by everything.

    It was two. `allocate()` consulted the high-water floor and the dry run did not,
    so after allocating 1-3 and deleting 0003.md the rehearsal said "would reserve
    ADR-3" and the real run returned 4 -- wrong in exactly the case _highwater was
    added for, and wrong in the command whose whole job is to say what will happen.
    A preview computed differently from the thing it previews is not a preview.
    """
    taken = used_numbers(vault, project)
    return max(max(taken) if taken else 0,
               _highwater(S.spool_dir(vault, KIND, project))) + 1


def field_lines(supersedes=(), expires_when=None, expires=None):
    """The structured field lines `allocate` writes under the heading (see the docstring)."""
    out = []
    if supersedes:
        out.append("- **Supersedes**: %s" % ", ".join("ADR-%d" % n for n in supersedes))
    if expires_when:
        out.append("- **Expires when**: %s" % " ".join(str(expires_when).split()))
    if expires:
        out.append("- **Expires**: %s" % expires)
    return out


def allocate(vault, project, title, sid=None, fields=()):
    """Reserve the next number by creating its slot exclusively. Returns (n, path).

    `fields` are extra lines written under the heading (field_lines()). The date goes in
    the allocation comment, which `render` strips: it is what `lineage` dates an ADR by
    when the body names no date, and it costs the rendered file nothing."""
    import datetime
    sid = S.session_id(sid)
    d = S.spool_dir(vault, KIND, project)
    d.mkdir(parents=True, exist_ok=True)
    for _ in range(MAX_RETRY):
        n = next_number(vault, project)
        p = d / ("%04d.md" % n)
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            continue                      # someone took it between our scan and here
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            raise
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("<!-- allocated by %s on %s -->\n## ADR-%d: %s\n%s"
                     % (sid, datetime.date.today().isoformat(), n, title or "TITLE",
                        "".join(l + "\n" for l in fields)))
        _raise_highwater(d, n)
        return n, p
    raise SystemExit("could not allocate an ADR number after %d attempts" % MAX_RETRY)


ALLOC_DATE = re.compile(r"^<!-- allocated by .+? on (\d{4}-\d{2}-\d{2}) -->\s*$")
MARKER_LINE = re.compile(r"<!--\s*captured:\s*\d{4}-\d{2}-\d{2}")
ADR_HEAD = re.compile(r"^##\s+ADR(?:-(\d+))?\b")        # `## ADR-3: title`, or an unnumbered `## ADR: title`


def _dated(lines, i):
    """Is the ADR whose heading is lines[i] already dated: a date in its title, a capture marker in the next
    three lines, or a date in a `Date:` field in its first six body lines?"""
    if ISO_DATE.search(lines[i]):
        return True
    if any(MARKER_LINE.search(l) for l in lines[i + 1:i + 4]):
        return True
    for l in lines[i + 1:i + 7]:
        if ADR_HEAD.match(l):
            break
        if re.search(r"\bdate\b[^\n]*?\d{4}-\d{2}-\d{2}", l, re.I):
            return True
    return False


def _slot_body(raw):
    """A slot's text without its allocation comment, with the allocation date kept as a capture marker after
    the heading unless the ADR is already dated (0.22.0): the date was recorded and then stripped, so the
    rendered decisions.md could not say when a decision was made."""
    stamp, kept = None, []
    for l in raw.splitlines():
        if l.startswith("<!-- allocated by"):
            m = ALLOC_DATE.match(l)
            if m:
                stamp = m.group(1)
            continue
        kept.append(l)
    if stamp:
        for i, l in enumerate(kept):
            if ADR_HEAD.match(l):
                if not _dated(kept, i):
                    kept.insert(i + 1, "<!-- captured: %s -->" % stamp)
                break
    return "\n".join(kept)


def render(vault, project):
    parts = []
    b = S.spool_dir(vault, KIND, project) / S.BASELINE
    if b.is_file():
        # read_owner keeps CRLF (0.21.0); the rendered file is LF, like every gt text write.
        parts.append(S.read_owner(b).replace("\r\n", "\n").rstrip("\n"))
    for n in sorted(slots(vault, project)):
        parts.append(_slot_body(S.read_owner(slots(vault, project)[n])).strip("\n"))
    return S.BANNER + "\n\n" + "\n\n".join(p for p in parts if p) + "\n"


def target(vault, project):
    """`project` is the RESOLVED path under Projects/ (see _resolve), never a bare slug."""
    return Path(vault) / "Projects" / project / "decisions.md"


def _resolve(a, allow_unregistered=False):
    """(vault, project path under Projects/) or (vault, None) after saying why on stderr.

    Before 0.18.1 every subcommand joined `Projects/` with the name it was given, so a
    sub-project's bare slug rendered into a new top-level folder and reported success."""
    v = S.vault_root(a.vault)
    try:
        return v, S.resolve_project(v, a.project, allow_unregistered=allow_unregistered)
    except S.ProjectNotFound as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return v, None


def _adopt_flat_spool(vault, name, rel, dry_run=False):
    """Move a sub-project's spool out of the flat folder the pre-0.18.1 bug used.

    `allocate gt-extensions` reserved into spool/decisions/gt-extensions/ while the
    sub-project's real spool is spool/decisions/golden-thread/gt-extensions/. The spool
    entry is authoritative, so it is MOVED (never copied) to where the project resolves,
    and the next merge renders it in the right place. Returns None when there was
    nothing to do or it was done, or the refusal text when both folders hold ADRs --
    merging two numbered sequences is a decision for a person."""
    if rel == name or "/" in name:
        return None
    flat, nested = S.spool_dir(vault, KIND, name), S.spool_dir(vault, KIND, rel)
    if not flat.is_dir():
        return None
    moving = sorted(f for f in flat.iterdir() if SLOT.match(f.name) or f.name == S.BASELINE)
    if not moving:
        return None
    # The sub-project's own spool normally exists already: create-project leaves its
    # scaffold baseline there. What may not happen is one NUMBER held twice, or two
    # baselines -- merging two sequences like that is a decision for a person.
    flat_nums = set(slots(vault, name)) | baseline_numbers(vault, name)
    clash = sorted(flat_nums & used_numbers(vault, rel))
    two_bases = (flat / S.BASELINE).is_file() and (nested / S.BASELINE).is_file()
    if clash or two_bases:
        return ("%s and %s both hold %s for %s -- a person must merge them (the flat one "
                "was written by the pre-0.18.1 slug bug)"
                % (flat, nested, ", ".join("ADR-%d" % n for n in clash) if clash
                   else "a baseline", rel))
    if dry_run:
        print("dry run: would move %d file(s) from %s to %s (written by the pre-0.18.1 "
              "slug bug)" % (len(moving), flat, nested), file=sys.stderr)
        return None
    nested.mkdir(parents=True, exist_ok=True)
    for f in moving:
        f.rename(nested / f.name)
    _raise_highwater(nested, max([_highwater(flat)] + list(flat_nums)))
    (flat / HIGHWATER).unlink(missing_ok=True)
    try:
        flat.rmdir()
    except OSError:
        pass                               # something else lives there; leave it be
    print("moved %s's ADR spool from %s to %s (written there by the pre-0.18.1 slug bug)"
          % (rel, flat, nested), file=sys.stderr)
    return None


def _note_stray_folder(vault, name, rel):
    """Name the stray top-level folder the bug left, without touching it."""
    if rel == name or "/" in name:
        return
    stray = Path(vault) / "Projects" / name
    if (stray / "decisions.md").is_file() and not (stray / "README.md").is_file():
        print("note: %s/decisions.md was written by the pre-0.18.1 slug bug and is not a "
              "project; %s's ADRs render into Projects/%s/decisions.md. Delete the stray "
              "folder once you have checked it holds nothing else." % (stray, name, rel),
              file=sys.stderr)


def _parse_supersedes(values):
    nums = []
    for v in values or ():
        for part in re.split(r"[,\s]+", v.strip()):
            m = re.fullmatch(r"(?:ADR-?)?(\d+)", part, re.I)
            if part and not m:
                raise ValueError("--supersedes takes ADR numbers (ADR-4 or 4), not %r" % part)
            if m:
                nums.append(int(m.group(1)))
    return sorted(set(nums))


# -- reading ADRs back: fields, supersession, lineage ---------------------------------

def _field_key(raw):
    k = raw.lower().replace("_", " ")
    return "rejected" if k.startswith("rejected") else k


def parse_adrs(text):
    """Every ADR in rendered decisions text, oldest number first.

    [{n, title, body, fields: {supersedes: [int], expires when, expires, date, decision,
      context, rejected: [str]}}]. An `ADR-N amendment` section is not a new decision:
    its text joins the ADR before it (the same rule `migrate` and vault_init apply)."""
    heads = list(HEAD.finditer(text))
    adrs = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        chunk = text[m.start():end]
        if m.group(2):
            if adrs:
                adrs[-1]["body"] += "\n" + chunk
            continue
        first, _, rest = chunk.partition("\n")
        title = re.sub(r"^##\s*ADR-\d+\s*:?\s*", "", first).strip()
        adrs.append({"n": int(m.group(1)), "title": title, "body": rest})
    for adr in adrs:
        adr["fields"] = _fields(adr["title"], adr["body"])
    return sorted(adrs, key=lambda d: d["n"])


def _fields(title, body):
    f = {"supersedes": [], "rejected": []}
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = FIELD.match(line)
        hm = re.match(r"^#{3,6}\s*(rejected(?: alternatives| options)?)\b", line, re.I)
        if hm:
            # `### Rejected alternatives` -- the section's bullets, up to the next heading.
            i += 1
            while i < len(lines) and not lines[i].startswith("#"):
                t = lines[i].strip().lstrip("-*").strip()
                if t:
                    f["rejected"].append(t)
                i += 1
            continue
        if not m:
            i += 1
            continue
        key, val = _field_key(m.group(1)), m.group(2).strip().strip("*").strip()
        indent = len(line) - len(line.lstrip())
        cont = []
        i += 1
        # Continuation: deeper-indented lines under the field (sub-bullets of a list).
        while i < len(lines) and lines[i].strip() and \
                len(lines[i]) - len(lines[i].lstrip()) > indent and not FIELD.match(lines[i]):
            cont.append(lines[i].strip().lstrip("-*").strip())
            i += 1
        if key == "supersedes":
            f["supersedes"] += [int(x) for x in ADR_REF.findall(val)] or \
                [int(x) for x in re.findall(r"\b(\d+)\b", val)]
        elif key == "rejected":
            f["rejected"] += ([val] if val else []) + cont
        elif key not in f:
            f[key] = " ".join([val] + cont).strip().strip('"\'')
    # A title such as "Use X (supersedes ADR-3)" is prose, but unambiguous prose.
    f["supersedes"] += [int(x) for x in re.findall(r"supersed\w*\s+ADR-?(\d+)", title, re.I)]
    f["supersedes"] = sorted(set(f["supersedes"]))
    return f


def project_adrs(vault, rel):
    """parse_adrs over what `merge` would render now -- the spool is authoritative, so a
    lineage asked before the merge still sees an ADR written a minute ago -- with each
    slot's allocation date filled in where the body names none."""
    adrs = parse_adrs(render(vault, rel)[len(S.BANNER):])
    stamps = {}
    for n, p in slots(vault, rel).items():
        m = re.search(r"<!-- allocated by .+? on (\d{4}-\d{2}-\d{2}) -->",
                      p.read_text(encoding="utf-8", errors="replace")[:400])
        if m:
            stamps[n] = m.group(1)
    for adr in adrs:
        f = adr["fields"]
        d = ISO_DATE.search(f.get("date", "")) or ISO_DATE.search(adr["title"])
        f["date"] = d.group(1) if d else stamps.get(adr["n"])
    return adrs


def superseded_by(adrs):
    """{n: [numbers of the ADRs that supersede n]} -- only edges between real ADRs."""
    have = {a["n"] for a in adrs}
    out = {}
    for a in adrs:
        for old in a["fields"]["supersedes"]:
            if old in have and old != a["n"]:
                out.setdefault(old, []).append(a["n"])
    return out


def _first_line(text):
    for line in (text or "").splitlines():
        t = line.strip().lstrip("-*").strip()
        if t and not FIELD.match(line) and not t.startswith("#") and not t.startswith("<!--"):
            return t
    return ""


def _reason(adr):
    """Why `adr` replaced what it supersedes: its Context, else its Decision, else the
    first line of prose in it."""
    f = adr["fields"]
    return (f.get("context") or f.get("decision") or _first_line(adr["body"])
            or "(no reason recorded)")


def lineage(adrs, topic):
    """[[adr, ...], ...]: one chain per connected supersession component that contains
    an ADR whose title or body mentions `topic`, each oldest number first."""
    t = topic.lower()
    by_n = {a["n"]: a for a in adrs}
    nbr = {}
    for a in adrs:
        for old in a["fields"]["supersedes"]:
            if old in by_n and old != a["n"]:
                nbr.setdefault(a["n"], set()).add(old)
                nbr.setdefault(old, set()).add(a["n"])
    seen, chains = set(), []
    for a in adrs:
        if a["n"] in seen or (t not in a["title"].lower() and t not in a["body"].lower()):
            continue
        comp, stack = set(), [a["n"]]
        while stack:
            n = stack.pop()
            if n in comp:
                continue
            comp.add(n)
            stack.extend(nbr.get(n, ()))
        seen |= comp
        chains.append([by_n[n] for n in sorted(comp)])
    return chains


def render_lineage(adrs, topic, project):
    chains = lineage(adrs, topic)
    if not chains:
        return "No decisions found for topic '%s'\n" % topic
    sup_by = superseded_by(adrs)
    by_n = {a["n"]: a for a in adrs}
    out = ['Decision history for: "%s" (%s)' % (topic, project)]
    for chain in chains:
        out.append("")
        for a in chain:
            f = a["fields"]
            later = sup_by.get(a["n"], [])
            head = "ADR-%d (%s) — %s" % (a["n"], f.get("date") or "date unknown", a["title"])
            if not later:
                out.append(head + " — current")
                rej = f.get("rejected") or []
                out.append("  Rejected alternatives: %s"
                           % ("; ".join(rej) if rej else "none recorded"))
            else:
                out.append(head)
                for n in later:
                    out.append("  → Superseded by ADR-%d because: %s" % (n, _reason(by_n[n])))
            if f.get("expires when") or f.get("expires"):
                out.append("  Expires: %s" % (f.get("expires when") or f.get("expires")))
    return "\n".join(out) + "\n"


def _undated(lines):
    """[(index of the heading line, ADR number, title)] for every ADR heading with no date anywhere."""
    out = []
    for i, l in enumerate(lines):
        m = ADR_HEAD.match(l)
        if m and not _dated(lines, i):
            out.append((i, int(m.group(1) or 0), l.split(":", 1)[1].strip() if ":" in l else l))
    return out


def _git_date(vault, path, line_no):
    """-> ("git" | "uncommitted", YYYY-MM-DD) for the commit that last changed this line, or None when git does
    not know the file. Never a guess: no repository, an untracked file or a failed blame is None."""
    import datetime
    import subprocess
    try:
        rel = os.path.relpath(str(path), str(vault)).replace(os.sep, "/")
        p = subprocess.run(["git", "-C", str(vault), "blame", "--line-porcelain", "-L", "%d,%d" % (line_no, line_no),
                            "--", rel], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or not p.stdout:
        # A file git does not track yet, inside a repository, is uncommitted; outside one, or on any other
        # failure, the date is unknown.
        try:
            inside = subprocess.run(["git", "-C", str(vault), "rev-parse", "--is-inside-work-tree"],
                                    capture_output=True, text=True, timeout=30)
            tracked = subprocess.run(["git", "-C", str(vault), "ls-files", "--error-unmatch", "--", rel],
                                     capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        if inside.returncode == 0 and inside.stdout.strip() == "true" and tracked.returncode != 0:
            return ("uncommitted", datetime.date.today().isoformat())
        return None
    first = p.stdout.splitlines()[0].split()
    if not first:
        return None
    if set(first[0]) == {"0"}:                      # "Not Committed Yet"
        return ("uncommitted", datetime.date.today().isoformat())
    t = re.search(r"^author-time (\d+)$", p.stdout, re.M)
    z = re.search(r"^author-tz ([+-])(\d\d)(\d\d)$", p.stdout, re.M)
    if not t:
        return None
    secs = int(t.group(1))
    if z:
        secs += (1 if z.group(1) == "+" else -1) * (int(z.group(2)) * 3600 + int(z.group(3)) * 60)
    return ("git", datetime.datetime.fromtimestamp(secs, datetime.timezone.utc).strftime("%Y-%m-%d"))


def _adr_label(n):
    return "ADR-%d" % n if n else "ADR"


def _spool_files(vault, project):
    d = S.spool_dir(vault, KIND, project)
    files = []
    if (d / S.BASELINE).is_file():
        files.append(d / S.BASELINE)
    files += [slots(vault, project)[n] for n in sorted(slots(vault, project))]
    return files


def cmd_date_backfill(a):
    """The one-time sequence that dates ADRs written before `render` kept the allocation date. Read-only unless
    --apply; with it, inserts `<!-- captured: DATE (git) -->` under each undated heading in the spool file."""
    v = S.vault_root(a.vault)
    base = S.spool_dir(v, KIND)
    if a.all:
        # every folder that holds spool files, sub-project spools (`parent/child`) included
        names = []
        for cur, dirs, files in os.walk(str(base)):
            dirs.sort()
            if any(f == S.BASELINE or SLOT.match(f) for f in files):
                names.append(os.path.relpath(cur, str(base)).replace(os.sep, "/"))
        names.sort()
    elif a.project:
        if not (base / a.project).is_dir():
            print("REFUSED: no ADR spool for project %r" % a.project, file=sys.stderr)
            return 2
        names = [a.project]
    else:
        print("REFUSED: name a project, or --all", file=sys.stderr)
        return 2
    total = known = unknown = written = 0
    for name in names:
        for f in _spool_files(v, name):
            raw = f.read_text(encoding="utf-8", errors="replace")
            lines = raw.split("\n")
            alloc = bool(lines) and ALLOC_DATE.match(lines[0])
            todo = [] if alloc else _undated(lines)
            inserts = []
            for i, n, title in todo:
                got = _git_date(v, f, i + 1)
                total += 1
                if got is None:
                    unknown += 1
                    print("  %s/%s %s: date unknown (git has no history for it) -- left undated" % (name, f.name, _adr_label(n)))
                    continue
                known += 1
                print("  %s/%s %s -> %s (%s)" % (name, f.name, _adr_label(n), got[1], got[0]))
                inserts.append((i, "<!-- captured: %s (%s) -->" % (got[1], got[0])))
            if a.apply and inserts and not getattr(a, "dry_run", False):
                for i, marker in sorted(inserts, reverse=True):
                    lines.insert(i + 1, marker)
                S.write_if_changed(f, "\n".join(lines))
                written += len(inserts)
    if not total:
        print("nothing to date: every ADR already carries a date")
        return 0
    print("%d ADR(s) with no date: %d dated from git, %d unknown%s" % (
        total, known, unknown,
        "; %d marker(s) written -- run `merge` to regenerate decisions.md" % written if a.apply
        else "; nothing written -- run again with --apply"))
    return 0


def cmd_lineage(a):
    v, rel = _resolve(a)
    if rel is None:
        return 2
    sys.stdout.write(render_lineage(project_adrs(v, rel), a.topic, rel))
    return 0


def cmd_allocate(a):
    v, rel = _resolve(a)
    if rel is None:
        return 2
    try:
        sup = _parse_supersedes(a.supersedes)
    except ValueError as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 2
    if a.expires and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.expires):
        print("REFUSED: --expires takes a date, YYYY-MM-DD (a condition goes in "
              "--expires-when)", file=sys.stderr)
        return 2
    refusal = _adopt_flat_spool(v, a.project, rel, dry_run=a.dry_run)
    if refusal:
        print("REFUSED: " + refusal, file=sys.stderr)
        return 3
    # A supersedes: that names no ADR is a broken link in the one structure lineage
    # walks; refuse it before a number is spent rather than render a dangling edge.
    unknown = [n for n in sup if n not in used_numbers(v, rel)]
    if unknown:
        print("REFUSED: --supersedes names %s, which %s has no ADR for"
              % (", ".join("ADR-%d" % n for n in unknown), rel), file=sys.stderr)
        return 2
    fields = field_lines(sup, a.expires_when, a.expires)
    if a.dry_run:
        # Deliberately does NOT reserve: a dry run that consumed a number would
        # leave a hole every time someone rehearsed.
        print("dry run: would reserve ADR-%d for %s (nothing written)"
              % (next_number(v, rel), rel), file=sys.stderr)
        return 0
    n, p = allocate(v, rel, a.title, a.id, fields)
    # The number on stdout ALONE, so a caller can use it directly; everything else
    # goes to stderr. A session that has to parse prose to learn its number will
    # eventually parse it wrong.
    print("reserved ADR-%d for %s -> %s" % (n, rel, p.name), file=sys.stderr)
    _emit_adr(v, rel, n, a.title, a.id)
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
    v, rel = _resolve(a)
    if rel is None:
        return 2
    t = target(v, rel)
    # Never render into a folder that is not a project. _resolve already guarantees a
    # README for the name it returns; this is the write path's own guard, so a future
    # caller that bypasses resolution still cannot recreate the stray-folder bug.
    if not (t.parent / "README.md").is_file():
        print("REFUSED: %s has no README.md, so it is not a project; decisions.md is not "
              "written there" % t.parent, file=sys.stderr)
        return 2
    refusal = _adopt_flat_spool(v, a.project, rel, dry_run=a.dry_run)
    if refusal:
        print("REFUSED: " + refusal, file=sys.stderr)
        return 3
    _note_stray_folder(v, a.project, rel)
    a.project = rel
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
    # Same precondition as gt_log.py's merge, for the same reason and with one difference.
    # hand_written() decides against the bytes it reads; write_if_changed() then read them
    # again and replaced. A save landing between those two reads was lost -- and here that is
    # worse than in log.md, because this path REFUSES on hand-written lines: finding none and
    # then clobbering the save that arrived a moment later defeats the refusal entirely,
    # silently, in the one case it exists to catch. Digest BEFORE the check, so the
    # precondition covers the decision rather than only the write.
    for _attempt in range(3):
        before = S.current_digest(t)
        found = S.hand_written(t, produced)
        if found:
            print("REFUSED: %s has %d hand-written line(s) no ADR slot holds; rendering would "
                  "delete them. Needs a person: move them into an ADR (gt_adr.py allocate), "
                  "then merge again:\n  %s"
                  % (t, len(found), "\n  ".join(found[:20])), file=sys.stderr)
            return 3
        changed = S.write_if_changed(t, produced, expect=before)
        if changed is not S.STALE:
            break
    else:
        print("REFUSED: %s changed underneath this merge three times — nothing was written.\n"
              "  Something else is writing it right now. Re-run when it settles."
              % t, file=sys.stderr)
        return 3
    print("%s/decisions.md %s" % (a.project, "updated" if changed else "unchanged (idempotent)"))
    return 0


def cmd_status(a):
    v, rel = _resolve(a)
    if rel is None:
        return 2
    a.project = rel
    used = sorted(used_numbers(v, a.project))
    # `next` is not len(used)+1 and not highest+1: the high-water floor holds numbers
    # that are spent and no longer on disk. Printing only "highest" invited exactly
    # the arithmetic the allocator refuses to do.
    print("  %s: %d ADR(s)%s, next ADR-%d"
          % (a.project, len(used), ", highest ADR-%d" % used[-1] if used else "",
             next_number(v, a.project)))
    for n, p in sorted(slots(v, a.project).items()):
        txt = p.read_text(encoding="utf-8", errors="replace")
        owner = (re.search(r"<!-- allocated by (.+?) -->", txt) or [None, "?"])[1]
        # The comment, the heading and any structured field lines `allocate` wrote are
        # all scaffolding: a slot holding only those has no decision in it yet.
        body = [l for l in txt.strip().splitlines()[2:] if l.strip() and not FIELD.match(l)]
        stub = "  UNFINISHED (allocated, no body)" if not body else ""
        print("    %s  ADR-%d  by %s%s" % (p.name, n, owner, stub))
    return 0


def cmd_migrate(a):
    v, rel = _resolve(a, allow_unregistered=True)
    if rel is None:
        return 2
    a.project = rel
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
    al.add_argument("--supersedes", action="append", default=[],
                    help="ADR number this one replaces (repeatable; writes a Supersedes field)")
    al.add_argument("--expires-when", default=None,
                    help="prose condition that ends this decision's truth")
    al.add_argument("--expires", default=None, help="YYYY-MM-DD after which to re-check it")
    db = sub.add_parser("date-backfill", parents=[common],
                        help="date ADRs that have no date, from git; read-only unless --apply (0.22.0)")
    db.add_argument("project", nargs="?", default=None)
    db.add_argument("--all", action="store_true")
    db.add_argument("--apply", action="store_true")
    for c in ("merge", "status", "migrate"):
        sp = sub.add_parser(c, parents=[common]); sp.add_argument("project")
    ln = sub.add_parser("lineage", parents=[common],
                        help="read-only: supersession chain(s) of ADRs mentioning a topic")
    ln.add_argument("project"); ln.add_argument("topic")
    args = p.parse_args(argv)
    for key, default in (("vault", None), ("id", None), ("dry_run", False)):
        if not hasattr(args, key):
            setattr(args, key, default)
    try:
        return {"allocate": cmd_allocate, "merge": cmd_merge, "status": cmd_status,
                "migrate": cmd_migrate, "lineage": cmd_lineage,
                "date-backfill": cmd_date_backfill}[args.cmd](args)
    except S.Unreadable as exc:
        # An existing-but-unreadable decisions.md, refused in one place for every
        # subcommand -- the same handler gt_log.py carries, for the same reason.
        #
        # 0.16.5 made gt_spool RAISE here rather than digest empty bytes: an unreadable
        # file used to satisfy the `expect` precondition -- reporting "nobody moved
        # underneath you" about a file nobody can see -- and was then overwritten.
        # Without this clause the refusal still holds and nothing is written either way,
        # but it arrives as a traceback, which reads as a crash rather than as the
        # deliberate decision it is. A refusal that looks like a bug gets worked around.
        print("REFUSED: %s.\n  Nothing was written. Fix the permissions or the owner "
              "(or wait for the sync client to finish), then run `merge`." % exc,
              file=sys.stderr)
        return 4
    except S.WriteRefused as exc:
        # The OS refused the write (0.20.1): one line naming the interpreter and the fix,
        # not a traceback. Nothing was written; the spool still holds every entry.
        print(exc.strerror or str(exc), file=sys.stderr)
        return 5


if __name__ == "__main__":
    # A refusal from the OS -- gt sandbox mode, or a macOS interpreter refusal -- is one line
    # naming the next step, not a traceback (0.20.1, B4). gt_errors sits beside this file.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gt_errors as _gte
    except ImportError:
        _gte = None
    raise SystemExit(_gte.run(main, "gt_adr") if _gte else main())
