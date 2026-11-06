#!/usr/bin/env python3
"""gt_farm_accept.py -- acceptance checks for an agent's patch, and applying an accepted patch back
(gt 0.22.0, gt-farm 0.21.3, agent farming phase E).

    gt_farm_accept.py evaluate --workspace DIR --test-command CMD [--allow GLOB]... [--deny GLOB]...
                               [--read-only-tests GLOB]... [--hidden DEST=SRC]... [--timeout SECS]
                               [--patch-out FILE] [--json]
    gt_farm_accept.py apply --repo DIR --patch FILE [--apply] [--dry-run]

    as a module:
      evaluate(workspace, *, test_command, allow=(), deny=(), read_only_tests=(), hidden_tests=None, timeout=300,
               allow_unisolated=False) -> {ok, why, tests_ran, tests_failed, changed_files, out_of_scope, patch,
                                           scope_why}
      apply_patch(repo_dir, patch, *, dry_run=True) -> {ok, why, files}

evaluate judges the workspace an agent worked in (gt_farm_workspace.make_workspace) against its baseline. The result
is ok only when ALL hold: (a) the diff stays in scope: every changed file matches an `allow` glob when any is given,
none matches a `deny` glob, and no test file or test-harness file is touched (a touched test file always rejects unless allow_test_changes); (b) the visible
test command passes through the sandbox with at least one test counted (unittest "Ran N tests", pytest "N passed";
output in any other format with no count is NOT ok); (c) when `hidden_tests` is given (a dict {path inside the
workspace: path of the held-out file}), those files are copied in, the same command runs again and passes, and they
are removed again, so the patch never contains them. The tests run through gt_farm_workspace.run_command, never
the shell directly, so they have no network and cannot write outside the workspace.

apply_patch validates with `git apply --check` against the REAL repository and refuses a path outside it, a link,
and a secret-like name (gt_farm_egress.deny_read). It changes files only when dry_run is False, and never commits.
The CLI applies only with --apply (--dry-run, the default, only checks). This tool writes into a code repository,
not the vault, so it takes no --vault.

THE LADDER CHECKS. gt_farm_ladder.CHECKS "tests-pass" and "diff-scope" read their input from the reply object, which
is the evaluate result (or {"__accept__": that result}): tests-pass reads ok, why, tests_ran, tests_failed;
diff-scope reads changed_files, out_of_scope, scope_why.

Exit codes: 0 accepted (evaluate) or the patch applies / was applied (apply) | 1 rejected, with the reason printed |
2 usage error or the workspace/repo could not be read.
"""
import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

GT_VERSION = "0.21.3"            # the gt-farm module version

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import gt_farm_egress as EG          # noqa: E402
import gt_farm_workspace as WS       # noqa: E402


def _norm(p):
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _match(path, pattern):
    """A glob matches the whole path, a bare name matches the file name anywhere, and `dir/` matches everything under it."""
    path, pat = _norm(path), _norm(pattern)
    if pat.endswith("/"):
        return path.startswith(pat)
    if fnmatch.fnmatchcase(path, pat):
        return True
    return "/" not in pat and fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pat)


def _any(path, patterns):
    return any(_match(path, p) for p in patterns)


# Files that decide HOW the tests run. An agent that adds or changes one can forge or hijack the run (a sitecustomize.py
# that prints "Ran 1 test ... OK" and exits 0 is enough), so a change to any of them is rejected wherever it sits in the
# tree, matched on the file name. allow_test_changes never covers them; only allow_harness_changes=True does, and that is
# dangerous: it lets the agent decide whether its own tests pass.
TEST_HARNESS_FILES = ("conftest.py", "sitecustomize.py", "usercustomize.py", "*.pth", "pytest.ini", "tox.ini",
                      "setup.cfg", ".coveragerc", "pyproject.toml", "noxfile.py", "Makefile")

# Files that ARE tests. A modified, deleted or added one is rejected by default, because with nothing frozen an agent can
# turn a failing test into `assert True` or add a dummy passing test and be accepted.
_TEST_NAME_GLOBS = ("test_*.py", "*_test.py", "*.test.js", "*.test.ts", "*.test.jsx", "*.test.tsx", "*_spec.*")
_TEST_DIRS = ("tests", "test", "__tests__")


def _is_harness(path):
    base = _norm(path).rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(base, g) for g in TEST_HARNESS_FILES)


