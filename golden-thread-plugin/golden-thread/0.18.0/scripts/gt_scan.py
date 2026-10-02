#!/usr/bin/env python3
"""gt_scan.py -- run the scans, and say which ones ran.

An AGGREGATOR. It owns no checking logic: every check lives in a leaf command that runs and is
tested on its own, and this sequences them. The moment an aggregator starts deciding things
itself, the leaves stop being the truth and there is a third behaviour nobody tests.

  gt_scan.py <path> [--vault V] [--only language,...] [--list] [--json]
             [--resume CHECKPOINT] [--no-checkpoint | --dry-run]

CHECKPOINTS (0.18.0). A run writes a progress checkpoint before its first member and after each
one (gt_checkpoint.py: `<vault>/Projects/golden-thread/spool/scan/...progress.json`), and
deletes it when the run completes. `--resume <checkpoint>` skips the members already done,
merges their recorded results with the new ones, and reports the whole run as one. The
`language` member is checkpointed per FILE inside it, so a resume also skips the files it had
already scanned. A run without `--resume` always starts fresh. `--no-checkpoint` (alias
`--dry-run`: it writes nothing to the vault) runs without one. Find a resumable run with
`gt_checkpoint.py find --tool scan --target <path>`.

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
import gt_checkpoint                                       # noqa: E402

# name -> (script, what it checks). A member is listed here only when it EXISTS; a leaf that is
# planned but unbuilt must not appear, because a listed-but-missing member reads as coverage.
# (script, what it checks, the aggregator flags this member ACCEPTS).
#
# The third element exists because forwarding a flag blindly is how an aggregator breaks its
# own members. `--all-files` means "also scan generated, vendored and static files" and only
# `language` has that idea -- `code` decides scope by a suffix/shebang union rule with nothing
# to widen. Forwarded to it anyway, it exited 2 on argparse usage, so `gt_scan.py . --all-files`
# reported "1 of 2 member(s) ran" and the second member was never checked. Found 2026-09-28 by
# an agent reading the file to document it, not by a test -- the flag combination was never
# exercised. A member's flag surface is now DECLARED, so a new member with a different one
# cannot silently do this again.
MEMBERS = {
    "language": ("gt_scan_language.py",
                 "encoding and naming, against the language definitions in effect",
                 frozenset({"--all-files", "--checkpoint", "--resume"})),
    "code": ("gt_scan_code.py",
             "source validation, against the lint rules in effect",
             frozenset()),
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
    ap.add_argument("--resume", metavar="CHECKPOINT",
                    help="continue an interrupted run from its checkpoint")
    ap.add_argument("--no-checkpoint", "--dry-run", dest="no_checkpoint", action="store_true",
                    help="write no checkpoint (nothing is written to the vault)")
    args = ap.parse_args(argv)

    have = available()
    if args.list:
        print("%-12s %-26s %s" % ("MEMBER", "SCRIPT", "CHECKS"))
        for name, (script, what, _flags) in sorted(MEMBERS.items()):
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

    asked_flags = ["--all-files"] if args.all_files else []
    # A flag is passed only to members that declare it, and a member that does NOT take it is
    # named rather than left to look as though the flag applied to it.
    ignored = {f: [n for n in wanted if f not in MEMBERS[n][2]] for f in asked_flags}
    if not args.json:
        # Say what is about to happen BEFORE it happens: composition is never a surprise.
        print("running %d scan member(s): %s" % (len(wanted), ", ".join(wanted)))
        for flag, members in ignored.items():
            if members:
                print("  note: %s does not apply to %s; that member scans its own scope"
                      % (flag, ", ".join(members)))

    ck, prior = None, []
    if args.resume:
        try:
            ck = gt_checkpoint.Checkpoint.load(args.resume, "scan")
        except gt_checkpoint.CheckpointError as exc:
            print("gt-scan: cannot resume: %s" % exc, file=sys.stderr)
            return USAGE
        if os.path.realpath(ck.data["target"]) != os.path.realpath(args.path) \
                or ck.items != wanted:
            print("gt-scan: cannot resume: that checkpoint is for %s with member(s) %s, not this "
                  "run" % (ck.data["target"], ", ".join(ck.items)), file=sys.stderr)
            return USAGE
        prior = list(ck.results)
        if not args.json:
            print("resuming: %d of %d member(s) already done (%s)"
                  % (ck.next_index, ck.total, ", ".join(ck.items[:ck.next_index]) or "none"))
    elif not args.no_checkpoint:
        try:
            ck = gt_checkpoint.Checkpoint.start(
                "scan", os.path.abspath(args.path), wanted,
                gt_checkpoint.find_vault(args.vault),
                args={"only": args.only, "all_files": args.all_files})
        except OSError as exc:
            print("gt-scan: note: no checkpoint (%s); an interruption will start over"
                  % exc.__class__.__name__, file=sys.stderr)
            ck = None

    results = list(prior)
    for i, n in enumerate(wanted):
        if i < len(prior):
            continue
        extra = [f for f in asked_flags if f in MEMBERS[n][2]]
        if ck is not None and "--checkpoint" in MEMBERS[n][2]:
            leaf_ck = (str(ck.path)[:-len(gt_checkpoint.SUFFIX)] + ".%s%s"
                       % (n, gt_checkpoint.SUFFIX))
            extra += (["--resume", leaf_ck] if os.path.isfile(leaf_ck)
                      else ["--checkpoint", leaf_ck])
        r = run_member(n, MEMBERS[n][0], args.path, args.vault, extra, args.timeout)
        if ck is not None and r.get("exit") == gt_checkpoint.ABORTED:
            # The member was interrupted with its own checkpoint in place. Not a result:
            # stop here so --resume continues inside that member, not after it.
            print("gt-scan: interrupted inside member %s; resume with --resume %s"
                  % (n, ck.path), file=sys.stderr)
            return gt_checkpoint.ABORTED
        results.append(r)
        if ck is not None:
            ck.done(i, r)
    if ck is not None:
        ck.finish()
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
