#!/usr/bin/env python3
"""gt_bench.py -- measure how wide (and where) work runs fastest, and write that profile instead
of guessing it.

    gt_bench.py [--dry-run] [--json] [--widths 1,2,4,8] [--units N] [--repeat R] [--budget S]
                [--location DIR] [--hosts [a,b]]
    gt_bench.py health [--json]          the execution rows gt_doctor.py reports
    gt_bench.py recall --fixture DIR [--retriever keyword|FILE.py ...] [--json]
                                         recall@1/3/10 of vault lookup (0.19.1)

WHY (owner, 2026-09-28): "Create a test suite for optimal performance and then maybe we add that
to golden-thread so you can get different setups for execution of tasks with golden-thread to
help optimize it for everyone." install.sh writes `parallel_profile` from the core count --
cpu_max = cores, io_max = 2 x cores -- and the 2x was reasoning, never a measurement. On the
machine that prompted this the limit was not the cores at all: load average 28 on 16 cores from
the scanner, the indexer and the sync daemon, before a single test ran.

WHAT IT DOES, in a bounded few minutes rather than a full suite:

  1. WIDTH. A synthetic workload -- file churn (write, fsync, read, delete) plus a subprocess per
     unit for the I/O curve; hashing in a subprocess per unit for the CPU curve -- at several
     worker counts, each repeated and the median kept. The KNEE (the smallest width reaching 90%
     of the best throughput) becomes io_max / cpu_max.
  2. LOCATION (--location DIR). The same I/O workload in DIR (e.g. the working tree) and in the
     system temp dir; a materially slower DIR is reported, with the advice to run from a copy.
  3. ENVIRONMENT. Load before and during, and the top processes that are not this run --
     named, never touched: this tool never disables or reconfigures security software.
  4. HOSTS (--hosts, opt-in). Each listed runner (the `runners` setting, or --hosts a,b) runs the
     same calibration over ssh -- this script on stdin, nothing else; nothing from the vault ever
     leaves the machine -- and the profile records its capacity, which prun.py --hosts uses to
     split units heaviest first.

WHAT IT WRITES: `parallel_profile` in vault-config.json, with `source: measured`, `measured_at`,
the workload, and the curve it chose from. Every number says how and when it was measured; a
field this run did not measure keeps the `default` it had. install.sh keeps a measured profile
across upgrades while the machine's core count and name are unchanged (gt_settings.py).
`--dry-run` prints the profile and writes nothing.

The run refuses to exceed --budget seconds (default 180): widths not reached are reported as not
measured, and a partial curve is marked `partial: true`.

GT_BENCH_FAKE='{"io": {"1": 10, "2": 19, ...}, "cpu": {...}}' replaces the measured throughputs
(tests, and rehearsing the knee rule).

Exit: 0 profile measured (and written unless --dry-run) | 1 nothing could be measured | 2 usage
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

KNEE = 0.90
DEFAULT_BUDGET = 180
IO_UNIT = r"""
import os, sys, tempfile
d = tempfile.mkdtemp(dir=sys.argv[1])
for i in range(40):
    p = os.path.join(d, "f%d" % i)
    with open(p, "wb") as fh:
        fh.write(os.urandom(4096)); fh.flush(); os.fsync(fh.fileno())
for i in range(40):
    with open(os.path.join(d, "f%d" % i), "rb") as fh:
        fh.read()
for i in range(40):
    os.unlink(os.path.join(d, "f%d" % i))
os.rmdir(d)
"""
CPU_UNIT = r"""
import hashlib
b = b"x" * (1 << 20)
h = hashlib.sha256()
for _ in range(60):
    h.update(b)
