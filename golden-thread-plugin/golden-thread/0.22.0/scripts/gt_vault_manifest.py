#!/usr/bin/env python3
"""gt_vault_manifest.py -- the vault layout manifest: where this vault keeps gt's own folders.

A vault may carry `vault.manifest.json` at its root. It is declarative data, read through this one
module by the write queue and the broker; nothing else parses it. Without the file (or with any file
this reader will not accept) every value is the one gt 0.21.4 hard-coded, so an ordinary vault is
untouched.

    {"manifest_version": 1,
     "queue_dir":  "Spool/queue",            # where write requests wait
     "broker_dir": "Spool/broker",           # the broker's log, conflicts, rejected
     "tools_dir":  "Spool/tools",            # the vault's copies of gt_task.py, gt_session.py ...
     "protected": {"exact": ["Private/ledger.md"], "prefixes": ["Secrets/"], "names": ["gen.md"]},
     "audit_exempt_prefixes": ["Dump/"]}

A manifest can only ADD. The floor it cannot lower: the built-in refused files, prefixes and names
(core-rules/, Sources/, log.md, TASKS.md, decisions.md, the spool and tools folders), the `.md`-only
rule, and the rule that gt's folders sit inside the vault, outside core-rules/, Sources/ and any
dot-folder. A file that is malformed, unreadable, of another `manifest_version`, carries a key this
version does not know, tries to switch a protection off (`"md_only": false`, an empty or non-object
`protected`, anything under `protected` but exact/prefixes/names) or points a folder out of the vault
is ignored WHOLE: the defaults apply and one line goes to stderr, once per process. The manifest file
itself is not a `.md`, so it can never be written through the queue.

Since the phase 84 review (0.22.0): a gt folder is checked against PLACE_FLOOR by its spelling AND
the path it resolves to (a link into core-rules/ is refused), may not already hold anything gt did
not put there, and may not overlap another; the file is read regular-file-only, non-blocking,
unlinked and capped at 256 KB; protected paths are compared through gt_review_target.fold.
SECURITY.md section 5.5d is the threat model.

    gt_vault_manifest.py [--vault V]              print the layout in effect and why
    gt_vault_manifest.py detect --vault V [--json] infer a manifest from the vault's own files
                                                   (READ-ONLY: it opens files, never writes one)

Descriptive keys (all optional, all validated; a bad value ignores the file whole like any other):
`naming` (foldered | dendron | hybrid), `frontmatter_dialect` (gt | obsidian | dendron | plain),
`layers` ({"global_memory": ["Memory"], "design": ["architecture.md"]}: extra review targets, so
gt_review_target.py sends writes there to the owner like it does for global-memory/ and design.md;
it only ADDS) and `id_policy` ({"scheme": "none" | "dendron-23char"}). With `dendron-23char` a note
gt CREATES gets a 23-character lowercase id built from the millisecond clock plus `created` and
`updated` in milliseconds; a note that already exists is never renamed, re-id'd or rewritten.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from gt_review_target import fold                              # noqa: E402  one folding, shared

MANIFEST_NAME = "vault.manifest.json"
VERSION = 1
KNOWN_KEYS = {"manifest_version", "queue_dir", "broker_dir", "tools_dir", "protected",
              "audit_exempt_prefixes", "md_only", "naming", "frontmatter_dialect", "layers", "id_policy"}
NAMINGS = ("foldered", "dendron", "hybrid")
DIALECTS = ("gt", "obsidian", "dendron", "plain")
ID_SCHEMES = ("none", "dendron-23char")
LAYER_KEYS = {"global_memory", "design"}
PROTECT_KEYS = {"exact", "prefixes", "names"}

# The built-ins. gt_write_queue.REFUSED_*/GENERATED_NAMES/QUEUE_REL and gt_broker.BROKER_REL/TOOLS_REL/
# AUDIT_* keep their names and are checked equal to these by the tests.
QUEUE_REL = Path("Projects") / "golden-thread" / "spool" / "queue"
BROKER_REL = Path("Projects") / "golden-thread" / "spool" / "broker"
TOOLS_REL = Path("Projects") / "golden-thread" / "tools"
GENERATED_NAMES = frozenset({"decisions.md"})
REFUSED_EXACT = frozenset({"log.md", "TASKS.md"})
REFUSED_PREFIXES = ("Sources/", "core-rules/", "Projects/golden-thread/core-rules/",
                    "Projects/golden-thread/sessions/", "Projects/golden-thread/spool/",
                    "Projects/golden-thread/tools/")
AUDIT_EXEMPT_PREFIXES = ("Sources/", "core-rules/", "Projects/golden-thread/core-rules/",
                         "Projects/golden-thread/spool/", "Projects/golden-thread/sessions/",
                         "Projects/golden-thread/tools/")
AUDIT_GENERATED_NAMES = frozenset({"decisions.md", "review-queue.md"})
AUDIT_GENERATED_EXACT = frozenset({"log.md", "TASKS.md"})
# Never written through the queue, whatever the manifest says (compared component by component
# through gt_review_target.fold, so a case, Unicode, trailing-dot or 8.3 spelling cannot sneak by).
FLOOR_DIRS = ("sources", "core-rules", "projects/golden-thread/core-rules")
# A gt folder (queue_dir, broker_dir, tools_dir) may not be placed in these either, by its spelling
# or by the path it RESOLVES to (phase 84 review H2/H3): the floor above, plus the folders other
# gt tools own or every session loads. The built-in value of a key is the one exception (the
# built-in queue and broker sit in spool/). PLACE_NAMES may not be any component of such a folder.
PLACE_FLOOR = FLOOR_DIRS + ("global-memory", "projects/golden-thread/sessions",
                            "projects/golden-thread/spool")
PLACE_NAMES = ("core-rules", "design.md", "log.md", "tasks.md", "decisions.md", "review-queue.md")
# What a gt folder named by the manifest may already hold (review M4): nothing of the owner's. A
# folder holding anything else is refused, because the queue takes every *.json in its folder as a
# request and moves the bad ones away. Dot-files (locks, temp files, .DS_Store) are always allowed.
GT_FOLDER_FILES = {
    "queue_dir": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.json$"),
    "broker_dir": re.compile(r"^log-\d{4}-\d{2}-\d{2}\.jsonl$"),
    "tools_dir": re.compile(r"^(gt_[A-Za-z0-9_]+|safe_write)\.py(\.orig)?$"),
}
GT_FOLDER_DIRS = {"queue_dir": set(), "broker_dir": {"conflicts", "rejected"},
                  "tools_dir": {"__pycache__"}}
MAX_MANIFEST = 256 * 1024
_O_READ = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
           | getattr(os, "O_NOFOLLOW", 0))


_SHORT = re.compile(r"^([^~]{1,6})~\d+(?:\.([^.]{0,3}))?$")


def _short_names(component: str, long: str) -> bool:
    """True when folded `component` is a Windows 8.3 short name (`SOURCE~1`, `CORE-R~1`,
    `DECISI~1.MD`) that may stand for folded `long`. Conservative: any short name whose stem and
    extension are prefixes of the long name's counts."""
    m = _SHORT.match(component)
    if not m:
        return False
    base, dot, ext = long.rpartition(".")
    if not dot:
        base, ext = long, ""
    clean = lambda s: re.sub(r"[ .+,;=\[\]]", "", s)          # noqa: E731  what 8.3 drops
    return clean(base).startswith(m.group(1)) and clean(ext).startswith(m.group(2) or "") \
        and bool(ext) == (m.group(2) is not None)


