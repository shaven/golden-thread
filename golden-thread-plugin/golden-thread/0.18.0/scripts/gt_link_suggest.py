#!/usr/bin/env python3
"""gt_link_suggest.py -- propose the missing (cross-domain first) links for a Knowledge page.

    gt_link_suggest.py suggest --vault V --page Knowledge/P.md [--limit 5] [--json]
    gt_link_suggest.py apply   --vault V --page Knowledge/P.md --to TARGET [--to TARGET ...]
                               [--forward-only] [--dry-run] [--json]

WHY THIS EXISTS (0.18.0, request 2026-09-24-cross-domain-link-suggestions). CONVENTIONS says
cross-domain links are the most valuable and the most often missed: they are what lets a
question hop from one area of the vault to another. The lint finds dangling and orphaned links,
and nothing helped CREATE the missing ones -- a session writing a new page rarely knows which
pages elsewhere in the vault should point back at it.

SUGGEST reads the page's title, tags and the first 300 characters of its body, then the
FRONTMATTER ONLY (title, tags, category, domain) of every other Knowledge page -- it stops
reading each file at the closing `---`, so the pass costs the same whatever the pages' length.
A candidate needs at least one shared tag or title keyword. It is ranked:

    1. cross-domain candidates first (a different domain from the page),
    2. then by score: 3 per shared tag, 1 per shared title keyword,
    3. then by title.

A page's DOMAIN is its frontmatter `domain:` if present, else the `## ` heading of index.md it
is listed under, else its first tag. Pages already linked from the page are never suggested.
Each suggestion names the candidate, the reason (the shared tags / keywords) and the link
type: `cross-domain` across domains; within one, `hub` (a hub/index/MOC page), `upstream`
(candidate is more general: concept > reference > runbook), `downstream` (more specific) or
`sibling` (same category). Up to --limit (default 5). No candidates prints nothing and exits 0,
so a caller can skip the pass silently.

APPLY writes only what the user selected: each --to is a candidate the user picked (by file
stem, title or path). For each, the page gains `- [[Target]]` in its `## Related` section and,
unless --forward-only, the target gains `- [[Page]]` in its own -- the back-link is part of the
selection the user made, never added on its own. A link already present on either side is not
written again. Every write is a write-queue `append` to the `Related` section (Core rule 1),
submitted all-or-nothing and drained once. Nothing is written without --to.

Exit: 0 ok (including "no candidates") | 1 a write was refused | 2 usage, no vault, no page.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINK = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
WORD = re.compile(r"[a-z0-9][a-z0-9+-]{3,}")
STOP = {"with", "from", "that", "this", "what", "when", "where", "which", "page", "pages",
        "into", "about", "your", "their", "them", "they", "have", "does", "more", "than",
        "only", "each", "every", "over", "under", "after", "before", "notes", "note", "and",
        "the", "for", "how", "why"}
LEVEL = {"concept": 0, "reference": 1, "runbook": 2}
HUB_TAGS = {"hub", "index", "moc", "map"}
BODY_CHARS = 300


def _queue():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import gt_write_queue                                         # noqa: PLC0415
    return gt_write_queue


# ------------------------------------------------------------------ reading ----

def _parse_fm(lines):
    fm, key = {}, None
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            key, val = m.group(1), m.group(2).strip()
            if val.startswith("[") and val.endswith("]"):
                fm[key] = [x.strip().strip("'\"") for x in val[1:-1].split(",") if x.strip()]
            else:
                fm[key] = val.strip("'\"")
        elif key and line.lstrip().startswith("- "):
            if not isinstance(fm.get(key), list):
                fm[key] = []
            fm[key].append(line.lstrip()[2:].strip().strip("'\""))
    return fm


def read_frontmatter(path: Path) -> dict:
    """Frontmatter only: reading stops at the closing `---` (the pass reads no bodies)."""
    out = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            if fh.readline().strip() != "---":
                return {}
            for line in fh:
                if line.strip() == "---":
                    break
                out.append(line.rstrip("\n"))
                if len(out) > 200:              # a runaway frontmatter is not frontmatter
                    break
    except OSError:
        return {}
    return _parse_fm(out)


def read_page(path: Path):
    """The page being linked FROM: frontmatter, its existing link targets, first 300 chars."""
    text = path.read_text(encoding="utf-8", errors="replace")
    body = text
    fm = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = _parse_fm(text[3:end].splitlines())
            body = text[end + 4:]
    links = {norm(x) for x in LINK.findall(text)}
    return fm, links, body.strip()[:BODY_CHARS]


def norm(target: str) -> str:
    t = str(target).strip().strip("[]").split("|")[0].split("#")[0].split("/")[-1]
    if t.lower().endswith(".md"):
        t = t[:-3]
    return t.strip().lower()


def tags_of(fm) -> list[str]:
    t = fm.get("tags") or []
    if isinstance(t, str):
        t = [x.strip() for x in t.split(",") if x.strip()]
    return [str(x).strip().lower().lstrip("#") for x in t if str(x).strip()]


def words(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP}


def index_domains(vault: Path) -> dict:
    """page stem (normalised) -> the `## ` heading of index.md it is listed under."""
    out, heading = {}, None
    try:
        lines = (vault / "index.md").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            heading = m.group(1).strip()
            continue
        if heading:
            for t in LINK.findall(line):
                out.setdefault(norm(t), heading)
    return out


def domain_of(stem: str, fm: dict, idx: dict) -> str:
    d = fm.get("domain")
    if isinstance(d, str) and d.strip():
        return d.strip().lower()
    if norm(stem) in idx:
        return idx[norm(stem)].lower()
    t = tags_of(fm)
    return t[0] if t else "(none)"


def link_type(page_fm, cand_fm, cand_stem, same_domain) -> str:
    if not same_domain:
        return "cross-domain"
    ctags = set(tags_of(cand_fm))
    if ctags & HUB_TAGS or re.search(r"\b(index|hub|map of content|moc)\b", cand_stem.lower()):
        return "hub"
    a = LEVEL.get(str(page_fm.get("category", "")).strip().lower())
    b = LEVEL.get(str(cand_fm.get("category", "")).strip().lower())
    if a is None or b is None or a == b:
        return "sibling"
    return "upstream" if b < a else "downstream"


def resolve_page(vault: Path, page: str) -> Path | None:
    p = page.strip().strip("[]")
    if "/" not in p:
        p = "Knowledge/" + p
    if not p.endswith(".md"):
        p += ".md"
    f = vault / p
    return f if f.is_file() and p.startswith("Knowledge/") else None


def knowledge_files(vault: Path):
    kdir = vault / "Knowledge"
    if not kdir.is_dir():
        return []
    return sorted(f for f in kdir.glob("*.md") if f.name != "_template.md")


# ------------------------------------------------------------------ suggest ----

def suggest(vault: Path, page: Path, limit: int = 5) -> list[dict]:
    pfm, plinks, head = read_page(page)
    ptags = set(tags_of(pfm))
    ptitle = str(pfm.get("title") or page.stem)
    pwords = words(ptitle + " " + page.stem + " " + head)
    pwords_title = words(ptitle + " " + page.stem)
    idx = index_domains(vault)
    pdomain = domain_of(page.stem, pfm, idx)
    out = []
    for f in knowledge_files(vault):
        if f.resolve() == page.resolve():
            continue
        if norm(f.stem) in plinks:
            continue                               # already linked from the page
        cfm = read_frontmatter(f)
        ctitle = str(cfm.get("title") or f.stem)
        if norm(ctitle) in plinks:
            continue
        shared_tags = sorted(ptags & set(tags_of(cfm)))
        cwords = words(ctitle + " " + f.stem)
        shared_words = sorted((cwords & pwords) | (pwords_title & cwords))
        score = 3 * len(shared_tags) + len(shared_words)
        if not score:
            continue
        cdomain = domain_of(f.stem, cfm, idx)
        reason = []
        if shared_tags:
            reason.append("shared tag%s: %s" % ("s" if len(shared_tags) > 1 else "",
                                                ", ".join(shared_tags)))
        if shared_words:
            reason.append("title keyword%s: %s" % ("s" if len(shared_words) > 1 else "",
                                                   ", ".join(shared_words)))
        same = cdomain == pdomain
        out.append({"target": f.stem, "title": ctitle, "path": "Knowledge/%s" % f.name,
                    "domain": cdomain, "page_domain": pdomain, "score": score,
                    "reason": "; ".join(reason),
                    "type": link_type(pfm, cfm, f.stem, same)})
    out.sort(key=lambda c: (c["type"] != "cross-domain", -c["score"], c["title"].lower()))
    return out[:max(0, limit)]


# ------------------------------------------------------------------ apply ----

def _has_link(path: Path, stem: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return norm(stem) in {norm(x) for x in LINK.findall(text)}


def plan_apply(vault: Path, page: Path, picks: list[str], forward_only: bool):
    """-> (writes, notes, unknown). Every --to must be one of the vault's Knowledge pages."""
    by = {}
    for f in knowledge_files(vault):
        by[norm(f.stem)] = f
        t = read_frontmatter(f).get("title")
        if isinstance(t, str) and t.strip():
            by.setdefault(norm(t), f)
    writes, notes, unknown = [], [], []
    today = datetime.date.today().isoformat()
    for pick in picks:
        f = by.get(norm(pick))
        if f is None or f.resolve() == page.resolve():
            unknown.append(pick)
            continue
        if _has_link(page, f.stem):
            notes.append("%s already links [[%s]]" % (page.stem, f.stem))
        else:
            writes.append({"path": "Knowledge/%s" % page.name, "op": "append",
                           "section": "Related",
                           "content": "- [[%s]] — link added %s (gt_link_suggest)\n" % (f.stem, today),
                           "hint": "link %s -> %s (user-selected)" % (page.stem, f.stem)})
        if forward_only:
            continue
        if _has_link(f, page.stem):
            notes.append("%s already links back to [[%s]]" % (f.stem, page.stem))
        else:
            writes.append({"path": "Knowledge/%s" % f.name, "op": "append",
                           "section": "Related",
                           "content": "- [[%s]] — back-link added %s (gt_link_suggest)\n"
                                      % (page.stem, today),
                           "hint": "back-link %s -> %s (user-selected)" % (f.stem, page.stem)})
    return writes, notes, unknown


