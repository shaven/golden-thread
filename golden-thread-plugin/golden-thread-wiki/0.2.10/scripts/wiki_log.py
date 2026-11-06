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

INSIDE A GOLDEN THREAD VAULT (0.2.4) -- one with a `Projects/golden-thread/` folder -- this
script writes nothing itself. Under gt's Core rule 1 vault content goes through the write
queue, and a script's own file writes are invisible to the PreToolUse guard, so:

  log    log.md is a GENERATED file there. The entry becomes one line,
         `<YYYY-MM-DD HH:MM TZ> [<op>] <title> — <line>; <line>`, handed to gt's
         `gt_log.py --vault VAULT add` (the vault's own Projects/golden-thread/tools copy
         first, else the gt release's templates/tools). The queue refuses log.md, so if no
         gt_log.py can be found the entry is NOT written: exit 3, the line on stderr.
  index  the edit becomes one write-queue request -- a replaced entry is a `replace-section`
         of the `##` section that holds it, a new entry an `append` (under --section, or at
         the end of the file) -- submitted with gt's gt_write_queue.submit(), which drains
         at once through gt_broker.py. If gt's scripts cannot be found the request JSON is
         deposited in `Projects/golden-thread/spool/queue/` directly (gt_write_queue.py
         schema 1) and waits for the next `gt_broker.py drain`. An entry above the first
         `##` heading cannot be reached by a section write: exit 1, nothing queued.

gt's scripts are looked for the way gt-flow looks for them: $GT_CORE_SCRIPTS when set (and
then ONLY there), else the newest gt release beside this module (plugin cache `gt/<v>`, or
`golden-thread/<v>` in a source checkout), else `~/.claude/plugins/cache/golden-thread-plugin/
gt/<newest>`. Outside a gt vault (a standalone LLM Wiki) both commands write directly, as
before 0.2.4.

Exit (gt vault, index): 0 applied, deduplicated, held or queued (the request is safe in the
queue) | 1 escalated to the owner, refused or rejected.
"""
import sys, os, re, argparse, datetime, hashlib, json, subprocess, tempfile

OPS = {"ingest", "query", "lint", "refresh", "graduate", "retire", "relocate"}
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_DIR = re.compile(r"\d+\.\d+\.\d+")
QUEUE_REL = os.path.join("Projects", "golden-thread", "spool", "queue")

# The section grammar of gt_write_queue.py, mirrored for the no-gt fallback: a request's
# base_sha256 must hash exactly what the broker will compare it with.
HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$")
ANY_TOP = re.compile(r"^#{1,2}\s")
FENCE = re.compile(r"^\s*(```|~~~)")


def is_gt_vault(vault):
    return os.path.isdir(os.path.join(vault, "Projects", "golden-thread"))


# -- finding gt ---------------------------------------------------------------------------
def _newest(root, sub, need):
    try:
        vs = [d for d in os.listdir(root) if VERSION_DIR.fullmatch(d)
              and all(os.path.isfile(os.path.join(root, d, sub, n)) for n in need)]
    except OSError:
        return None
    if not vs:
        return None
    return os.path.join(root, max(vs, key=lambda s: tuple(int(x) for x in s.split("."))), sub)


def gt_dirs(sub, need):
    """Directories holding every file in `need`, best first. `sub` is `scripts` or
    `templates/tools` inside a gt release."""
    env = os.environ.get("GT_CORE_SCRIPTS")
    if env is not None:
        out = [env if sub == "scripts" else os.path.join(os.path.dirname(env), sub)]
    else:
        parent = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
        out = [_newest(os.path.join(parent, "gt"), sub, need),
               _newest(os.path.join(parent, "golden-thread"), sub, need),
               _newest(os.path.expanduser("~/.claude/plugins/cache/golden-thread-plugin/gt"),
                       sub, need)]
    return [d for d in out if d and all(os.path.isfile(os.path.join(d, n)) for n in need)]


def load_write_queue():
    """gt's gt_write_queue module (with gt_broker.py beside it), or None."""
    for d in gt_dirs("scripts", ("gt_write_queue.py", "gt_broker.py")):
        added = d not in sys.path
        if added:
            sys.path.insert(0, d)
        try:
            import importlib
            sys.modules.pop("gt_write_queue", None)
            wq = importlib.import_module("gt_write_queue")
            if callable(getattr(wq, "submit", None)):
                return wq
        except Exception:
            pass
        finally:
            if added and d in sys.path:
                sys.path.remove(d)
    return None


