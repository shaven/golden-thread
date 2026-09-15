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

A receipt covers a file only if it is NEWER than that file. That is the whole trick,
and it is why this stores a timestamp rather than a hash of the tree: editing a file
after the tests pass invalidates the receipt automatically, with nothing to remember
and nothing to recompute. Time is the one thing both the editor and the test runner
already agree on.

The ledger is ~/.claude/golden-thread/test-runs.jsonl -- machine-local, append-only,
and pruned to the most recent MAX_LINES so it cannot grow without bound. A receipt
means nothing on another machine: it records that a particular working tree passed
here.
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


def record(repo, what, ok, tests=0, elapsed=0.0):
    root = repo_root(repo)
    entry = {"repo": root, "what": what, "ok": bool(ok), "tests": int(tests or 0),
             "elapsed": round(float(elapsed or 0.0), 1), "at": time.time(),
             "at_human": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "head": head(root)}
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


def latest(repo, ok_only=True):
    root = repo_root(repo)
    rows = [r for r in read() if r.get("repo") == root and (r.get("ok") or not ok_only)]
    return max(rows, key=lambda r: r.get("at", 0)) if rows else None


def covers(repo, files):
    """-> (ok, receipt_or_None, stale_file_or_None).

    A passing receipt covers the change only if nothing in `files` has been touched
    since it was written. The first file newer than the receipt is named, because
    "your tests are stale" is unactionable and "you edited X after the run" is not.
    """
    r = latest(repo, ok_only=True)
    if not r:
        return False, None, None
    when = r.get("at", 0)
    for f in files:
        try:
            if os.path.getmtime(f) > when:
                return False, r, f
        except OSError:
            continue                      # deleted or unreadable: not evidence of staleness
    return True, r, None


def main():
    ap = argparse.ArgumentParser(description="record and query test-run receipts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--repo", default=".")
    rec.add_argument("--what", required=True)
    rec.add_argument("--tests", type=int, default=0)
    rec.add_argument("--elapsed", type=float, default=0.0)
    g = rec.add_mutually_exclusive_group(required=True)
    g.add_argument("--ok", action="store_true")
    g.add_argument("--failed", action="store_true")
    lat = sub.add_parser("latest")
    lat.add_argument("--repo", default=".")
    chk = sub.add_parser("check")
    chk.add_argument("--repo", default=".")
    chk.add_argument("--files", nargs="*", default=[])
    a = ap.parse_args()

    if a.cmd == "record":
        e = record(a.repo, a.what, ok=a.ok, tests=a.tests, elapsed=a.elapsed)
        print("recorded %s: %s%s in %s" % ("PASS" if e["ok"] else "FAIL", e["what"],
                                           " (%d tests)" % e["tests"] if e["tests"] else "",
                                           e["repo"]))
        return 0
    if a.cmd == "latest":
        r = latest(a.repo, ok_only=False)
        print(json.dumps(r, indent=2) if r else "no receipt for this repo")
        return 0 if r else 1
    ok, r, stale = covers(a.repo, a.files)
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
