#!/usr/bin/env python3
"""gt_metrics.py -- one row per execution of every repeated dev process, in every project.

    gt_metrics.py record  --process ID --duration S --exit N [--project P] [--repo R]
                          [--units N] [--parallel N] [--retries N] [--reruns N] [--tokens N]
                          [--scope full|scoped] [--trait T ...] [--args ...] [--vault V] [--dry-run]
    gt_metrics.py time    --process ID [--project P] -- <command ...>    run it and record it
    gt_metrics.py report  [--project P] [--json] [--vault V]             baselines + regressions
    gt_metrics.py peers   [--kind K] [--json] [--vault V]                like processes compared
    gt_metrics.py mark    --process ID --change TEXT [--rollback CMD] [--start T] [--project P]
    gt_metrics.py verify  --process ID [--project P] [--json] [--vault V]  did the change pay?

WHY (owner, 2026-10-01): "This isn't just about builds. It is about all processes inside of how a
dev works ... Maybe it should capture metrics on different executions across the board ... That
way it would have something to compare it to decide if there are things it can speed up." The
optimizer (gt_optimize_exec.py, `/gt:gt-optimize --execution`) reads these rows; without them every
saving it proposed would be a guess.

WHAT A ROW IS. {v, project, process, kind, start, duration, exit, units, parallel, retries,
reruns, tokens, machine, args_hash, head, config_hash, scope, traits, type}. `kind` is the part of
the process id before the first colon (tests, release, build, deploy, data, skill, agent, ...), so
`tests:suite` in two projects is comparable.

WHERE ROWS LIVE. Per project, as a GENERATED store outside the write queue (like events.jsonl):
<vault>/Projects/<slug>/metrics/<machine>-<YYYY-MM>.jsonl -- one file per machine per month, so
two machines syncing one vault never append to the same file. A project with no folder in the
vault (or no vault at all) records to ~/.claude/golden-thread/metrics/<slug>/ instead; `report`
reads both. The project is --project, else $GT_METRICS_PROJECT, else `.gt/project` in the repo,
else the repo folder's name.

WHAT IS NEVER STORED. Command arguments: `--args` is hashed (sha256, 16 hex) and only the hash is
kept, so two runs can be told apart without the arguments ever touching disk. Every stored string
(process id, traits, machine) is checked for a credential shape first; one that looks like a
secret is replaced by its hash. The setting `execution_metrics off` records nothing at all.

BASELINES. Each process's rolling baseline is the median and p90 of duration, tokens and units
over its last BASELINE_N successful runs, the newest excluded. A newest run whose duration is
above max(p90, median * 1.25) is a REGRESSION, and the report names what changed since the
baseline: commit, settings hash, scope, parallelism, units, traits.

CHANGES. `mark` records an applied optimization (what changed, how to roll it back) as a marker
row. `verify` compares the runs after the newest marker with the baseline before it: a change
that does not beat its baseline is reported as such, with the rollback.

Exit: 0 ok | 1 report: a regression / verify: the change did not pay | 2 usage
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCHEMA = 1
BASELINE_N = 20
REGRESSION_FACTOR = 1.25
LOCAL_ROOT = os.path.expanduser("~/.claude/golden-thread/metrics")
CONFIG = os.path.expanduser("~/.claude/vault-config.json")
KINDS = ("tests", "release", "build", "deploy", "data", "skill", "agent", "writeback",
         "validation", "install", "bench")
# Credential shapes, checked on every stored string. Deliberately broad: a false positive costs a
# readable process name (it is stored as its hash); a false negative stores a secret.
SECRET_SHAPES = (
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|bearer|authorization)\s*[=:]"),
    re.compile(r"\b(sk|pk|rk|ghp|gho|ghs|xox[abpr])[-_][A-Za-z0-9_-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}"),
)


# ------------------------------------------------------------------- basics ----

def _config():
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def enabled():
    try:
        import gt_settings
        return gt_settings.get("execution_metrics") != "off"
    except Exception:
        return str(_config().get("execution_metrics", "on")).lower() != "off"


def vault_path(explicit=None):
    v = explicit or os.environ.get("GT_VAULT") or _config().get("vault_path")
    return os.path.abspath(os.path.expanduser(v)) if v else None


def h16(text):
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def looks_secret(text):
    if not text:
        return False
    return any(rx.search(text) for rx in SECRET_SHAPES)


def clean(text):
    """A string fit to store: itself, or `hash:<16 hex>` if it looks like a credential."""
    text = str(text or "")
    return "hash:" + h16(text) if looks_secret(text) else text


def machine():
    return clean(socket.gethostname().split(".")[0] or "unknown")


def _git(repo, *args):
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def project_for(repo=None, explicit=None):
    if explicit:
        return explicit
    if os.environ.get("GT_METRICS_PROJECT"):
        return os.environ["GT_METRICS_PROJECT"]
    root = _git(repo or os.getcwd(), "rev-parse", "--show-toplevel") or (repo or os.getcwd())
    try:
        with open(os.path.join(root, ".gt", "project")) as fh:
            slug = fh.read().strip()
            if slug:
                return slug
    except OSError:
        pass
    return os.path.basename(os.path.realpath(root)) or "unknown"


def config_hash():
    """A hash of the settings that change how work executes, so a regression can name a
    setting change without storing the settings."""
    c = _config()
    keys = sorted(k for k in c if k.startswith(("parallel", "test_", "execution_", "runners")))
    return h16(json.dumps({k: c[k] for k in keys}, sort_keys=True, default=str))


def _safe_slug(slug):
    return re.sub(r"[^A-Za-z0-9._/-]", "-", slug).strip("/").replace("..", "-") or "unknown"


def store_dir(project, vault=None):
    """The vault project's metrics/ folder when the project exists there, else the local one."""
    slug = _safe_slug(project)
    if vault and os.path.isdir(os.path.join(vault, "Projects", slug)):
        return os.path.join(vault, "Projects", slug, "metrics")
    return os.path.join(LOCAL_ROOT, slug)