# -- log ----------------------------------------------------------------------------------
def _one_line(s):
    return " ".join(str(s).split())


def do_log(vault, a):
    if a.op not in OPS:
        sys.exit(f"op must be one of {sorted(OPS)}")
    if is_gt_vault(vault):
        return log_via_gt(vault, a)
    path = os.path.join(vault, "log.md")
    entry = f"\n## [{datetime.date.today()}] {a.op} | {a.title}\n" + "".join(f"- {l}\n" for l in a.line or [])
    with open(path, "a", encoding="utf-8") as f:
        if os.path.getsize(path) and not open(path, "rb").read()[-1:] == b"\n":
            f.write("\n")
        f.write(entry)
    print(entry.strip())
    return 0


def log_via_gt(vault, a):
    stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    line = f"{stamp} [{a.op}] {_one_line(a.title)}"
    extra = [_one_line(l) for l in a.line or [] if _one_line(l)]
    if extra:
        line += " — " + "; ".join(extra)
    tools = [os.path.join(vault, "Projects", "golden-thread", "tools")]
    tools += gt_dirs(os.path.join("templates", "tools"), ("gt_log.py", "gt_spool.py"))
    tool = next((os.path.join(d, "gt_log.py") for d in tools
                 if os.path.isfile(os.path.join(d, "gt_log.py"))), None)
    if tool is None:
        print("wiki_log: log.md is generated in a Golden Thread vault and gt_log.py was not "
              "found (vault tools, gt release, plugin cache); the write queue refuses log.md, "
              "so this entry was NOT written:\n  " + line, file=sys.stderr)
        return 3
    p = subprocess.run([sys.executable, tool, "--vault", vault, "add", line],
                       capture_output=True, text=True)
    if p.stdout:
        sys.stdout.write(p.stdout)
    if p.stderr:
        sys.stderr.write(p.stderr)
    print(line)
    return p.returncode


# -- index --------------------------------------------------------------------------------
def entry_pattern(page):
    return re.compile(r"^- \[\[" + re.escape(page) + r"(?:\|[^\]]*)?\]\].*$", re.M)


def do_index(vault, a):
    if is_gt_vault(vault):
        return index_via_queue(vault, a)
    path = os.path.join(vault, "index.md")
    text = open(path, encoding="utf-8").read()
    new = f"- [[{a.page}]] — {a.summary}"
    pat = entry_pattern(a.page)
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
    return 0


def _find_section(lines, section):
    want = section.strip().lower()
    fenced, start = False, None
    for i, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if start is not None and ANY_TOP.match(line):
            return start, i
        m = HEADING.match(line)
        if start is None and m and m.group(1).strip().lower() == want:
            start = i
    return (start, len(lines)) if start is not None else None


def _section_body(lines, section):
    rng = _find_section(lines, section)
    if rng is None:
        return None
    return "\n".join(lines[rng[0] + 1:rng[1]]).strip("\n")


def _heading_above(lines, i):
    """The `##` heading whose section holds line i, or None (preamble, or under a `#`)."""
    fenced, heading = False, None
    for line in lines[:i]:
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if ANY_TOP.match(line):
            m = HEADING.match(line)
            heading = m.group(1).strip() if m else None
    return heading