def _is_test_file(path):
    n = _norm(path)
    segs = n.split("/")
    if any(s in _TEST_DIRS for s in segs[:-1]):
        return True
    return any(fnmatch.fnmatchcase(segs[-1], g) for g in _TEST_NAME_GLOBS)


def _scope(files, allow, deny, read_only_tests, allow_test_changes=False, allow_harness_changes=False):
    """-> (out_of_scope paths, [plain reasons])"""
    bad, why = [], []
    for f in files:
        if _is_harness(f) and not allow_harness_changes:
            bad.append(f)
            why.append("a file that controls how the tests run was changed: %s. Restore it; it may not be edited." % f)
        elif _any(f, read_only_tests) or (_is_test_file(f) and not allow_test_changes):
            bad.append(f)
            why.append("a test file was changed: %s. Tests must not be edited to make them pass." % f)
        elif _any(f, deny):
            bad.append(f)
            why.append("%s is in a denied location; leave it unchanged" % f)
        elif allow and not _any(f, allow):
            bad.append(f)
            why.append("%s is outside the files this task may change (%s); revert it" % (f, ", ".join(allow)))
    return bad, why


_TAMPER = "the test output looks tampered with: run the tests again yourself"
_RE_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s$")
_RE_UT_OK = re.compile(r"^OK(?: \((.*)\))?$")
_RE_UT_FAIL = re.compile(r"^FAILED \((.*)\)$")
_RE_PY_BANNER = re.compile(r"^=+ .*(passed|failed|error).* in [\d.]+s.*=+$")
_RE_PY_Q = re.compile(r"^\d+ (passed|failed|error|errors|skipped|xfailed|xpassed)\b.* in [\d.]+s(?: \(.*\))?$")


def _kv(text, key):
    return sum(int(x) for x in re.findall(r"(?<!expected )\b%s=(\d+)" % key, text or ""))


def _parse(out):
    """Read ONLY the runner's own summary lines, anchored. -> {runner, ran, failed, tampered, passed_text}.
    unittest: one 'Ran N tests in Ts' line, then (next non-empty line) 'OK' / 'OK (...)' / 'FAILED (...)', and nothing
    summary-like after it. pytest: one summary line, the last summary-like line. Anything else counts nothing."""
    res = {"runner": None, "ran": 0, "failed": 0, "tampered": False, "passed_text": False,
           "total": 0, "skipped": 0, "xfailed": 0}
    lines = [ln.rstrip() for ln in str(out).splitlines()]
    ran_idx = [i for i, ln in enumerate(lines) if _RE_RAN.match(ln)]
    ut_status = [i for i, ln in enumerate(lines) if _RE_UT_OK.match(ln) or _RE_UT_FAIL.match(ln)]
    py_idx = [i for i, ln in enumerate(lines) if _RE_PY_BANNER.match(ln) or _RE_PY_Q.match(ln)]
    if ran_idx and py_idx:
        res["tampered"] = True
        return res
    if ran_idx:
        res["runner"] = "unittest"
        if len(ran_idx) != 1 or len(ut_status) > 1:
            res["tampered"] = True
            return res
        i = ran_idx[0]
        nxt = next((j for j in range(i + 1, len(lines)) if lines[j].strip()), None)
        if nxt is None or not ut_status or ut_status[0] != nxt:
            if ut_status:
                res["tampered"] = True          # a status line that is not right after the 'Ran' line
            return res                          # (e.g. 'NO TESTS RAN'): nothing counted
        n = int(_RE_RAN.match(lines[i]).group(1))
        m = _RE_UT_OK.match(lines[nxt])
        inner = (m.group(1) if m else _RE_UT_FAIL.match(lines[nxt]).group(1)) or ""
        res["total"], res["skipped"], res["xfailed"] = n, _kv(inner, "skipped"), _kv(inner, "expected failures")
        if m:
            res["ran"], res["passed_text"] = max(n - res["skipped"], 0), True
        else:
            res["ran"], res["failed"] = n, _kv(inner, "failures") + _kv(inner, "errors")
            res["failed"] = res["failed"] or 1
        return res
    if len(py_idx) > 1 or (py_idx and py_idx[0] != max(i for i, ln in enumerate(lines) if ln.strip()
                                                       and not ln.startswith("[truncated"))):
        res["tampered"] = bool(py_idx)
        return res
    if py_idx:
        res["runner"] = "pytest"
        ln = lines[py_idx[0]]
        passed = sum(int(x) for x in re.findall(r"(\d+) passed", ln))
        failed = sum(int(x) for x in re.findall(r"(\d+) (?:failed|errors?)\b", ln))
        res["ran"], res["failed"], res["passed_text"] = passed + failed, failed, bool(passed) and not failed
        res["skipped"] = sum(int(x) for x in re.findall(r"(\d+) skipped", ln))
        res["xfailed"] = sum(int(x) for x in re.findall(r"(\d+) xfailed", ln))
        res["total"] = passed + failed + res["skipped"] + res["xfailed"] + sum(
            int(x) for x in re.findall(r"(\d+) xpassed", ln))
    return res


