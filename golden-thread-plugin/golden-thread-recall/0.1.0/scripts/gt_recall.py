#!/usr/bin/env python3
"""gt_recall.py -- rank vault pages for a question by BM25 (gt-recall 0.1.0).

    gt_recall.py "<question>" --vault V [--k 10] [--json]

Stage 1 of vault recall, standard library only, read-only, no index file. Every markdown page
under Knowledge/, Projects/ and global-memory/ is scored with BM25 (k1 1.5, b 0.75): a word in
the page's name, a heading or its index.md line counts three times a word in the body, and a long
page is discounted against a short one with the same hits. A folder holding gt_lock.py's
`.gt-locked` stub is never read.

`retrieve()` returns {"results": [paths, best first], "scores": {...}, "hits": [{path, level,
score, method}], "locked": [...]}. `method` says which retriever found the hit ("bm25") so a later
stage can say otherwise.
"""
import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

LEVELS = ("Knowledge", "Projects", "global-memory")
LOCK_STUB = ".gt-locked"
K1, B, TITLE_WEIGHT = 1.5, 0.75, 3
STOP = set("""a an and are as at be by can did do does for from has have how i if in is it its
many may of on or our should so that the their them there these this to was we what when where
which who why will with would you your about after any been but could into more most much not
only other than then they those through under very were""".split())
WORD = re.compile(r"[a-z0-9][a-z0-9_-]*")
HEADING = re.compile(r"(?m)^#{1,6}\s+(.*)$")


def words(text):
    return [w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _terms(text):
    return [_stem(w) for w in words(text)]


def pages(vault, locked=None):
    vault = Path(vault)
    out = []
    for top in LEVELS:
        base = vault / top
        if not base.is_dir():
            continue
        found = []
        for root, dirs, files in os.walk(str(base)):
            if LOCK_STUB in files:
                dirs[:] = []
                if locked is not None:
                    locked.append(Path(root).relative_to(vault).as_posix())
                continue
            found += [Path(root) / f for f in files if f.endswith(".md")]
        out += sorted(p for p in found if p.is_file())
    return out


def _index_lines(vault):
    out = {}
    try:
        for line in (Path(vault) / "index.md").read_text(encoding="utf-8").splitlines():
            for m in re.finditer(r"\[\[([^\]|#]+)", line):
                out[m.group(1).strip().lower()] = line
    except OSError:
        pass
    return out


def _doc(vault, p, idx):
    """-> (weighted term counts, length) for one page, or None when it cannot be read."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rel = p.relative_to(vault).as_posix()
    title = _terms(rel.replace("/", " ").replace(".md", "")) + _terms(idx.get(p.stem.lower(), ""))
    for h in HEADING.findall(text):
        title += _terms(h)
    body = _terms(text)
    tf = {}
    for t in body:
        tf[t] = tf.get(t, 0) + 1
    for t in title:
        tf[t] = tf.get(t, 0) + TITLE_WEIGHT
    return tf, len(body) + TITLE_WEIGHT * len(title)


def score_pages(question, vault, locked=None):
    q = set(_terms(question))
    if not q:
        return {}
    vault = Path(vault)
    idx = _index_lines(vault)
    docs = {}
    for p in pages(vault, locked):
        d = _doc(vault, p, idx)
        if d:
            docs[p.relative_to(vault).as_posix()] = d
    if not docs:
        return {}
    n = len(docs)
    avg = sum(length for _, length in docs.values()) / n or 1.0
    df = {t: sum(1 for tf, _ in docs.values() if t in tf) for t in q}
    scores = {}
    for rel, (tf, length) in docs.items():
        s = 0.0
        for t in q:
            f = tf.get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * length / avg))
        if s:
            scores[rel] = round(s, 4)
    return scores


def retrieve(question, vault, k=10):
    locked = []
    scores = score_pages(question, vault, locked)
    ranked = sorted(scores, key=lambda r: (-scores[r], r))[:k]
    read = (["index.md"] if (Path(vault) / "index.md").is_file() else []) + ranked[:3]
    hits = [{"path": r, "level": r.split("/", 1)[0], "score": scores[r], "method": "bm25"}
            for r in ranked]
    return {"results": ranked, "scores": {r: scores[r] for r in ranked}, "hits": hits,
            "read": read, "locked": sorted(locked)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("question")
    ap.add_argument("--vault", required=True, help="the vault to search (Core rule 2: named)")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = retrieve(a.question, a.vault, a.k)
    if r["locked"]:
        sys.stderr.write("note: %d locked folder(s) not searched (%s)\n"
                         % (len(r["locked"]), ", ".join(r["locked"])))
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        for rel in r["results"]:
            print("%8.3f  %s" % (r["scores"][rel], rel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
