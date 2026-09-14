#!/usr/bin/env python3
"""feature_requests.py — validate and move Golden Thread feature requests.

The queue lives beside the source, in Projects2/gt-feature-requests/:

    new/ -> reviewed/ -> accepted/ -> implemented/
                     \\-> rejected/

The format a request must follow is FEATURE-REQUEST-FORMAT.md; this script is the
executable form of that document, so the two cannot quietly disagree. Run
`validate` BEFORE any code is written for a request: a request that does not pass
here is not implemented, however good the idea.

  feature_requests.py validate <request.md> [--src <plugin-root>] [--json]
  feature_requests.py validate --all <queue-dir> [--src <plugin-root>] [--json]
  feature_requests.py move <request.md> <stage> --note "<reasoning>" [--queue <dir>]
  feature_requests.py list <queue-dir>

`--src` is the plugin root the request is checked against (gt-src, or the repo's
golden-thread-plugin/). Component paths are resolved against the root, then against
the newest version directory of each plugin, so `skills/gt-lint` and `install.sh`
both resolve.

Exit codes for validate: 0 ready for review, 1 reject (reason code printed),
3 escalate to the owner (breaking change without owner override), 2 usage error.
Standard library only.
"""
import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

STAGES = ("new", "reviewed", "accepted", "rejected", "implemented")
ENUMS = {
    "requested_by": {"claude-auto", "owner"},
    "category": {"feature", "bugfix", "refactor", "docs", "tooling"},
    "priority": {"low", "medium", "high"},
    "breaking_change": {"yes", "no"},
    "owner_override": {"yes", "no"},
}
REQUIRED_KEYS = ("id", "title", "requested_by", "date", "category", "priority",
                 "breaking_change", "owner_override", "affected_components")
REQUIRED_SECTIONS = ("Motivation", "Description", "Acceptance Criteria", "Test Plan")
ID_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*$")
NEW_PREFIX = "(new)"


# -- parsing ------------------------------------------------------------------------
def parse(text):
    """-> (frontmatter dict, {section title: body}). A small YAML subset:
    `key: value`, `key:` followed by `  - item` lines, quoted strings."""
    fm, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        key = None
        for line in m.group(1).splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            item = re.match(r"^\s+-\s*(.*)$", line)
            if item and key is not None:
                if not isinstance(fm.get(key), list):
                    fm[key] = []
                fm[key].append(_unquote(item.group(1)))
                continue
            kv = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
            if kv:
                key, val = kv.group(1), kv.group(2).strip()
                if val in ("[]", ""):
                    fm[key] = [] if val == "[]" else ""
                elif val.startswith("[") and val.endswith("]"):
                    fm[key] = [_unquote(x.strip()) for x in val[1:-1].split(",") if x.strip()]
                else:
                    fm[key] = _unquote(val)
    sections, cur = {}, None
    for line in body.splitlines():
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h and not line.startswith("###"):
            cur = h.group(1)
            sections[cur] = ""
        elif cur is not None:
            sections[cur] += line + "\n"
    return fm, sections


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


# -- source resolution ----------------------------------------------------------------
def resolve_roots(src):
    """-> [src, newest release of every plugin under src] — dev/plugins.py decides which."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import plugins
    return [src] + ([v for _, v, _ in plugins.discover(src)] if src.is_dir() else [])


def new_component(comp):
    """-> (is_new, path). New files are marked `(new) path` or `path (new …)`."""
    c = comp.strip()
    if c.lower().startswith(NEW_PREFIX):
        return True, c[len(NEW_PREFIX):].strip()
    m = re.match(r"^(\S+)\s+\(new\b[^)]*\)\s*$", c, re.I)
    if m:
        return True, m.group(1)
    return False, c


def component_exists(roots, comp):
    p = comp.split()[0].rstrip("/").rstrip(",")
    return any((r / p).exists() for r in roots)


# -- validation -------------------------------------------------------------------------
def validate(path, src=None, queue=None):
    """-> dict(verdict, reason, errors, warnings, id)."""
    errors, warnings = [], []
    reason = None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return {"file": str(path), "verdict": "reject", "reason": "unreadable",
                "errors": [str(e)], "warnings": []}
    fm, sec = parse(text)

    missing = [k for k in REQUIRED_KEYS if k not in fm or fm[k] in ("", [])]
    for k in missing:
        errors.append(f"missing frontmatter key: {k}")
    for s in REQUIRED_SECTIONS:
        if s not in sec or not sec[s].strip():
            errors.append(f"missing or empty section: ## {s}")
    if missing or any(e.startswith("missing or empty section") for e in errors):
        reason = "incomplete"

    for k, allowed in ENUMS.items():
        v = str(fm.get(k, "")).strip().lower()
        if v and v not in allowed:
            errors.append(f"{k}: '{fm.get(k)}' is not one of {sorted(allowed)}")
            reason = reason or "invalid-value"

    rid = str(fm.get("id", ""))
    m = ID_RE.match(rid)
    if rid and not m:
        errors.append(f"id '{rid}' must be YYYY-MM-DD-kebab-title")
        reason = reason or "invalid-value"
    if rid and Path(path).stem != rid:
        errors.append(f"filename '{Path(path).name}' must be '{rid}.md'")
        reason = reason or "invalid-value"
    if m and str(fm.get("date", "")) != m.group(1):
        errors.append(f"date '{fm.get('date')}' does not match the id's date {m.group(1)}")
        reason = reason or "invalid-value"
    if fm.get("date"):
        try:
            datetime.date.fromisoformat(str(fm["date"]))
        except ValueError:
            errors.append(f"date '{fm['date']}' is not an ISO date")
            reason = reason or "invalid-value"

    comps = fm.get("affected_components", [])
    if isinstance(comps, str):
        comps = [comps]
        warnings.append("affected_components should be a list")
    if src:
        roots = resolve_roots(Path(src))
        for c in comps:
            is_new, name = new_component(c)
            if is_new:
                if name and component_exists(roots, name):
                    warnings.append(f"component marked (new) already exists: {name}")
            elif not component_exists(roots, c):
                errors.append(f"affected component does not exist in source: {c} "
                              f"(prefix it with '(new)' if the request creates it)")
                reason = reason or "stale-reference"

    crit = sec.get("Acceptance Criteria", "")
    boxes = re.findall(r"^\s*-\s*\[[ xX]\]\s*(.+)$", crit, re.M)
    if "Acceptance Criteria" in sec and not boxes:
        errors.append("## Acceptance Criteria needs at least one '- [ ] …' checkbox")
        reason = reason or "underspecified"
    for b in boxes:
        if len(b.split()) < 3:
            errors.append(f"acceptance criterion too vague to test: '{b.strip()}'")
            reason = reason or "underspecified"
    if "Test Plan" in sec and boxes and len(sec["Test Plan"].split()) < 10:
        errors.append("## Test Plan must say how each criterion will be tested")
        reason = reason or "underspecified"

    if queue and rid:
        for st in STAGES:
            other = Path(queue) / st / f"{rid}.md"
            if other.exists() and other.resolve() != Path(path).resolve():
                errors.append(f"duplicate: {rid} already exists in {st}/")
                reason = reason or "duplicate"

    terms = load_scrub_terms()
    if terms:
        hits = sorted({t for t in terms if re.search(t, text, re.I)})
        if hits:
            warnings.append(f"request names {len(hits)} employer-specific term(s); "
                            "fine in the queue, but the implementation must not carry them into gt-src")

    if errors:
        verdict = "reject"
    elif str(fm.get("breaking_change")).lower() == "yes" and str(fm.get("owner_override")).lower() != "yes":
        verdict, reason = "escalate", "breaking-change-needs-owner"
    else:
        verdict, reason = "ready", None
    return {"file": str(path), "id": rid, "verdict": verdict, "reason": reason,
            "errors": errors, "warnings": warnings}


def load_scrub_terms():
    """Employer terms live OUTSIDE the public repo — see dev/scrub_check.py."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from scrub_check import load_terms
        return [t.pattern for t in load_terms(required=False)]
    except Exception:
        return []


