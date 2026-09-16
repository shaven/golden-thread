#!/usr/bin/env python3
"""gt_validation.py -- what a file was VERIFIED to do, and when that stopped being true.

    gt_validation.py record --file F --verdict holds --checked "..." --gap "..." --by "..."
    gt_validation.py check  --file F        # exit 0 covered, 1 stale, 2 never validated
    gt_validation.py show   --file F        # the definition, as established
    gt_validation.py list                   # everything with a receipt, and its state

WHY THIS EXISTS

Every serious defect in 0.16.0 was a CLAIM THAT OUTLIVED ITS IMPLEMENTATION. A manifest row
shape that stopped matching. A `lint` check listed as running that was wired to nothing. An
aggregator counting installed rather than declared members. Each was true when written, and
nothing tied the claim to the code's current state, so nothing could notice when it stopped
being true. A docstring is a claim; it is not a control.

So a completed validation WRITES the definition, stamped with the file's content hash. Edit the
file and the receipt no longer covers it -- automatically, with nothing to remember and nothing
to recompute. The definition goes visibly stale instead of quietly wrong.

TWO RULES IT MUST HOLD, both learned the hard way

1. THE RECEIPT RECORDS WHAT WAS VERIFIED, INCLUDING THE GAPS. `--gap` is not optional
   decoration: a receipt that flattens to "validated" manufactures exactly the assurance this
   codebase keeps having to dig back out. A validation that could not determine something must
   say so, in the artefact, forever.
2. ABSENCE OF A RECEIPT READS AS UNKNOWN, NOT AS FINE. `check` on a file nobody validated exits
   2, distinct from both covered and stale -- the same rule as "a member that could not run is
   not a pass".

WHY CONTENT HASH, NOT TIME. gt_test_receipt.py uses time because a test run covers a whole tree
and the clock is the only thing the editor and the runner agree on. A validation is about ONE
file's behaviour, so the file's bytes are the better anchor: a hash cannot drift with a clock,
survives a copy between machines, and is exact -- any edit at all, however small, invalidates
the claim it was made about.

WHY THE LEDGER IS IN THE REPO. A test receipt attests that a tree passed HERE, so it is
machine-local. A validation attests to what the code DOES, which is a property of the code and
travels with it. It is a plain JSONL file a reviewer can read in a diff.
"""
import argparse
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# <repo>/dev/validations.jsonl when run from a checkout; falls back beside the script.
LEDGER_NAMES = ("dev/validations.jsonl",)
MAX_LINES = 2000
VERDICTS = ("holds", "broken", "cannot-verify")


def repo_root(start):
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def ledger_path(anchor=None):
    root = repo_root(anchor or os.getcwd()) or repo_root(HERE)
    if root:
        for name in LEDGER_NAMES:
            p = os.path.join(root, name)
            if os.path.isdir(os.path.dirname(p)):
                return p
    return os.path.join(os.path.expanduser("~/.claude/golden-thread"), "validations.jsonl")


