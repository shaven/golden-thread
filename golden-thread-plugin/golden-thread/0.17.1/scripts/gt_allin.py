#!/usr/bin/env python3
"""gt_allin.py -- run every check in one command, and never let a skipped one look like a pass.

  gt_allin.py [--vault V] [--repo PATH] [--only a,b] [--list] [--json]

An AGGREGATOR, like gt_scan.py, and for the same reason: it owns no checking logic. Every member
is a command that runs and is tested on its own. The moment this file starts deciding things
itself there is a third behaviour nobody tests.

MEMBERS are discovered from the release; one that is not installed is REPORTED, never skipped.

WHY THIS DOES NOT PUSH

"Scan, validate, push, implement" was the original shape, and pushing does not belong in it.
Push is outward-facing and effectively irreversible -- other people fetch it, CI acts on it,
and a bad commit is a public fact. Worse, an aggregator is the one place where a partial run is
easy to mistake for a complete one, and this repository's own Core rule 4 says never commit code
whose tests you have not seen pass. A command that pushed at the end of a run where one member
could not execute would break the rule it exists to enforce.

So this reports, and a person pushes. `--suggest-push` prints the command it would have run,
which keeps the convenience and leaves the decision where it belongs. There is deliberately no
flag that makes this push by itself.

THE RULE IT SHARES WITH gt_scan: "a member could not run" outranks "a member found something".
The headline says how many of how many ran, always, because a finding count on its own cannot
tell "nothing is wrong" from "nothing was checked".

Exit: 0 all clean | 1 findings, every member ran | 2 usage | 3 a member could not run
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gt_aggregate                                        # noqa: E402

# name -> (script, args builder, what it is). `needs_vault` members are skipped WITH A REPORT
# when no vault is given, never silently.
MEMBERS = {
    "scan": {"script": "gt_scan.py", "what": "code against the language definitions in effect",
             "needs": ("repo",)},
    "lint": {"script": "gt_lint.py", "what": "vault structure: links, orphans, index gaps",
             "needs": ("vault",)},
    "optimize": {"script": "gt_optimize.py",
                 "what": "vault content that costs context (report only, never applied here)",
                 "needs": ("vault",)},
    "doctor": {"script": "gt_doctor.py", "what": "install health: versions, drift, wiring",
               "needs": ()},
}

CLEAN, FINDINGS, USAGE, NO_ANSWER = (gt_aggregate.CLEAN, gt_aggregate.FINDINGS,
                                     gt_aggregate.USAGE, gt_aggregate.NO_ANSWER)


def available():
    return {n: m for n, m in MEMBERS.items()
            if os.path.isfile(os.path.join(HERE, m["script"]))}


def build_cmd(name, meta, vault, repo):
    cmd = [sys.executable, os.path.join(HERE, meta["script"])]
    if name == "scan":
        cmd.append(repo)
        if vault:
            cmd += ["--vault", vault]
    elif name == "lint":
        cmd.append(vault)
    elif name == "optimize":
        # Report only. --apply is never passed from here: a sweep command that edits files as a
        # side effect of "checking everything" is how content gets changed without anyone
        # deciding to change it.
        cmd += ["--vault", vault]
    elif name == "doctor":
        if vault:
            cmd += ["--vault", vault]
    return cmd


def run_member(name, meta, vault, repo, timeout):
    """Every reason a member cannot run is reported the same way: as not having run."""
    if not os.path.isfile(os.path.join(HERE, meta["script"])):
        return {"member": name, "ran": False, "exit": None, "status": "could-not-run",
                "detail": "%s is not installed" % meta["script"], "output": ""}
    missing = [n for n in meta["needs"] if not (vault if n == "vault" else repo)]
    if missing:
        return {"member": name, "ran": False, "exit": None, "status": "could-not-run",
                "detail": "needs --%s" % ", --".join(missing), "output": ""}
    return gt_aggregate.run_member(name, build_cmd(name, meta, vault, repo), timeout)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", help="the vault to check (never inferred)")
    ap.add_argument("--repo", help="the code tree to scan")
    ap.add_argument("--only", help="comma-separated member names")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--suggest-push", action="store_true",
                    help="print the push command; this tool never runs it")
    ap.add_argument("--timeout", type=int, default=gt_aggregate.DEFAULT_TIMEOUT_S,
                    help="seconds any one member may take (default %d)"
                         % gt_aggregate.DEFAULT_TIMEOUT_S)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    have = available()
    if args.list:
        print("%-10s %-20s %s" % ("MEMBER", "SCRIPT", "WHAT IT CHECKS"))
        for name, m in sorted(MEMBERS.items()):
            mark = "" if name in have else "   (NOT INSTALLED)"
            print("%-10s %-20s %s%s" % (name, m["script"], m["what"], mark))
        print("\nThis command never pushes and never applies a change. See --suggest-push.")
        return 0

    # The denominator is what this command DECLARES it checks. Using the installed set meant a
    # missing member vanished and a partial install read as a complete clean sweep.
    wanted, err = gt_aggregate.resolve_wanted(MEMBERS, have, args.only)
    if err:
        print(err, file=sys.stderr)
        return USAGE
    if not args.json:
        print("running %d member(s): %s" % (len(wanted), ", ".join(wanted)))
        print("this command reports only -- it does not push, commit or apply anything\n")

    results = [run_member(n, MEMBERS[n], args.vault, args.repo, args.timeout)
               for n in wanted]
    ran = [r for r in results if r["ran"]]
    failed = [r for r in results if not r["ran"]]
    found = [r for r in ran if r["status"] == "findings"]

    if args.json:
        print(json.dumps({"version": 1, "asked": wanted,
                          "headline": "%d of %d member(s) ran; %d reported findings"
                                      % (len(ran), len(wanted), len(found)),
                          "ran": [r["member"] for r in ran],
                          "could_not_run": [{"member": r["member"], "exit": r["exit"],
                                             "detail": r["detail"]} for r in failed],
                          "results": results}, indent=2))
    else:
        for r in results:
            print("=== %s: %s ===" % (r["member"], r["status"]))
            if r["output"]:
                print(r["output"])
            if not r["ran"] and r["detail"]:
                print("  %s" % r["detail"])
            print()
        gt_aggregate.summarise(results, wanted)
        if args.suggest_push:
            # "Everything that RAN was clean" was true and misleading: it never said how few
            # ran. Suggest only when every DECLARED member ran clean, and name the roster.
            if failed or found or len(wanted) < len(MEMBERS):
                print("\nNOT suggesting a push: %d of %d declared member(s) ran, %d could not "
                      "run, %d reported findings."
                      % (len(ran), len(MEMBERS), len(failed), len(found)))
            else:
                print("\nAll %d declared member(s) ran clean: %s"
                      % (len(MEMBERS), ", ".join(sorted(MEMBERS))))
                print("To push, run it yourself:")
                print("  git -C %s push" % (args.repo or "<repo>"))
                print("This tool does not push: it cannot know whether the tests you are "
                      "relying on were actually run in this session.")

    if failed:
        return NO_ANSWER
    return FINDINGS if found else CLEAN


if __name__ == "__main__":
    sys.exit(main())
