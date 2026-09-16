#!/usr/bin/env python3
"""gt_allin_commit.py -- commit, but only once the evidence exists. Never pushes.

  gt_allin_commit.py --repo PATH -m "message" [--vault V] [--allow-findings]
                     [--allow-default-branch] [--dry-run]

WHY THIS IS A SEPARATE COMMAND FROM gt_allin.py

`gt_allin` reports and changes nothing; this changes the repository. Keeping them apart means
the sweep can be run freely, by anyone, at any time, without wondering what it will do -- and
committing stays a thing someone chose. A single command that checked *and* committed would
make every routine check a potential write.

WHY IT COMMITS BUT WILL NOT PUSH

A commit is local and reversible: `git reset` undoes it and nobody else ever saw it. A push is
outward-facing and effectively permanent -- other people fetch it, CI acts on it, and a bad
commit becomes a public fact. That asymmetry is the whole line. There is no --push flag.

WHY IT CHECKS THE RECEIPT ITSELF

`guard_test_before_commit` already denies a commit with no passing test receipt -- but it is a
PreToolUse hook, so it sees the MODEL running `git commit` in a Bash tool call and never sees a
SCRIPT running it through subprocess. A script that shelled out to git would walk straight past
the one mechanism enforcing Core rule 4. So the same check is made here, directly, against the
same ledger (gt_test_receipt.py). The protection cannot depend on who is holding the pen.

Exit: 0 committed | 1 refused, and why | 2 usage | 3 a check could not run
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BRANCHES = ("main", "master")


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, **kw)


def staged_files(repo):
    out = git(repo, "diff", "--cached", "--name-only")
    if out.returncode != 0:
        return None
    return [ln.strip() for ln in out.stdout.split("\n") if ln.strip()]


def run_script(name, args):
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        return None, "%s is not installed" % name
    proc = subprocess.run([sys.executable, path] + args, capture_output=True, text=True)
    return proc, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True, help="the repository to commit in")
    ap.add_argument("-m", "--message", required=True, help="the commit message")
    ap.add_argument("--vault", help="vault, so the vault-side checks can run too")
    ap.add_argument("--allow-findings", action="store_true",
                    help="commit even though a check reported findings")
    ap.add_argument("--allow-default-branch", action="store_true",
                    help="commit directly to main/master")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every check and say what would happen; commit nothing")
    args = ap.parse_args(argv)

    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(os.path.join(repo, ".git")):
        top = git(repo, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            print("not a git repository: %s" % repo, file=sys.stderr)
            return 2
        repo = top.stdout.strip()

    files = staged_files(repo)
    if files is None:
        print("could not read the index of %s" % repo, file=sys.stderr)
        return 3
    if not files:
        print("nothing is staged in %s -- stage what you mean to commit first" % repo)
        return 1

    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    print("repo:   %s" % repo)
    print("branch: %s" % branch)
    print("staged: %d file(s)" % len(files))
    for f in files[:20]:
        print("    %s" % f)
    if len(files) > 20:
        print("    ... and %d more" % (len(files) - 20))
    print()

    refusals = []

    # 1. The checks. A member that COULD NOT RUN is fatal regardless of --allow-findings:
    #    "I could not check" is not a finding to be accepted, it is an unknown.
    allin_args = ["--repo", repo]
    if args.vault:
        allin_args += ["--vault", args.vault]
    proc, err = run_script("gt_allin.py", allin_args)
    if err:
        refusals.append("the checks could not run: %s" % err)
    else:
        print("--- checks ---")
        print(proc.stdout.rstrip() or "(no output)")
        print()
        if proc.returncode == 3:
            refusals.append("a check could not run; what it covers is unknown, so this is "
                            "not something --allow-findings can wave through")
        elif proc.returncode == 1 and not args.allow_findings:
            refusals.append("checks reported findings; re-run with --allow-findings if you "
                            "have decided they are acceptable")
        elif proc.returncode not in (0, 1, 3):
            refusals.append("the checks exited %d, which is not a result this understands"
                            % proc.returncode)

    # 2. The test receipt, checked HERE because a PreToolUse hook cannot see this process.
    proc, err = run_script("gt_test_receipt.py",
                           ["check", "--repo", repo, "--files"] + files)
    if err:
        refusals.append("test receipts unavailable: %s" % err)
    elif proc.returncode != 0:
        refusals.append("no passing test receipt covers every staged file. Run the tests, then "
                        "commit -- editing a file after a run invalidates the receipt, which "
                        "is the point. (%s)" % (proc.stdout.strip() or "gt_test_receipt check"))

    # 3. The default branch.
    if branch in DEFAULT_BRANCHES and not args.allow_default_branch:
        refusals.append("this is %s; branch first, or pass --allow-default-branch if you "
                        "really mean to commit here" % branch)

    if refusals:
        print("REFUSING TO COMMIT")
        for r in refusals:
            print("  - %s" % r)
        return 1

    if args.dry_run:
        print("DRY RUN: every check passed; this would have committed %d file(s)." % len(files))
        return 0

    out = git(repo, "commit", "-m", args.message)
    if out.returncode != 0:
        print(out.stdout + out.stderr, file=sys.stderr)
        return 3
    print(out.stdout.strip())
    print("\nCommitted. NOT pushed -- a commit is reversible here, a push is not.")
    print("  git -C %s push" % repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
