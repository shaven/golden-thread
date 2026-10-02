#!/usr/bin/env python3
"""Receipts for test runs: what ran, in which repo, at which commit, and did it pass.

`core_test_before_commit` says code is not committed until its tests have been seen to
pass. A rule like that needs EVIDENCE, and "I ran them" is not evidence -- it is the
same claim the rule exists to stop trusting. So a run writes a receipt, and the commit
guard reads receipts.

    gt_test_receipt.py record --repo . --what "tests/run.sh" --ok --tests 681
    gt_test_receipt.py record --repo . --what "pytest" --failed
    gt_test_receipt.py latest --repo .            # the newest receipt, as JSON
    gt_test_receipt.py check --repo . [--files a b c]   # exit 0 if one covers these
    gt_test_receipt.py run --repo . [--dry-run]   # discover the repo's test command, run it,
                                                  # record the receipt (0 pass | 1 fail | 3 none)
    gt_test_receipt.py record --repo . --what "tests/run.sh --affected" --ok \
        --scope scoped --files-from affected.json    # a SCOPED receipt (0.18.0), see below
    gt_test_receipt.py check --repo . --files a b --allow-scoped   # what the commit guard asks
                                                                   # on a feature branch

SCOPED RECEIPTS (0.18.0). `tests/run.sh --affected` runs only the tests mapped to the changed
files and records a receipt that names those files. Such a receipt covers ONLY the files it
names, and only when the caller asks with `allow_scoped` -- which the commit guard does on a
branch that is not the default branch (setting `scoped_receipts`, default on). `check` without
`--allow-scoped` -- the release gates' question -- still needs a FULL-suite receipt, exactly as
before: a scoped row is invisible to `latest()` unless asked for.

`run` exists so `/gt:gt-allin` can run the tests too (owner, 2026-09-28: "add all the other
checks into allin"). It does not decide HOW a repo is tested: it asks the commit guard's own
`entry_point()` -- the one discovery table -- so the command allin runs is exactly the one the
guard would demand a receipt for. A second table would drift from the guard's, and a test run
that did not satisfy the gate would be the result.

A receipt covers a file only if it is NEWER than that file. That is the whole trick,
and it is why this stores a timestamp rather than a hash of the tree: editing a file
after the tests pass invalidates the receipt automatically, with nothing to remember
and nothing to recompute. Time is the one thing both the editor and the test runner
already agree on.

The ledger is ~/.claude/golden-thread/test-runs.jsonl -- machine-local, and REWRITTEN on
every `record`: the rows are read, the new one appended, the oldest dropped past MAX_LINES,
and the whole file replaced atomically, so it cannot grow without bound. It is not
append-only, and calling it that was wrong (found 2026-09-18): pruning is the point, and it
is safe here in a way it would not be in dev/validations.jsonl, because a receipt is
disposable evidence about a moment rather than a record anyone reviews later. A receipt
means nothing on another machine: it records that a particular working tree passed here.
"""
import argparse
import json
import os
import subprocess
import sys
import time

LEDGER = os.path.expanduser("~/.claude/golden-thread/test-runs.jsonl")
MAX_LINES = 500


def repo_root(path):
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return os.path.realpath(out.stdout.strip())
    except Exception:
        pass
    return os.path.realpath(path)


def head(path):
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def read():
    try:
        with open(LEDGER) as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return []


def record(repo, what, ok, tests=0, elapsed=0.0, scope=None, files=None):
    root = repo_root(repo)
    entry = {"repo": root, "what": what, "ok": bool(ok), "tests": int(tests or 0),
             "elapsed": round(float(elapsed or 0.0), 1), "at": time.time(),
             "at_human": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "head": head(root)}
    if scope == "scoped":
        entry["scope"] = "scoped"
        entry["files"] = sorted({os.path.relpath(os.path.join(root, f), root)
                                 for f in (files or [])})
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    rows = read()
    rows.append(entry)
    rows = rows[-MAX_LINES:]
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, LEDGER)
    return entry


def latest(repo, ok_only=True, scoped=False):
    """The newest FULL receipt (a scoped one only when `scoped=True` is asked for)."""
    root = repo_root(repo)
    rows = [r for r in read() if r.get("repo") == root and (r.get("ok") or not ok_only)
            and (scoped or r.get("scope") != "scoped")]
    return max(rows, key=lambda r: r.get("at", 0)) if rows else None


