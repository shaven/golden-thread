#!/usr/bin/env python3
"""vault_hints.py -- UserPromptSubmit: name up to three vault pages the prompt may need (0.19.1).

ON BY DEFAULT since 0.21.0 (owner, 2026-10-08; shipped off in 0.19.1): `vault_hints: off` in
vault-config.json turns it off.

A session only uses the vault when it thinks to look, so an answer already filed is re-derived.
Loading the vault every turn is ruled out (core_no_auto_load_memory_index, and cost). This adds
at most three lines -- "- <title> -- <path>" -- for the index.md entries whose title and summary
share the most words with the prompt. It reads ONE file, index.md, and never a page body: the
session decides whether to open anything.

Each line says how old its page is ("updated 3d ago"), from the file's modified time (0.22.0).

Nothing is added when the setting is off, when no entry shares at least MIN_HITS words with the
prompt, when there is no vault or index, or when matching runs past its time budget
(GT_HINTS_BUDGET_MS, default 800). It always exits 0 and never blocks a prompt.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
MAX_HINTS = 3
MIN_HITS = 2
REPEAT_TURNS = 10                                   # a title shown this recently is not shown again (0.21.4)


def _quiet():
    sys.exit(0)


def _config():
    try:
        with open(os.path.expanduser("~/.claude/vault-config.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _seen_path(session):
    sid = "".join(c for c in str(session or "")[:8] if c.isalnum() or c in "-_") or "none"
    return Path(os.path.expanduser("~/.claude/golden-thread/state")) / ("vault-hints-%s.json" % sid)


def _fresh(found, session):
    """Drop titles shown within the last REPEAT_TURNS prompts of this session, record the rest.
    Any failure on the state file keeps every title: a repeat costs a few tokens, a miss costs a hint."""
    try:
        path = _seen_path(session)
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        n = int(st.get("n", 0)) + 1
        seen = {k: v for k, v in (st.get("seen") or {}).items() if n - int(v) < REPEAT_TURNS}
        keep = [(t, r) for t, r in found if t not in seen]
        for t, _r in keep:
            seen[t] = n
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(str(tmp), "wb") as fh:
            fh.write(json.dumps({"n": n, "seen": seen}).encode("utf-8"))
        os.replace(str(tmp), str(path))
        return keep
    except Exception:
        return found


def _age(vault, rel):
    """0.22.0: " (updated 3d ago)" from the page's modified time -- one stat, never a read of the body.
    "" when the page is not there to stat (a locked folder, or a title with no file)."""
    try:
        days = int((time.time() - (Path(vault) / rel).stat().st_mtime) // 86400)
    except Exception:
        return ""
    if days < 1:
        return " (updated today)"
    return " (updated %dd ago)" % days if days < 100 else " (updated %dmo ago)" % (days // 30)


def _superseded(vault, rel):
    """0.22.0: " -- superseded by New Page" when the page's frontmatter says `superseded_by: [[New Page]]`.
    Reads the first lines of the page up to the frontmatter's end, and only that one value; never the body."""
    try:
        with open(Path(vault) / rel, encoding="utf-8", errors="replace") as f:
            if f.readline().strip() != "---":
                return ""
            for _ in range(60):
                line = f.readline()
                if not line or line.strip() == "---":
                    break
                if line.startswith("superseded_by:"):
                    import re
                    m = re.search(r"\[\[([^\]|#]+)", line)
                    return " — superseded by %s" % m.group(1).strip() if m else ""
    except Exception:
        return ""
    return ""


def hints(prompt, vault):
    for d in (HERE, HERE.parent / "scripts"):            # installed beside it, or in a release
        sys.path.insert(0, str(d))
    import gt_keyword_recall as kr
    q = {kr._stem(w) for w in kr.words(prompt)}
    if not q:
        return []
    import re
    scored = []
    # A Knowledge/ folder locked by gt_lock.py (0.20.1) is absent: one stat for its stub, and
    # an entry whose page is missing there is never surfaced, even by title (it may be locked).
    locked = (Path(vault) / "Knowledge" / ".gt-locked").exists()
    for line in (Path(vault) / "index.md").read_text(encoding="utf-8").splitlines():
        m = re.search(r"\[\[([^\]|#]+)", line)
        if not m:
            continue
        title = m.group(1).strip()
        hits = len(q & {kr._stem(w) for w in kr.words(line)})
        if hits >= MIN_HITS:
            rel = "Knowledge/%s.md" % title
            if locked:
                if (Path(vault) / rel).exists() or (Path(vault) / (rel + ".age")).exists():
                    continue                              # inside the locked folder: absent
                scored.append((-hits, title, title))
                continue
            scored.append((-hits, title, rel if (Path(vault) / rel).exists() else title))
    return [(t, r) for _h, t, r in sorted(scored)[:MAX_HINTS]]


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _quiet()
    cfg = _config()
    if str(cfg.get("vault_hints", "on")).lower() != "on":
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
    found = _fresh(found, (payload or {}).get("session_id"))
    if not found:
        _quiet()
    # Titles only when the page is Knowledge/<title>.md (the common case): the path is derivable.
    lines = ["Vault may cover this (Knowledge/<title>.md; Read it if it helps):"]
    lines += [("- %s" % title if rel == "Knowledge/%s.md" % title else "- %s — %s" % (title, rel))
              + _age(vault, rel) + _superseded(vault, rel) for title, rel in found]
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": "\n".join(lines)}}))
    sys.exit(0)


if __name__ == "__main__":
    # paranoid.signed_manifest (0.21.0): when gt's code is sealed, a hook checks its own files
    # first (gt_integrity_gate: one stat() when the group is off). Only as a hook -- the CLI
    # forms of this script stay usable, and are the repair route.
    try:
        import gt_integrity_gate as _gig
    except ImportError:
        _gig = None
    if _gig is not None:
        _gig.check(__file__, "reporter", when=True)
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)                                       # fail open, silently
