#!/usr/bin/env python3
"""Run the suite in parallel, one process per TestCase class.

    prun.py                 # every test, parallel
    prun.py test_gt_lint    # one module (or module.Class), still parallel
    prun.py -j 4            # cap the workers
    prun.py --affected      # only the tests mapped to the files changed on this branch (0.18.1)
    prun.py --hosts a,b     # split the units across ssh runners, heaviest first (0.18.1)

## Why this is safe here, and where the limit is

Every test in this suite builds its own throwaway HOME under `tempfile.mkdtemp` and
never touches the real `~/.claude` or the real vault — that is what `Sandbox` is for.
So two test classes cannot see each other's state, and running them at once is only a
scheduling question.

The unit of parallelism is the CLASS, not the file: `test_install.py` alone is 115s of
the ~510s total, and per-file splitting would leave that as the floor. It is also not
the individual test — `setUp` cost (a full install, a fresh vault) is per-test already,
and a process per test would spend more on interpreter startup than it saves.

The work is I/O-bound almost end to end: each test shells out to install.sh, vault_init
or a hook and waits. That is why more workers than cores still helps, and why the
default is generous rather than exactly `ncpu`.

## Load-aware, not core-count-aware (0.18.1)

The worker count starts from the ceiling the settings allow and is scaled DOWN by the machine's
measured load, memory pressure and other parallel runs already going (gt_load.recommend). As
units finish, each is compared with its own time from the previous run; when they run at more
than twice their baseline the pool shrinks instead of growing (gt_load.Governor). On 2026-10-01
a run sized from the core count met a machine already at load ~50: install tests took 160-315 s
against ~30 s and the owner's keystrokes were dropped. Units are also started heaviest-first
from those recorded times, so the slowest class is never the last one started.
GT_TEST_LOAD_AWARE=0 turns both off.

## --affected: a scoped run (0.18.1)

Maps every file changed since the branch left the default branch (committed, staged, unstaged
and untracked) to the test modules that name it, and runs only those. A changed code file no
test names, or a change to the harness or installer, selects the FULL suite -- an unmapped
change is an unknown, and an unknown is not a pass. With GT_AFFECTED_OUT set it writes the
mapping there, which tests/run.sh turns into a SCOPED receipt: accepted by the commit guard on a
feature branch only, never by a release gate.

## --hosts: units on other machines (0.18.1)

The tree (tracked + untracked-not-ignored files) is shipped once to each listed ssh runner (the
gt setting `runners`, or `--hosts a,b`; `local` means this machine), the units are split by the
runners' measured capacity heaviest-first, and every failure is printed under the host it ran
on. An unreachable runner is SKIPPED AND REPORTED and its units go to the others; a unit no host
reported is a failure, never a pass. GT_PRUN_SSH replaces `ssh` (tests use a local stand-in).

## What it does NOT change

Output. A failing run must show the same traceback as `unittest` would, so failures are
printed verbatim, grouped by the unit that produced them, after the summary line. The
exit code is 0 only when every unit exited 0.
"""
import argparse
import concurrent.futures
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable or "python3"
COUNT = re.compile(r"^Ran (\d+) test", re.M)
UNIT_LINE = re.compile(r"^(ok  |FAIL) (\S+)\s+([\d.]+)s\s+(\d+) test", re.M)
TIMINGS = Path(os.path.expanduser("~/.claude/golden-thread/prun-timings.json"))


def units(selectors):
    """-> ['module.Class', ...] for everything selected."""
    loader = unittest.TestLoader()
    out = []
    if selectors:
        for sel in selectors:
            # A selector may already name a class or a single test: pass it through.
            if "." in sel:
                out.append(sel)
                continue
            out.extend(classes_in(sel))
        return out
    for f in sorted(HERE.glob("test_*.py")):
        out.extend(classes_in(f.stem))
    return out


