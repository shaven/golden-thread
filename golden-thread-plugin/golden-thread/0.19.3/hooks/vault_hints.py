#!/usr/bin/env python3
"""vault_hints.py -- UserPromptSubmit: name up to three vault pages the prompt may need (0.19.1).

OFF BY DEFAULT (owner, 2026-10-02): the `vault_hints` setting in vault-config.json turns it on.

A session only uses the vault when it thinks to look, so an answer already filed is re-derived.
Loading the vault every turn is ruled out (core_no_auto_load_memory_index, and cost). This adds
at most three lines -- "- <title> -- <path>" -- for the index.md entries whose title and summary
share the most words with the prompt. It reads ONE file, index.md, and never a page body: the
session decides whether to open anything.

Nothing is added when the setting is off, when no entry shares at least MIN_HITS words with the
prompt, when there is no vault or index, or when matching runs past its time budget
(GT_HINTS_BUDGET_MS, default 800). It always exits 0 and never blocks a prompt.
"""
import json
import os
import sys
import threading
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
MAX_HINTS = 3
MIN_HITS = 2


def _quiet():
    sys.exit(0)


def _config():
    try:
        with open(os.path.expanduser("~/.claude/vault-config.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def hints(prompt, vault):
    for d in (HERE, HERE.parent / "scripts"):            # installed beside it, or in a release
        sys.path.insert(0, str(d))
    import gt_keyword_recall as kr
    q = {kr._stem(w) for w in kr.words(prompt)}
    if not q:
        return []
    import re
    scored = []
    for line in (Path(vault) / "index.md").read_text(encoding="utf-8").splitlines():
        m = re.search(r"\[\[([^\]|#]+)", line)
        if not m:
            continue
        title = m.group(1).strip()
        hits = len(q & {kr._stem(w) for w in kr.words(line)})
        if hits >= MIN_HITS:
            rel = "Knowledge/%s.md" % title
            scored.append((-hits, title, rel if (Path(vault) / rel).exists() else title))
    return [(t, r) for _h, t, r in sorted(scored)[:MAX_HINTS]]


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _quiet()
    cfg = _config()
    if str(cfg.get("vault_hints", "off")).lower() != "on":
        _quiet()
    vault = cfg.get("vault_path")
    prompt = (payload or {}).get("prompt") or ""
    if not vault or not prompt or not (Path(vault) / "index.md").is_file():
        _quiet()
    try:
        budget = max(0.0, float(os.environ.get("GT_HINTS_BUDGET_MS", "800")) / 1000.0)
    except ValueError:
        budget = 0.8
    if budget <= 0:
        _quiet()
    box = {}
    t = threading.Thread(target=lambda: box.update(r=hints(prompt, vault)), daemon=True)
    t.start()
    t.join(budget)
    found = box.get("r") if not t.is_alive() else None
    if not found:
        _quiet()
    lines = ["The vault may already cover this (titles only; open a page with Read if it helps):"]
    lines += ["- %s — %s" % (title, rel) for title, rel in found]
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": "\n".join(lines)}}))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)                                       # fail open, silently
