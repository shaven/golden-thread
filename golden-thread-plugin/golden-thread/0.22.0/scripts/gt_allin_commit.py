#!/usr/bin/env python3
"""gt_allin_commit.py -- commit, but only once the evidence exists. Never pushes.

  gt_allin_commit.py --repo PATH -m "message" [--vault V] [--allow-findings]
                     [--allow-default-branch] [--dry-run] [--timeout S]

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

WHY THE SCOPE CONTRACT IS REPORTED HERE AND --allow-findings DOES NOT ACCEPT IT (0.21.4)

When the repository has an approved plan for this branch with a `scope:` block, the commit step
runs `gt_scope.py check --staged` and prints it: every staged file or new dependency outside the
contract, with the plan's written reason when there is one. The scope_gate setting decides what
an UNJUSTIFIED deviation does: warn (default) prints it and the commit goes ahead, block refuses.
A refusal is not a finding to accept. --allow-findings stands for "I looked at what the checks
found and it is fine", and the point of the contract is that the reason is WRITTEN, in the
plan's `## Deviations`, where the next reader finds it; one flag that waived the question would
leave the feature with nothing to enforce. The fix is a line, which is cheap.

WHY THE LOCAL REVIEW IS ADVISORY AND OPT-IN (0.21.4)

With `commit_local_review` on (or unset under `farm_first` auto, with a calibrated local engine), the staged
diff goes to a local engine for a second reader's opinion, printed under "local review". It never refuses a
commit and `--allow-findings` has nothing to do with it: in the trial that motivated it (research 2026-10-15)
3 of 10 claims were real defects no test had caught and 7 needed Claude to dismiss them, so its output is
UNVERIFIED suggestions, not a check. The diff is scanned for credentials and prompt injection first and any
hit skips the review; the engine must be `local: yes` and calibrated (gt_farm_review.py).

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
TIMEOUT_S = 300          # per check; --timeout or the allin_timeout setting (0.19.1)
N_CHECKS = 12            # the checks run one after another: the whole run gets N x that
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


def setting_timeout():
    """The allin_timeout setting (seconds per check), or TIMEOUT_S."""
    try:
        import json
        with open(os.path.expanduser("~/.claude/vault-config.json"), encoding="utf-8") as fh:
            return int(json.load(fh).get("allin_timeout") or TIMEOUT_S)
    except Exception:
        return TIMEOUT_S


def run_script(name, args, timeout=TIMEOUT_S):
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        return None, "%s is not installed" % name
    try:
        proc = subprocess.run([sys.executable, path] + args, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "%s did not finish within %ds" % (name, timeout)
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


def scope_gate_setting():
    """scope_gate (off | warn | block), the default when it cannot be read."""
    try:
        import gt_settings
        value = gt_settings.get("scope_gate")
    except Exception:                            # noqa: BLE001 - an unreadable setting is the default
        value = None
    return value if value in ("off", "warn", "block") else "warn"


def scope_step(repo, dry_run, timeout):
    """The scope contract (0.21.4). -> (text to print or None, refusal or None).

    Not applicable (no approved plan for this branch, a draft, another branch) exits 0 and never
    refuses. An unjustified deviation refuses only under scope_gate block, and no flag waives it.
    A check that could not run refuses only under block: an unknown is not a pass, but warn is
    the setting that says "tell me, do not stop me"."""
    gate = scope_gate_setting()
    if gate == "off":
        return None, None
    proc, err = run_script("gt_scope.py", ["check", "--repo", repo, "--staged", "--gate", gate]
                           + (["--dry-run"] if dry_run else []), timeout=timeout)
    if err:
        text = "scope: could not run: %s" % err
        return text, ("the scope check could not run (%s); scope_gate is block" % err
                      if gate == "block" else None)
    text = (proc.stdout or "").rstrip()
    if proc.returncode == 0:
        return text, None
    if proc.returncode == 1:
        return text, ("the change is outside the plan's scope contract (listed above under "
                      "'scope'). Add a line to the plan's ## Deviations for each unjustified item "
                      "-- what it is, why it was needed, the simpler alternative you rejected -- "
                      "or take the change out. --allow-findings does not accept this")
    text = (text + "\n" if text else "") + "scope: could not complete: %s" % (
        (proc.stderr or "").strip() or "exit %d" % proc.returncode)
    return text, ("the scope check could not complete (exit %d); scope_gate is block"
                  % proc.returncode if gate == "block" else None)


def local_review_step(repo, vault):
    """The opt-in second opinion from a local engine (0.21.4). -> text to print, or None.

    ADVISORY ONLY: this never returns a refusal, a failure or an exit code, whatever happens. It runs when
    the setting commit_local_review is on, or unset under farm_first auto; gt-farm's gt_farm_review.py then
    checks that a calibrated local engine exists, scans the staged diff for credentials and injection BEFORE
    sending it (any hit skips the review) and prints the findings as unverified suggestions. gt-farm not
    installed is one line when the setting is explicitly on, and nothing otherwise."""
    try:
        import json
        with open(os.path.expanduser("~/.claude/vault-config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        first = str(cfg.get("farm_first") or "suggest").lower()
        setting = str(cfg["commit_local_review"]).lower() if cfg.get("commit_local_review") is not None else None
    except Exception:                            # noqa: BLE001 - an unreadable config is the defaults
        first, setting = "suggest", None
    if first == "off" or setting == "off" or (setting is None and first != "auto"):
        return None
    try:
        import gt_farm_link
        tool = gt_farm_link.farm_script("gt_farm_review.py")
    except Exception:                            # noqa: BLE001
        tool = None
    if not tool:
        return ("local review skipped: the gt-farm module is not installed (install.sh --with farm)"
                if setting == "on" else None)
    args = [sys.executable, tool, "review", "--repo", repo, "--if-enabled"] + (["--vault", vault] if vault else [])
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=1200)
    except (subprocess.SubprocessError, OSError) as exc:
        return "local review skipped: %s" % type(exc).__name__
    text = (proc.stdout or "").strip()
    return text or None


# 0.21.0 (open since 2026-10-02: "should vault findings block a code commit?"). These members
# judge the VAULT or this machine's install, never the staged code, so their findings are
# ADVISORY when the repository being committed is not the vault: printed, never a refusal. In
# the vault itself they block as before. A member that COULD NOT RUN still refuses: an unknown
# is never advisory. scan, secrets and validations judge the repo and always block.
VAULT_SCOPE = ("lint", "optimize", "runbooks", "wiki", "doctor")


def split_findings(allin_text, repo, vault):
    """-> (blocking members, advisory members) among those gt_allin reported findings for."""
    import re
    found = re.findall(r"^=== ([A-Za-z0-9_-]+): findings ===$", allin_text or "", re.M)
    same = False
    if vault:
        try:
            same = os.path.realpath(vault) == os.path.realpath(repo)
        except OSError:
            same = False
    if not found:
        return ["(unparsed)"], []          # findings we cannot attribute: refuse, as before
    blocking = [m for m in found if same or m not in VAULT_SCOPE]
    advisory = [m for m in found if m not in blocking]
    return blocking, advisory


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True, help="the repository to commit in")
    ap.add_argument("-m", "--message", required=True, help="the commit message")
    ap.add_argument("--vault", help="vault, so the vault-side checks can run too")
    ap.add_argument("--allow-findings", action="store_true",
                    help="commit even though a check reported findings (a scope "
                         "deviation is not one: add a ## Deviations line)")
    ap.add_argument("--allow-default-branch", action="store_true",
                    help="commit directly to main/master")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every check and say what would happen; commit nothing")
    ap.add_argument("--timeout", type=int,
                    help="seconds each check may take (default: the allin_timeout setting, "
                         "else %d)" % TIMEOUT_S)
    args = ap.parse_args(argv)
    per_check = args.timeout or setting_timeout()

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
        print("    %s" % os.path.relpath(f, repo).replace(os.sep, "/"))
    if len(files) > 20:
        print("    ... and %d more" % (len(files) - 20))
    print()

    refusals = []
    import time
    started = time.time()

    # 1. The checks. A member that COULD NOT RUN is fatal regardless of --allow-findings:
    #    "I could not check" is not a finding to be accepted, it is an unknown.
    # `tests` is skipped ON PURPOSE (0.19.1): step 2 checks the test receipt, which is the
    # evidence the suite passed. Re-running the suite here put it on the committing machine
    # (gt's: 3000 tests, the load that froze a Mac) and could never finish in the limit.
    allin_args = ["--repo", repo, "--skip", "tests", "--timeout", str(per_check)]
    if args.vault:
        allin_args += ["--vault", args.vault]
    proc, err = run_script("gt_allin.py", allin_args, timeout=per_check * N_CHECKS + 60)
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
            blocking, advisory = split_findings(proc.stdout, repo, args.vault)
            if blocking:
                refusals.append("checks reported findings (%s); re-run with --allow-findings if "
                                "you have decided they are acceptable" % ", ".join(blocking))
            if advisory:
                print("ADVISORY (not blocking): %s reported findings about the vault or this "
                      "install, not about the code being committed -- they do not stop a commit "
                      "to a code repository. Run /gt:gt-allin to see them; fix them in the "
                      "vault." % ", ".join(advisory))
                print()
        elif proc.returncode not in (0, 1, 3):
            refusals.append("the checks exited %d, which is not a result this understands"
                            % proc.returncode)

    # 1b. The project's RELEASE PIPELINE (0.18.1). When the repo has adopted one, the run above
    #     included its pre-commit steps (`release.sh --until owner-gate`, the `pipeline` member).
    #     A failed step refuses the commit, and --allow-findings cannot wave it through: a gate
    #     that did not pass is not a finding someone may accept.
    try:
        import gt_pipeline
        if os.path.isfile(os.path.join(repo, gt_pipeline.STEPS_FILE)) and not err:
            last = gt_pipeline.last_run(gt_pipeline.repo_root(repo))
            if not last or last.get("finished", 0) < started:
                refusals.append("the release pipeline's pre-commit steps did not run in this "
                                "check; run gt_pipeline.py run --repo %s --until owner-gate"
                                % repo)
            elif last.get("rc") != 0:
                refusals.append("release pipeline step %s FAILED; a pre-commit gate that did not "
                                "pass cannot be accepted with --allow-findings"
                                % (last.get("failed") or "?"))
    except ImportError:
        pass

    # 2. The test receipt, checked HERE because a PreToolUse hook cannot see this process.
    proc, err = run_script("gt_test_receipt.py",
                           ["check", "--repo", repo, "--files"] + files)
    if err:
        refusals.append("test receipts unavailable: %s" % err)
    elif proc.returncode != 0:
        refusals.append("no passing test receipt covers every staged file. Run the tests, then "
                        "commit -- editing a file after a run invalidates the receipt, which "
                        "is the point. (%s)" % (proc.stdout.strip() or "gt_test_receipt check"))

    # 2b. The scope contract (0.21.4): the plan's allowed files and new dependencies, against
    #     what is staged. Reported always; refuses only under scope_gate block, and --allow-findings
    #     is not consulted: the fix is a line in the plan's ## Deviations.
    scope_text, scope_refusal = scope_step(repo, args.dry_run, per_check)
    if scope_text:
        print("--- scope ---")
        print(scope_text)
        print()
    if scope_refusal:
        refusals.append(scope_refusal)

    # 2c. The local second opinion (0.21.4, opt-in). Advisory: printed, never a refusal.
    review_text = local_review_step(repo, args.vault)
    if review_text:
        print("--- local review ---")
        print(review_text)
        print()

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