# -- moving -----------------------------------------------------------------------------
def move(path, stage, note, queue=None):
    path = Path(path)
    if stage not in STAGES:
        sys.exit(f"stage must be one of {STAGES}")
    queue = Path(queue) if queue else path.parent.parent
    if path.parent.name in ("implemented", "rejected"):
        sys.exit(f"{path.name} is in {path.parent.name}/ — final; not moved")
    dest = queue / stage / path.name
    if dest.exists():
        sys.exit(f"{dest} already exists — not overwritten")
    heading = "## Implementation" if stage == "implemented" else "## Evaluator Notes"
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    text = path.read_text(encoding="utf-8").rstrip("\n")
    text += f"\n\n{heading}\n\n_{stamp} — moved {path.parent.name}/ → {stage}/_\n\n{note.strip()}\n"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)
    path.unlink()
    with open(queue / "evaluator-log.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": stamp, "id": path.stem, "from": path.parent.name,
                             "to": stage, "note": note.strip().splitlines()[0][:200]}) + "\n")
    print(f"{path.parent.name}/{path.name} → {stage}/")


def cmd_list(queue):
    for st in STAGES:
        files = sorted((Path(queue) / st).glob("*.md"))
        print(f"{st}/ ({len(files)})")
        for f in files:
            fm, _ = parse(f.read_text(encoding="utf-8"))
            print(f"  {f.stem}  [{fm.get('category','?')}/{fm.get('priority','?')}]  {fm.get('title','')}")


def _print(res):
    mark = {"ready": "READY", "reject": "REJECT", "escalate": "ESCALATE"}[res["verdict"]]
    print(f"{mark:8s} {Path(res['file']).name}" + (f"  ({res['reason']})" if res["reason"] else ""))
    for e in res["errors"]:
        print(f"   ✗ {e}")
    for w in res["warnings"]:
        print(f"   ! {w}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("file", nargs="?")
    v.add_argument("--all", metavar="QUEUE_DIR")
    v.add_argument("--src", help="plugin root to resolve affected_components against")
    v.add_argument("--json", action="store_true")
    mv = sub.add_parser("move")
    mv.add_argument("file")
    mv.add_argument("stage")
    mv.add_argument("--note", required=True)
    mv.add_argument("--queue")
    ls = sub.add_parser("list")
    ls.add_argument("queue")
    a = ap.parse_args(argv)

    if a.cmd == "move":
        move(a.file, a.stage, a.note, a.queue)
        return 0
    if a.cmd == "list":
        cmd_list(a.queue)
        return 0
    if bool(a.file) == bool(a.all):
        ap.error("validate takes a file or --all <queue-dir>, not both")
    if a.all:
        files = sorted((Path(a.all) / "new").glob("*.md"))
        results = [validate(f, a.src, a.all) for f in files]
    else:
        q = Path(a.file).resolve().parent.parent
        results = [validate(a.file, a.src, q if (q / "new").is_dir() else None)]
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            _print(r)
    worst = {"ready": 0, "escalate": 3, "reject": 1}
    return max((worst[r["verdict"]] for r in results), default=0)


if __name__ == "__main__":
    sys.exit(main())
