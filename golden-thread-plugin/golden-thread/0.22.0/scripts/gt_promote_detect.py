#!/usr/bin/env python3
"""gt_promote_detect.py -- list promotion candidates a script can see. Never promotes.

    gt_promote_detect.py --vault V [--project SLUG] [--threshold PCT] [--work] [--json] [--dry-run]

/gt:gt-work asks the user to flag promotion candidates, and at the end of a session that step is
the one skipped. Three of the signals are mechanical, so this finds them and /gt:gt-work shows
them; each still needs the user's yes, through /gt:gt-promote or /gt:gt-optimize --demote.

  memory -> research    a Projects/<slug>/memory/ note changed in 3 or more of the last 5 git
                        commits that touched that project's memory/ folder. A note edited
                        session after session has stopped being a transient finding and belongs
                        in research.md or decisions.md. (Needs the vault to be a git repo;
                        without one this signal is skipped and the output says so.)
  research -> Knowledge two projects' research.md sections whose distinct words overlap by at
                        least the `promotion_overlap` setting (default 80%), measured against the
                        smaller section. The shared finding belongs on one Knowledge/ page.
  global -> demotion    a global-memory note naming a project: exactly gt_lint's
                        `global-scope-leak` check, run here rather than reimplemented, so there is
                        one definition of a scope leak. (gt-optimize's `single-project-global`
                        sharpens the same evidence to "names exactly one".)

Overlap is a normalised word-set comparison: lower-cased words, no stemming, no libraries.
Sections under 12 distinct words are not compared; a heading alone is not a finding.

`--work` means "called by /gt:gt-work": the `promotion_candidates` setting is honoured (off ->
exit 0, silent). Nothing found prints nothing and exits 0.

Exit: 0 nothing found (or switched off) | 1 candidates listed | 2 usage
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

COMMITS = 5
MIN_HITS = 3
MIN_WORDS = 12
# A word in more sections than this says nothing about which two sections share a finding.
COMMON = 50
WORD = re.compile(r"[a-z0-9][a-z0-9_-]+")


def setting(name, fallback):
    try:
        import gt_settings                                       # noqa: PLC0415
        v = gt_settings.get(name)
        return v if v else fallback
    except Exception:                                            # noqa: BLE001
        return fallback


def project_slugs(vault, only=None):
    root = os.path.join(vault, "Projects")
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ("memory", ".git", "spool"))
        if "README.md" in filenames and dirpath != root:
            rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
            if only is None or rel == only or rel.split("/")[0] == only:
                out.append(rel)
    return out


def git(vault, *args):
    try:
        p = subprocess.run(["git", "-C", vault] + list(args), capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def memory_churn(vault, slugs):
    """-> (candidates, skipped_reason)."""
    if git(vault, "rev-parse", "--is-inside-work-tree") is None:
        return [], "the vault is not a git repository"
    out = []
    for slug in slugs:
        mem = "Projects/%s/memory" % slug
        if not os.path.isdir(os.path.join(vault, mem)):
            continue
        log = git(vault, "log", "-n", str(COMMITS), "--name-only", "--format=@@%H", "--", mem)
        if not log:
            continue
        counts, commits = {}, 0
        for line in log.splitlines():
            if line.startswith("@@"):
                commits += 1
            elif line.strip().startswith(mem + "/") and line.strip().endswith(".md"):
                counts[line.strip()] = counts.get(line.strip(), 0) + 1
        for rel, n in sorted(counts.items()):
            if os.path.basename(rel).upper() == "MEMORY.MD" or n < MIN_HITS:
                continue
            if not os.path.isfile(os.path.join(vault, rel)):
                continue
            out.append({"signal": "memory-to-research", "path": rel,
                        "destination": "Projects/%s/research.md or decisions.md" % slug,
                        "why": "changed in %d of the last %d commits to %s/ -- edited session "
                               "after session, so it is settled enough for the durable tier"
                               % (n, commits, mem)})
    return out, None


def sections(text):
    out, cur, fence = [], None, False
    for line in text.split("\n"):
        if line.strip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and line.startswith("## "):
            cur = {"heading": line[3:].strip(), "body": []}
            out.append(cur)
        elif cur is not None:
            cur["body"].append(line)
    return out


def research_overlap(vault, slugs, threshold):
    secs = []
    for slug in slugs:
        rel = "Projects/%s/research.md" % slug
        try:
            with open(os.path.join(vault, rel), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for s in sections(text):
            ws = set(WORD.findall(" ".join(s["body"]).lower()))
            if len(ws) >= MIN_WORDS:
                secs.append((slug, rel, s["heading"], ws))
    index = {}
    for i, (_slug, _rel, _h, ws) in enumerate(secs):
        for w in ws:
            index.setdefault(w, []).append(i)
    shared = {}
    for w, ids in index.items():
        if len(ids) > COMMON:
            continue
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                i, j = ids[x], ids[y]
                if secs[i][0] != secs[j][0]:
                    shared[(i, j)] = shared.get((i, j), 0) + 1
    out = []
    for (i, j), n in sorted(shared.items()):
        a, b = secs[i], secs[j]
        # Recomputed on the full sets: the index skipped common words to stay fast, and the
        # overlap reported must be the true one.
        both = len(a[3] & b[3])
        pct = 100.0 * both / min(len(a[3]), len(b[3]))
        if pct >= threshold:
            out.append({"signal": "research-to-knowledge",
                        "path": "%s#%s" % (a[1], a[2]), "other": "%s#%s" % (b[1], b[2]),
                        "overlap_pct": round(pct, 1),
                        "destination": "Knowledge/<page>.md",
                        "why": "%.0f%% word overlap with %s#%s -- the same finding in two "
                               "projects belongs on one Knowledge page both link to"
                               % (pct, b[1], b[2])})
    return sorted(out, key=lambda c: -c["overlap_pct"])


def global_leaks(vault):
    """gt_lint's own check, run as-is: one definition of a scope leak."""
    import gt_lint                                               # noqa: PLC0415
    findings = []
    gt_lint.check_global_scope_leak(Path(vault), findings, gt_lint.read_suppress_list(Path(vault)))
    out = []
    for f in findings:
        m = re.search(r"project slug '([^']+)'", f["message"])
        slug = m.group(1) if m else "<slug>"
        out.append({"signal": "global-demotion", "path": f["path"],
                    "destination": "Projects/%s/memory/" % slug,
                    "why": "%s (gt_lint global-scope-leak); demote with gt_optimize.py --demote "
                           "%s --to project-memory --project %s" % (f["message"], f["path"], slug)})
    return out