def same_name(component: str, protected: str) -> bool:
    """One path component against one protected name, as the file systems compare them."""
    a, b = fold(component), fold(protected)
    return a == b or _short_names(a, b)


def split_rel(rel: str) -> list:
    return [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]


def under(parts, prefix) -> bool:
    """True when the component list `parts` starts with the components of `prefix` (a
    vault-relative path, a trailing slash allowed), compared by same_name."""
    pre = split_rel(prefix) if isinstance(prefix, str) else list(prefix)
    return bool(pre) and len(parts) >= len(pre) and all(same_name(a, b) for a, b in zip(parts, pre))


def resolved_parts(vault, parts):
    """-> the components of where vault-relative `parts` really lead (links followed), or None
    when that is outside the vault (or the vault itself)."""
    root = os.path.realpath(os.fspath(vault))
    real = os.path.realpath(os.path.join(root, *parts)) if parts else root
    try:
        if os.path.commonpath([root, real]) != root or real == root:
            return None
    except ValueError:
        return None
    return split_rel(os.path.relpath(real, root).replace(os.sep, "/"))


@dataclass(frozen=True)
class Manifest:
    queue_rel: Path
    broker_rel: Path
    tools_rel: Path
    refused_exact: frozenset
    generated_names: frozenset
    refused_prefixes: tuple
    audit_exempt_prefixes: tuple
    audit_generated_names: frozenset
    audit_generated_exact: frozenset
    md_only: bool = True
    source: str = field(default="default", compare=False)    # "default" | "manifest"
    problem: str | None = field(default=None, compare=False)   # why a present file was ignored
    naming: str = "foldered"
    frontmatter_dialect: str = "gt"
    id_scheme: str = "none"
    layer_global_memory: tuple = ()      # extra review-target top folders (added to global-memory/)
    layer_design: tuple = ()             # extra review-target file names (added to design.md)


