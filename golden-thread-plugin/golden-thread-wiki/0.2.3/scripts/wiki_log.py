#!/usr/bin/env python3
"""wiki_log.py - deterministic writers for the two navigation files every
skill touches: log.md (append-only) and index.md (catalog).

  log    append `## [YYYY-MM-DD] <op> | <title>` + bullet lines
  index  add or replace the entry for a page under a section

Usage:
  wiki_log.py VAULT log OP "Title" --line "..." [--line "..."]
  wiki_log.py VAULT index "Page Title" "one-line summary" [--section "Tools"]

OP must be one of the closed vocabulary: ingest query lint refresh graduate
retire relocate. Index entries are `- [[Page]] — summary`; an existing entry
for the page is replaced in place, a new one is appended to --section
(default: end of file). Both commands print what they wrote.
"""
import sys, os, re, argparse, datetime

OPS = {"ingest", "query", "lint", "refresh", "graduate", "retire", "relocate"}

def do_log(vault, a):
    if a.op not in OPS:
        sys.exit(f"op must be one of {sorted(OPS)}")
    path = os.path.join(vault, "log.md")
    entry = f"\n## [{datetime.date.today()}] {a.op} | {a.title}\n" + "".join(f"- {l}\n" for l in a.line or [])
    with open(path, "a", encoding="utf-8") as f:
        if os.path.getsize(path) and not open(path, "rb").read()[-1:] == b"\n":
            f.write("\n")
        f.write(entry)
    print(entry.strip())

def do_index(vault, a):
    path = os.path.join(vault, "index.md")
    text = open(path, encoding="utf-8").read()
    new = f"- [[{a.page}]] — {a.summary}"
    pat = re.compile(r"^- \[\[" + re.escape(a.page) + r"(?:\|[^\]]*)?\]\].*$", re.M)
    if pat.search(text):
        text = pat.sub(lambda _: new, text, count=1); action = "replaced"  # a function, so backslashes stay literal
    elif a.section:
        m = re.search(r"^## " + re.escape(a.section) + r"\s*$", text, re.M)
        if not m:
            sys.exit(f"section '## {a.section}' not found in index.md")
        nxt = re.search(r"^## ", text[m.end():], re.M)
        end = m.end() + (nxt.start() if nxt else len(text[m.end():]))
        block = text[m.end():end].rstrip("\n")
        text = text[:m.end()] + block + "\n" + new + "\n\n" + text[end:].lstrip("\n"); action = f"added to {a.section}"
    else:
        text = text.rstrip("\n") + "\n" + new + "\n"; action = "appended"
    open(path, "w", encoding="utf-8").write(text)
    print(f"{action}: {new}")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("vault")
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("log"); l.add_argument("op"); l.add_argument("title"); l.add_argument("--line", action="append")
    i = sub.add_parser("index"); i.add_argument("page"); i.add_argument("summary"); i.add_argument("--section")
    a = ap.parse_args()
    (do_log if a.cmd == "log" else do_index)(a.vault, a)

if __name__ == "__main__":
    main()