def covers_scoped(repo, files):
    """-> (ok, receipt, stale_file). Each file must be covered by the newest FULL receipt or by
    a passing SCOPED receipt that names it -- whichever is newer -- and be older than it."""
    root = repo_root(repo) or repo
    full = latest(repo, ok_only=True)
    scoped = [r for r in read() if r.get("repo") == root and r.get("ok")
              and r.get("scope") == "scoped"]
    used = None
    for f in files:
        full_path = f if os.path.isabs(f) else os.path.join(root, f)
        rel = os.path.relpath(full_path, root)
        cands = [r for r in scoped if rel in (r.get("files") or [])]
        if full:
            cands.append(full)
        if not cands:
            return False, None, f
        best = max(cands, key=lambda r: r.get("at", 0))
        try:
            if os.path.getmtime(full_path) > best.get("at", 0):
                return False, best, f
        except OSError:
            return False, best, f
        used = best if used is None or best.get("at", 0) > used.get("at", 0) else used
    return (True, used, None) if used else (False, None, None)


def covers(repo, files, allow_scoped=False):
    """-> (ok, receipt_or_None, stale_file_or_None).

    A passing receipt covers the change only if nothing in `files` has been touched
    since it was written. The first file newer than the receipt is named, because
    "your tests are stale" is unactionable and "you edited X after the run" is not.

    TWO FIXES, both found by validation on 2026-09-16, and both of which made this
    control silently useless rather than noisily broken:

    1. PATHS ARE RESOLVED AGAINST THE REPO ROOT. `files` comes from
       `git diff --cached --name-only` and is repo-RELATIVE, but this used to stat it
       against the CALLER's cwd. Run from anywhere except the repo root -- which is the
       normal case for a PreToolUse hook, whose cwd is the session's project, not
       necessarily the repo being committed -- every path missed, every miss was swallowed
       below, and a stale receipt covered everything. Same receipt, same repo, same file
       edited after the run: exit 1 from the repo root, exit 0 from /tmp.

    2. AN UNRESOLVABLE PATH FAILS CLOSED. `except OSError: continue` treated "I could not
       stat this" as "this is not evidence of staleness", so a deleted file, or any path
       that did not resolve, was always covered -- `git rm` never needed evidence at all.
       A path that cannot be checked is an UNKNOWN, and this codebase's rule is that an
       unknown is not a pass. It is now reported as the reason, and the caller refuses.
    """
    if allow_scoped:
        return covers_scoped(repo, files)
    r = latest(repo, ok_only=True)
    if not r:
        return False, None, None
    when = r.get("at", 0)
    root = repo_root(repo) or repo
    for f in files:
        full = f if os.path.isabs(f) else os.path.join(root, f)
        try:
            if os.path.getmtime(full) > when:
                return False, r, f
        except OSError:
            # Deleted, renamed, or a name this process cannot resolve. Removing a file is a
            # change like any other and needs evidence the tests were seen to pass after it.
            return False, r, f
    return True, r, None


# ---- check receipts (0.18.0) --------------------------------------------------------
#
# gt_check.py -- the validation host -- records what its checkers saw here, in a sibling
# ledger. Same shape of reasoning as a test receipt, ONE difference: a check receipt names
# the CONTENT HASH of every file, not a time. A test run covers a tree; a checker verdict is
# about particular bytes, and the commit guard asks about the bytes in the index, which a
# timestamp cannot identify (stage, edit, re-stage the old bytes: the times all move).

CHECK_LEDGER = os.path.join(os.path.dirname(LEDGER), "check-runs.jsonl")


def read_checks():
    try:
        with open(CHECK_LEDGER) as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return []


