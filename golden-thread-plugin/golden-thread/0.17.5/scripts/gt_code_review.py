#!/usr/bin/env python3
"""gt_code_review.py -- the deterministic FRAMEWORK for code review. gt ships no opinions.

    gt_code_review.py dimensions [--vault V] [--json]
    gt_code_review.py plan       <path> [--vault V] [--staged] [--json]
    gt_code_review.py validate   <findings.json> --root <path> [--json]
    gt_code_review.py report     <findings.json> --root <path> --vault V [--ledger F]

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT.

gt does NOT perform the review. It cannot: "does this abstraction earn its keep" has no
mechanical oracle, and a tool claiming to test that is testing something else. What gt does is
everything AROUND the judgement, which is most of what makes a review trustworthy rather than
decorative:

    plan      which dimensions, over which files -> handed to whatever does the judging
    validate  a finding that cites a file which does not exist, or a line out of range, is
              REJECTED MECHANICALLY before a human ever reads it
    report    only findings that survived validation and verification reach the record, and a
              previously rejected finding never returns as new

So the judgement layer is yours. An agent, a model, a person with a checklist -- gt takes its
findings as JSON and holds them to a contract. That is the seam: gt is testable precisely
because it never pretends to be the reviewer.

NO DIMENSIONS SHIP. The `review` slot is empty by DESIGN, not omission (owner, 2026-09-28):
"create a framework for the type of review, but not automatically tie it in, so the user or
company can add what they want." A dimension is a pack entry, so a company's own review types
arrive through the same registry as everything else and get precedence, SHADOWED reporting,
retraction, scrub checks and provenance for free -- their pack always beats a shipped one.

THEREFORE: ZERO DIMENSIONS IS NOT A CLEAN REVIEW. It is "nothing was reviewed", it exits
non-zero, and it says how to add one. A framework that reports success when nothing is
configured is the inert-artifact failure this release has found six times.

Exit: 0 ok | 1 findings / rejected findings | 2 usage | 3 nothing configured or nothing in
scope -- in either case no review happened, which is never the same as a clean one.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gt_registry                                        # noqa: E402
import gt_scan_code                                       # noqa: E402
import gt_staged                                          # noqa: E402

SLOT = "review"
OK, FINDINGS, USAGE, NOTHING = 0, 1, 2, 3

# A finding's required shape. Deliberately small: every key here is one a VALIDATOR can check
# against the filesystem or a closed vocabulary. Anything a validator could only take on trust
# -- a rationale, a confidence -- is optional and never gates anything.
REQUIRED = ("dimension", "file", "line", "severity", "message")
SEVERITIES = ("info", "warn", "error")


def dimensions(vault):
    """-> (list of dimension dicts, problems). Straight from the registry, no defaults."""
    effective, _shadowed, _retracted, problems = gt_registry.resolve(SLOT, vault=vault)
    out = []
    for rec in effective:
        e = rec["entry"]
        out.append({"id": e.get("id") or "?", "title": e.get("title") or e.get("id") or "",
                    "files": e.get("files"), "rubric": e.get("rubric") or "",
                    "severity_max": e.get("severity_max") or "error",
                    "model_intent": e.get("model_intent") or "balanced",
                    "source": rec["source"], "tier": rec["tier"]})
    return out, problems


def scope_files(root: Path, staged: bool, excludes):
    """The files a review covers -- the SAME definition gt_scan_code uses, imported not copied.

    Two answers to "what counts as code" would be two tools disagreeing about the same tree,
    and the one nobody runs would be the one that was right.
    """
    if staged:
        with gt_staged.scan_staged(root, prefix="gt-review-staged-") as (staged_root, rels):
            if staged_root is None:
                return [], ("not a git repository" if not gt_staged.is_a_repo(root)
                            else "nothing staged")
            return sorted(rels), None
    files = []
    for p in gt_scan_code.walk(root):
        rel = str(p.relative_to(root)) if p != root else p.name
        ok, _why = gt_scan_code.in_scope(p, rel, excludes)
        if ok:
            files.append(rel)
    return sorted(files), None


def applicable(dim, files):
    import fnmatch
    if not dim.get("files"):
        return list(files)
    return [f for f in files if fnmatch.fnmatch(f, dim["files"])]


def cmd_dimensions(a) -> int:
    dims, problems = dimensions(a.vault)
    if a.json:
        print(json.dumps({"dimensions": dims,
                          "problems": [{"where": w, "what": t} for w, t in problems]}, indent=2))
    else:
        for d in dims:
            print("%-22s %-10s %-9s %s" % (d["id"], d["tier"], d["model_intent"],
                                           d["title"][:60]))
        for where, what in problems:
            print("PROBLEM %s: %s" % (where, what), file=sys.stderr)
        print(empty_help() if not dims else "%d dimension(s) configured" % len(dims))
    return NOTHING if not dims else OK


def empty_help() -> str:
    return (
        "0 review dimensions are configured — NOTHING WOULD BE REVIEWED.\n"
        "\n"
        "gt ships none on purpose: a review dimension is an opinion about your code, and it\n"
        "belongs to you or your company rather than to this tool. Add one as a pack in\n"
        "  <vault>/Projects/golden-thread/packs/review.<name>.pack.json\n"
        "\n"
        '  {\"schema\": 1, \"slot\": \"review\", \"name\": \"mine\", \"tier\": \"D\",\n'
        '   \"spdx\": \"MIT\", \"provenance\": {...}, \"dco\": \"Signed-off-by: ...\",\n'
        '   \"entries\": [{\"id\": \"cli-correctness\",\n'
        '                \"title\": \"CLI behaviour and exit codes\",\n'
        '                \"files\": \"*scripts/*.py\",\n'
        '                \"rubric\": \"Every failure path exits non-zero and names ...\"}]}\n'
        "\n"
        "Your pack beats any shipped dimension of the same id, and every dimension it\n"
        "replaces is reported as SHADOWED rather than disappearing.")


def cmd_plan(a) -> int:
    """What WOULD be reviewed. The handoff to whatever does the judging."""
    root = Path(a.path).resolve()
    dims, problems = dimensions(a.vault)
    if not dims:
        print(empty_help(), file=sys.stderr)
        return NOTHING
    files, why = scope_files(root, a.staged, a.exclude)
    if why or not files:
        # An empty scope is an OUTCOME, never a silent upgrade to reviewing everything --
        # which is exactly what the upstream toolkit this borrows its shape from does.
        print("gt-code-review: nothing in scope (%s) — no review planned"
              % (why or "no files matched"))
        return NOTHING
    plan = []
    for d in dims:
        targets = applicable(d, files)
        plan.append({"dimension": d["id"], "title": d["title"], "rubric": d["rubric"],
                     "model_intent": d["model_intent"], "severity_max": d["severity_max"],
                     "files": targets, "source": d["source"]})
    if a.json:
        print(json.dumps({"root": str(root), "files": len(files), "plan": plan,
                          "problems": [{"where": w, "what": t} for w, t in problems]}, indent=2))
    else:
        for row in plan:
            print("%-22s %4d file(s)   %s" % (row["dimension"], len(row["files"]),
                                              row["title"][:50]))
        print("gt-code-review: %d dimension(s) over %d file(s) in scope"
              % (len(plan), len(files)))
    return OK


def validate_findings(rows, root: Path, dims):
    """-> (kept, rejected). A finding must be CHECKABLE, or it does not count.

    This is the part a model cannot do for itself and the part the upstream toolkit leaves to
    one: it asks an LLM whether the cited snippet exists. gt asks the filesystem. A finding
    naming a file that is not there, or a line past the end of it, is rejected before anyone
    reads it -- which turns a hallucinated location from a credibility problem into a caught
    error.
    """
    known = {d["id"] for d in dims}
    kept, rejected = [], []
    cache = {}
    for i, row in enumerate(rows):
        def reject(why):
            rejected.append({"index": i, "why": why,
                             "finding": {k: row.get(k) for k in ("dimension", "file", "line")}})
        if not isinstance(row, dict):
            rejected.append({"index": i, "why": "not an object", "finding": None})
            continue
        missing = [k for k in REQUIRED if row.get(k) in (None, "")]
        if missing:
            reject("missing required field(s): %s" % ", ".join(missing))
            continue
        if row["dimension"] not in known:
            reject("dimension %r is not configured" % row["dimension"])
            continue
        if row["severity"] not in SEVERITIES:
            reject("severity %r is not one of %s" % (row["severity"], "/".join(SEVERITIES)))
            continue
        # `confirmed: false` never reaches a report. The field is optional -- a review with no
        # verification pass simply omits it -- but when present it is honoured.
        if row.get("confirmed") is False:
            reject("not confirmed by verification")
            continue
        target = (root / row["file"])
        if not target.is_file():
            reject("file does not exist: %s" % row["file"])
            continue
        if row["file"] not in cache:
            try:
                cache[row["file"]] = len(target.read_text(encoding="utf-8",
                                                          errors="replace").splitlines())
            except OSError:
                cache[row["file"]] = 0
        n = cache[row["file"]]
        try:
            line = int(row["line"])
        except (TypeError, ValueError):
            reject("line is not a number: %r" % row["line"])
            continue
        if line < 1 or line > max(n, 1):
            reject("line %d is outside %s, which has %d line(s)" % (line, row["file"], n))
            continue
        kept.append(row)
    return kept, rejected


def load_rows(path: Path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, "could not read %s (%s)" % (path, exc.__class__.__name__)
    if isinstance(data, dict):
        data = data.get("findings")
    if not isinstance(data, list):
        return None, "expected a JSON list of findings, or an object with a `findings` list"
    return data, None


def cmd_validate(a) -> int:
    rows, why = load_rows(Path(a.findings))
    if why:
        print("gt-code-review: %s" % why, file=sys.stderr)
        return USAGE
    dims, _problems = dimensions(a.vault)
    if not dims:
        print(empty_help(), file=sys.stderr)
        return NOTHING
    kept, rejected = validate_findings(rows, Path(a.root).resolve(), dims)
    if a.json:
        print(json.dumps({"kept": kept, "rejected": rejected}, indent=2))
    else:
        for r in rejected:
            print("REJECTED [%s] %s" % (r["index"], r["why"]))
        print("gt-code-review: %d finding(s) kept, %d rejected of %d submitted"
              % (len(kept), len(rejected), len(rows)))
    return FINDINGS if (kept or rejected) else OK


def ledger_keys(path: Path):
    if not path or not Path(path).is_file():
        return set()
    out = set()
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            out.add((d.get("dimension"), d.get("file"), d.get("line")))
    except OSError:
        return set()
    return out


def cmd_report(a) -> int:
    rows, why = load_rows(Path(a.findings))
    if why:
        print("gt-code-review: %s" % why, file=sys.stderr)
        return USAGE
    dims, _problems = dimensions(a.vault)
    if not dims:
        print(empty_help(), file=sys.stderr)
        return NOTHING
    root = Path(a.root).resolve()
    kept, rejected = validate_findings(rows, root, dims)

    # A finding declined before must not come back as new. Without this a review re-proposes
    # what was already considered and dismissed, and people stop reading it.
    declined = ledger_keys(Path(a.ledger)) if a.ledger else set()
    fresh = [f for f in kept
             if (f["dimension"], f["file"], int(f["line"])) not in declined]
    repeats = len(kept) - len(fresh)

    date = datetime.date.today().isoformat()
    lines = ["# Code review — %s" % date, "",
             "%d dimension(s) configured · %d finding(s) submitted · %d kept · %d rejected"
             % (len(dims), len(rows), len(kept), len(rejected)), ""]
    if repeats:
        lines.append("%d previously declined finding(s) suppressed by the ledger." % repeats)
        lines.append("")
    for f in fresh:
        lines.append("- **%s** `%s:%s` — %s" % (f["severity"], f["file"], f["line"],
                                                str(f["message"])[:200]))
    if rejected:
        lines += ["", "## Rejected before review", ""]
        lines += ["- [%s] %s" % (r["index"], r["why"]) for r in rejected]
    body = "\n".join(lines) + "\n"

    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(body, encoding="utf-8")
        print("gt-code-review: wrote %s — %d kept, %d rejected, %d suppressed"
              % (a.out, len(fresh), len(rejected), repeats))
    else:
        print(body)
    return FINDINGS if fresh or rejected else OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the deterministic half of code review")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dimensions"); d.set_defaults(fn=cmd_dimensions)
    d.add_argument("--vault"); d.add_argument("--json", action="store_true")

    p = sub.add_parser("plan"); p.set_defaults(fn=cmd_plan)
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--vault"); p.add_argument("--json", action="store_true")
    p.add_argument("--staged", action="store_true")
    p.add_argument("--exclude", action="append", default=[])

    v = sub.add_parser("validate"); v.set_defaults(fn=cmd_validate)
    v.add_argument("findings"); v.add_argument("--root", required=True)
    v.add_argument("--vault"); v.add_argument("--json", action="store_true")

    r = sub.add_parser("report"); r.set_defaults(fn=cmd_report)
    r.add_argument("findings"); r.add_argument("--root", required=True)
    r.add_argument("--vault"); r.add_argument("--ledger"); r.add_argument("--out")

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
