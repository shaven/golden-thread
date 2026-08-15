#!/usr/bin/env python3
"""wiki_lint.py - deterministic health checks for an LLM Wiki vault.

Report-only: never modifies any file. The LLM (or human) interprets the
report and proposes fixes; the owner approves them.

Checks:
  1. broken-links        [[wikilinks]] pointing at pages/sources that do not exist
  2. orphans             Knowledge pages with no inbound links from any page or index
  3. missing-reciprocal  A links B but B does not link A (Knowledge pages only)
  4. unsourced           Knowledge pages with empty/missing sources, or sources
                         pointing at files that do not exist
  5. superseded-cited    pages citing a source that a newer source supersedes
  6. review-due          pages whose `updated` (or mtime) is older than N days
                         (default 90). Age is a REVIEW signal, not staleness.
  7. index-mismatch      index entries without files / Knowledge files without
                         index entries
  8. status-schema       status field missing or not in the allowed set
  9. expiry-declared     pages carrying an expires_when condition, surfaced so
                         the owner can verify whether the condition has been met
 10. unlinked-mention    a page's body mentions another page's title without
                         linking it - a missing-link candidate. Only distinctive
                         titles are matched (2+ words or 10+ chars), on word
                         boundaries, outside code blocks and existing links

review-due exempts principle-kind pages: category `decision`, or an explicit
`kind: principle` in frontmatter. Principles do not decay; only facts do.
--queue FILE additionally writes review-due + expiry-declared items to a
markdown review queue file (regenerated wholesale each run).

Declined findings stay declined: <vault>/lint-declines.md is an append-only
ledger of finding lines the owner has rejected. Any finding whose text
appears in it is suppressed from reports (counted in the summary as
suppressed). One finding per line, `- ` prefix, exact text as reported;
anything after " | " on the line is treated as the decline rationale.

Usage: wiki_lint.py VAULT_PATH [--days 90] [--json]
Exit code: 0 clean, 1 findings (usable as a CI gate).
"""
import sys, os, re, json, datetime

LINK = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
STATUSES = {"seed", "growing", "mature", "stale"}

def frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end]
    fm, key = {}, None
    for line in raw.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            key = m.group(1)
            val = m.group(2).strip()
            fm[key] = val if val else []
        elif key is not None and line.lstrip().startswith("- "):
            if not isinstance(fm.get(key), list):
                fm[key] = []
            fm[key].append(line.lstrip()[2:].strip().strip('"').strip("'"))
    return fm, text[end + 4:]

def norm(target):
    t = str(target).strip().strip('"').strip("'")
    t = t.strip("[]")  # frontmatter values may keep their [[ ]] wrapper
    t = t.replace("\\|", "|")  # Obsidian escapes pipes inside tables: [[page\|alias]]
    t = t.split("|")[0].split("#")[0]
    t = t.rstrip("\\")  # any remaining trailing escape backslash
    t = t.split("/")[-1]
    if t.lower().endswith(".md"):
        t = t[:-3]
    return t.strip().lower()