def plan_index_write(text, a):
    """-> (write dict for the queue, action label). Exits on a write that cannot be made."""
    new = f"- [[{a.page}]] — {a.summary}"
    lines = text.replace("\r\n", "\n").split("\n") if text else []
    pat = entry_pattern(a.page)
    hit = next((i for i, l in enumerate(lines) if pat.fullmatch(l)), None)
    if hit is not None:
        heading = _heading_above(lines, hit)
        rng = _find_section(lines, heading) if heading else None
        if rng is None or not (rng[0] < hit < rng[1]):
            sys.exit(f"the entry for [[{a.page}]] is not inside a `## ` section of index.md, so "
                     "the write queue cannot replace it; edit that line by hand")
        changed = list(lines)
        changed[hit] = new
        return ({"path": "index.md", "op": "replace-section", "section": heading,
                 "content": _section_body(changed, heading), "new": new,
                 "hint": f"wiki_log.py index: replace the entry for [[{a.page}]]"}, "replaced")
    if a.section:
        if not re.search(r"^## " + re.escape(a.section) + r"\s*$", text, re.M):
            sys.exit(f"section '## {a.section}' not found in index.md")
        return ({"path": "index.md", "op": "append", "section": a.section, "content": new,
                 "new": new, "hint": f"wiki_log.py index: add [[{a.page}]]"},
                f"added to {a.section}")
    return ({"path": "index.md", "op": "append", "content": new, "new": new,
             "hint": f"wiki_log.py index: add [[{a.page}]]"}, "appended")


def _session():
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
        if os.environ.get(var):
            return os.environ[var].strip()
    return "anonymous"


def _slug(text, n):
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:n].strip("-") or "x"


def deposit_request(vault, w, lines):
    """No gt scripts: write the request file gt_write_queue.build()/deposit() would."""
    now = datetime.datetime.now(datetime.timezone.utc)
    session = _session()
    base = None
    if w["op"] == "replace-section":
        cur = _section_body(lines, w["section"])
        base = None if cur is None else hashlib.sha256(cur.encode("utf-8")).hexdigest()
    req = {"schema": 1,
           "id": "%s-%s-%s" % (now.strftime("%Y%m%dT%H%M%S.%fZ"), _slug(session, 8),
                               _slug(w["path"], 60)),
           "submitted": now.isoformat(timespec="microseconds"),
           "session": session, "origin": "session", "path": w["path"], "op": w["op"],
           "section": w.get("section"), "content": w["content"], "key": None,
           "base_sha256": base, "hint": w.get("hint")}
    q = os.path.join(vault, QUEUE_REL)
    os.makedirs(q, exist_ok=True)
    rid, n = req["id"], 1
    while os.path.exists(os.path.join(q, req["id"] + ".json")):
        n += 1
        req["id"] = "%s-%d" % (rid, n)
    fd, tmp = tempfile.mkstemp(prefix=".req.", suffix=".tmp", dir=q)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(req, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, os.path.join(q, req["id"] + ".json"))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return req["id"]


def index_via_queue(vault, a):
    path = os.path.join(vault, "index.md")
    try:
        text = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        text = ""
    w, action = plan_index_write(text, a)
    new = w.pop("new")
    wq = load_write_queue()
    if wq is None:
        rid = deposit_request(vault, w, text.replace("\r\n", "\n").split("\n") if text else [])
        print(f"{action}: {new} [queued {rid}; gt's scripts were not found, so it is applied "
              "by the next `gt_broker.py drain`]")
        return 0
    from pathlib import Path
    results, note = wq.submit(Path(vault), [w])
    r = results[0]
    d = r["decision"]
    why = r.get("reason") or note or ""
    if d in ("apply", "deduplicate"):
        print(f"{action}: {new} [{d}]")
        return 0
    if d in ("held", "queued"):
        print(f"{action}: {new} [{d}: {why}; {wq.drain_hint(Path(vault))}]")
        return 0
    print(f"wiki_log: {action} NOT written: {new} [{d}: {why}]", file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("vault")
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("log"); l.add_argument("op"); l.add_argument("title"); l.add_argument("--line", action="append")
    i = sub.add_parser("index"); i.add_argument("page"); i.add_argument("summary"); i.add_argument("--section")
    a = ap.parse_args()
    return (do_log if a.cmd == "log" else do_index)(a.vault, a) or 0

if __name__ == "__main__":
    sys.exit(main())
