#!/usr/bin/env python3
"""scrub_check.py — prove a tree carries no employer- or machine-specific strings.

This repo is public. The strings it must never contain are, by their nature, not
something the repo can list — a scrub list naming the employer would itself be the
leak. So the TERMS live outside the repo, and this script only knows where to find
them:

  1. $GT_SCRUB_TERMS, if set, else
  2. <vault_path>/Projects/golden-thread/scrub-terms.txt  (vault_path from
     ~/.claude/vault-config.json — the vault is a private repo)

One regular expression per line, matched case-insensitively; a line starting with
`#` is a comment (whole lines only — a pattern may itself contain `#`).

  scrub_check.py <path> [<path> ...]        # exit 0 clean, 1 hits, 2 cannot check

It scans text files, the members of .zip archives (deflated, so grep cannot see in),
and the extracted text of PDFs (font-subset encoded, so grep returns a false clean).
PDF text needs `pypdf`; when it is not importable, PDFs are reported UNSCANNED and the
exit is 2 — a check that could not look is not a clean result. Pass --no-pdf to skip
them knowingly.

A missing or empty terms file is also exit 2, never "clean": with nothing to look for,
every tree passes, which is the failure this script exists to prevent.
"""
import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".DS_Store"}


def terms_path():
    env = os.environ.get("GT_SCRUB_TERMS")
    if env:
        return Path(env).expanduser()
    try:
        cfg = json.loads((Path.home() / ".claude" / "vault-config.json").read_text())
        return Path(cfg["vault_path"]) / "Projects" / "golden-thread" / "scrub-terms.txt"
    except Exception:
        return None


def load_terms(required=True):
    p = terms_path()
    pats = []
    if p and p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(re.compile(line, re.I))
    if required and not pats:
        sys.stderr.write(f"scrub_check: no terms loaded from {p or '(no path)'} — cannot check\n")
        sys.exit(2)
    return pats


def _hits(text, pats):
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        for p in pats:
            if p.search(line):
                out.append((i, p.pattern))
    return out


def scan_file(path, pats, pdf=True):
    """-> (list of (location, line, pattern), unscanned_reason or None)."""
    found = []
    suf = path.suffix.lower()
    try:
        if suf == ".zip":
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    data = zf.read(info.filename).decode("utf-8", "replace")
                    found += [(f"{path}!{info.filename}", ln, p) for ln, p in _hits(data, pats)]
            return found, None
        if suf == ".pdf":
            if not pdf:
                return [], "skipped (--no-pdf)"
            try:
                from pypdf import PdfReader
            except ImportError:
                return [], "pypdf not importable"
            text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
            return [(str(path), ln, p) for ln, p in _hits(text, pats)], None
        raw = path.read_bytes()
        if b"\0" in raw[:4096]:
            return [], None                       # other binary: nothing readable to leak
        return [(str(path), ln, p) for ln, p in _hits(raw.decode("utf-8", "replace"), pats)], None
    except Exception as e:
        return [], f"error: {e}"


def walk(paths):
    for root in paths:
        root = Path(root)
        if root.is_file():
            yield root
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.endswith((".pyc", ".DS_Store")):
                    continue
                yield Path(dirpath) / f


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan for employer/machine-specific strings.")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--no-pdf", action="store_true", help="skip PDFs knowingly")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    pats = load_terms()
    hits, unscanned, n = [], [], 0
    for f in walk(a.paths):
        n += 1
        h, why = scan_file(f, pats, pdf=not a.no_pdf)
        hits += h
        if why and not why.startswith("skipped"):
            unscanned.append((str(f), why))
    for loc, ln, p in hits:
        print(f"HIT   {loc}:{ln}  /{p}/")
    for loc, why in unscanned:
        print(f"UNSCANNED {loc}  ({why})")
    if not a.quiet or hits or unscanned:
        print(f"scrub_check: {n} files, {len(pats)} terms, {len(hits)} hit(s), {len(unscanned)} unscanned")
    if hits:
        return 1
    return 2 if unscanned else 0


if __name__ == "__main__":
    sys.exit(main())