def _count(out):
    """-> (ran, failed) from the runner's own anchored summary; (0, 0) when the format is not recognised or tampered."""
    r = _parse(out)
    return (0, 0) if r["tampered"] else (r["ran"], r["failed"])


UNTRUSTED = ("the test run could not be trusted: the check test did not run. Use a test command that finds all tests "
             "in the folder (for example python -m unittest discover)")
SHADOW_WHY = "a file that could replace the test runner or a standard module was added: %s"

# Names a planted file or folder must never take: the runners and the modules they and the host's Python start with.
RUNNER_NAMES = ("pytest", "_pytest", "nose", "nose2", "tox", "pluggy", "coverage", "py")
# Always banned, even where the running Python's list of standard modules is missing (before 3.10).
CRITICAL_MODULES = ("unittest", "pytest", "doctest", "json", "os", "sys", "re", "subprocess", "site", "builtins",
                    "importlib", "typing", "test", "argparse", "traceback", "inspect", "io", "types", "functools",
                    "collections", "contextlib", "pathlib", "shutil", "signal", "time", "threading", "atexit",
                    "runpy", "pkgutil", "warnings", "logging", "ctypes", "encodings", "abc", "codecs", "posixpath",
                    "ntpath", "genericpath", "stat", "_thread", "sysconfig", "pdb", "code", "trace", "tokenize")
_METADATA_DIR_SUFFIXES = (".dist-info", ".egg-info")
_METADATA_FILE_SUFFIXES = (".egg-link", ".pth", ".egg")
_PLUGIN_FILES = ("entry_points.txt", "pytest.ini")


def _installed_stdlib_names():
    """Python before 3.10 has no sys.stdlib_module_names: read the names from the standard library folder instead."""
    out = set(sys.builtin_module_names)
    try:
        import pkgutil
        out |= {m.name for m in pkgutil.iter_modules([os.path.dirname(os.__file__)])}
    except Exception:                      # noqa: BLE001 -- the critical list below still holds
        pass
    return out


def _stdlib_names():
    names = set(getattr(sys, "stdlib_module_names", None) or _installed_stdlib_names()) | set(CRITICAL_MODULES)
    return names | set(RUNNER_NAMES)


def _mod_name(seg):
    """The module name a path segment would import as: a folder's name, a file's name without its extension."""
    base = seg.split(".", 1)[0] if not seg.endswith(".py") else seg[:-3]
    return base


def _shadow_scope(files, pkg_roots, existed):
    """-> (bad paths, plain reasons). A changed path is refused when it could take the place of the test runner or a
    standard module: at the top of the workspace, or directly in a folder that holds the visible tests (`pkg_roots`;
    both are folders the test run puts on sys.path), its first name is a standard module or a runner; or, at any depth,
    it is install metadata (*.dist-info, *.egg-info, *.egg-link, *.pth, entry_points.txt) or a compiled cache.
    A standard-module name that the baseline already had (`existed(path)`, e.g. a project's own parser.py) is let through
    unless it is critical to the run; runner names and critical modules are never let through."""
    banned = _stdlib_names()
    critical = set(RUNNER_NAMES) | set(CRITICAL_MODULES)
    bad, why = [], []
    roots = {""} | {d for d in pkg_roots}
    for f in files:
        segs = [x for x in _norm(f).split("/") if x]
        hit = False
        for i, seg in enumerate(segs):
            if seg == "__pycache__" or seg.endswith(_METADATA_DIR_SUFFIXES) or seg.endswith(_METADATA_FILE_SUFFIXES) \
                    or seg in _PLUGIN_FILES:
                hit = True
        if not hit:
            parent = "/".join(segs[:-1])
            for root in roots:
                rs = [x for x in root.split("/") if x]
                if segs[:len(rs)] == rs and len(segs) > len(rs):
                    first = _mod_name(segs[len(rs)])
                    if first in critical or (first in banned and not existed(f)):
                        hit = True
                        break
        if hit:
            bad.append(f)
            why.append(SHADOW_WHY % f)
    return bad, why