def defaults(problem: str | None = None) -> Manifest:
    return Manifest(QUEUE_REL, BROKER_REL, TOOLS_REL, REFUSED_EXACT, GENERATED_NAMES,
                    REFUSED_PREFIXES, AUDIT_EXEMPT_PREFIXES, AUDIT_GENERATED_NAMES,
                    AUDIT_GENERATED_EXACT, True, "default", problem)


class Rejected(Exception):
    pass


_cache: dict = {}
_warned: set = set()


def _reset_for_tests() -> None:
    _cache.clear()
    _warned.clear()
    _last_ms[0] = 0
    _used_ids.clear()


def _strings(val, what: str, *, name_only: bool = False) -> list:
    if not isinstance(val, list) or not all(isinstance(s, str) and s.strip() for s in val):
        raise Rejected("%s must be a list of non-empty strings" % what)
    for s in val:
        if "\\" in s or "\0" in s or s.startswith("/") or re.match(r"^[A-Za-z]:", s) or ".." in s.split("/"):
            raise Rejected("%s holds a path that is not vault-relative: %r" % (what, s))
        if name_only and "/" in s:
            raise Rejected("%s holds a name with a slash: %r" % (what, s))
    return val


def _dir(vault: Path, val, what: str, built: Path | None = None) -> Path:
    if not isinstance(val, str) or not val.strip():
        raise Rejected("%s must be a non-empty string" % what)
    if "\\" in val or "\0" in val or val.startswith("/") or re.match(r"^[A-Za-z]:", val):
        raise Rejected("%s must be relative to the vault, with forward slashes" % what)
    parts = val.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise Rejected("%s may not contain empty, `.`, `..` or dot-prefixed segments" % what)
    real = resolved_parts(vault, parts)
    if real is None:
        raise Rejected("%s resolves outside the vault" % what)
    is_built = built is not None and (under(parts, built.as_posix()) and len(parts) == len(built.parts))
    # The spelling AND the resolved path, each component folded: `Sources./q`, `SOURCE~1/q` and a
    # link `Knowledge/cr -> ../core-rules` all land in the floor (review H2/H3).
    for shape in (parts, real):
        if is_built and under(shape, built.as_posix()):
            continue
        for f in PLACE_FLOOR:
            if under(shape, f):
                raise Rejected("%s may not sit inside %s/" % (what, f))
        for c in shape:
            if any(same_name(c, n) for n in PLACE_NAMES):
                raise Rejected("%s may not pass through a folder named like %s" % (what, _shown(c)))
    if not is_built:
        _gt_folder_only(vault, parts, what)
    return Path(*parts)