def _spool():
    """The vault tools' gt_spool (templates/tools): the ONE slug -> Projects/<path>
    resolver (0.18.1), shared with gt_adr and vault_init."""
    tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "templates", "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import gt_spool
    return gt_spool


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", required=True, help="the vault (never inferred)")
    ap.add_argument("--project", help="limit the memory signal to one project slug")
    ap.add_argument("--threshold", type=float, default=None,
                    help="research overlap %% (default: setting promotion_overlap, 80)")
    ap.add_argument("--work", action="store_true",
                    help="called by /gt:gt-work: honour the promotion_candidates setting")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="accepted for the vault-tool contract; this tool never writes")
    args = ap.parse_args(argv)

    if args.work and setting("promotion_candidates", "on") == "off":
        return 0
    if args.project and ("\\" in args.project or args.project in (".", "..")
                         or ".." in args.project.split("/")):
        print("--project takes a slug, not a path: %r" % args.project, file=sys.stderr)
        return 2
    vault = os.path.abspath(os.path.expanduser(args.vault))
    try:
        os.stat(vault)       # a refused stat (sandbox mode) is a refusal, not "not a vault"
    except (FileNotFoundError, NotADirectoryError):
        pass
    if not os.path.isdir(vault):
        print("not a vault directory: %s" % vault, file=sys.stderr)
        return 2
    if args.project:
        # The shared resolver (0.18.1): a bare sub-project slug means its parent/child path,
        # which is what project_slugs() and the overlap paths are keyed on.
        S = _spool()
        try:
            args.project = S.resolve_project(vault, args.project,
            allow_unregistered=True)  # an existing folder, as before; never creates
        except S.ProjectNotFound as exc:
            print("gt_promote_detect: %s" % exc, file=sys.stderr)
            return 2
    threshold = args.threshold
    if threshold is None:
        try:
            threshold = float(setting("promotion_overlap", "80"))
        except ValueError:
            threshold = 80.0

    mine = project_slugs(vault, args.project)
    churn, skipped = memory_churn(vault, mine)
    # Overlap is ACROSS projects, so it always reads every project; --project keeps only the
    # pairs that involve it.
    overlap = research_overlap(vault, project_slugs(vault), threshold)
    if args.project:
        tag = "Projects/%s/" % args.project
        overlap = [c for c in overlap if c["path"].startswith(tag) or c["other"].startswith(tag)]
    cands = churn + overlap + global_leaks(vault)

    if args.json:
        print(json.dumps({"version": 1, "threshold_pct": threshold,
                          "skipped": {"memory-to-research": skipped} if skipped else {},
                          "candidates": cands}, indent=2))
        return 1 if cands else 0
    if not cands:
        return 0
    print("Promotion candidates (%d) -- nothing moves without your yes:" % len(cands))
    for c in cands:
        print("  [%s] %s\n      -> %s\n      %s" % (c["signal"], c["path"], c["destination"],
                                                   c["why"]))
    if skipped:
        print("  (memory -> research not checked: %s)" % skipped)
    return 1


if __name__ == "__main__":
    # A refusal from the OS -- gt sandbox mode, or a macOS interpreter refusal -- is one line
    # naming the next step, not a traceback (0.20.1, B4). gt_errors sits beside this file.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gt_errors as _gte
    except ImportError:
        _gte = None
    raise SystemExit(_gte.run(main, "gt_promote_detect") if _gte else main())