def kind_of(process):
    k = process.split(":", 1)[0].lower()
    return k if k else "other"


# ------------------------------------------------------------------ record ----

def make_row(process, duration, exit_code, project, repo=None, units=None, parallel=None,
             retries=0, reruns=0, tokens=None, scope=None, traits=(), args=None, start=None,
             row_type="run", change=None, rollback=None):
    repo = repo or os.getcwd()
    row = {"v": SCHEMA, "type": row_type, "project": clean(project), "process": clean(process),
           "kind": kind_of(clean(process)),
           "start": round(start if start is not None else time.time() - float(duration or 0), 3),
           "duration": round(float(duration or 0), 3), "exit": int(exit_code or 0),
           "units": units, "parallel": parallel, "retries": int(retries or 0),
           "reruns": int(reruns or 0), "tokens": tokens, "machine": machine(),
           "args_hash": h16("\0".join(args)) if args else None,
           "head": _git(repo, "rev-parse", "--short=12", "HEAD") or None,
           "config_hash": config_hash(), "scope": clean(scope) if scope else None,
           "traits": sorted({clean(t) for t in traits if t})}
    if row_type == "change":
        row["change"] = clean(change)
        row["rollback"] = clean(rollback) if rollback else None
    return row


def append(row, vault=None, dry_run=False):
    d = store_dir(row["project"], vault)
    path = os.path.join(d, "%s-%s.jsonl" % (row["machine"],
                                             datetime.date.today().strftime("%Y-%m")))
    if dry_run:
        return path
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def record_quietly(process, duration, exit_code, project=None, repo=None, **kw):
    """For callers in Python (gt skills, prun): never raises, never prints."""
    try:
        if not enabled():
            return None
        row = make_row(process, duration, exit_code, project_for(repo, project), repo=repo, **kw)
        return append(row, vault_path())
    except Exception:
        return None


# -------------------------------------------------------------------- read ----

def read_rows(vault=None, project=None):
    dirs = []
    if vault and os.path.isdir(os.path.join(vault, "Projects")):
        pat = os.path.join(vault, "Projects", _safe_slug(project) if project else "**", "metrics")
        dirs += glob.glob(pat, recursive=True)
    if project:
        dirs.append(os.path.join(LOCAL_ROOT, _safe_slug(project)))
    else:
        dirs += glob.glob(os.path.join(LOCAL_ROOT, "*"))
    rows = []
    for d in sorted(set(dirs)):
        for f in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
            try:
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(r, dict) and r.get("process"):
                            rows.append(r)
            except OSError:
                continue
    rows.sort(key=lambda r: r.get("start", 0))
    return rows