def _gt_folder_only(vault: Path, parts, what: str) -> None:
    """Refuse a folder that already holds something the queue, broker or tools did not put there
    (review M4): `queue_dir: Knowledge` would make every note of the owner's a request."""
    d = os.path.join(os.fspath(vault), *parts)
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return
    except NotADirectoryError:
        raise Rejected("%s names a file, not a folder" % what)
    except OSError as e:
        raise Rejected("%s cannot be listed: %s" % (what, e.strerror or e))
    files, dirs = GT_FOLDER_FILES[what], GT_FOLDER_DIRS[what]
    for n in names:
        if n.startswith("."):
            continue
        full = os.path.join(d, n)
        if os.path.isdir(full) and not os.path.islink(full):
            ok = n in dirs
        else:
            ok = os.path.isfile(full) and not os.path.islink(full) and bool(files.match(n))
        if not ok:
            raise Rejected("%s already holds %r, which gt did not put there; name an absent or "
                           "empty folder" % (what, n))


def _shown(key) -> str:
    """A manifest key for a message: repr'd, every control character escaped and the length
    capped, so an ESC sequence in a hostile key never reaches the terminal (review L1)."""
    r = repr(key)
    r = "".join(c if c.isprintable() else "\\x%02x" % ord(c) if ord(c) < 256 else "\\u%04x" % ord(c)
                for c in r)
    return r if len(r) <= 80 else r[:77] + "..."


def _choice(val, allowed: tuple, what: str) -> str:
    if not isinstance(val, str) or val not in allowed:
        raise Rejected("%s must be one of %s" % (what, ", ".join(allowed)))
    return val


