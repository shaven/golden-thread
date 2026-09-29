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

EXIT CODES, as the code actually spends them. Every REFUSAL is exit 1 and prints its reasons
under `REFUSING TO COMMIT` -- including "a check could not run", which is a refusal like any
other here rather than a separate code. What --allow-findings cannot wave through is a property
of the refusal text, not of the exit code. 3 is kept for the cases where this command could not
get far enough to refuse or commit: the index could not be read, or `git commit` itself failed.
Claiming "3 = a check could not run" was wrong in both halves (found 2026-09-18).

Exit: 0 committed (or --dry-run with every check passed) | 1 refused, and why -- a failed check,
a missing receipt, a protected branch, or a check that could not run | 2 usage | 3 this command
could not complete: the index was unreadable, or `git commit` failed
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Compared casefolded, and only ever as a fallback: the repo's real default comes from
# origin/HEAD when there is one. `Main`, `MASTER` and `MaIn` all committed unguarded against
# the old exact-lowercase pair (validation 2026-09-16).
DEFAULT_BRANCHES = ("main", "master", "trunk", "develop", "production")
TIMEOUT_S = 300
# A commit that is halfway through something must not be concluded by a tool nobody told.
IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, **kw)


def staged_files(repo):
    """-> ABSOLUTE paths of what is staged, or None.

    Absolute, and via -z, for two reasons validation found on 2026-09-16: the relative names
    were handed to gt_test_receipt, which stat'd them against ITS cwd and so found nothing
    whenever this was run from anywhere but the repo root -- every file then counted as covered
    and a stale receipt let the commit through. And git quotes non-ASCII names by default, which
    does not stat either."""
    out = git(repo, "diff", "--cached", "--name-only", "-z")
    if out.returncode != 0:
        return None
    return [os.path.join(repo, n) for n in out.stdout.split("\0") if n.strip()]


def run_script(name, args):
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        return None, "%s is not installed" % name
    try:
        proc = subprocess.run([sys.executable, path] + args, capture_output=True, text=True,
                              timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, "%s did not finish within %ds" % (name, TIMEOUT_S)
    except OSError as exc:
        return None, "%s could not be run: %s" % (name, exc)
    return proc, None


def current_branch(repo):
    """-> (branch or None, why). None means unborn or detached -- both of which reported as
    the literal string "HEAD" from `rev-parse --abbrev-ref`, so a first commit on an unborn
    `main` sailed past the default-branch guard (validation 2026-09-16)."""
    out = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip(), None
    if git(repo, "rev-parse", "--verify", "HEAD").returncode != 0:
        return None, "this repository has no commits yet, so the branch it would create is "\
                     "unverifiable here"
    return None, "HEAD is detached; a commit here is reachable only through the reflog"


def default_branch(repo):
    """The repo's OWN default, from origin/HEAD, rather than a guess from a hard-coded list."""
    out = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip().split("/", 1)[-1]
    return None


def in_progress(repo):
    gitdir = git(repo, "rev-parse", "--git-dir").stdout.strip()
    if not gitdir:
        return None
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(repo, gitdir)
    for marker in IN_PROGRESS:
        if os.path.exists(os.path.join(gitdir, marker)):
            return marker
    return None


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

    branch, branch_why = current_branch(repo)
    print("repo:   %s" % repo)
    print("branch: %s" % (branch or "(none: %s)" % branch_why))
    print("staged: %d file(s)" % len(files))
    for f in files[:20]:
        print("    %s" % os.path.relpath(f, repo))
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
        # stderr is PRINTED, not discarded: a crashing checker exits 1 exactly like one
        # reporting findings, and the traceback was the only way to tell them apart.
        if (proc.stderr or "").strip():
            print("--- checks (stderr) ---")
            print(proc.stderr.rstrip())
            print()
        if proc.returncode == 1 and "Traceback (most recent call last)" in (proc.stderr or "") \
                and not (proc.stdout or "").strip():
            refusals.append("the checks CRASHED rather than reporting; that is an unknown, not "
                            "a finding, and --allow-findings cannot accept it")
        elif proc.returncode == 3:
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

    # 3. The default branch -- casefolded, against the repo's own default where it has one.
    repo_default = default_branch(repo)
    protected = {b.casefold() for b in DEFAULT_BRANCHES}
    if repo_default:
        protected.add(repo_default.casefold())
    if branch is None and not args.allow_default_branch:
        refusals.append("%s; pass --allow-default-branch if you mean to commit anyway"
                        % branch_why)
    elif branch and branch.casefold() in protected and not args.allow_default_branch:
        refusals.append("this is %s; branch first, or pass --allow-default-branch if you "
                        "really mean to commit here" % branch)

    # 4. Something already in progress. Concluding a merge with a message written for an
    #    ordinary commit produced a two-parent commit and cleared MERGE_HEAD, silently.
    marker = in_progress(repo)
    if marker:
        refusals.append("a %s is in progress; finish it with git yourself rather than letting "
                        "this conclude it with a message written for an ordinary commit"
                        % marker.replace("_HEAD", "").replace("rebase-", "rebase ").lower())

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
    if (out.stderr or "").strip():
        # Hook output arrives here. It used to be swallowed, so a post-commit hook that
        # pushed left no trace at all beneath a line claiming nothing was pushed.
        print(out.stderr.rstrip())
    print("\nCommitted. THIS TOOL did not push -- it has no push flag and never runs one.")
    ahead = git(repo, "rev-list", "--count", "@{u}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() == "0":
        # Not a guarantee of anything this tool did: `git commit` runs the repo's own hooks,
        # and a post-commit hook can push. Saying "NOT pushed" flatly was a claim about the
        # repository that this tool is not in a position to make (validation 2026-09-16).
        print("NOTE: HEAD is not ahead of its upstream. A commit hook in this repository may "
              "have pushed it -- check `git log origin/HEAD` if that matters.")
    else:
        print("  git -C %s push" % repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