def _hidden_count(sources):
    """How many tests the held-out files hold (def test... lines); a file that cannot be read counts 0."""
    n = 0
    for src in sources:
        try:
            with open(str(src), encoding="utf-8", errors="replace") as fh:
                n += len(re.findall(r"^\s*(?:async\s+)?def\s+test\w*\s*\(", fh.read(), re.M))
        except OSError:
            pass
    return n


def _mentions(sources, word):
    for src in sources:
        try:
            with open(str(src), encoding="utf-8", errors="replace") as fh:
                if word in fh.read():
                    return True
        except OSError:
            pass
    return False


def _sentinel_source(token):
    return ("import unittest\n\n\nclass SentinelCheck(unittest.TestCase):\n"
            "    def test_sentinel_%s(self):\n        self.fail(%r)\n" % (token, "GT-SENTINEL-" + token))


def _place_sentinel(root, folder, token):
    """Write the host's check test into `folder` of the copy at `root`. -> the path written."""
    d = os.path.join(root, *[x for x in folder.split("/") if x])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "test_zz_sentinel_%s.py" % token)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_sentinel_source(token))
    return path


def _sentinel_shown(body, token):
    """True when the runner's output shows the check test FAILING by name, with its unique message."""
    name = "test_sentinel_" + token
    named = re.search(r"^(?:FAIL|FAILED|ERROR)\b[^\n]*\b%s\b|^_{3,} [^\n]*\b%s\b[^\n]* _{3,}$" % (name, name), body, re.M)
    return bool(named) and ("AssertionError: GT-SENTINEL-" + token) in body


def _exec(workspace, cmd, timeout, allow_unisolated):
    """Run the test command in the sandbox. -> {error} or {rc, body, timed_out, head, tail}"""
    res = WS.run_command(workspace, {"command": cmd}, timeout=float(timeout), max_out=20000,
                         allow_unisolated=allow_unisolated,
                         env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
    if res.startswith("error:"):
        return {"error": "the tests could not be run: " + res[len("error:"):].strip()}
    head = res.split("\n", 1)[0]
    m = re.match(r"exit (-?\d+|None)", head)
    rc = int(m.group(1)) if m and m.group(1) not in ("None",) else None
    return {"rc": rc, "body": res.split("\n", 1)[1] if "\n" in res else "", "timed_out": "timed out" in head,
            "tail": res[-400:].strip()}


def _judge(x, timeout, token, expected_total=None, strict=True, skip_allow=0, xfail_allow=0):
    """Decide one test run that carried the host's check test. -> (ok, info, why); info has total, ran, failed (the
    check test left out), skipped, xfailed.

    The run counts only when ALL hold: its summary is the runner's own, once, and anchored (not tampered); the check
    test is shown FAILING by name and message and is the only failure; the number of tests run is the number expected
    (equal when `strict`, otherwise at least); no more tests were skipped or expected-to-fail than the allowances."""
    info = {"total": 0, "ran": 0, "failed": 0, "skipped": 0, "xfailed": 0}
    if x.get("error"):
        return False, info, x["error"]
    if x["timed_out"]:
        return False, info, "the tests timed out after %ss; make the code faster or the change smaller" % timeout
    p = _parse(x["body"])
    if p["tampered"]:
        return False, info, _TAMPER
    rc, tail = x["rc"], x["tail"]
    info.update(total=p["total"], ran=max(p["total"] - p["skipped"] - 1, 0), failed=p["failed"],
                skipped=p["skipped"], xfailed=p["xfailed"])
    if p["total"] == 0:
        return False, info, ("no tests were counted: the command succeeded but its output shows no anchored "
                             "'Ran N tests ... OK' or pytest summary line, so nothing proves the tests ran"
                             if rc == 0 else
                             "the test command exited with %s; run it and fix what it reports. Last output: %s" % (rc, tail))
    shown = _sentinel_shown(x["body"], token)
    if not shown:
        if p["failed"] == 0 and rc not in (0, 1, None):
            return False, info, "the test command exited with %s; run it and fix what it reports. Last output: %s" % (rc, tail)
        return False, info, UNTRUSTED
    if p["failed"] < 1 or rc not in (0, 1):
        if rc not in (0, 1, None):
            return False, info, "the test command exited with %s; run it and fix what it reports. Last output: %s" % (rc, tail)
        return False, info, _TAMPER           # the output says the check test failed, the summary says nothing failed
    info["failed"] = p["failed"] - 1
    if info["failed"]:
        return False, info, "%d test(s) failed; fix them and run the tests again. Last output: %s" % (info["failed"], tail)
    if expected_total is not None and (p["total"] != expected_total if strict else p["total"] < expected_total):
        return False, info, UNTRUSTED + " (%d tests ran, %d were expected)" % (p["total"], expected_total)
    if p["skipped"] > skip_allow:
        return False, info, "tests were skipped that ran before: %d" % (p["skipped"] - skip_allow)
    if p["xfailed"] > xfail_allow:
        return False, info, "tests were skipped that ran before: %d" % (p["xfailed"] - xfail_allow)
    return True, info, ""


def _run_tests(workspace, cmd, timeout, allow_unisolated, token=None, **kw):
    """Run `cmd` in `workspace` and judge it as _judge does. -> (ok, ran, failed, why). Without a `token` there is no
    check test: only the exit code and the anchored summary decide (kept for callers that judge a plain run)."""
    x = _exec(workspace, cmd, timeout, allow_unisolated)
    if token:
        ok, info, why = _judge(x, timeout, token, **kw)
        return ok, info["ran"], info["failed"], why
    if x.get("error"):
        return False, 0, 0, x["error"]
    if x["timed_out"]:
        return False, 0, 0, "the tests timed out after %ss; make the code faster or the change smaller" % timeout
    p = _parse(x["body"])
    if p["tampered"]:
        return False, 0, 0, _TAMPER
    rc, ran, failed = x["rc"], p["ran"], p["failed"]
    if rc != 0:
        if failed:
            return False, ran, failed, "%d test(s) failed; fix them and run the tests again. Last output: %s" % (
                failed, x["tail"])
        return False, ran, failed, "the test command exited with %s; run it and fix what it reports. Last output: %s" % (
            rc, x["tail"])
    if ran == 0:
        return False, 0, 0, ("no tests were counted: the command succeeded but its output shows no anchored "
                             "'Ran N tests ... OK' or pytest summary line, so nothing proves the tests ran")
    if failed or not p["passed_text"]:
        return False, ran, failed, "%d test(s) failed" % failed if failed else "the test summary does not say the tests passed"
    return True, ran, failed, ""


def _test_dirs(root):
    """Folders (relative, '/'-separated) of the copy at `root` that hold a visible test .py file, shallowest first."""
    out = set()
    for dp, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in WS.EXTRA_SKIP_DIRS]
        for n in names:
            rel = os.path.relpath(os.path.join(dp, n), root).replace(os.sep, "/")
            if n.endswith(".py") and _is_test_file(rel):
                out.add(rel.rsplit("/", 1)[0] if "/" in rel else "")
    return sorted(out, key=lambda d: (d.count("/") if d else -1, d))