def file_digest(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def rel_to_repo(path):
    root = repo_root(os.path.dirname(os.path.abspath(path)))
    ap = os.path.abspath(path)
    return os.path.relpath(ap, root).replace(os.sep, "/") if root else ap


def load(anchor=None):
    p = ledger_path(anchor)
    out = []
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue          # one unreadable row must not hide the rest
    except OSError:
        pass
    return out


def latest_for(rel, anchor=None):
    rows = [r for r in load(anchor) if r.get("file") == rel]
    return rows[-1] if rows else None


def cmd_record(args):
    digest = file_digest(args.file)
    if digest is None:
        print("cannot read %s" % args.file, file=sys.stderr)
        return 2
    if not args.checked:
        print("--checked is required: a receipt that does not say WHAT was verified is not "
              "evidence of anything", file=sys.stderr)
        return 2
    row = {
        "file": rel_to_repo(args.file),
        "sha256": digest,
        "verdict": args.verdict,
        "checked": args.checked,
        # Recorded even when empty, so a reader can tell "nothing was left undetermined" from
        # "nobody thought about what was left undetermined".
        "gaps": args.gap or [],
        "by": args.by or "unattributed",
        "at": int(time.time()),
    }
    p = ledger_path(args.file)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    rows = load(args.file)
    rows.append(row)
    with open(p, "w", encoding="utf-8") as fh:
        for r in rows[-MAX_LINES:]:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    print("recorded: %s %s (%s)" % (row["file"], row["verdict"], row["sha256"][:12]))
    if not row["gaps"]:
        print("NOTE: no --gap recorded. A validation that found nothing it could not determine "
              "is unusual; if that is really so, say so with --gap none.")
    return 0


def state_of(rel, path, anchor=None):
    """-> (state, receipt). States: covered | stale | never."""
    r = latest_for(rel, anchor)
    if not r:
        return "never", None
    return ("covered" if r.get("sha256") == file_digest(path) else "stale"), r


def cmd_check(args):
    rel = rel_to_repo(args.file)
    state, r = state_of(rel, args.file, args.file)
    if state == "never":
        print("NEVER VALIDATED: %s has no receipt. That is unknown, not clean." % rel)
        return 2
    when = time.strftime("%Y-%m-%d", time.localtime(r.get("at", 0)))
    if state == "stale":
        print("STALE: %s was validated on %s, and has changed since.\n"
              "  What was established then no longer describes this file." % (rel, when))
        return 1
    print("covered: %s validated %s -- %s" % (rel, when, r["verdict"]))
    return 0 if r["verdict"] == "holds" else 1


def cmd_show(args):
    rel = rel_to_repo(args.file)
    state, r = state_of(rel, args.file, args.file)
    if state == "never":
        print("%s has never been validated." % rel)
        return 2
    print("# %s" % rel)
    print("\nverdict:   %s" % r["verdict"])
    print("validated: %s by %s"
          % (time.strftime("%Y-%m-%d", time.localtime(r.get("at", 0))), r.get("by")))
    print("state:     %s" % ("CURRENT" if state == "covered"
                             else "STALE -- the file changed after this was established"))
    print("\n## What was verified\n")
    for line in r["checked"]:
        print("- %s" % line)
    print("\n## What was NOT determined\n")
    if r.get("gaps"):
        for line in r["gaps"]:
            print("- %s" % line)
    else:
        print("- (nothing recorded, which is itself unverified)")
    return 0 if state == "covered" else 1


def cmd_list(args):
    rows = {}
    for r in load(args.anchor):
        rows[r["file"]] = r
    if not rows:
        print("no validations recorded")
        return 0
    print("%-10s %-9s %s" % ("STATE", "VERDICT", "FILE"))
    stale = 0
    for rel, r in sorted(rows.items()):
        root = repo_root(HERE) or "."
        state = "covered" if r.get("sha256") == file_digest(os.path.join(root, rel)) else "stale"
        stale += state == "stale"
        print("%-10s %-9s %s" % (state, r["verdict"], rel))
    if stale:
        print("\n%d file(s) changed after they were validated; their recorded definitions no "
              "longer describe them." % stale)
    return 1 if stale else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="write what a validation established")
    r.add_argument("--file", required=True)
    r.add_argument("--verdict", required=True, choices=VERDICTS)
    r.add_argument("--checked", action="append", required=True,
                   help="one thing that was verified; repeatable")
    r.add_argument("--gap", action="append",
                   help="one thing that could NOT be determined; repeatable")
    r.add_argument("--by", help="who or what ran the validation")
    r.set_defaults(func=cmd_record)
    c = sub.add_parser("check", help="is the recorded definition still current?")
    c.add_argument("--file", required=True)
    c.set_defaults(func=cmd_check)
    s = sub.add_parser("show", help="the definition, as established")
    s.add_argument("--file", required=True)
    s.set_defaults(func=cmd_show)
    ls = sub.add_parser("list", help="everything with a receipt, and its state")
    ls.add_argument("--anchor", help="a path inside the repo to resolve the ledger from")
    ls.set_defaults(func=cmd_list)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