# ------------------------------------------------------------------ CLI ----

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_link_suggest.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("suggest", help="propose up to --limit links for one page (read-only)")
    s.add_argument("--vault")
    s.add_argument("--page", required=True)
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--json", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="accepted for uniformity; suggest never writes")
    p = sub.add_parser("apply", help="write the links the user selected, through the write queue")
    p.add_argument("--vault")
    p.add_argument("--page", required=True)
    p.add_argument("--to", action="append", default=[], required=True,
                   help="a candidate the user selected (stem, title or path); repeat")
    p.add_argument("--forward-only", action="store_true",
                   help="only the page's own link; no back-link in the target")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    q = _queue()
    vault = q.find_vault(a.vault)
    if vault is None:
        print("gt_link_suggest: no vault -- pass --vault", file=sys.stderr)
        return 2
    page = resolve_page(vault, a.page)
    if page is None:
        print("gt_link_suggest: %s is not a Knowledge page in %s" % (a.page, vault), file=sys.stderr)
        return 2

    if a.cmd == "suggest":
        cands = suggest(vault, page, a.limit)
        if a.json:
            print(json.dumps({"page": "Knowledge/%s" % page.name, "candidates": cands}, indent=1))
        elif cands:
            print("Link suggestions for [[%s]] (domain: %s) -- each adds a link both ways:"
                  % (page.stem, cands[0]["page_domain"]))
            for i, c in enumerate(cands, 1):
                print("  %d. [[%s]]  %-12s  %s  (domain: %s)"
                      % (i, c["target"], c["type"], c["reason"], c["domain"]))
        return 0

    writes, notes, unknown = plan_apply(vault, page, a.to, a.forward_only)
    for u in unknown:
        print("gt_link_suggest: REFUSED %s -- not another Knowledge page in this vault" % u,
              file=sys.stderr)
    if unknown:
        return 1
    results, note = [], None
    if writes and not a.dry_run:
        results, note = q.submit(vault, writes)
    elif writes:
        results = [{"path": w["path"], "op": w["op"], "decision": "dry-run",
                    "content": w["content"].strip()} for w in writes]
    bad = [r for r in results if r.get("decision") in ("refused", "reject")]
    if a.json:
        print(json.dumps({"results": results, "notes": notes, "note": note}, indent=1))
    else:
        for r in results:
            print("gt_link_suggest: %s § Related -> %s%s" % (
                r["path"], r.get("decision"), (" (%s)" % r["reason"]) if r.get("reason") else ""))
        for n in notes:
            print("gt_link_suggest: %s -- not written again" % n)
        if note:
            print("gt_link_suggest: %s; %s" % (note, q.drain_hint(vault)))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