def _pct(values, p):
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 3)


def baseline(runs):
    """-> {n, duration:{median,p90}, tokens:{...}, units:{...}} over the last BASELINE_N."""
    ok = [r for r in runs if r.get("exit") == 0][-BASELINE_N:]
    out = {"n": len(ok)}
    for f in ("duration", "tokens", "units"):
        vals = [r.get(f) for r in ok if isinstance(r.get(f), (int, float))]
        out[f] = {"median": round(statistics.median(vals), 3) if vals else None,
                  "p90": _pct(vals, 0.9)}
    return out


def what_changed(base_runs, newest):
    """Name the inputs the newest run does not share with its baseline."""
    out = []
    for field, label in (("head", "commit"), ("config_hash", "settings"), ("scope", "scope"),
                         ("parallel", "parallelism"), ("units", "units"),
                         ("args_hash", "arguments"), ("machine", "machine")):
        seen = [r.get(field) for r in base_runs if r.get(field) is not None]
        if not seen:
            continue
        common = max(set(map(str, seen)), key=lambda v: [str(x) for x in seen].count(v))
        now = newest.get(field)
        if now is not None and str(now) != common:
            out.append("%s %s -> %s" % (label, common, now))
    base_traits = set()
    for r in base_runs:
        base_traits |= set(r.get("traits") or [])
    added = sorted(set(newest.get("traits") or []) - base_traits)
    if added:
        out.append("traits added: %s" % ", ".join(added))
    return out


def by_process(rows):
    out = {}
    for r in rows:
        if r.get("type", "run") != "run":
            continue
        out.setdefault((r.get("project"), r["process"]), []).append(r)
    return out


def analyse(rows):
    """-> per-process summaries with baseline and a regression verdict."""
    out = []
    for (project, process), runs in sorted(by_process(rows).items()):
        newest = runs[-1]
        prior = runs[:-1]
        base = baseline(prior)
        reg = None
        med, p90 = base["duration"]["median"], base["duration"]["p90"]
        if base["n"] >= 3 and med and newest.get("exit") == 0:
            limit = max(p90 or 0, med * REGRESSION_FACTOR)
            if newest["duration"] > limit:
                reg = {"duration": newest["duration"], "limit": round(limit, 3),
                       "slower_by": round(newest["duration"] / med, 2),
                       "changed": what_changed([r for r in prior if r.get("exit") == 0]
                                               [-BASELINE_N:], newest)}
        out.append({"project": project, "process": process, "kind": newest.get("kind"),
                    "runs": len(runs), "newest": newest, "baseline": base, "regression": reg})
    return out


def per_unit(summary):
    b = summary["baseline"]
    d, u = b["duration"]["median"], b["units"]["median"]
    if d and u:
        return d / u
    return None


def peers(rows, kind=None):
    """Like processes (same kind) across projects, cheapest per unit first, and what the
    cheapest does differently from each costlier one."""
    sums = [s for s in analyse(rows) if (kind is None or s["kind"] == kind) and per_unit(s)]
    out = []
    by_kind = {}
    for s in sums:
        by_kind.setdefault(s["kind"], []).append(s)
    for k, group in sorted(by_kind.items()):
        projects = {s["project"] for s in group}
        if len(projects) < 2:
            continue
        group.sort(key=per_unit)
        best = group[0]
        for s in group[1:]:
            if s["project"] == best["project"]:
                continue
            ratio = per_unit(s) / per_unit(best)
            if ratio < 2:
                continue
            diffs = []
            bn, sn = best["newest"], s["newest"]
            if (bn.get("parallel") or 1) > (sn.get("parallel") or 1):
                diffs.append("runs %s-wide where this runs %s-wide"
                             % (bn.get("parallel"), sn.get("parallel") or 1))
            if bn.get("scope") == "scoped" and sn.get("scope") != "scoped":
                diffs.append("runs a scoped subset where this runs in full")
            extra = sorted(set(bn.get("traits") or []) - set(sn.get("traits") or []))
            if extra:
                diffs.append("uses %s" % ", ".join(extra))
            out.append({"kind": k, "process": s["process"], "project": s["project"],
                        "per_unit": round(per_unit(s), 3), "peer_project": best["project"],
                        "peer_process": best["process"], "peer_per_unit": round(per_unit(best), 3),
                        "ratio": round(ratio, 1),
                        "differences": diffs or ["no recorded difference -- compare the two "
                                                 "processes' steps by hand"]})
    return out