def record_check(repo, files, ok, event="files"):
    """One row: {repo, at, head, ok, event, files: {rel: {sha256, checkers: {key: verdict}}}}."""
    root = repo_root(repo)
    entry = {"repo": root, "ok": bool(ok), "event": event, "at": time.time(),
             "at_human": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "head": head(root),
             "files": files}
    os.makedirs(os.path.dirname(CHECK_LEDGER), exist_ok=True)
    rows = read_checks()
    rows.append(entry)
    rows = rows[-MAX_LINES:]
    tmp = CHECK_LEDGER + ".tmp"
    with open(tmp, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    os.replace(tmp, CHECK_LEDGER)
    return entry


def check_covers(repo, hashes):
    """-> (ok, why, checker). `hashes` is {rel: sha256 of the content being committed}.

    Each file needs the NEWEST receipt row that saw exactly these bytes, and every checker
    on that row must have passed. `cannot-check` is not a pass. A file the receipt saw with
    no applicable checker is covered: it was checked, and nothing applied. The first
    uncovered file is named, and the failing checker with it."""
    root = repo_root(repo)
    rows = sorted((r for r in read_checks() if r.get("repo") == root),
                  key=lambda r: r.get("at", 0), reverse=True)
    for rel, want in sorted(hashes.items()):
        row = next((r for r in rows
                    if ((r.get("files") or {}).get(rel) or {}).get("sha256") == want), None)
        if row is None:
            return False, ("no check receipt covers the staged content of %s" % rel), None
        for key, verdict in sorted((row["files"][rel].get("checkers") or {}).items()):
            if verdict != "pass":
                return False, ("%s: checker %s reported %s on this content"
                               % (rel, key, verdict)), key
    return True, None, None


def _discovery():
    """The commit guard's module, from beside this script (the installed hooks dir) or from
    the release's hooks/ (a copy run from the tree)."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "guard_test_before_commit.py"),
                 os.path.join(here, "..", "hooks", "guard_test_before_commit.py")):
        if os.path.isfile(cand):
            spec = importlib.util.spec_from_file_location("gt_guard_test_discovery", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


def run(repo, dry_run=False):
    root = repo_root(repo)
    if not root:
        print("not a git repository: %s" % repo, file=sys.stderr)
        return 3
    mod = _discovery()
    if mod is None:
        print("cannot find the commit guard's test discovery -- is gt installed?",
              file=sys.stderr)
        return 3
    how = mod.entry_point(root)
    if not how:
        print("no test entry point in %s (looked for: %s, package.json test, Makefile test)"
              % (root, ", ".join(m for m, _ in mod.TEST_ENTRY_POINTS)), file=sys.stderr)
        return 3
    if dry_run:
        print("would run `%s` in %s and record the receipt" % (how, root))
        return 0
    print("running `%s` in %s" % (how, root))
    t0 = time.time()
    try:
        proc = subprocess.run(how, shell=True, cwd=root, capture_output=True, text=True)
    except OSError as exc:
        print("could not start `%s`: %s" % (how, exc), file=sys.stderr)
        return 3
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-25:]
    print("\n".join(tail))
    ok = proc.returncode == 0
    record(root, "gt-allin: " + how, ok=ok, elapsed=time.time() - t0)
    print("%s: `%s` exited %d" % ("PASS" if ok else "FAIL", how, proc.returncode))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="record and query test-run receipts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--repo", default=".")
    rec.add_argument("--what", required=True)
    rec.add_argument("--tests", type=int, default=0)
    rec.add_argument("--elapsed", type=float, default=0.0)
    rec.add_argument("--scope", choices=("full", "scoped"), default="full")
    rec.add_argument("--files-from", help="with --scope scoped: the files it covers -- a JSON "
                                          "file with a `files` list (prun.py --affected), or "
                                          "one path per line")
    g = rec.add_mutually_exclusive_group(required=True)
    g.add_argument("--ok", action="store_true")
    g.add_argument("--failed", action="store_true")
    lat = sub.add_parser("latest")
    lat.add_argument("--repo", default=".")
    chk = sub.add_parser("check")
    chk.add_argument("--repo", default=".")
    chk.add_argument("--files", nargs="*", default=[])
    chk.add_argument("--allow-scoped", action="store_true",
                     help="accept a scoped receipt for the files it names (feature branches)")
    rn = sub.add_parser("run", help="discover the repo's test command, run it, record it")
    rn.add_argument("--repo", default=".")
    rn.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.cmd == "run":
        return run(a.repo, a.dry_run)
    if a.cmd == "record":
        files = None
        if a.scope == "scoped":
            if not a.files_from:
                print("--scope scoped needs --files-from: a scoped receipt covers only the "
                      "files it names", file=sys.stderr)
                return 2
            with open(a.files_from) as fh:
                raw = fh.read()
            try:
                files = json.loads(raw).get("files") or []
            except (ValueError, AttributeError):
                files = [l.strip() for l in raw.splitlines() if l.strip()]
        e = record(a.repo, a.what, ok=a.ok, tests=a.tests, elapsed=a.elapsed,
                   scope=a.scope, files=files)
        print("recorded %s: %s%s in %s" % ("PASS" if e["ok"] else "FAIL", e["what"],
                                           " (%d tests)" % e["tests"] if e["tests"] else "",
                                           e["repo"]))
        return 0
    if a.cmd == "latest":
        r = latest(a.repo, ok_only=False)
        print(json.dumps(r, indent=2) if r else "no receipt for this repo")
        return 0 if r else 1
    ok, r, stale = covers(a.repo, a.files, allow_scoped=a.allow_scoped)
    if ok:
        print("covered by %s at %s" % (r["what"], r["at_human"]))
        return 0
    if r and stale:
        print("receipt is stale: %s changed after %s" % (stale, r["at_human"]))
    else:
        print("no passing receipt for this repo")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