def _parse(vault: Path, doc) -> Manifest:
    if not isinstance(doc, dict):
        raise Rejected("the manifest must be a JSON object")
    if doc.get("manifest_version") != VERSION or isinstance(doc.get("manifest_version"), bool):
        raise Rejected("manifest_version %r is not the supported %d" % (doc.get("manifest_version"), VERSION))
    unknown = sorted(set(doc) - KNOWN_KEYS)
    if unknown:
        raise Rejected("unknown key %s (a manifest may add protection, never remove it)"
                       % _shown(unknown[0]))
    if "md_only" in doc and doc["md_only"] is not True:
        raise Rejected("md_only cannot be switched off")
    d = defaults()
    queue, broker, tools = d.queue_rel, d.broker_rel, d.tools_rel
    if "queue_dir" in doc:
        queue = _dir(vault, doc["queue_dir"], "queue_dir", QUEUE_REL)
    if "broker_dir" in doc:
        broker = _dir(vault, doc["broker_dir"], "broker_dir", BROKER_REL)
    if "tools_dir" in doc:
        tools = _dir(vault, doc["tools_dir"], "tools_dir", TOOLS_REL)
    # the three gt folders stay apart: no one of them inside another
    trio = (("queue_dir", queue), ("broker_dir", broker), ("tools_dir", tools))
    for i, (wa, a) in enumerate(trio):
        for wb, b in trio[i + 1:]:
            if under(list(a.parts), b.as_posix()) or under(list(b.parts), a.as_posix()):
                raise Rejected("%s and %s overlap" % (wa, wb))
    exact, names, prefixes = set(d.refused_exact), set(d.generated_names), list(d.refused_prefixes)
    if "protected" in doc:
        p = doc["protected"]
        if not isinstance(p, dict) or not p:
            raise Rejected("protected must be an object of exact/prefixes/names to add")
        if set(p) - PROTECT_KEYS:
            raise Rejected("protected holds %s; only exact, prefixes and names can be added"
                           % _shown(sorted(set(p) - PROTECT_KEYS)[0]))
        exact |= set(_strings(p.get("exact", []), "protected.exact"))
        names |= set(_strings(p.get("names", []), "protected.names", name_only=True))
        for s in _strings(p.get("prefixes", []), "protected.prefixes"):
            prefixes.append(s if s.endswith("/") else s + "/")
    # a moved gt folder is as untouchable through the queue as the built-in one
    for moved, built in ((queue, QUEUE_REL), (broker, BROKER_REL), (tools, TOOLS_REL)):
        if moved != built:
            prefixes.append(moved.as_posix() + "/")
    audit = list(d.audit_exempt_prefixes)
    if "audit_exempt_prefixes" in doc:
        audit += _strings(doc["audit_exempt_prefixes"], "audit_exempt_prefixes")
    for moved, built in ((queue, QUEUE_REL), (broker, BROKER_REL), (tools, TOOLS_REL)):
        if moved != built:
            audit.append(moved.as_posix() + "/")
    naming, dialect, scheme = d.naming, d.frontmatter_dialect, d.id_scheme
    if "naming" in doc:
        naming = _choice(doc["naming"], NAMINGS, "naming")
    if "frontmatter_dialect" in doc:
        dialect = _choice(doc["frontmatter_dialect"], DIALECTS, "frontmatter_dialect")
    if "id_policy" in doc:
        ip = doc["id_policy"]
        if not isinstance(ip, dict) or set(ip) != {"scheme"}:
            raise Rejected('id_policy must be {"scheme": "none" | "dendron-23char"}')
        scheme = _choice(ip["scheme"], ID_SCHEMES, "id_policy.scheme")
    gm, design = (), ()
    if "layers" in doc:
        ly = doc["layers"]
        if not isinstance(ly, dict) or not ly or set(ly) - LAYER_KEYS:
            raise Rejected("layers holds only global_memory and design, each a list of names to add")
        gm = tuple(_strings(ly.get("global_memory", []), "layers.global_memory", name_only=True))
        design = tuple(_strings(ly.get("design", []), "layers.design", name_only=True))
    return Manifest(queue, broker, tools, frozenset(exact), frozenset(names),
                    tuple(dict.fromkeys(prefixes)), tuple(dict.fromkeys(audit)),
                    d.audit_generated_names, d.audit_generated_exact, True, "manifest", None,
                    naming, dialect, scheme, gm, design)


def _warn_once(vault: Path, why: str) -> None:
    key = (str(vault), why)
    if key in _warned:
        return
    _warned.add(key)
    try:
        print("gt: ignoring %s in %s (%s); using the built-in layout" % (MANIFEST_NAME, vault, why),
              file=sys.stderr)
    except Exception:                                           # a closed stderr never fails a write
        pass


