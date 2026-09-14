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
exit is 2 — a check that could not look is not a clean result. When `$GT_PDF_HOST` (or
`pdf_host` in ~/.claude/vault-config.json) names a host that HAS pypdf, the text is
extracted there instead, so the machine that builds releases never needs it installed.
Pass --no-pdf to skip
them knowingly.

A missing or empty terms file is also exit 2, never "clean": with nothing to look for,
every tree passes, which is the failure this script exists to prevent.
"""
import argparse
import json
import os
import shlex
import re
import subprocess
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


def pdf_host():
    """Host to extract PDF text on when pypdf is not importable here.

    `$GT_PDF_HOST`, else `pdf_host` in ~/.claude/vault-config.json. Opt-in by
    design: this copies the file to another machine, which must be a deliberate
    choice rather than something a scrubber does on its own initiative.
    """
    h = os.environ.get("GT_PDF_HOST")
    if h:
        return h.strip()
    try:
        import json
        cfg = os.path.expanduser("~/.claude/vault-config.json")
        with open(cfg) as fh:
            return (json.load(fh).get("pdf_host") or "").strip() or None
    except Exception:
        return None


def _remote_pdf_text(path, host):
    """Extract a PDF's text on `host`, which has pypdf. -> (text, error).

    Why this exists: PDF text is font-subset encoded, so grep returns a FALSE CLEAN
    on it -- a scan of these files once passed six while two carried a leaked string.
    The check therefore needs a real PDF reader, and the one machine here that has
    one is not the machine that builds releases. Without this, every release scrub
    reported five files UNSCANNED, and an unscanned file is not a clean file.

    Call pypdf by absolute interpreter and let ssh fail loudly rather than probing
    with `command -v` first: on this host a non-interactive `command -v` has reported
    tools missing that were present, so the probe is less reliable than the attempt.
    """
    import shutil
    import subprocess
    import uuid
    if not shutil.which("scp") or not shutil.which("ssh"):
        return None, "ssh/scp not available for remote PDF scan"
    remote = "/tmp/gt-scrub-%s.pdf" % uuid.uuid4().hex[:12]
    try:
        r = subprocess.run(["scp", "-q", "-o", "ConnectTimeout=10", str(path),
                            "%s:%s" % (host, remote)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return None, "scp to %s failed: %s" % (host, (r.stderr or "").strip()[:120])
        prog = ("import sys\n"
                "from pypdf import PdfReader\n"
                "print('\\n'.join((p.extract_text() or '') for p in "
                "PdfReader(sys.argv[1]).pages))")
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host,
                            "python3 -c %s %s" % (shlex.quote(prog), shlex.quote(remote))],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return None, "pypdf on %s failed: %s" % (host, (r.stderr or "").strip()[:120])
        return r.stdout, None
    except subprocess.TimeoutExpired:
        return None, "remote PDF scan timed out"
    except Exception as exc:
        return None, "remote PDF scan error: %s" % exc
    finally:
        try:
            subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, "rm", "-f", remote],
                           capture_output=True, timeout=60)
        except Exception:
            pass


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
                # Not importable here. Fall back to a host that has it, when one is
                # configured -- reporting UNSCANNED is correct but useless on a
                # machine that will never have pypdf installed.
                host = pdf_host()
                if not host:
                    return [], ("pypdf not importable (set GT_PDF_HOST, or pdf_host "
                                "in ~/.claude/vault-config.json, to scan remotely)")
                text, err = _remote_pdf_text(path, host)
                if err:
                    return [], err
                return [(str(path), ln, p) for ln, p in _hits(text, pats)], None
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


def repo_files(repo):
    """Every file a push of `repo` would publish: tracked, plus untracked-but-not-ignored.

    Why the whole repository and not a list of directories: until 0.12.9 the release gate
    scrubbed only paths under golden-thread-plugin/, so everything at the repository root
    -- CHANGELOG.md, README.md, docs/, and a sync handoff naming internal systems -- was
    published to the public remote without ever being scanned. Found 2026-09-13 with the
    file already live. A path list is a list of what someone remembered; `git ls-files`
    is what actually ships. Untracked files are included so a new file in a release that
    is not committed yet is scanned before the commit, not after the push.

    Returns None when git cannot enumerate -- the caller reports that as exit 2, never
    as clean.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    root = Path(repo)
    files = []
    for rel in out.decode("utf-8", "surrogateescape").split("\0"):
        if not rel:
            continue
        p = root / rel
        if p.is_file() and not rel.endswith((".pyc", ".DS_Store")):   # tracked-but-deleted in the tree: nothing to publish
            files.append(p)
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan for employer/machine-specific strings.")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--repo", help="scan every file a push of this git repository publishes "
                                   "(tracked + untracked-not-ignored), from its root")
    ap.add_argument("--no-pdf", action="store_true", help="skip PDFs knowingly")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    if not a.paths and not a.repo:
        ap.error("give paths to scan, or --repo <dir>")
    pats = load_terms()
    hits, unscanned, n = [], [], 0
    targets = list(walk(a.paths))
    if a.repo:
        rf = repo_files(a.repo)
        if rf is None:
            print(f"UNSCANNED {a.repo}  (git could not list the repository)")
            return 2
        targets += rf
    for f in targets:
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
