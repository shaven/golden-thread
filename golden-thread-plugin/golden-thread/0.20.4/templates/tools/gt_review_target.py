#!/usr/bin/env python3
"""gt_review_target -- is this vault path a REVIEW TARGET, decided by IDENTITY, not spelling (0.20.1).

Shipped twice, byte for byte: in gt's scripts/ (the hooks dir, where gt_broker.py runs from) and
in templates/tools/ (the vault's Projects/golden-thread/tools/), so a vault tool can import it
from beside itself. tests/test_gt_review_target.py fails the build when the two copies differ.

WHY. The write broker never writes a `design.md` or anything under the vault's `global-memory/`:
those go to the owner as a #conflict task. Until 0.20.1 it decided that with an exact-case string
match (`name == "design.md"`, `rel.startswith("global-memory/")`). The independent re-review of
the 0.20.2 sandbox tools showed, on macOS APFS (case-insensitive, Unicode-normalising), that all
of these were APPLIED instead of escalated, through the real queue and the real sandbox inbox:

    Projects/alpha/DESIGN.md            another spelling of one file
    Projects/alpha/de<U+017F>ign.md       U+017F LONG S: os.path.samefile() with design.md is True
    GLOBAL-MEMORY/MEMORY.md             another spelling of one directory
    Projects/alpha/gmlink/MEMORY.md     gmlink is a symlink to global-memory/
    Projects/alpha/link.md              a symlink to design.md

A name is a claim about a file; the file is what matters. So this module asks two questions of
the real path, and either answer is enough:

  IDENTITY  os.path.samefile() / the (st_dev, st_ino) of every existing prefix of the path
            against <vault>/global-memory, and of the target against its sibling design.md --
            whatever spelling, link or volume rule got the caller there.
  SPELLING  every component of the path, AND of its fully resolved form, folded the way the
            file systems fold names before comparing: Unicode NFKC, casefold, zero-width and
            other format characters removed, surrounding whitespace and trailing dots and spaces
            stripped, an NTFS `::$DATA` stream suffix dropped, and a Windows 8.3 short name
            (`DESIGN~1.MD`, `GLOBAL~1`) read as the long name it stands for. This layer
            applies on every platform, so a name that would be the review target on a
            case-insensitive volume is treated as one on a case-sensitive volume too. That is
            deliberately conservative: a file spelled `de<U+017F>ign.md` on Linux is a different file,
            and is escalated to the owner all the same.

What it does NOT match: `xdesign.md`, `design.md.bak`, `design-notes.md`, `global-memory-x/`,
`notes/global-memory/x.md` -- a review target is a file NAMED design.md (any spelling), or a path
whose first component, at the vault root, is global-memory (any spelling or link).

SYMLINKS. A symlink in the path is refused for review when it cannot be shown harmless: one that
dangles (its target does not exist, so what it will become is unknown), or one that leaves the
directory it sits in (a link that reaches out of its own project or folder is how a write lands
somewhere other than the path says). A link that stays inside its own directory is judged by the
resolved path like any other.

Public:  review_target(vault, rel) -> None | (kind, why)
         kind is "design.md", "global-memory/" or "symlink". Stdlib only; never raises.
         same_path(vault, a, b) / claim_covers(vault, claimed, rel) -> bool: the same identity
         and folding for Core rule 1's claims (see "claim matching" at the end of this file).
"""
from __future__ import annotations

import os
import re
import unicodedata

DESIGN = "design.md"
GLOBAL_MEMORY = "global-memory"

_STREAM = re.compile(r"(::\$data|:\$data)$", re.I)
_SHORT_DESIGN = re.compile(r"^design~\d+\.md$")
_SHORT_GM = re.compile(r"^global~\d+$")


