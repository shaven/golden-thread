#!/usr/bin/env python3
"""gt_sweep.py -- the WEEKLY cadence: the whole tree, reported rather than blocking.

    gt_sweep.py --vault V [--path P] [--only secrets,code] [--json]

THREE CADENCES, and this is the third. The other two are deliberately narrow:

    tests/run.sh   the file set the scanners define    fails the run
    commit gate    the staged diff only                denies the commit
    THIS           the whole tree                      REPORTS, never blocks

The narrow two are what keep the gates usable: a commit gate that scanned the whole tree would
punish you for someone else's old code, so it looks only at what you are adding. But that means
nothing ever re-examines what is already there -- a rule added today never sees a file nobody
touches, and a credential committed before the gate existed stays committed. The sweep is the
answer to "what is true about the tree as a whole", and because it can surface old debt it must
never block anything: a report you can read on a Monday, not a wall in front of a commit.

WHY IT WRITES TO THE VAULT. A sweep whose output scrolls past in a terminal has told nobody
anything. Each run files a verdict, a count and a scope through gt_check_report -- never a
finding's content, because that file is committed and pushed and a credential's location must
not be. The history is what answers "is this getting better or worse", which is the only
question a weekly number is good for.

RUN IT FROM launchd, the same way gt_lint_weekly.py is wired (see the golden-thread runbook).
Harmless by hand. It exits non-zero only when it could not RUN -- findings are a report, not a
failure, or the weekly job would page you for pre-existing debt every Monday for ever.

Exit: 0 the sweep ran (with or without findings) | 2 usage | 3 a member could not run, so the
sweep does not cover what it claims to cover.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

RAN, USAGE, COULD_NOT_RUN = 0, 2, 3

# name -> (script, what it looks at, the baseline file it honours)
MEMBERS = {
    "secrets": ("gt_secrets.py", "credentials in the wrong place", "secrets-baseline.json"),
    "code": ("gt_scan_code.py", "source validation against the lint rules", "code-baseline.json"),
}
# Archived release directories and the like. A sweep that reports the same finding once per
# retained release is a sweep nobody reads twice -- measured on gt's own repo, where the
# credential scan went from 48 findings to 10 on this exclusion alone.
DEFAULT_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/__pycache__/**")


def run_member(name, path: Path, excludes, vault: Path | None):
    script, _, baseline_name = MEMBERS[name]
    tool = HERE / script
    if not tool.is_file():
        return {"member": name, "status": "could-not-run", "why": "%s is not installed" % script,
                "findings": None}
    cmd = [sys.executable, str(tool), str(path), "--json"]
    for g in excludes:
        cmd += ["--exclude", g]
    baseline = path / ".gt" / baseline_name
    if baseline.is_file():
        cmd += ["--baseline", str(baseline)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {"member": name, "status": "could-not-run",
                "why": "output was not JSON (exit %d)" % proc.returncode, "findings": None}
    n = len(data.get("findings") or [])
    skipped = data.get("skipped") or []
    # Exit 2 from gt_secrets and 4 from gt_scan_code both mean "nothing was scanned", which is
    # a could-not-run however few findings came back. A count of zero from a scan that did not
    # happen is the single failure mode this whole release has been chasing.
    if proc.returncode in (2, 4):
        return {"member": name, "status": "could-not-run",
                "why": "the scanner reported it could not run (exit %d)" % proc.returncode,
                "findings": n}
    return {"member": name, "status": "ran", "findings": n,
            "skipped": [s.get("rule") for s in skipped if isinstance(s, dict)],
            "summary": data.get("summary") or {}}


def record(vault: Path, member: str, result: dict, scope: str):
    tool = HERE / "gt_check_report.py"
    if not tool.is_file() or not vault:
        return
    verdict = ("cannot-run" if result["status"] != "ran"
               else ("findings" if result["findings"] else "clean"))
    cmd = [sys.executable, str(tool), "record", "--vault", str(vault),
           "--check", "%s-weekly" % member, "--verdict", verdict, "--scope", scope]
    if result.get("findings") is not None:
        cmd += ["--count", str(result["findings"])]
    subprocess.run(cmd, capture_output=True, text=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the weekly whole-tree sweep")
    ap.add_argument("--vault", required=True,
                    help="where the report is filed. Required and never inferred: a sweep "
                         "that guessed its vault could file a report into the wrong one.")
    ap.add_argument("--path", default=".", help="the tree to sweep (default: cwd)")
    ap.add_argument("--only", help="comma-separated member names; default all")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    wanted = [m.strip() for m in a.only.split(",")] if a.only else list(MEMBERS)
    unknown = [m for m in wanted if m not in MEMBERS]
    if unknown:
        # Usage, not a silent skip: asking for a member that does not exist and getting a
        # clean sweep is how a typo becomes a check nobody notices is missing.
        print("gt-sweep: unknown member(s): %s. Known: %s"
              % (", ".join(unknown), ", ".join(MEMBERS)), file=sys.stderr)
        return USAGE

    path = Path(a.path).resolve()
    vault = Path(a.vault).expanduser()
    excludes = list(DEFAULT_EXCLUDES) + list(a.exclude)
    scope = os.path.basename(str(path)) or str(path)

    results = [run_member(m, path, excludes, vault) for m in wanted]
    for m, r in zip(wanted, results):
        record(vault, m, r, scope)

    ran = [r for r in results if r["status"] == "ran"]
    stuck = [r for r in results if r["status"] != "ran"]
    total = sum(r["findings"] or 0 for r in ran)

    if a.json:
        print(json.dumps({"path": str(path), "asked": wanted, "results": results,
                          "findings": total}, indent=2))
    else:
        for r in results:
            if r["status"] == "ran":
                extra = (", %d rule(s) SKIPPED" % len(r["skipped"])) if r.get("skipped") else ""
                print("  %-9s %d finding(s)%s" % (r["member"], r["findings"] or 0, extra))
            else:
                print("  %-9s COULD NOT RUN — %s" % (r["member"], r["why"]))
        # How many ran out of how many were asked for, always, and before the finding count.
        print("gt-sweep: %d of %d member(s) ran over %s; %d finding(s)"
              % (len(ran), len(wanted), scope, total))
        if stuck:
            print("A member that could not run is NOT a clean sweep: what it checks is "
                  "unknown for this tree.")

    return COULD_NOT_RUN if stuck else RAN


if __name__ == "__main__":
    sys.exit(main())