def _read_change(workspace):
    """-> (files, patch, error). git can fail with a short read when a file grows under it: that is data, not a crash."""
    try:
        return sorted(WS.changed_files(workspace)), WS.take_patch(workspace), None
    except WS.WorkspaceError as e:
        return None, None, e


def _growing(err):
    return err is not None and any(m in str(err) for m in ("short read", "failed to insert into database",
                                                          "file changed as we read it"))


def _quiet_patch(workspace):
    """-> (files, patch, why): the agent's change twice, 0.3 s apart. A difference means something still writes there.
    A read that git itself loses because a file grew under it counts as the same thing; any other read error is raised."""
    f1, p1, e1 = _read_change(workspace)
    time.sleep(0.3)
    f2, p2, e2 = _read_change(workspace)
    for e in (e1, e2):
        if e is not None and not _growing(e):
            raise e
    if e1 is not None or e2 is not None or f1 != f2 or p1 != p2:
        return f2 or [], p2 or "", "files were still changing after the agent finished"
    return f2, p2, ""


def evaluate(workspace, *, test_command, allow=(), deny=(), read_only_tests=(), hidden_tests=None, timeout=300,
             allow_unisolated=False, allow_test_changes=False, allow_harness_changes=False):
    """Judge an agent's workspace. -> {ok, why, tests_ran, tests_failed, changed_files, out_of_scope, patch, scope_why,
    needs_review, baseline_ok, baseline_tests, tests_skipped}

    The judging never happens in the folder the agent used. The patch is read from it (twice, 0.3 s apart: it must not
    change), then the BASELINE and the patched tree are rebuilt in fresh copies, and the scope checks and every test run
    happen there. Each run carries a host-made check test that must be seen failing, as the only failure, with the
    expected number of tests around it, so a run cut short or forged by the code under test is not accepted; the
    test command must therefore find every test in the folder (python -m unittest discover, or pytest). A run that
    skips or expects to fail more tests than the untouched baseline did is refused.

    A modified, deleted or added test file is rejected by default. allow_test_changes=True (opt in, for a caller that
    really wants a new test) lifts that for pure test files only; the caller's read_only_tests stay frozen. Files that
    control how the tests run (TEST_HARNESS_FILES: conftest.py, sitecustomize.py, *.pth, pytest.ini, ...) are rejected
    unless allow_harness_changes=True, which is DANGEROUS (the agent then decides whether its own tests pass) and is
    never implied by allow_test_changes. A file that could replace the test runner or a standard module is always
    rejected. `needs_review` is True in every result: an accepted patch is a candidate for a person to read."""
    r = {"ok": False, "why": "", "tests_ran": 0, "tests_failed": 0, "changed_files": [], "out_of_scope": [],
         "patch": "", "scope_why": "", "needs_review": True, "baseline_ok": None, "baseline_tests": 0,
         "tests_skipped": 0}
    try:
        files, patch, loud = _quiet_patch(workspace)
    except (WS.WorkspaceError, OSError) as e:
        r["why"] = "the workspace could not be read: %s" % e
        return r
    r["changed_files"], r["patch"] = files, patch
    if loud:
        r["why"] = "rejected: " + loud
        return r
    bad, reasons = _scope(files, tuple(allow or ()), tuple(deny or ()), tuple(read_only_tests or ()),
                        allow_test_changes, allow_harness_changes)
    r["out_of_scope"], r["scope_why"] = bad, "; ".join(reasons)
    if bad:
        r["why"] = "rejected, out of scope: " + r["scope_why"]
        return r
    if not files:
        r["why"] = "rejected: the agent changed no files, so there is nothing to apply"
        return r
    tmp = tempfile.mkdtemp(prefix="gt-farm-eval-")
    try:
        return _evaluate_in_copies(r, workspace, tmp, files, patch, test_command, allow, deny, read_only_tests,
                                   hidden_tests, timeout, allow_unisolated, allow_test_changes, allow_harness_changes)
    except (WS.WorkspaceError, OSError) as e:
        r["ok"] = False
        r["why"] = "rejected: the clean copy could not be made: %s" % e
        return r
    finally:
        WS.rmtree(tmp)