def verify(rows, project, process):
    """Runs after the newest change marker vs the baseline before it."""
    rows = [r for r in rows if r.get("project") == project and r.get("process") == process]
    marks = [r for r in rows if r.get("type") == "change"]
    if not marks:
        return {"verdict": "no-change", "detail": "no change is marked for %s" % process}
    mark = marks[-1]
    before = [r for r in rows if r.get("type", "run") == "run" and r["start"] < mark["start"]]
    after = [r for r in rows if r.get("type", "run") == "run" and r["start"] >= mark["start"]]
    b, a = baseline(before), baseline(after)
    if not b["n"] or not a["n"]:
        return {"verdict": "unmeasured", "change": mark.get("change"),
                "detail": "%d run(s) before and %d after the change: not enough to measure"
                          % (b["n"], a["n"]), "rollback": mark.get("rollback")}
    bm, am = b["duration"]["median"], a["duration"]["median"]
    saved = round(bm - am, 3)
    verdict = "beat" if am < bm * 0.95 else "did-not-beat"
    return {"verdict": verdict, "change": mark.get("change"), "before_median": bm,
            "after_median": am, "saved_per_run": saved,
            "saved_pct": round(100.0 * saved / bm, 1) if bm else None,
            "runs_before": b["n"], "runs_after": a["n"],
            "rollback": mark.get("rollback") if verdict != "beat" else None}


# --------------------------------------------------------------------- CLI ----