def classes_in(module):
    """Class names in a module, found without importing it.

    Importing every test module in THIS process would run module-level code (some of
    it reads the release) and could fail for reasons unrelated to the tests. A regex
    over the source is enough for `class X(...)` at column 0.
    """
    path = HERE / (module + ".py")
    if not path.is_file():
        return [module]
    names = re.findall(r"^class\s+([A-Za-z_]\w*)\s*\(", path.read_text(encoding="utf-8"), re.M)
    # Base classes that hold helpers and no tests still run harmlessly (0 tests), but
    # skipping the obvious ones keeps the output tidy.
    names = [n for n in names if not n.endswith("Base")]
    return ["%s.%s" % (module, n) for n in names] or [module]


def run_unit(unit):
    started = time.time()
    p = subprocess.run([PY, "-m", "unittest", "-v", unit],
                       capture_output=True, text=True, cwd=str(HERE))
    out = (p.stdout or "") + (p.stderr or "")
    m = COUNT.search(out)
    rc = p.returncode
    # (0.20.0) Python 3.12+ exits 5 when a unit ran no tests; a helper base class (WatchTest,
    # ChooseCase...) is exactly that, and older Pythons exit 0 for it, as the note in
    # classes_in() assumes. Only the "no tests" exit is forgiven, never a real failure.
    if rc == 5 and "NO TESTS RAN" in out and not (m and int(m.group(1))):
        rc = 0
    return {"unit": unit, "rc": rc, "out": out,
            "tests": int(m.group(1)) if m else 0,
            "secs": time.time() - started}


def _settings_jobs(want):
    """Worker count from the registered settings, falling back to the old default.

    `parallel_work` / `parallel_max` are the machine-wide budget (see the Core rule
    core_parallel_when_beneficial), so the test runner honours the same numbers as
    everything else rather than keeping a private policy. Precedence: `-j` beats
    GT_TEST_JOBS beats the settings beats the fallback.

    The fallback matters: these tests run from a clone that may have no vault and no
    ~/.claude at all, and a runner that refuses to run because a settings file is
    missing would be worse than one that guesses well.
    """
    import importlib.util
    io_default = min((os.cpu_count() or 4) + 4, 20)
    rel = os.environ.get("GT_TEST_VERSION") or _newest_release()
    if not rel:
        return io_default
    src = HERE.parent / "golden-thread" / rel / "scripts" / "gt_settings.py"
    if not src.is_file():
        return io_default
    try:
        spec = importlib.util.spec_from_file_location("gt_settings_for_prun", str(src))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.parallel_jobs(want, io_bound=True)
    except Exception:
        return io_default


def _release_scripts():
    rel = os.environ.get("GT_TEST_VERSION") or _newest_release()
    d = HERE.parent / "golden-thread" / rel / "scripts" if rel else None
    return d if d and d.is_dir() else None


def _release_module(name):
    """Import a script from the release under test; None when it is not there."""
    d = _release_scripts()
    if not d or not (d / (name + ".py")).is_file():
        return None
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        spec = importlib.util.spec_from_file_location(name + "_for_prun", str(d / (name + ".py")))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception:
        return None


# ---- timings: the per-unit baseline (heaviest-first order, back-off, host split) ----

def _repo_key():
    return str(HERE.parent)


def load_timings():
    try:
        return json.loads(TIMINGS.read_text()).get(_repo_key(), {})
    except (OSError, ValueError, AttributeError):
        return {}


def save_timings(results):
    try:
        data = json.loads(TIMINGS.read_text()) if TIMINGS.is_file() else {}
    except (OSError, ValueError):
        data = {}
    mine = data.setdefault(_repo_key(), {})
    for r in results:
        if r["rc"] == 0 and r["tests"]:
            mine[r["unit"]] = round(r["secs"], 2)
    try:
        TIMINGS.parent.mkdir(parents=True, exist_ok=True)
        tmp = TIMINGS.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(str(tmp), str(TIMINGS))
    except OSError:
        pass


def heaviest_first(todo, timings):
    """Longest-processing-time first: unknown units (could be heavy) lead, then by time."""
    return sorted(todo, key=lambda u: (u in timings, -timings.get(u, 0.0)))


