#!/usr/bin/env python3
"""Run the suite in parallel, one process per TestCase class.

    prun.py                 # every test, parallel
    prun.py test_gt_lint    # one module (or module.Class), still parallel
    prun.py -j 4            # cap the workers

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

## What it does NOT change

Output. A failing run must show the same traceback as `unittest` would, so failures are
printed verbatim, grouped by the unit that produced them, after the summary line. The
exit code is 0 only when every unit exited 0.
"""
import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable or "python3"
COUNT = re.compile(r"^Ran (\d+) test", re.M)


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
    return {"unit": unit, "rc": p.returncode, "out": out,
            "tests": int(m.group(1)) if m else 0,
            "secs": time.time() - started}


def main(argv=None):
    ap = argparse.ArgumentParser(description="run the test suite in parallel")
    ap.add_argument("selectors", nargs="*", help="module or module.Class")
    ap.add_argument("-j", "--jobs", type=int,
                    default=int(os.environ.get("GT_TEST_JOBS") or 0),
                    help="worker processes (default: cores + 4, I/O-bound)")
    a = ap.parse_args(argv)

    jobs = a.jobs or min((os.cpu_count() or 4) + 4, 20)
    todo = units(a.selectors)
    if not todo:
        print("no tests selected")
        return 1

    started = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for r in pool.map(run_unit, todo):
            results.append(r)
            print("%s %-52s %5.1fs  %3d test(s)"
                  % ("ok  " if r["rc"] == 0 else "FAIL", r["unit"], r["secs"], r["tests"]),
                  flush=True)

    total = sum(r["tests"] for r in results)
    failed = [r for r in results if r["rc"] != 0]
    elapsed = time.time() - started
    print("\nRan %d tests in %.1fs across %d unit(s), %d worker(s)"
          % (total, elapsed, len(results), jobs))

    if failed:
        # Verbatim, so a failure here reads exactly like a failure under unittest.
        for r in failed:
            print("\n" + "=" * 70)
            print("FAILED UNIT: %s" % r["unit"])
            print("=" * 70)
            print(r["out"].rstrip())
        print("\nFAILED (%d unit(s): %s)"
              % (len(failed), ", ".join(r["unit"] for r in failed)))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