def _evaluate_in_copies(r, workspace, tmp, files, patch, test_command, allow, deny, read_only_tests, hidden_tests,
                        timeout, allow_unisolated, allow_test_changes, allow_harness_changes):
    base = WS.fresh_copy(workspace, os.path.join(tmp, "base"), with_patch=False)["workspace"]
    p1 = WS.fresh_copy(workspace, os.path.join(tmp, "patched"), patch=patch)
    pdir = p1["workspace"]
    files = sorted(WS.changed_files(pdir))
    r["changed_files"], r["patch"] = files, WS.take_patch(pdir)
    bad, reasons = _scope(files, tuple(allow or ()), tuple(deny or ()), tuple(read_only_tests or ()),
                        allow_test_changes, allow_harness_changes)
    visible_dirs = _test_dirs(base)
    hidden = dict(hidden_tests or {})
    roots = set(visible_dirs) | {_norm(d).rsplit("/", 1)[0] for d in hidden if "/" in _norm(d)}
    sbad, swhy = _shadow_scope(files, roots, lambda f: os.path.exists(os.path.join(base, *_norm(f).split("/"))))
    bad, reasons = bad + [b for b in sbad if b not in bad], reasons + swhy
    r["out_of_scope"], r["scope_why"] = bad, "; ".join(reasons)
    if bad:
        r["why"] = "rejected, out of scope: " + r["scope_why"]
        return r
    token = os.urandom(6).hex()
    folder = ""
    if hidden:
        folder = _norm(sorted(hidden)[0]).rsplit("/", 1)[0] if "/" in _norm(sorted(hidden)[0]) else ""
    elif visible_dirs:
        folder = visible_dirs[0]
    # 1. the untouched baseline, once: how many tests it has and how many it skips
    bx = _exec(base, test_command, timeout, allow_unisolated)
    bp = _parse(bx["body"]) if not bx.get("error") and not bx["timed_out"] else None
    r["baseline_ok"] = bool(bp and not bp["tampered"] and bp["total"] > 0)
    base_total = bp["total"] if r["baseline_ok"] else None
    base_skip = bp["skipped"] if r["baseline_ok"] else 0
    base_xf = bp["xfailed"] if r["baseline_ok"] else 0
    r["baseline_tests"] = base_total or 0
    adds_tests = any(_is_test_file(f) for f in files)
    # 2. the patched tree with the check test, visible tests only
    _place_sentinel(pdir, folder, token)
    ok, info, why = _judge(_exec(pdir, test_command, timeout, allow_unisolated), timeout, token,
                           expected_total=None if base_total is None else base_total + 1,
                           strict=not adds_tests, skip_allow=base_skip, xfail_allow=base_xf)
    r["tests_ran"], r["tests_failed"], r["tests_skipped"] = info["ran"], info["failed"], info["skipped"]
    if not ok:
        r["why"] = "rejected: " + why
        return r
    if hidden:
        # 3. a third copy for the held-out run, so nothing the visible run started can reach it
        h = WS.fresh_copy(workspace, os.path.join(tmp, "held"), patch=patch)["workspace"]
        for dest, src in sorted(hidden.items()):
            target = os.path.join(os.path.realpath(h), *_norm(dest).split("/"))
            if not os.path.realpath(target).startswith(os.path.realpath(h) + os.sep):
                r["why"] = "rejected: hidden test path %s leaves the workspace" % dest
                return r
            if os.path.exists(target):
                r["why"] = ("rejected: %s already exists in the workspace, so the held-out test cannot be put "
                            "there; the agent may not create files at the held-out test paths" % dest)
                return r
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(str(src), target)
        _place_sentinel(h, folder, token)
        n_hidden = _hidden_count(hidden.values())
        hok, hinfo, hwhy = _judge(_exec(h, test_command, timeout, allow_unisolated), timeout, token,
                                  expected_total=None if base_total is None else base_total + n_hidden + 1,
                                  strict=not adds_tests and not _mentions(hidden.values(), "parametrize"),
                                  skip_allow=min(base_skip, info["skipped"]), xfail_allow=min(base_xf, info["xfailed"]))
        if not hok:
            r["tests_failed"] = max(hinfo["failed"], 1)
            r["why"] = "rejected: the held-out tests failed although the visible tests pass: " + hwhy
            return r
        r["tests_ran"], r["tests_skipped"] = hinfo["ran"], hinfo["skipped"]
    r["ok"] = True
    r["why"] = "The tests pass on a clean copy. Treat this as a candidate patch: review it before you apply it."
    return r