def read_regular(path, cap: int) -> bytes:
    """-> the bytes of `path`, opened only as a regular file and never blocking: a symlink is
    refused (lstat, and O_NOFOLLOW where the OS has it), a FIFO, device or socket is refused
    before and after the open (O_NONBLOCK, then fstat), and a file longer than `cap` is refused.
    Raises OSError or Rejected (review M3)."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise Rejected("a symlink, which is not followed")
    if not stat.S_ISREG(st.st_mode):
        raise Rejected("not a regular file")
    fd = os.open(path, _O_READ)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Rejected("not a regular file")
        if st.st_size > cap:
            raise Rejected("larger than %d KB" % (cap // 1024))
        chunks, left = [], cap + 1
        while left > 0:
            b = os.read(fd, min(left, 65536))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
        if len(data) > cap:
            raise Rejected("larger than %d KB" % (cap // 1024))
        return data
    finally:
        os.close(fd)


def load(vault) -> Manifest:
    """-> the layout in effect for this vault. Never raises; never returns anything lower than the floor."""
    vault = Path(vault)
    f = vault / MANIFEST_NAME
    try:
        st = os.lstat(f)                    # the file itself: a symlink is refused, not followed
        sig = (st.st_mtime_ns, st.st_size, st.st_mode)
    except FileNotFoundError:
        return defaults()
    except OSError as e:
        sig, why = None, "unreadable: %s" % (e.strerror or e)
        return _remember(vault, sig, defaults(why))
    key = str(vault)
    hit = _cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        try:
            raw = read_regular(f, MAX_MANIFEST).decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise Rejected("unreadable: %s" % (getattr(e, "strerror", None) or e))
        try:
            doc = json.loads(raw)
        except ValueError:
            raise Rejected("not valid JSON")
        m = _parse(vault, doc)
    except Rejected as e:
        m = defaults(str(e))
    except Exception as e:                                      # a surprise is still a fallback
        m = defaults("unexpected %s" % type(e).__name__)
    return _remember(vault, sig, m)


def _remember(vault: Path, sig, m: Manifest) -> Manifest:
    _cache[str(vault)] = (sig, m)
    if m.problem:
        _warn_once(vault, m.problem)
    return m


# ---------------------------------------------------------------- id_policy (on create only) ----
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_last_ms = [0]
_used_ids: set = set()


def _b36(n: int, width: int) -> str:
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out.rjust(width, "0")


def _mint(now_ms=None):
    """-> (id, ms). 9 base-36 characters of the millisecond clock, then 14 random ones: 23 in all.
    The clock is forced strictly forward within a process, so two ids never share a prefix and the
    ids sort in creation order; a set backs it up against a random collision."""
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if ms <= _last_ms[0]:
        ms = _last_ms[0] + 1
    _last_ms[0] = ms
    while True:
        i = _b36(ms, 9) + "".join(secrets.choice(_B36) for _ in range(14))
        if i not in _used_ids:
            _used_ids.add(i)
            return i, ms


def new_note_id(vault, now_ms=None):
    """-> a fresh note id when this vault's id_policy asks for one, else None. Touches no file."""
    if load(vault).id_scheme != "dendron-23char":
        return None
    return _mint(now_ms)[0]


def new_note_frontmatter(vault, now_ms=None) -> str:
    """-> the `id:`, `created:` and `updated:` lines (one trailing newline each) for a note gt is
    CREATING, or "" under `scheme: none`. Never used on an existing note."""
    if load(vault).id_scheme != "dendron-23char":
        return ""
    i, ms = _mint(now_ms)
    return "id: %s\ncreated: %d\nupdated: %d\n" % (i, ms, ms)


# ------------------------------------------------------------------------- detect (read-only) ----
PRIVATE_NAMES = {"private", "journal", "personal", "secrets", "finance", "health"}
SPOOL_NAMES = {"spool", "queue"}
MEMORY_NAMES = {"memory", "memories", "globalmemory"}
_FM_ID = re.compile(r"^id:\s*([0-9A-Za-z]{23})\s*$", re.M)
_FM_MS = re.compile(r"^created:\s*(\d{13})\s*$", re.M)
SCAN_CAP = 20000      # notes looked at
SAMPLE_CAP = 300      # front-matter heads read
HEAD_BYTES = 2048


def _walk_notes(root: Path):
    """-> (notes, top_dirs, empty_top_dirs): vault-relative note paths (dot-folders skipped)."""
    notes, tops = [], set()
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if not d.startswith("."))
        rel = os.path.relpath(dp, root).replace(os.sep, "/")
        if rel == ".":
            tops = set(dns)
        for n in sorted(fns):
            if n.lower().endswith(".md") and len(notes) < SCAN_CAP:
                notes.append(n if rel == "." else (Path(rel) / n).as_posix())
    empty = set()
    for t in tops:
        try:
            if not os.listdir(root / t):
                empty.add(t)
        except OSError:
            pass
    return notes, tops, empty