# ---- --affected ------------------------------------------------------------------

DOC_EXT = {".md", ".markdown", ".rst", ".txt", ".html", ".htm", ".pdf", ".png", ".jpg",
           ".jpeg", ".gif", ".svg", ".ico", ".webp", ".csv"}
# A change to any of these can break any test: the full suite, always.
EVERYTHING = {"tests/_harness.py", "tests/prun.py", "tests/run.sh", "install.sh",
              "selftest.sh", "package.sh"}


def _git(root, *args):
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ""


def changed_files(root, base=None):
    """Repo-relative paths changed since the branch point, plus uncommitted and untracked."""
    if not base:
        ref = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
        cands = [ref] if ref else []
        cands += ["refs/heads/main", "refs/heads/master"]
        for c in cands:
            mb = _git(root, "merge-base", "HEAD", c).strip()
            if mb:
                base = mb
                break
    names = set()
    if base:
        names |= set(_git(root, "diff", "--name-only", base, "HEAD").split("\n"))
    names |= set(_git(root, "diff", "--name-only", "HEAD").split("\n"))
    names |= set(_git(root, "ls-files", "-o", "--exclude-standard").split("\n"))
    return sorted(n for n in names if n.strip())


def affected(selectors_root=None, base=None):
    """-> {"files", "modules", "full", "reasons"} for the tests the changed files need."""
    root = Path(_git(HERE, "rev-parse", "--show-toplevel").strip() or HERE.parent)
    plugin = HERE.parent.resolve()
    files = changed_files(root, base)
    tests = {f.stem: f.read_text(encoding="utf-8", errors="replace")
             for f in sorted(HERE.glob("test_*.py"))}
    modules, reasons, full = set(), [], False
    for rel in files:
        p = (root / rel).resolve()
        try:
            prel = p.relative_to(plugin).as_posix()
        except ValueError:
            prel = None
        if Path(rel).suffix.lower() in DOC_EXT:
            continue
        if prel in EVERYTHING:
            full = True
            reasons.append("%s changes what every test runs on" % rel)
            continue
        name = Path(rel).name
        if prel and prel.startswith("tests/") and Path(rel).stem in tests:
            modules.add(Path(rel).stem)
            continue
        stem = Path(rel).stem
        pats = [re.compile(r"(?<![\w.])%s(?![\w])" % re.escape(name))]
        if len(stem) >= 4 and stem != name:
            pats.append(re.compile(r"(?<![\w.])%s(?![\w])" % re.escape(stem)))
        hit = {m for m, text in tests.items() if any(px.search(text) for px in pats)}
        if hit:
            modules |= hit
        else:
            full = True
            reasons.append("no test names %s, so nothing scoped can cover it" % rel)
    return {"files": files, "modules": sorted(modules), "full": full, "reasons": reasons,
            "base": base}


# ---- test temp dir and the cached install --------------------------------------

def tmpdir_for_tests():
    """GT_TEST_TMPDIR, else the `test_tmpdir` setting; None leaves TMPDIR alone."""
    explicit = os.environ.get("GT_TEST_TMPDIR")
    if explicit:
        return Path(os.path.expanduser(explicit))
    m = _release_module("gt_settings")
    try:
        mode = m.get("test_tmpdir") if m else "off"
    except Exception:
        mode = "off"
    if mode != "noindex":
        return None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "gt-tests.noindex"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "gt-tests"


def _newest_release():
    d = HERE.parent / "golden-thread"
    vs = [p.name for p in d.glob("*/") if re.fullmatch(r"\d+\.\d+\.\d+", p.name)
          and (p / ".claude-plugin" / "plugin.json").is_file()] if d.is_dir() else []
    return max(vs, key=lambda v: [int(x) for x in v.split(".")]) if vs else None