def fold(name: str) -> str:
    """One path component as the file systems compare it (see the module docstring)."""
    s = unicodedata.normalize("NFKC", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Cf")
    s = s.casefold().strip()
    s = _STREAM.sub("", s)
    s = s.rstrip(". ")
    return s.strip()


def _is_design(component: str) -> bool:
    f = fold(component)
    return f == DESIGN or bool(_SHORT_DESIGN.match(f))


def _is_gm(component: str) -> bool:
    f = fold(component)
    return f == GLOBAL_MEMORY or bool(_SHORT_GM.match(f))


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _spelling(parts) -> tuple | None:
    """-> (kind, why) when the vault-relative `parts` spell a review target."""
    if parts and _is_gm(parts[0]) and len(parts) > 1:
        return ("global-memory/", "global-memory/")
    if parts and _is_design(parts[-1]):
        return ("design.md", "design.md")
    return None


def review_target(vault, rel: str):
    try:
        return _review_target(os.fspath(vault), rel)
    except Exception:                                           # noqa: BLE001
        # Undecidable is not "fine": a path this cannot examine goes to the owner.
        return ("symlink", "its identity could not be established")


def _review_target(vault: str, rel: str):
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    hit = _spelling(parts)
    if hit:
        return hit
    root = os.path.realpath(vault)
    gm = os.path.join(vault, GLOBAL_MEMORY)

    # Walk the path one component at a time: identity of each prefix, and every link's reach.
    cur = vault
    for i, comp in enumerate(parts):
        nxt = os.path.join(cur, comp)
        if os.path.islink(nxt):
            real = os.path.realpath(nxt)
            if not os.path.exists(real):
                return ("symlink", "%s is a dangling symlink" % "/".join(parts[:i + 1]))
            here = os.path.realpath(cur)
            if os.path.commonpath([here, real]) != here:
                return ("symlink", "%s is a symlink that leaves its own directory"
                        % "/".join(parts[:i + 1]))
        if os.path.exists(nxt) and os.path.isdir(gm) and i < len(parts) - 1 and _same(nxt, gm):
            return ("global-memory/", "global-memory/")
        cur = nxt

    full = os.path.join(vault, *parts) if parts else vault
    # The fully resolved path, spelled out: a link to design.md or into global-memory/.
    real = os.path.realpath(full)
    try:
        if os.path.commonpath([root, real]) == root and real != root:
            rparts = os.path.relpath(real, root).replace(os.sep, "/").split("/")
            hit = _spelling(rparts)
            if hit:
                return hit
            if os.path.isdir(gm) and _same(os.path.dirname(real), gm):
                return ("global-memory/", "global-memory/")
    except ValueError:
        pass
    # The target against the design.md beside it (a name the string layer could not fold).
    if os.path.exists(full):
        sib = os.path.join(os.path.dirname(full), DESIGN)
        if os.path.exists(sib) and _same(full, sib):
            return ("design.md", "design.md")
    return None


# ------------------------------------------------------------------ claim matching ----
#
# Core rule 1's claims name files as strings (`- `Projects/a/research.md``). Compared as strings,
# `PROJECTS/A/Research.md`, a long-s spelling, a symlink to the file, or a trailing-dot name is a
# DIFFERENT file than the claimed one, though on a case-insensitive or normalising volume it is the
# same, so a write through a variant walked past another live session's claim (0.20.1). Claims are
# now compared by the same identity and folding as the review targets above.

def _fold_parts(path: str) -> list:
    return [fold(p) for p in path.replace("\\", "/").split("/") if p not in ("", ".")]


def same_path(vault, a: str, b: str) -> bool:
    """True when vault-relative `a` and `b` name one file: equal after folding every component, or
    the same inode (both exist), or equal after resolving links. Never raises."""
    try:
        if _fold_parts(a) == _fold_parts(b):
            return True
        vault = os.fspath(vault)
        pa, pb = os.path.join(vault, *a.replace("\\", "/").split("/")), \
            os.path.join(vault, *b.replace("\\", "/").split("/"))
        if os.path.exists(pa) and os.path.exists(pb) and _same(pa, pb):
            return True
        root = os.path.realpath(vault)
        ra, rb = os.path.realpath(pa), os.path.realpath(pb)
        return (os.path.commonpath([root, ra]) == root and os.path.commonpath([root, rb]) == root
                and _fold_parts(os.path.relpath(ra, root).replace(os.sep, "/"))
                == _fold_parts(os.path.relpath(rb, root).replace(os.sep, "/")))
    except Exception:                                           # noqa: BLE001
        return False


def claim_covers(vault, claimed: str, rel: str) -> bool:
    """True when a claim on `claimed` (a file, or a directory prefix) covers vault-relative `rel`:
    `rel` is `claimed` or below it, by identity or by folded spelling."""
    try:
        claimed = claimed.strip().rstrip("/")
        if not claimed:
            return False
        if same_path(vault, claimed, rel):
            return True
        cp, rp = _fold_parts(claimed), _fold_parts(rel)
        if rp[:len(cp)] == cp and len(rp) > len(cp):
            return True
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        for i in range(1, len(parts)):                          # a claimed directory, by inode
            if same_path(vault, claimed, "/".join(parts[:i])):
                return True
    except Exception:                                           # noqa: BLE001
        pass
    return False