def _note_head(real_root: str, path: Path):
    """-> the first HEAD_BYTES of a note as text, or None. A FIFO, device or other non-regular file
    is skipped without blocking, and a symlink is read only when it lands inside the vault
    (review M3): detect must never hang, and never read outside the vault it was pointed at."""
    try:
        target = os.path.realpath(path)
        if os.path.commonpath([real_root, target]) != real_root:
            return None
        return _head_bytes(target).decode("utf-8", "replace")
    except (OSError, ValueError, Rejected):
        return None


def _head_bytes(path: str) -> bytes:
    """`path` is already resolved, so O_NOFOLLOW refuses a link swapped in since."""
    fd = os.open(path, _O_READ)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Rejected("not a regular file")
        return os.read(fd, HEAD_BYTES)
    finally:
        os.close(fd)


def detect(vault) -> dict:
    """Infer a manifest from the vault's own files. READ-ONLY: it lists folders and opens a bounded
    sample of notes for reading, and writes, creates, renames and touches nothing. -> a dict of
    `naming`, `frontmatter_dialect`, `existing_manifest`, `basis` (what it was based on) and
    `proposed` (a manifest the reader accepts, holding only what differs from the defaults)."""
    root = Path(vault)
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    notes, tops, empty = _walk_notes(root)
    basis = ["%d Markdown note(s) outside dot-folders" % len(notes)]
    dotted = [n for n in notes if "." in Path(n).stem]
    foldered = [n for n in notes if "/" in n and "." not in Path(n).stem]
    share = (len(dotted) / len(notes)) if notes else 0.0
    if share >= 0.6:
        naming = "dendron"
    elif share >= 0.15 and len(dotted) >= 2 and foldered:
        naming = "hybrid"
    else:
        naming = "foldered"
    basis.append("naming %s: %d dotted-name note(s) (%.0f%%), %d note(s) in sub-folders with plain names"
                 % (naming, len(dotted), share * 100, len(foldered)))
    # front-matter dialect, from a bounded sample of heads
    heads = fm_id = fm_ms = 0
    real_root = os.path.realpath(root)
    for n in notes[:SAMPLE_CAP]:
        head = _note_head(real_root, root / n)
        if head is None:
            continue
        if not head.startswith("---"):
            continue
        heads += 1
        end = head.find("\n---", 3)
        fm = head[3:end] if end > 0 else head
        fm_id += bool(_FM_ID.search(fm))
        fm_ms += bool(_FM_MS.search(fm))
    gt_vault = (root / "core-rules").is_dir() or (root / "Projects" / "golden-thread").is_dir()
    if heads and fm_id * 2 >= heads and fm_ms * 2 >= heads:
        dialect = "dendron"
    elif gt_vault:
        dialect = "gt"
    elif (root / ".obsidian").is_dir():
        dialect = "obsidian"
    else:
        dialect = "plain"
    basis.append("front matter %s: %d of %d sampled note(s) carry a 23-character id, %d a millisecond "
                 "`created`; gt vault folders %s; .obsidian %s"
                 % (dialect, fm_id, heads, fm_ms, "present" if gt_vault else "absent",
                    "present" if (root / ".obsidian").is_dir() else "absent"))
    proposed: dict = {"manifest_version": VERSION}
    if naming != "foldered":
        proposed["naming"] = naming
    if notes and dialect != "gt":
        proposed["frontmatter_dialect"] = dialect
    if naming == "dendron" and dialect == "dendron":
        proposed["id_policy"] = {"scheme": "dendron-23char"}
        basis.append("id_policy: ids are 23 characters with millisecond created/updated, so new notes "
                     "are given the same")
    priv = sorted(t for t in tops if t.lower() in PRIVATE_NAMES)
    if priv:
        proposed["protected"] = {"prefixes": [t + "/" for t in priv]}
        basis.append("protected: top-level folder(s) named like private areas: %s" % ", ".join(priv))
    spool = sorted(t for t in empty if t.lower() in SPOOL_NAMES)
    if spool and not (root / MANIFEST_NAME).exists():
        t = spool[0]
        proposed["queue_dir"] = "%s/queue" % t
        proposed["broker_dir"] = "%s/broker" % t
        proposed["tools_dir"] = "%s/tools" % t
        basis.append("queue_dir/broker_dir/tools_dir: %s/ is an empty top-level folder, so it holds "
                     "nothing of yours" % t)
    mem = sorted(t for t in tops if t.lower().replace("-", "").replace("_", "") in MEMORY_NAMES
                 and t.lower() != "global-memory")
    if mem:
        proposed["layers"] = {"global_memory": mem}
        basis.append("layers.global_memory: %s looks like a memory layer, so writes there go to you "
                     "for review" % ", ".join(mem))
    if len(basis) == 3:
        basis.append("nothing else to propose: every other value stays the built-in default")
    out = {"naming": naming, "frontmatter_dialect": dialect,
           "existing_manifest": (root / MANIFEST_NAME).exists(), "basis": basis, "proposed": proposed}
    # a proposal this reader would refuse is a bug here, not something to hand the user
    try:
        _parse(root, proposed)
    except Rejected as e:                                      # pragma: no cover
        out["proposed"] = {"manifest_version": VERSION}
        out["basis"].append("a drafted key was dropped because the reader would refuse it: %s" % e)
    return out