# ------------------------------------------------------------------------------------ apply back
def _git_apply(repo, args, patch):
    return subprocess.run(["git", "apply"] + args, cwd=repo, input=patch.encode("utf-8"), capture_output=True)


def apply_patch(repo_dir, patch, *, dry_run=True):
    """Validate (and, when dry_run is False, apply) `patch` in the real repository; never commits.
    -> {ok, why, files}"""
    res = {"ok": False, "why": "", "files": []}
    repo = os.path.realpath(str(repo_dir))
    if not os.path.isdir(repo):
        res["why"] = "%s is not a folder" % repo_dir
        return res
    if not isinstance(patch, str) or not patch.strip():
        res["why"] = "the patch is empty"
        return res
    if shutil.which("git") is None:
        res["why"] = "git was not found"
        return res
    pr = _git_apply(repo, ["--numstat", "-z"], patch)
    if pr.returncode != 0:
        res["why"] = "the patch is not a valid diff: " + pr.stderr.decode("utf-8", "replace").strip()[:300]
        return res
    parts = pr.stdout.decode("utf-8", "replace").split("\0")
    files, i = [], 0
    while i < len(parts) and parts[i]:
        cols = parts[i].split("\t", 2)
        if len(cols) == 3 and cols[2] == "":          # a rename: "a\tb\t" NUL old NUL new
            files += [parts[i + 1], parts[i + 2]] if i + 2 < len(parts) else []
            i += 3
        else:
            files.append(cols[2] if len(cols) == 3 else parts[i])
            i += 1
    files = sorted({f for f in files if f})
    res["files"] = files
    for f in files:
        n = f.replace("\\", "/")
        segs = [x for x in n.split("/") if x]
        if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in segs:
            res["why"] = "refused: %s lies outside the repository" % f
            return res
        real = os.path.realpath(os.path.join(repo, *segs))
        if real != repo and not real.startswith(repo + os.sep):
            res["why"] = "refused: %s resolves outside the repository (a link?)" % f
            return res
        if ".git" in segs:
            res["why"] = "refused: %s is inside .git" % f
            return res
        why = EG.deny_read(n)
        if why:
            res["why"] = "refused: %s looks like a secret or credential file (%s); do not apply it" % (f, why)
            return res
    if re.search(r"^(old|new) mode ", patch, re.M):
        res["why"] = "refused: the patch changes a file's permissions (an 'old mode' or 'new mode' line); remove that change"
        return res
    if re.search(r"^(new|deleted) file mode 100755", patch, re.M):
        res["why"] = "refused: the patch creates an executable file (mode 100755); remove that change"
        return res
    if re.search(r"^(new|old) file mode 120000", patch, re.M) or re.search(r"^index [0-9a-f]+\.\.[0-9a-f]+ 120000", patch, re.M):
        res["why"] = "refused: the patch creates or changes a symbolic link"
        return res
    chk = _git_apply(repo, ["--check"], patch)
    if chk.returncode != 0:
        res["why"] = ("the patch does not apply cleanly to the repository (it has changed since the workspace was "
                      "copied?): " + chk.stderr.decode("utf-8", "replace").strip()[:300])
        return res
    if dry_run:
        res["ok"], res["why"] = True, "the patch applies cleanly to %d file(s); nothing was changed (dry run)" % len(files)
        return res
    ap = _git_apply(repo, [], patch)
    if ap.returncode != 0:
        res["why"] = "git apply failed: " + ap.stderr.decode("utf-8", "replace").strip()[:300]
        return res
    res["ok"], res["why"] = True, "applied to %d file(s); nothing was committed" % len(files)
    return res