"""


def _fake():
    raw = os.environ.get("GT_BENCH_FAKE")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _run_units(code, width, n, where):
    py = sys.executable or "python3"

    def one(_):
        subprocess.run([py, "-c", code, where], capture_output=True)
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=width) as pool:
        list(pool.map(one, range(n)))
    return n / max(1e-6, time.time() - t0)


def curve(kind, widths, units, repeat, where, budget_end):
    """-> ([[width, units_per_s], ...], partial)."""
    fake = _fake()
    out, partial = [], False
    for w in widths:
        if fake is not None:
            v = (fake.get(kind) or {}).get(str(w))
            if v is not None:
                out.append([w, float(v)])
            continue
        if time.time() >= budget_end:
            partial = True
            break
        n = max(units, 2 * w)
        code = IO_UNIT if kind == "io" else CPU_UNIT
        vals = [_run_units(code, w, n, where) for _ in range(repeat)]
        out.append([w, round(statistics.median(vals), 2)])
    return out, partial


def knee(points):
    """The smallest width reaching KNEE of the best throughput."""
    if not points:
        return None
    best = max(v for _, v in points)
    for w, v in sorted(points):
        if v >= KNEE * best:
            return w
    return points[-1][0]


def load_now():
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None


def top_processes(n=5):
    """The busiest processes that are not this run: names only, never touched."""
    try:
        p = subprocess.run(["ps", "-Ao", "pid=,pcpu=,comm="], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    me = {os.getpid()}
    rows = []
    for line in p.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, cpu = int(parts[0]), float(parts[1])
        except ValueError:
            continue
        name = os.path.basename(parts[2].strip())
        if pid in me or "gt_bench" in name or cpu < 1.0:
            continue
        rows.append((cpu, name))
    rows.sort(reverse=True)
    return [{"process": nm, "cpu_pct": c} for c, nm in rows[:n]]


def calibrate(widths, units, repeat, budget, where=None):
    start = time.time()
    end = start + budget
    where = where or tempfile.gettempdir()
    env = {"load_before": load_now(), "top_before": top_processes()}
    io, p1 = curve("io", widths, units, repeat, where, end)
    env["load_during"] = load_now()
    cpu, p2 = curve("cpu", widths, units, repeat, where, end)
    env["top_during"] = top_processes()
    return {"io": io, "cpu": cpu, "partial": p1 or p2, "environment": env,
            "elapsed_s": round(time.time() - start, 1)}


def location_ratio(dir_, width, units, budget_end):
    """I/O throughput in dir_ relative to the system temp dir (>1 = dir_ is slower)."""
    if time.time() >= budget_end:
        return None
    tmp = _run_units(IO_UNIT, width, units, tempfile.gettempdir())
    there = _run_units(IO_UNIT, width, units, dir_)
    return round(tmp / there, 2) if there else None


# ------------------------------------------------------------------ hosts ----

def _ssh():
    import shlex
    return shlex.split(os.environ.get("GT_PRUN_SSH", "ssh")) + ["-o", "BatchMode=yes",
                                                               "-o", "ConnectTimeout=10"]


def bench_host(host, widths, units, repeat, budget):
    """Run this script on `host` over ssh (stdin), calibration only. -> dict or {error}."""
    with open(os.path.abspath(__file__), encoding="utf-8") as fh:
        code = fh.read()
    args = ["python3", "-", "--remote", "--json", "--widths", ",".join(map(str, widths)),
            "--units", str(units), "--repeat", str(repeat), "--budget", str(budget)]
    try:
        p = subprocess.run(_ssh() + [host, " ".join(args)], input=code, capture_output=True,
                           text=True, timeout=budget + 120)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": "could not run: %s" % exc}
    if p.returncode != 0:
        return {"error": "unreachable or failed (exit %d)" % p.returncode}
    try:
        d = json.loads(p.stdout)
    except ValueError:
        return {"error": "returned no profile"}
    best = max((v for _, v in d.get("io") or []), default=None)
    return {"io_max": knee(d.get("io")), "cpu_max": knee(d.get("cpu")), "units_per_s": best,
            "curve": {"io": d.get("io"), "cpu": d.get("cpu")}, "partial": d.get("partial"),
            "measured_at": _now()}


def _now():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")


# ----------------------------------------------------------------- health ----

def rosetta_translated():
    """True when this process runs translated (x86_64 under Rosetta on Apple silicon)."""
    if sys.platform != "darwin":
        return False
    try:
        p = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True,
                           text=True, timeout=5)
        return p.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


WATCHED = ("/Library/CloudStorage/", "/Dropbox/", "/OneDrive", "/Google Drive/",
           "/Mobile Documents/")


def health():
    """-> [(state, summary, fix)] for the execution environment. state: ok | warn."""
    rows = []
    try:
        import gt_settings
        prof = gt_settings._load().get("parallel_profile") or {}
    except Exception:
        prof = {}
    if prof.get("source") == "measured":
        age = ""
        try:
            when = datetime.datetime.strptime(prof.get("measured_at", "")[:16], "%Y-%m-%d %H:%M")
            age = ", %d day(s) old" % (datetime.datetime.now() - when).days
        except ValueError:
            pass
        rows.append(("ok", "parallel profile MEASURED %s%s: io_max %s, cpu_max %s"
                     % (prof.get("measured_at", "?"), age, prof.get("io_max"),
                        prof.get("cpu_max")), ""))
    else:
        rows.append(("ok", "parallel profile is the DEFAULT rule of thumb%s, not measured"
                     % (" (detected %s)" % prof["detected_at"] if prof.get("detected_at")
                        else ""), "gt_bench.py measures it"))
    if rosetta_translated():
        rows.append(("warn", "this shell runs TRANSLATED under Rosetta (x86_64 on Apple "
                             "silicon): every process it starts is translated",
                     "run Claude Code from a native (arm64) terminal and use a native python3 "
                     "(e.g. /usr/bin/python3 or an arm64 Homebrew one)"))
    else:
        rows.append(("ok", "native %s shell" % platform.machine(), ""))
    tmp = os.path.realpath(tempfile.gettempdir())
    if any(w in tmp + "/" for w in WATCHED):
        rows.append(("warn", "TMPDIR (%s) is inside a synced folder" % tmp,
                     "gt_settings.py set test_tmpdir noindex, or point TMPDIR elsewhere"))
    return rows


# -------------------------------------------------------------------- CLI ----

# ---- recall (0.19.1, request recall-benchmark) ---------------------------------------------
#
# Does vault lookup find the right page? A fixture (vault/ + questions.json) is asked every
# question; each retriever's ranked pages are scored recall@1/3/10, with the files it read and an
# approximate token count (characters / 4 -- labelled approximate, not measured). A retriever is
# `keyword` (gt_keyword_recall, the gt-query path) or a .py file defining
# retrieve(question, vault, k) -> [paths] or {"results": [...], "read": [...]}. One that cannot
# load or raises is COULD-NOT-RUN with a nonzero exit, never a zero score.
RECALL_LOG = Path.home() / ".claude" / "golden-thread" / "bench-recall.jsonl"


def _load_retriever(spec):
    import importlib.util
    if spec == "keyword":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gt_keyword_recall
        return "keyword", gt_keyword_recall.retrieve
    p = Path(spec)
    mod_spec = importlib.util.spec_from_file_location("gt_bench_retriever_%s" % p.stem, str(p))
    if mod_spec is None or not p.is_file():
        raise FileNotFoundError("no retriever at %s" % p)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return p.stem, mod.retrieve


def run_recall(spec, vault, questions):
    import time as _t
    row = {"name": Path(spec).stem if spec != "keyword" else "keyword", "status": "ran"}
    try:
        row["name"], fn = _load_retriever(spec)
        t0, per = _t.monotonic(), []
        for q in questions:
            s0 = _t.monotonic()
            out = fn(q["question"], vault, 10)
            results = out.get("results", []) if isinstance(out, dict) else list(out or [])
            read = out.get("read", results[:3]) if isinstance(out, dict) else results[:3]
            rank = results.index(q["expected"]) + 1 if q["expected"] in results else None
            chars = 0
            for rel in read:
                try:
                    chars += (Path(vault) / rel).stat().st_size
                except OSError:
                    pass
            per.append({"id": q["id"], "level": q.get("level"), "expected": q["expected"],
                        "found_rank": rank, "files_read": len(read),
                        "approx_tokens": chars // 4, "ms": round((_t.monotonic() - s0) * 1000, 1)})
    except Exception as exc:                                     # noqa: BLE001
        row.update(status="could-not-run", error="%s: %s" % (exc.__class__.__name__, exc))
        for k in ("recall@1", "recall@3", "recall@10"):
            row[k] = None
        return row
    n = len(per) or 1
    for k in (1, 3, 10):
        row["recall@%d" % k] = round(sum(1 for x in per if x["found_rank"] and x["found_rank"] <= k)
                                     / n, 3)
    row.update(questions=per, files_read=sum(x["files_read"] for x in per),
               approx_tokens=sum(x["approx_tokens"] for x in per),
               wall_s=round(_t.monotonic() - t0, 2))
    return row


def recall_main(argv):
    # A real subparser, so `gt_bench.py recall --help` lists these flags (and the docstring-flag
    # gate can see the verb); the calibration CLI above stays as it was.
    root = argparse.ArgumentParser(prog="gt_bench.py")
    ap = root.add_subparsers(dest="cmd").add_parser(
        "recall", description="recall@k of vault lookup on a fixture")
    ap.add_argument("--fixture", required=True, help="a directory holding vault/ and questions.json")
    ap.add_argument("--retriever", action="append",
                    help="`keyword` (default) or a .py defining retrieve(); repeat to compare")
    ap.add_argument("--json", action="store_true")
    a = root.parse_args(["recall"] + list(argv))
    fx = Path(a.fixture)
    try:
        questions = json.loads((fx / "questions.json").read_text(encoding="utf-8"))["questions"]
    except Exception as exc:
        print("gt_bench recall: cannot read %s/questions.json (%s)" % (fx, exc), file=sys.stderr)
        return 2
    rows = [run_recall(s, fx / "vault", questions) for s in (a.retriever or ["keyword"])]
    record = {"at": _now(), "fixture": str(fx), "questions": len(questions),
              "retrievers": rows}
    try:
        RECALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RECALL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": record["at"], "fixture": record["fixture"],
                                 "retrievers": [{k: v for k, v in r.items() if k != "questions"}
                                                for r in rows]}) + "\n")
    except OSError:
        pass
    if a.json:
        print(json.dumps(record, indent=2))
    else:
        print("recall on %s (%d questions; tokens approximate, chars/4)" % (fx, len(questions)))
        print("  %-20s %-14s %8s %8s %9s %10s %11s" % ("retriever", "status", "recall@1",
                                                       "recall@3", "recall@10", "files read",
                                                       "~tokens"))
        for r in rows:
            if r["status"] != "ran":
                print("  %-20s %-14s %s" % (r["name"], "COULD-NOT-RUN", r.get("error", "")))
                continue
            print("  %-20s %-14s %8.3f %8.3f %9.3f %10d %11d" % (
                r["name"], "ran", r["recall@1"], r["recall@3"], r["recall@10"],
                r["files_read"], r["approx_tokens"]))
    return 1 if any(r["status"] != "ran" for r in rows) else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["recall"]:
        return recall_main(argv[1:])
    if argv[:1] == ["health"]:
        rows = health()
        if "--json" in argv:
            print(json.dumps([{"state": s, "summary": m, "fix": f} for s, m, f in rows],
                             indent=2))
        else:
            for s, m, f in rows:
                print("%-4s %s%s" % (s, m, ("  -- " + f) if f and s != "ok" else ""))
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the profile; write nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--widths", help="comma-separated worker counts (default 1,2,4,.. to 2x cores)")
    ap.add_argument("--units", type=int, default=16, help="units per width (at least 2x width)")
    ap.add_argument("--repeat", type=int, default=3, help="runs per width; the median is kept")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="seconds, at most")
    ap.add_argument("--location", help="also compare I/O in this directory with the temp dir")
    ap.add_argument("--hosts", nargs="?", const="",
                    help="also calibrate these ssh runners (no value: the `runners` setting)")
    ap.add_argument("--remote", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    cores = os.cpu_count() or 4
    if a.widths:
        try:
            widths = sorted({int(x) for x in a.widths.split(",") if x.strip()})
        except ValueError:
            print("--widths takes integers", file=sys.stderr)
            return 2
    else:
        widths, w = [], 1
        while w <= min(64, cores * 2):
            widths.append(w)
            w *= 2
    if not widths or widths[0] < 1:
        print("--widths must be positive", file=sys.stderr)
        return 2

    res = calibrate(widths, a.units, max(1, a.repeat), a.budget)
    if a.remote:                                   # running on a runner: report, nothing else
        print(json.dumps(res))
        return 0
    if not res["io"] and not res["cpu"]:
        print("nothing could be measured within the %ds budget" % a.budget)
        return 1

    try:
        import gt_settings
        base = gt_settings.detect_machine()
        cfg = gt_settings._load()
    except Exception:
        base, cfg = {"cores": cores}, {}
    old = cfg.get("parallel_profile") or {}
    prof = dict(base)
    prof.update({
        "cpu_max": knee(res["cpu"]) or old.get("cpu_max") or base.get("cpu_max"),
        "io_max": knee(res["io"]) or old.get("io_max") or base.get("io_max"),
        "source": "measured", "measured_at": _now(), "workload": "synthetic",
        "method": "knee at %d%% of the best throughput, median of %d run(s) per width"
                  % (int(KNEE * 100), max(1, a.repeat)),
        "curve": {"io": res["io"], "cpu": res["cpu"]}, "partial": res["partial"],
        "environment": res["environment"], "elapsed_s": res["elapsed_s"],
    })
    if not res["cpu"]:
        prof["cpu_max_source"] = "default"
    if not res["io"]:
        prof["io_max_source"] = "default"
    if a.location:
        ratio = location_ratio(os.path.abspath(os.path.expanduser(a.location)),
                               prof["io_max"] or 2, a.units, time.time() + a.budget)
        prof["location"] = {"dir": a.location, "slower_than_temp": ratio,
                            "advice": ("run from a local copy (git archive) -- I/O there is "
                                       "%.1fx slower than the temp dir" % ratio)
                            if ratio and ratio > 1.2 else "no material difference"}
    if a.hosts is not None:
        hosts = [h for h in (a.hosts or (cfg.get("runners") or "")).split(",") if h.strip()]
        prof["hosts"] = dict(old.get("hosts") or {})
        for h in hosts:
            r = bench_host(h.strip(), widths, a.units, max(1, a.repeat), a.budget)
            if "error" in r:
                print("runner %s: %s -- not measured" % (h, r["error"]), file=sys.stderr)
                continue
            prof["hosts"][h.strip()] = r

    if a.json:
        print(json.dumps(prof, indent=2))
    else:
        print("parallel_profile (%s): io_max %s, cpu_max %s -- measured %s in %.0fs%s"
              % ("would write" if a.dry_run else "written", prof["io_max"], prof["cpu_max"],
                 prof["measured_at"], res["elapsed_s"], " (PARTIAL: budget reached)"
                 if res["partial"] else ""))
        for kind in ("io", "cpu"):
            print("  %-3s curve: %s" % (kind, "  ".join("%dw=%.1f/s" % (w, v)
                                                         for w, v in res[kind]) or "not measured"))
        env = res["environment"]
        print("  load: %s before, %s during" % (env["load_before"], env["load_during"]))
        busy = ", ".join("%s %.0f%%" % (t["process"], t["cpu_pct"]) for t in env["top_during"])
        if busy:
            print("  busiest other processes (named, never touched): %s" % busy)
        if prof.get("location"):
            print("  location: %s" % prof["location"]["advice"])
        for h, r in (prof.get("hosts") or {}).items():
            print("  runner %s: io_max %s, %.1f units/s" % (h, r.get("io_max"),
                                                           r.get("units_per_s") or 0))
    if a.dry_run:
        return 0
    if not cfg.get("vault_path"):
        print("not written: vault-config.json has no vault_path (run /gt:gt-init first)")
        return 0
    cfg["parallel_profile"] = prof
    gt_settings._save(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