def load(vault):
    pages, sources = {}, {}
    kdir, sdir = os.path.join(vault, "Knowledge"), os.path.join(vault, "Sources")
    for d, store in ((kdir, pages), (sdir, sources)):
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(d, fn)
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            fm, body = frontmatter(text)
            store[norm(fn)] = {
                "file": fn, "path": path, "fm": fm,
                "links": {norm(x) for x in LINK.findall(body)},
                "mtime": os.path.getmtime(path),
            }
    index_links = set()
    ipath = os.path.join(vault, "index.md")
    if os.path.exists(ipath):
        with open(ipath, encoding="utf-8", errors="replace") as f:
            index_links = {norm(x) for x in LINK.findall(f.read())}
    return pages, sources, index_links

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    vault = sys.argv[1]
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 90
    as_json = "--json" in sys.argv
    queue_file = sys.argv[sys.argv.index("--queue") + 1] if "--queue" in sys.argv else None

    declines = set()
    dpath = os.path.join(vault, "lint-declines.md")
    if os.path.exists(dpath):
        with open(dpath, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("- "):
                    declines.add(line[2:].split(" | ")[0].strip())

    pages, sources, index_links = load(vault)
    all_slugs = set(pages) | set(sources)
    findings = {k: [] for k in ("broken-links","orphans","missing-reciprocal",
        "unsourced","superseded-cited","review-due","index-mismatch","status-schema",
        "expiry-declared","unlinked-mention")}

    superseded = {}
    for slug, s in sources.items():
        sup = s["fm"].get("supersedes")
        for tgt in (sup if isinstance(sup, list) else [sup] if sup else []):
            superseded[norm(tgt)] = slug

    inbound = {slug: set() for slug in pages}
    for slug, p in pages.items():
        for tgt in p["links"]:
            if tgt in inbound and tgt != slug:
                inbound[tgt].add(slug)

    today = datetime.date.today()
    for slug, p in pages.items():
        for tgt in p["links"]:
            if tgt not in all_slugs:
                findings["broken-links"].append(f"{p['file']} -> [[{tgt}]]")
        if not inbound[slug] and slug not in index_links:
            findings["orphans"].append(p["file"])
        for tgt in p["links"]:
            if tgt in pages and slug not in pages[tgt]["links"]:
                findings["missing-reciprocal"].append(f"{p['file']} -> {pages[tgt]['file']} (no link back)")
        src = p["fm"].get("sources")
        src_list = src if isinstance(src, list) else [src] if src else []
        if not src_list:
            findings["unsourced"].append(p["file"])
        else:
            for s in src_list:
                if norm(s) not in sources:
                    findings["unsourced"].append(f"{p['file']} cites missing source [[{norm(s)}]]")
                if norm(s) in superseded:
                    findings["superseded-cited"].append(
                        f"{p['file']} cites [[{norm(s)}]] superseded by [[{superseded[norm(s)]}]]")
        exp = p["fm"].get("expires_when")
        if exp:
            findings["expiry-declared"].append(f"{p['file']} (expires_when: {exp})")
        is_principle = (str(p["fm"].get("category")) == "decision"
                        or str(p["fm"].get("kind")) == "principle")
        upd = p["fm"].get("updated")
        try:
            ref = datetime.date.fromisoformat(str(upd)) if upd else datetime.date.fromtimestamp(p["mtime"])
        except ValueError:
            ref = datetime.date.fromtimestamp(p["mtime"])
        if not is_principle and (today - ref).days > days:
            findings["review-due"].append(f"{p['file']} (last touch {ref.isoformat()})")
        st = p["fm"].get("status")
        if not st or str(st) not in STATUSES:
            findings["status-schema"].append(f"{p['file']} (status: {st!r})")

    # unlinked mentions: distinctive titles appearing unlinked in other pages
    distinctive = {}
    for slug, pg in pages.items():
        title = str(pg["fm"].get("title") or pg["file"][:-3])
        if len(title.split()) >= 2 or len(title) >= 10:
            distinctive[slug] = title
    for slug, p in pages.items():
        with open(p["path"], encoding="utf-8", errors="replace") as f:
            body = f.read()
        body = re.sub(r"^---\n.*?\n---\n", "", body, flags=re.S)   # frontmatter
        body = re.sub(r"```.*?```", "", body, flags=re.S)            # code blocks
        body = re.sub(r"\[\[[^\]]*\]\]", "", body)                  # existing links
        low = body.lower()
        for tslug, title in distinctive.items():
            if tslug == slug or tslug in p["links"]:
                continue
            if re.search(r"(?<![\w])" + re.escape(title.lower()) + r"(?![\w])", low):
                findings["unlinked-mention"].append(
                    f"{p['file']} mentions \"{title}\" without linking [[{title}]]")

    for slug in index_links - all_slugs:
        findings["index-mismatch"].append(f"index lists [[{slug}]] but no file exists")
    for slug, p in pages.items():
        if slug not in index_links:
            findings["index-mismatch"].append(f"{p['file']} missing from index.md")

    suppressed = 0
    if declines:
        for k in findings:
            kept = [x for x in findings[k] if x not in declines]
            suppressed += len(findings[k]) - len(kept)
            findings[k] = kept

    total = sum(len(v) for v in findings.values())
    if queue_file:
        with open(queue_file, "w", encoding="utf-8") as qf:
            items = findings["review-due"] + findings["expiry-declared"]
            qf.write("# Review queue\n\nRegenerated by wiki-lint. "
                     f"Pending: {len(items)}\n\n")
            for item in sorted(items):
                qf.write(f"- [ ] {item}\n")
    if as_json:
        print(json.dumps({"total": total, "pages": len(pages), "sources": len(sources),
                          "suppressed": suppressed, "findings": findings}, indent=2))
    else:
        print(f"# Wiki lint report\n\nPages: {len(pages)} | Sources: {len(sources)} | "
              f"Findings: {total} | Suppressed by declines: {suppressed}\n")
        for k, v in findings.items():
            print(f"## {k} ({len(v)})")
            for item in sorted(v):
                print(f"- {item}")
            print()
    sys.exit(1 if total else 0)

if __name__ == "__main__":
    main()
