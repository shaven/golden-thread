#!/usr/bin/env python3
"""gt_optimize_exec.py -- the EXECUTION member of /gt:gt-optimize: how work runs, not what it costs
in context.

    gt_optimize_exec.py --vault V [--project P] [--repo R] [--json]     report, ranked by cost
    gt_optimize_exec.py --vault V --apply ID [--dry-run]                apply one accepted finding

Reached as `gt_optimize.py --execution ...` (the same arguments), or directly.

WHY (owner, 2026-10-01): "we have to add that to the optimize worker for golden-thread. It should
be able to look at things like this and optimize how they execute instead of just leaving things
as default. This is horrible." -- and, for every other project: "It should try to find ways to
optimize it as well." The context optimizer finds words that cost tokens on every turn; this
finds executions that cost minutes (and tokens) on every run.

WHAT IT READS, never guesses: the execution rows gt_metrics.py records (per project), the per-unit
test timings tests/prun.py keeps, the `parallel_profile` and the execution settings, and -- with
--repo -- the repo's shell scripts.

WHAT IT REPORTS, each finding with its MEASURED cost (seconds per run, tokens where recorded) and
the expected saving, ranked by cost:

  serial-bottleneck   a process that runs several units one at a time
  redundant-rerun     the same process re-run on the same commit, settings and arguments
  full-when-scoped    the full suite re-run several times a day where a scoped run would do
  slow-step           a pipeline step taking most of its pipeline's time, or a test unit far
                      slower than its peers
  repeated-install    install-heavy test units dominating the suite's time
  regression          a run outside its own rolling baseline, naming what changed since
  peer                a like process in another project that is much cheaper per unit
  default             a default never revisited: an unmeasured parallel profile, test temp files
                      in a watched folder
  recipe-candidate    a hand-written step script a recipe could generate (gt_recipe.py)

A finding about gt's own development is labelled `gt-development`; any other project's findings are
about that project's processes (`project`).

APPLYING. A finding that names an exact change (a setting, a measurement) can be applied with
--apply ID after the owner says yes; the change is recorded as a marker (gt_metrics.py mark) with
its rollback, so `gt_metrics.py verify` later reports the measured before/after against the
baseline -- a change that does not beat it is reported, with the rollback offered. A finding with
no mechanical change says what to change and is never applied by this tool.

Exit: 0 nothing found | 1 findings (or the applied change's result) | 2 usage
"""
import argparse
import glob
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gt_metrics                                           # noqa: E402

GT_PROJECT = "golden-thread"
TIMINGS = os.path.expanduser("~/.claude/golden-thread/prun-timings.json")
MIN_SERIAL_UNITS = 4
MIN_SERIAL_SECS = 10.0
RERUN_WINDOW_S = 24 * 3600
SLOW_UNIT_FACTOR = 3.0
SLOW_UNIT_SECS = 30.0
SLOW_STEP_SHARE = 0.5


def _fid(kind, *parts):
    return "%s-%s" % (kind, hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8])


def finding(kind, project, process, cost_s, saving_s, message, change, tokens=None,
            apply=None, rollback=None, detail=None):
    return {"id": _fid(kind, project, process), "kind": kind, "project": project,
            "process": process,
            "scope": "gt-development" if project == GT_PROJECT else "project",
            "cost_s": round(cost_s or 0.0, 1), "saving_s": round(saving_s or 0.0, 1),
            "tokens": tokens, "message": message, "change": change, "apply": apply,
            "rollback": rollback, "detail": detail or []}