def run_local(todo, jobs, cap, load_aware):
    """The pool: starts `jobs` units, then lets the Governor shrink or regrow it as units finish."""
    timings = load_timings()
    todo = heaviest_first(todo, timings)
    gl = _release_module("gt_load") if load_aware else None
    gov = gl.Governor(jobs, cap, timings) if gl else None
    results, pending, it = [], set(), iter(todo)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(jobs, cap)) as pool:
        def fill():
            limit = gov.limit if gov else jobs
            while len(pending) < limit:
                u = next(it, None)
                if u is None:
                    return
                pending.add(pool.submit(run_unit, u))
        fill()
        while pending:
            done, _ = concurrent.futures.wait(pending,
                                              return_when=concurrent.futures.FIRST_COMPLETED)
            for f in done:
                pending.discard(f)
                r = f.result()
                results.append(r)
                print("%s %-52s %5.1fs  %3d test(s)"
                      % ("ok  " if r["rc"] == 0 else "FAIL", r["unit"], r["secs"], r["tests"]),
                      flush=True)
                if gov:
                    before = len(gov.events)
                    gov.finished(r["unit"], r["secs"])
                    for e in gov.events[before:]:
                        print("load-aware: %s" % e, flush=True)
            fill()
    return results


# ---- --hosts ----------------------------------------------------------------------

def _ssh():
    return shlex.split(os.environ.get("GT_PRUN_SSH", "ssh")) + ["-o", "BatchMode=yes",
                                                               "-o", "ConnectTimeout=10"]


def _snapshot(root):
    """tar.gz bytes of every tracked + untracked-not-ignored file under the repo root."""
    names = [n for n in _git(root, "ls-files", "-co", "--exclude-standard", "-z").split("\0") if n]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for n in names:
            p = Path(root) / n
            if p.is_file():
                tar.add(str(p), arcname=n, recursive=False)
    return buf.getvalue()


def host_capacity(hosts):
    """Measured units/s per host from parallel_profile.hosts (gt_bench.py), else 1.0 each."""
    m = _release_module("gt_settings")
    prof = {}
    try:
        prof = (m._load().get("parallel_profile") or {}).get("hosts") or {} if m else {}
    except Exception:
        prof = {}
    out = {}
    for h in hosts:
        cap = (prof.get(h) or {}).get("units_per_s") or (prof.get(h) or {}).get("io_max")
        out[h] = float(cap) if cap else 1.0
    return out


def split_units(todo, hosts, timings, capacity):
    """Greedy LPT: heaviest unit to the host that would finish it soonest."""
    load = {h: 0.0 for h in hosts}
    plan = {h: [] for h in hosts}
    for u in heaviest_first(todo, timings):
        w = timings.get(u, 30.0)
        h = min(hosts, key=lambda x: (load[x] + w) / capacity.get(x, 1.0))
        plan[h].append(u)
        load[h] += w
    return plan