def _vault_arg(given):
    cand = given or os.environ.get("GT_VAULT")
    if not cand:
        try:
            cand = json.loads((Path.home() / ".claude" / "vault-config.json")
                              .read_text(encoding="utf-8")).get("vault_path")
        except Exception:
            cand = None
    return cand


def _detect_main(argv) -> int:
    ap = argparse.ArgumentParser(prog="gt_vault_manifest.py detect",
                                 description="infer a vault layout manifest (read-only)")
    ap.add_argument("--vault")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    cand = _vault_arg(a.vault)
    if not cand:
        print("gt_vault_manifest: no vault found; pass --vault", file=sys.stderr)
        return 2
    try:
        r = detect(Path(cand).expanduser())
    except OSError as e:
        print("gt_vault_manifest: cannot read %s: %s" % (cand, e.strerror or e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(r, indent=2))
        return 0
    print("naming: %s\nfrontmatter_dialect: %s" % (r["naming"], r["frontmatter_dialect"]))
    if r["existing_manifest"]:
        print("a %s is already there; the proposal below is a comparison, not a replacement" % MANIFEST_NAME)
    print("based on:")
    for b in r["basis"]:
        print("  - " + b)
    print("proposed %s:" % MANIFEST_NAME)
    print(json.dumps(r["proposed"], indent=2))
    print("Nothing was written.")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "detect":
        return _detect_main(argv[1:])
    ap = argparse.ArgumentParser(description="print the vault layout in effect")
    ap.add_argument("--vault")
    ap.add_argument("--json", action="store_true", help="print the layout in effect as JSON")
    a = ap.parse_args(argv)
    cand = _vault_arg(a.vault)
    if not cand:
        print("gt_vault_manifest: no vault found; pass --vault", file=sys.stderr)
        return 2
    m = load(Path(cand).expanduser())
    if a.json:
        print(json.dumps({"source": m.source, "ignored": m.problem or None,
                          "queue_dir": m.queue_rel.as_posix(), "broker_dir": m.broker_rel.as_posix(),
                          "tools_dir": m.tools_rel.as_posix()}, indent=2))
        return 0
    print("layout source: %s%s" % (m.source, " (manifest ignored: %s)" % m.problem if m.problem else ""))
    print("queue_dir:  %s\nbroker_dir: %s\ntools_dir:  %s" % (m.queue_rel.as_posix(),
          m.broker_rel.as_posix(), m.tools_rel.as_posix()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