def _median(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return statistics.median(vals) if vals else None


# ----------------------------------------------------------- from metrics ----

def from_metrics(rows, cores):
    out = []
    groups = gt_metrics.by_process(rows)
    for (project, process), runs in sorted(groups.items()):
        ok = [r for r in runs if r.get("exit") == 0]
        if not ok:
            continue
        med = _median([r["duration"] for r in ok])
        units = _median([r.get("units") for r in ok])
        par = _median([r.get("parallel") for r in ok]) or 1
        tokens = _median([r.get("tokens") for r in ok])
        # serial bottleneck
        if units and units >= MIN_SERIAL_UNITS and par <= 1 and med >= MIN_SERIAL_SECS:
            width = max(2, min(int(units), cores))
            saving = med * (1 - 1.0 / width) * 0.7          # conservative: I/O and setup share
            out.append(finding(
                "serial-bottleneck", project, process, med, saving,
                "%s runs %d units one at a time (median %.0fs)" % (process, units, med),
                "run its independent units %d-wide (tests/prun.py, `parallel_work on`, or the "
                "step's own -j)" % width, tokens,
                apply=(["gt_settings.py", "set", "parallel_work", "on"]
                       if process.startswith("tests") else None),
                rollback=("gt_settings.py set parallel_work off"
                          if process.startswith("tests") else None)))
        # redundant re-runs: same commit + settings + args, passing, within a day
        seen, wasted, reps = {}, 0.0, 0
        for r in ok:
            k = (r.get("head"), r.get("config_hash"), r.get("args_hash"), r.get("scope"),
                 r.get("machine"))
            prev = seen.get(k)
            if prev is not None and r["start"] - prev < RERUN_WINDOW_S:
                wasted += r["duration"]
                reps += 1
            seen[k] = r["start"]
        if reps:
            out.append(finding(
                "redundant-rerun", project, process, wasted, wasted,
                "%s re-ran %d time(s) with no change in commit, settings or arguments "
                "(%.0fs spent re-proving a result)" % (process, reps, wasted),
                "trust the receipt: re-run only after a change (gt_test_receipt.py check), or run "
                "the scoped subset (tests/run.sh --affected)", tokens))
        # full suite run several times a day where a scoped run would do
        if gt_metrics.kind_of(process) == "tests":
            full = [r for r in ok if r.get("scope") != "scoped"]
            by_day = {}
            for r in full:
                day = time.strftime("%Y-%m-%d", time.localtime(r["start"]))
                by_day.setdefault(day, []).append(r)
            busy = {d: rs for d, rs in by_day.items() if len(rs) >= 3}
            if busy:
                scoped = [r for (p2, pr2), rs in groups.items() if p2 == project
                          for r in rs if r.get("scope") == "scoped" and r.get("exit") == 0]
                sm = _median([r["duration"] for r in scoped]) or med * 0.2
                extra = sum(len(rs) - 1 for rs in busy.values())
                saving = max(0.0, (med - sm)) * extra
                out.append(finding(
                    "full-when-scoped", project, process, med * extra, saving,
                    "the full suite ran %d time(s) in one day on %d day(s) (median %.0fs each)"
                    % (max(len(rs) for rs in busy.values()), len(busy), med),
                    "on a feature branch run `tests/run.sh --affected` (a scoped receipt the "
                    "commit guard accepts); keep the full suite for the release gate",
                    tokens, apply=["gt_settings.py", "set", "scoped_receipts", "on"],
                    rollback="gt_settings.py set scoped_receipts off"))
    # slow pipeline step: one step taking most of its pipeline's time
    for project in sorted({p for p, _ in groups}):
        steps = {pr: rs for (p, pr), rs in groups.items()
                 if p == project and pr.startswith("release:")}
        if len(steps) < 2:
            continue
        meds = {pr: _median([r["duration"] for r in rs if r.get("exit") == 0])
                for pr, rs in steps.items()}
        meds = {k: v for k, v in meds.items() if v}
        total = sum(meds.values())
        for pr, m in sorted(meds.items(), key=lambda kv: -kv[1]):
            if total and m / total >= SLOW_STEP_SHARE and m >= MIN_SERIAL_SECS:
                out.append(finding(
                    "slow-step", project, pr, m, m * 0.3,
                    "pipeline step %s takes %d%% of the pipeline (median %.0fs of %.0fs)"
                    % (pr.split(":", 1)[1], round(100 * m / total), m, total),
                    "make it cheaper: a scoped run, a cache, or running it once per release "
                    "rather than per commit (gt_pipeline.py set %s --cmd ...)"
                    % pr.split(":", 1)[1]))
            break
    # regressions against each process's own baseline
    for s in gt_metrics.analyse(rows):
        reg = s["regression"]
        if reg:
            med = s["baseline"]["duration"]["median"] or 0
            out.append(finding(
                "regression", s["project"], s["process"], reg["duration"] - med,
                reg["duration"] - med,
                "%s took %.0fs, %.1fx its baseline median (%.0fs)" % (s["process"],
                                                                     reg["duration"],
                                                                     reg["slower_by"], med),
                "find what changed since the baseline: %s" % ("; ".join(reg["changed"]) or
                                                              "nothing recorded changed -- "
                                                              "look at the machine's load"),
                detail=reg["changed"]))
    for p in gt_metrics.peers(rows):
        out.append(finding(
            "peer", p["project"], p["process"], p["per_unit"], p["per_unit"] - p["peer_per_unit"],
            "%s costs %.2fs per unit, %.1fx %s's %s (%.2fs per unit)"
            % (p["process"], p["per_unit"], p["ratio"], p["peer_project"], p["peer_process"],
               p["peer_per_unit"]),
            "do what the cheaper one does: %s" % "; ".join(p["differences"]),
            detail=p["differences"]))
    return out


# ------------------------------------------------------- from test timings ----

def from_timings(path=TIMINGS):
    out = []
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        return out
    for repo, units in sorted(data.items()):
        if not isinstance(units, dict) or len(units) < 5:
            continue
        project = GT_PROJECT if os.path.isfile(os.path.join(repo, "tests", "prun.py")) \
            and os.path.isdir(os.path.join(repo, "golden-thread")) else os.path.basename(repo)
        vals = list(units.values())
        med = statistics.median(vals)
        total = sum(vals)
        slow = sorted(((u, t) for u, t in units.items()
                       if t >= SLOW_UNIT_SECS and t >= med * SLOW_UNIT_FACTOR),
                      key=lambda kv: -kv[1])
        for u, t in slow[:5]:
            out.append(finding(
                "slow-step", project, "tests:unit:" + u, t, t - med,
                "test unit %s takes %.0fs, %.1fx the median unit (%.1fs)" % (u, t, t / med, med),
                "split the class or share its expensive setUp (tests/_harness.cached_sandbox for "
                "a shared install); as the slowest unit it sets the suite's floor"))
        inst = sum(t for u, t in units.items() if "install" in u.lower())
        if total and inst / total >= 0.25:
            out.append(finding(
                "repeated-install", project, "tests:install", inst, inst * 0.5,
                "install-heavy units are %d%% of the suite's unit time (%.0fs of %.0fs)"
                % (round(100 * inst / total), inst, total),
                "build the install once per run and copy it (tests/_harness.cached_sandbox) "
                "wherever the install is a fixture rather than the subject"))
    return out


# ---------------------------------------------------------- from settings ----

def from_settings(cfg):
    out = []
    prof = cfg.get("parallel_profile") or {}
    if prof.get("source") != "measured":
        out.append(finding(
            "default", GT_PROJECT, "settings:parallel_profile", 0, 0,
            "parallel_profile is %s: the worker ceilings are a rule of thumb (io_max = 2 x cores), "
            "never measured on this machine" % ("missing" if not prof else "the install-time guess"),
            "measure it: gt_bench.py writes a profile marked `source: measured`",
            apply=["gt_bench.py"], rollback="gt_settings.py detect-machine --write --force"))
    if sys.platform == "darwin" and str(cfg.get("test_tmpdir", "off")).lower() == "off" \
            and not os.environ.get("GT_TEST_TMPDIR"):
        out.append(finding(
            "default", GT_PROJECT, "settings:test_tmpdir", 0, 0,
            "test temp files go to the system TMPDIR, where the indexer, sync agents and virus "
            "scanner may all react to every throwaway install",
            "put them in a folder those ignore: gt_settings.py set test_tmpdir noindex",
            apply=["gt_settings.py", "set", "test_tmpdir", "noindex"],
            rollback="gt_settings.py set test_tmpdir off"))
    return out


# ------------------------------------------------------------ from a repo ----

BANNER = re.compile(r"^\s*(#\s*[─=-]{2,}\s*\d+\.|echo\s+\"==\s*\d+\.)", re.M)


def from_repo(repo, project):
    out = []
    if not repo or not os.path.isdir(repo):
        return out
    for path in sorted(glob.glob(os.path.join(repo, "**", "*.sh"), recursive=True)):
        if "/." in path.replace(repo, "") or "/node_modules/" in path:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if "GENERATED by gt_recipe.py" in text:
            continue
        lines = text.count("\n")
        banners = len(BANNER.findall(text))
        if lines >= 80 and banners >= 3:
            rel = os.path.relpath(path, repo).replace(os.sep, "/")
            out.append(finding(
                "recipe-candidate", project, "script:" + rel, 0, 0,
                "%s is %d hand-written lines with %d numbered steps" % (rel, lines, banners),
                "write a recipe and generate it (gt_recipe.py render <recipe> --out %s): the "
                "scaffolding is then tested once and the script needs only a smoke run" % rel))
    return out


# --------------------------------------------------------------------- run ----

def analyse(vault, project=None, repo=None):
    try:
        with open(gt_metrics.CONFIG) as fh:
            cfg = json.load(fh)
    except Exception:
        cfg = {}
    rows = gt_metrics.read_rows(vault, project)
    cores = os.cpu_count() or 4
    found = from_metrics(rows, cores) + from_timings() + from_settings(cfg)
    if repo:
        found += from_repo(repo, project or gt_metrics.project_for(repo))
    if project:
        found = [f for f in found if f["project"] == project or f["process"].startswith("settings:")]
    seen, uniq = set(), []
    for f in sorted(found, key=lambda f: (-f["cost_s"], f["id"])):
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    return uniq


def apply_finding(vault, f, dry_run=False):
    if not f.get("apply"):
        print("%s has no mechanical change -- it says what to change: %s" % (f["id"], f["change"]))
        return 2
    script, args = f["apply"][0], f["apply"][1:]
    cmd = [sys.executable or "python3", os.path.join(HERE, script)] + args
    if dry_run:
        print("would run: %s" % " ".join(cmd[1:]))
        print("would mark the change for %s, rollback: %s" % (f["process"], f["rollback"]))
        return 0
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print("the change exited %d; nothing was marked" % rc)
        return 1
    row = gt_metrics.make_row(f["process"], 0, 0, f["project"], row_type="change",
                              change=f["change"], rollback=f["rollback"], start=time.time())
    path = gt_metrics.append(row, vault)
    print("applied %s and marked it (%s). `gt_metrics.py verify --process %s --project %s` "
          "reports the measured before/after once new runs exist; a change that does not beat "
          "its baseline is reported with the rollback: %s"
          % (f["id"], path, f["process"], f["project"], f["rollback"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", help="the vault whose projects' metrics are read")
    ap.add_argument("--project", help="limit to one project slug")
    ap.add_argument("--repo", help="also look at this repo's scripts")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", metavar="ID", help="apply one finding (after the owner's yes)")
    ap.add_argument("--dry-run", action="store_true", help="with --apply: say what would change")
    ap.add_argument("--execution", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    vault = gt_metrics.vault_path(a.vault)
    found = analyse(vault, a.project, a.repo)
    if a.apply:
        f = next((x for x in found if x["id"] == a.apply), None)
        if not f:
            print("no current finding has id %s (findings are recomputed from the metrics on "
                  "every run)" % a.apply)
            return 2
        return apply_finding(vault, f, a.dry_run)
    if a.json:
        print(json.dumps({"version": 1, "findings": found}, indent=2))
        return 1 if found else 0
    if not found:
        print("execution: nothing to optimize in the recorded runs")
        return 0
    print("EXECUTION -- ranked by measured cost (%d finding(s))" % len(found))
    for f in found:
        print("  [%s] %-17s %-12s %-26s cost %6.1fs  saves ~%6.1fs%s"
              % (f["id"], f["kind"], f["scope"], (f["project"] or "")[:26], f["cost_s"],
                 f["saving_s"], "  tokens %s" % f["tokens"] if f["tokens"] else ""))
        print("      %s" % f["message"])
        print("      change: %s%s" % (f["change"], "  (applicable: --apply %s)" % f["id"]
                                      if f["apply"] else ""))
    print("\nNothing has been changed. Apply one, after the owner says yes, with --apply ID.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