# ------------------------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_farm_accept.py", description=__doc__.split("Exit codes")[0].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    ev = sub.add_parser("evaluate", help="judge an agent's workspace")
    ev.add_argument("--workspace", required=True)
    ev.add_argument("--test-command", required=True)
    ev.add_argument("--allow", action="append", default=[])
    ev.add_argument("--deny", action="append", default=[])
    ev.add_argument("--read-only-tests", action="append", default=[])
    ev.add_argument("--hidden", action="append", default=[], metavar="DEST=SRC",
                    help="held-out test: path inside the workspace = file to copy there for the run")
    ev.add_argument("--timeout", type=int, default=300)
    ev.add_argument("--patch-out", help="write the accepted patch to this file")
    ev.add_argument("--json", action="store_true")
    ev.add_argument("--allow-test-changes", action="store_true", help="let the agent add or edit test files")
    ev.add_argument("--allow-harness-changes", action="store_true",
                    help="DANGEROUS: let the agent change conftest.py, sitecustomize.py, pytest.ini and the like")
    ev.add_argument("--allow-unisolated", action="store_true", help="run the tests with no sandbox (not advised)")
    ap_ = sub.add_parser("apply", help="check, or apply, a patch in the real repository")
    ap_.add_argument("--repo", required=True)
    ap_.add_argument("--patch", required=True)
    ap_.add_argument("--apply", action="store_true", help="change the files (default: only check)")
    ap_.add_argument("--dry-run", action="store_true", help="only check (the default)")
    a = ap.parse_args(argv)
    if a.cmd == "evaluate":
        hidden = {}
        for h in a.hidden:
            if "=" not in h:
                print("--hidden wants DEST=SRC, got %r" % h)
                return 2
            d, s = h.split("=", 1)
            hidden[d] = s
        r = evaluate(a.workspace, test_command=a.test_command, allow=a.allow, deny=a.deny,
                     read_only_tests=a.read_only_tests, hidden_tests=hidden or None, timeout=a.timeout,
                     allow_unisolated=a.allow_unisolated, allow_test_changes=a.allow_test_changes,
                     allow_harness_changes=a.allow_harness_changes)
        if r["ok"] and a.patch_out:
            with open(a.patch_out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(r["patch"])
        if a.json:
            print(json.dumps(r, indent=2))
        else:
            print(r["why"])
        return 0 if r["ok"] else 1
    if a.cmd == "apply":
        if a.apply and a.dry_run:
            print("--apply and --dry-run contradict each other")
            return 2
        try:
            with open(a.patch, encoding="utf-8") as fh:
                patch = fh.read()
        except OSError as e:
            print("cannot read the patch: %s" % e)
            return 2
        r = apply_patch(a.repo, patch, dry_run=not a.apply)
        print(r["why"])
        return 0 if r["ok"] else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