def run_hosts(todo, hosts, jobs):
    root = Path(_git(HERE, "rev-parse", "--show-toplevel").strip() or HERE.parent)
    rel = HERE.parent.resolve().relative_to(root.resolve()).as_posix()
    reach, skipped = [], []
    for h in hosts:
        if h == "local":
            reach.append(h)
            continue
        p = subprocess.run(_ssh() + [h, "true"], capture_output=True, text=True)
        (reach if p.returncode == 0 else skipped).append(h)
    for h in skipped:
        print("SKIPPED runner %s: unreachable -- its units go to the others; it is NOT counted "
              "as passing" % h, flush=True)
    if not reach:
        print("no runner reachable (%s): nothing ran" % ", ".join(hosts))
        return None, skipped
    timings = load_timings()
    plan = split_units(todo, reach, timings, host_capacity(reach))
    blob = _snapshot(root) if any(h != "local" for h in reach) else b""
    version = os.environ.get("GT_TEST_VERSION", "")
    results, lock = [], threading.Lock()

    def one(h):
        units_h = plan[h]
        if not units_h:
            return
        if h == "local":
            cmd = [PY, str(HERE / "prun.py"), "-j", str(jobs), "--no-load-aware"] + units_h
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(HERE),
                               env=dict(os.environ, GT_PRUN_NO_METRICS="1"))
            out, rc = p.stdout + p.stderr, p.returncode
        else:
            runid = "gt-prun-%d-%d" % (os.getpid(), int(time.time()))
            remote = ("mkdir -p ~/gt-remote/{id} && tar -xzf - -C ~/gt-remote/{id} && "
                      "cd ~/gt-remote/{id}/{rel} && H=$(mktemp -d) && umask 022 && "
                      "HOME=$H GT_TEST_VERSION={ver} GT_PRUN_NO_METRICS=1 "
                      "PYTHONDONTWRITEBYTECODE=1 python3 tests/prun.py -j {j} {units}; "
                      "rc=$?; rm -rf $H ~/gt-remote/{id}; exit $rc").format(
                id=runid, rel=shlex.quote(rel), ver=shlex.quote(version), j=jobs,
                units=" ".join(shlex.quote(u) for u in units_h))
            p = subprocess.run(_ssh() + [h, remote], input=blob, capture_output=True)
            out = (p.stdout or b"").decode("utf-8", "replace") + \
                  (p.stderr or b"").decode("utf-8", "replace")
            rc = p.returncode
        seen = {}
        for m in UNIT_LINE.finditer(out):
            seen[m.group(2)] = {"unit": m.group(2), "rc": 0 if m.group(1) == "ok  " else 1,
                                "secs": float(m.group(3)), "tests": int(m.group(4)),
                                "host": h, "out": ""}
        for u in units_h:
            if u not in seen:                       # never a pass: the host did not report it
                seen[u] = {"unit": u, "rc": 1, "secs": 0.0, "tests": 0, "host": h,
                           "out": "%s did not report this unit (exit %s)" % (h, rc)}
        for r in seen.values():
            if r["rc"] != 0 and not r["out"]:
                blk = re.search(r"FAILED UNIT: %s\n=+\n(.*?)(?=\n=+\nFAILED UNIT:|\nFAILED \(|\Z)"
                                % re.escape(r["unit"]), out, re.S)
                r["out"] = blk.group(1) if blk else out[-4000:]
        with lock:
            for r in seen.values():
                results.append(r)
                print("%s %-52s %5.1fs  %3d test(s)  [%s]"
                      % ("ok  " if r["rc"] == 0 else "FAIL", r["unit"], r["secs"], r["tests"],
                         h), flush=True)

    threads = [threading.Thread(target=one, args=(h,)) for h in reach]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, skipped


def record_metrics(results, elapsed, rc, jobs, scope, traits):
    if os.environ.get("GT_PRUN_NO_METRICS") == "1":
        return
    gm = _release_module("gt_metrics")
    if not gm:
        return
    gm.record_quietly(process="tests:%s" % ("affected" if scope == "scoped" else "suite"),
                      duration=elapsed, exit_code=rc,
                      project=os.environ.get("GT_METRICS_PROJECT") or "golden-thread",
                      repo=str(HERE.parent), units=len(results), parallel=jobs, scope=scope,
                      traits=traits)


