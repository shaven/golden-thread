#!/usr/bin/env python3
"""gt_scan.py -- run the scans, and say which ones ran.

An AGGREGATOR. It owns no checking logic: every check lives in a leaf command that runs and is
tested on its own, and this sequences them. The moment an aggregator starts deciding things
itself, the leaves stop being the truth and there is a third behaviour nobody tests.

  gt_scan.py <path> [--vault V] [--only language,...] [--list] [--json]

MEMBERS are discovered from the release, so a leaf that is not installed is REPORTED as absent
rather than quietly skipped.

THE RULE THIS FILE EXISTS FOR: "a member could not run" outranks "a member found something".

An aggregator is exactly where a step that did not happen gets absorbed into an overall pass.
If the language scan finds nothing because it could not load its definitions, and this prints
"scan complete, 0 findings", the aggregation has manufactured the assurance the scan was
supposed to provide. So every member's status is printed individually, a member that could not
run sets exit 3 even when another member succeeded, and the summary line says how many ran out
of how many were asked for -- never just the finding count.

Exit: 0 all members clean | 1 findings, every member ran | 2 usage | 3 a member could not run
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gt_aggregate                                        # noqa: E402

# name -> (script, what it checks). A member is listed here only when it EXISTS; a leaf that is
# planned but unbuilt must not appear, because a listed-but-missing member reads as coverage.
MEMBERS = {
    "language": ("gt_scan_language.py",
                 "encoding and naming, against the language definitions in effect"),
}

# Leaf exit codes, so the aggregator interprets rather than guesses.
CLEAN, FINDINGS, USAGE, NO_ANSWER = (gt_aggregate.CLEAN, gt_aggregate.FINDINGS,
                                     gt_aggregate.USAGE, gt_aggregate.NO_ANSWER)
# The leaf's "not one definition resolved". It is already a could-not-run to gt_aggregate, which
# treats anything outside clean/findings that way -- but until 2026-09-18 nothing READ this
# constant, so the comment above was a claim about an interpretation that never happened and the
# reader saw a bare `exit=4`. Now it names the condition, which is the difference between "the
# scan failed" and "the scan had nothing to scan with, so install or fix the packs".
NOTHING_LOADED = 4
NOTHING_LOADED_DETAIL = ("the leaf loaded no definitions, so nothing was scanned -- check "
                         "`gt_registry.py sources`; packs may be missing or failing to verify")


def available():
    return {name: meta for name, meta in MEMBERS.items()
            if os.path.isfile(os.path.join(HERE, meta[0]))}


def run_member(name, script, path, vault, extra, timeout):
    """Not installed, timed out, crashed, or exited for any reason other than clean/findings --
    all of it reported the same way: as not having run. See gt_aggregate."""
    full = os.path.join(HERE, script)
    if not os.path.isfile(full):
        return {"member": name, "ran": False, "exit": None, "status": "could-not-run",
                "detail": "%s is not installed" % script, "output": ""}
    cmd = [sys.executable, full, path]
    if vault:
        cmd += ["--vault", vault]
    result = gt_aggregate.run_member(name, cmd + extra, timeout)
    if result["exit"] == NOTHING_LOADED:
        # Interpreted, not guessed at. The leaf's own stderr says this too, but the aggregator's
        # COULD NOT RUN line is what a reader sees first, and `exit=4` alone said nothing.
        result["detail"] = ("%s | %s" % (NOTHING_LOADED_DETAIL, result["detail"]))[:400] \
            if result["detail"] else NOTHING_LOADED_DETAIL
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", nargs="?", help="the tree to scan")
    ap.add_argument("--vault", help="the vault whose local packs apply")
    ap.add_argument("--only", help="comma-separated member names")
    ap.add_argument("--list", action="store_true", help="print the members and exit")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all-files", action="store_true")
    ap.add_argument("--timeout", type=int, default=gt_aggregate.DEFAULT_TIMEOUT_S)
    args = ap.parse_args(argv)

    have = available()
    if args.list:
        print("%-12s %-26s %s" % ("MEMBER", "SCRIPT", "CHECKS"))
        for name, (script, what) in sorted(MEMBERS.items()):
            mark = "" if name in have else "   (NOT INSTALLED)"
            print("%-12s %-26s %s%s" % (name, script, what, mark))
        return 0
    if not args.path:
        ap.error("a path is required")

    # Declared set as the denominator: an uninstalled member must not vanish from the count.
    wanted, err = gt_aggregate.resolve_wanted(MEMBERS, have, args.only)
    if err:
        print(err, file=sys.stderr)
        return USAGE

    extra = ["--all-files"] if args.all_files else []
    if not args.json:
        # Say what is about to happen BEFORE it happens: composition is never a surprise.
        print("running %d scan member(s): %s" % (len(wanted), ", ".join(wanted)))

    results = [run_member(n, MEMBERS[n][0], args.path, args.vault, extra,
                          args.timeout) for n in wanted]
    ran = [r for r in results if r["ran"]]
    failed = [r for r in results if not r["ran"]]
    found = [r for r in ran if r["status"] == "findings"]

    if args.json:
        print(json.dumps({"version": 1, "path": os.path.abspath(args.path),
                          "headline": "%d of %d member(s) ran; %d reported findings"
                                      % (len(ran), len(wanted), len(found)),
                          "asked": wanted, "ran": [r["member"] for r in ran],
                          "could_not_run": [{"member": r["member"], "exit": r["exit"],
                                             "detail": r["detail"]} for r in failed],
                          "results": results}, indent=2))
    else:
        for r in results:
            if r["output"]:
                print("\n--- %s ---" % r["member"])
                print(r["output"])
        print()
        gt_aggregate.summarise(results, wanted)

    if failed:
        return NO_ANSWER
    return FINDINGS if found else CLEAN


if __name__ == "__main__":
    sys.exit(main())