def _fmt_s(v):
    return "-" if v is None else ("%.1fs" % v)


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", default=argparse.SUPPRESS,
                        help="vault (default: $GT_VAULT, then vault-config.json)")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="say where the row would go; write nothing")
    common.add_argument("--project", default=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", parents=[common], help="append one execution row")
    rec.add_argument("--process", required=True)
    rec.add_argument("--duration", type=float, required=True)
    rec.add_argument("--exit", type=int, required=True)
    rec.add_argument("--start", type=float)
    rec.add_argument("--repo")
    for f in ("units", "parallel", "retries", "reruns", "tokens"):
        rec.add_argument("--" + f, type=int)
    rec.add_argument("--scope", choices=("full", "scoped"))
    rec.add_argument("--trait", action="append", default=[])
    rec.add_argument("--args", nargs=argparse.REMAINDER,
                     help="the command's arguments: HASHED, never stored")
    tm = sub.add_parser("time", parents=[common], help="run a command and record it")
    tm.add_argument("--process", required=True)
    tm.add_argument("--trait", action="append", default=[])
    tm.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("report", parents=[common], help="baselines and regressions")
    pr = sub.add_parser("peers", parents=[common], help="like processes across projects")
    pr.add_argument("--kind")
    mk = sub.add_parser("mark", parents=[common], help="record an applied optimization")
    mk.add_argument("--process", required=True)
    mk.add_argument("--change", required=True)
    mk.add_argument("--rollback")
    mk.add_argument("--repo")
    mk.add_argument("--start", type=float, help="when the change took effect (default: now)")
    vf = sub.add_parser("verify", parents=[common], help="did the newest change beat its baseline")
    vf.add_argument("--process", required=True)
    vf.add_argument("--repo")
    a = ap.parse_args(argv)
    vault = vault_path(getattr(a, "vault", None))
    dry = getattr(a, "dry_run", False)
    as_json = getattr(a, "json", False)
    project_arg = getattr(a, "project", None)

    if a.cmd in ("record", "time", "mark") and not enabled():
        print("execution_metrics is off: nothing recorded")
        if a.cmd == "time" and a.command:
            cmd = a.command[1:] if a.command[0] == "--" else a.command
            return subprocess.run(cmd).returncode
        return 0

    if a.cmd == "record":
        project = project_for(a.repo, project_arg)
        args = a.args[1:] if a.args and a.args[0] == "--" else a.args
        row = make_row(a.process, a.duration, a.exit, project, repo=a.repo, units=a.units,
                       parallel=a.parallel, retries=a.retries, reruns=a.reruns, tokens=a.tokens,
                       scope=a.scope, traits=a.trait, args=args, start=a.start)
        path = append(row, vault, dry)
        print("%s %s %.1fs exit %d -> %s" % ("would record" if dry else "recorded",
                                              row["process"], row["duration"], row["exit"], path))
        return 0
    if a.cmd == "time":
        cmd = a.command[1:] if a.command and a.command[0] == "--" else a.command
        if not cmd:
            print("time: give the command after --", file=sys.stderr)
            return 2
        t0 = time.time()
        rc = subprocess.run(cmd).returncode
        row = make_row(a.process, time.time() - t0, rc, project_for(None, project_arg),
                       traits=a.trait, args=cmd[1:], start=t0)
        path = append(row, vault, dry)
        print("%s %s %.1fs exit %d -> %s" % ("would record" if dry else "recorded",
                                              row["process"], row["duration"], rc, path),
              file=sys.stderr)
        return rc
    if a.cmd == "mark":
        project = project_for(a.repo, project_arg)
        row = make_row(a.process, 0, 0, project, repo=a.repo, row_type="change",
                       change=a.change, rollback=a.rollback,
                       start=a.start if a.start is not None else time.time())
        path = append(row, vault, dry)
        print("%s change for %s: %s -> %s" % ("would mark" if dry else "marked", a.process,
                                              row["change"], path))
        return 0

    rows = read_rows(vault, project_arg)
    if a.cmd == "verify":
        project = project_for(a.repo, project_arg)
        res = verify(rows, project, a.process)
        if as_json:
            print(json.dumps(res, indent=2))
        else:
            if res["verdict"] in ("beat", "did-not-beat"):
                print("%s: %s -- median %.1fs before (%d runs), %.1fs after (%d runs): %+.1fs "
                      "(%s%%) per run"
                      % (a.process, res["verdict"].upper(), res["before_median"],
                         res["runs_before"], res["after_median"], res["runs_after"],
                         -res["saved_per_run"], res["saved_pct"]))
                if res["verdict"] == "did-not-beat":
                    print("the change (%s) did not beat its baseline.%s"
                          % (res["change"], " Roll back: %s" % res["rollback"]
                             if res.get("rollback") else " No rollback was recorded."))
            else:
                print("%s: %s" % (a.process, res["detail"]))
        return 1 if res["verdict"] == "did-not-beat" else 0
    if a.cmd == "peers":
        res = peers(rows, a.kind)
        if as_json:
            print(json.dumps(res, indent=2))
        elif not res:
            print("no like processes in two or more projects differ by 2x or more per unit")
        else:
            for p in res:
                print("%s %s (%s): %.2fs/unit, %.1fx the cost of %s %s (%.2fs/unit)"
                      % (p["kind"], p["process"], p["project"], p["per_unit"], p["ratio"],
                         p["peer_project"], p["peer_process"], p["peer_per_unit"]))
                for d in p["differences"]:
                    print("    the cheaper one %s" % d if not d.startswith("no ") else "    " + d)
        return 0
    # report
    res = analyse(rows)
    if as_json:
        print(json.dumps({"version": 1, "processes": res}, indent=2, default=str))
        return 1 if any(s["regression"] for s in res) else 0
    if not res:
        print("no execution metrics recorded%s" % (" for %s" % project_arg if project_arg else ""))
        return 0
    print("%-22s %-28s %5s %9s %9s %9s  %s" % ("PROJECT", "PROCESS", "RUNS", "MEDIAN", "P90",
                                              "NEWEST", "VERDICT"))
    for s in res:
        b = s["baseline"]["duration"]
        verdict = ("REGRESSION %.1fx: %s" % (s["regression"]["slower_by"],
                                             "; ".join(s["regression"]["changed"]) or
                                             "nothing recorded changed")
                   if s["regression"] else ("baseline n=%d" % s["baseline"]["n"]))
        print("%-22s %-28s %5d %9s %9s %9s  %s" % (str(s["project"])[:22], s["process"][:28],
                                                   s["runs"], _fmt_s(b["median"]),
                                                   _fmt_s(b["p90"]),
                                                   _fmt_s(s["newest"]["duration"]), verdict))
    return 1 if any(s["regression"] for s in res) else 0


if __name__ == "__main__":
    sys.exit(main())