def main(argv=None):
    ap = argparse.ArgumentParser(description="run the test suite in parallel")
    ap.add_argument("selectors", nargs="*", help="module or module.Class")
    ap.add_argument("-j", "--jobs", type=int,
                    default=int(os.environ.get("GT_TEST_JOBS") or 0),
                    help="worker processes (default: the parallel_max setting; "
                         "auto = cores + 4, I/O-bound)")
    ap.add_argument("--affected", action="store_true",
                    help="only the tests mapped to the files changed on this branch")
    ap.add_argument("--base", help="with --affected: the ref to diff against "
                                   "(default: the merge-base with the default branch)")
    ap.add_argument("--print-affected", action="store_true",
                    help="print the --affected mapping as JSON and run nothing")
    ap.add_argument("--hosts", help="comma-separated ssh runners (`local` = here); "
                                    "default: the `runners` setting when --hosts is given empty")
    ap.add_argument("--no-load-aware", action="store_true",
                    help="size the pool from the settings only")
    a = ap.parse_args(argv)

    tmp = tmpdir_for_tests()
    if tmp:
        tmp.mkdir(parents=True, exist_ok=True)
        os.environ["TMPDIR"] = str(tmp)
        tempfile.tempdir = None

    scope, traits = "full", []
    selectors = a.selectors
    aff = None
    if a.affected or a.print_affected:
        aff = affected(base=a.base)
        if a.print_affected:
            print(json.dumps(aff, indent=2))
            return 0
        print("affected: %d changed file(s) -> %s" % (
            len(aff["files"]), "FULL suite (%s)" % "; ".join(aff["reasons"][:3])
            if aff["full"] else "%d module(s): %s" % (len(aff["modules"]),
                                                      " ".join(aff["modules"]))), flush=True)
        if not aff["full"]:
            if not aff["modules"]:
                print("no code changed: nothing to run")
                return 0
            selectors = aff["modules"]
        scope, traits = "scoped", ["affected"]

    todo = units(selectors)
    cap = a.jobs or _settings_jobs(len(todo))
    if not todo:
        print("no tests selected")
        return 1
    load_aware = not a.no_load_aware and os.environ.get("GT_TEST_LOAD_AWARE", "1") != "0"
    jobs = cap
    if load_aware:
        gl = _release_module("gt_load")
        if gl:
            jobs, why = gl.recommend(min(cap, len(todo)) if todo else cap)
            if jobs < min(cap, len(todo)):
                print("load-aware: starting %d of %d worker(s): %s" % (jobs, cap, why),
                      flush=True)
                traits.append("load-aware")

    # One cached install per run, shared by every unit (tests/_harness.cached_install).
    cache_dir = Path(tempfile.mkdtemp(prefix="gt-install-cache-"))
    os.environ.setdefault("GT_TEST_INSTALL_CACHE", str(cache_dir))
    started = time.time()
    skipped_hosts = []
    if a.hosts is not None:
        hosts = [h.strip() for h in a.hosts.split(",") if h.strip()]
        if not hosts:
            m = _release_module("gt_settings")
            hosts = [h for h in ((m.get("runners") if m else "") or "").split(",") if h]
        if not hosts:
            print("--hosts: no runners given and the `runners` setting is empty")
            shutil.rmtree(cache_dir, ignore_errors=True)
            return 2
        results, skipped_hosts = run_hosts(todo, hosts, jobs)
        if results is None:
            shutil.rmtree(cache_dir, ignore_errors=True)
            return 1
        traits.append("hosts")
    else:
        results = run_local(todo, jobs, cap, load_aware)
    shutil.rmtree(cache_dir, ignore_errors=True)

    total = sum(r["tests"] for r in results)
    failed = [r for r in results if r["rc"] != 0]
    elapsed = time.time() - started
    print("\nRan %d tests in %.1fs across %d unit(s), %d worker(s)%s"
          % (total, elapsed, len(results), jobs,
             "" if a.hosts is None else " per host"))
    if skipped_hosts:
        print("runners skipped as unreachable: %s" % ", ".join(skipped_hosts))
    if a.hosts is None:
        save_timings(results)
    record_metrics(results, elapsed, 1 if failed else 0, jobs, scope, traits)

    if failed:
        # Verbatim, so a failure here reads exactly like a failure under unittest.
        for r in failed:
            print("\n" + "=" * 70)
            print("FAILED UNIT: %s%s" % (r["unit"], "  [host %s]" % r["host"]
                                         if r.get("host") else ""))
            print("=" * 70)
            print(r["out"].rstrip())
        print("\nFAILED (%d unit(s): %s)"
              % (len(failed), ", ".join(r["unit"] for r in failed)))
        return 1
    # The mapping is written only for a run that PASSED, so run.sh cannot turn a red or empty
    # run into a scoped receipt.
    if aff is not None and os.environ.get("GT_AFFECTED_OUT"):
        Path(os.environ["GT_AFFECTED_OUT"]).write_text(json.dumps(aff, indent=2))
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
