#!/usr/bin/env python3
"""Check a `git commit` the MODEL types against the approved plan's scope contract (0.21.4).

    guard_scope_contract.py          # the PreToolUse hook (payload on stdin)

## Why this is its own guard

`/gt:gt-allin-commit` and `/gt:gt-implement` run `gt_scope.py check --staged`, and a `git commit`
typed in a Bash call went around both: the first release of the scope contract said so in its
CHANGELOG ("Known, and not fixed"). This closes it. It is a separate guard from
`guard_test_before_commit` on purpose, and the reasons are the design:

  * That guard has six early exits that are right for the TEST gate and wrong here -- test_gate
    off, a docs-only commit, a repo with no test entry point, a passing receipt. Threading a second
    verdict through them means either skipping the scope check on every one, or touching a
    Core-rule mechanism to add a feature.
  * Its escapes (`.gt-no-test-gate`, `GT_TEST_GATE=off`, `test_gate off`) say "this repo has no
    tests". Its own refusal teaches the model to type the second one, so a scope refusal that
    honoured it would be waived by every session that had ever skipped a test run -- the bypass
    `--allow-findings` was refused for. The two gates are independent, as `commit_checks` and the
    foreign-checkout guard already are.
  * Its denials are recorded as uses of `core_test_before_commit`. A scope refusal is not a use of
    that rule; it is registered by `install.sh` (no vault needed), not by `install-core-rules`.
  * Hooks on one event run in parallel, and this one has `matcher: Bash`, so Read/Edit/Grep calls
    start no process for it at all.

The command parser is NOT copied: `guard_foreign_checkout` already splits a command line into
simple commands with the quoting, `cd`/`-C` target tracking and "I cannot be sure" cases proven
against real commit messages, and this imports it.

## What it checks

A Bash command with a `git commit` in it, whose target repository (the cwd, `git -C`, `cd`) has an
approved plan bound to the current branch with a `scope:` block. Then, with the `scope_gate`
setting (the same one `gt_allin_commit.py` reads, through the same `gt_scope.run_check`):

    off    nothing, no output
    warn   the deviation report is added as `additionalContext`; the commit is not touched
    block  an UNJUSTIFIED deviation is refused, naming each file or dependency and the fix (a line
           in the plan's `## Deviations`, or take the change out). Justified ones are only shown.

Not applicable -- no plan, a draft, a plan for another branch, a detached HEAD, a plan from before
0.21.4, an unreadable gate -- never blocks and adds nothing.

## What the commit WILL hold

At hook time the command has not run. `git add a.py && git commit -m x` is one Bash call whose index
is still empty, and `git commit -am x` stages nothing up front, so reading the index would judge
nothing at all -- on the commonest shape. The guard therefore copies the index to a throwaway file,
replays the `git add ...` segments that come before the commit (and `-a` as `git add -u`) against
the COPY with GIT_INDEX_FILE, and runs the check on that. The real index, the work tree and the plan
are never written; `git add` writes the same blobs the real one is about to, and nothing else.

What it will not guess at, it does not judge (no output): a glob or brace in an `git add` operand
(the shell and git match them differently), `-p`/`-i`, an unknown flag; a commit that names paths,
`-o`/`-i`/`-p`; a git verb that changes the index or HEAD earlier in the line (`restore --staged`,
`reset`, `rm`, `stash`, `checkout`, `mv`... -- which is how a model repairs a refusal, so judging the
index before it would refuse the repair); a merge, cherry-pick, revert or rebase in progress; a
command it cannot analyse (`$VAR`, `$(...)`, `eval`, `sh -c`). Files created by an earlier command in
the same line are not there to be seen. All of these err towards a commit going unchecked, never
towards a refusal that is not earned.

## Opt-outs, deliberately few

`scope_gate off` (a security setting: with gt unlock on, changing it needs the fresh confirmation)
and the plan's own `## Deviations`. There is NO environment switch and no per-repo file: a word the
session can type in front of the command is a word that switches off "the contract it is held to".
The test gate's escapes do not apply here (see above).

## FAIL OPEN, always -- and SAY SO

Unparseable payload, unparseable shell, no git, not a repo, a git that hangs past the budget, any
exception: no objection (exit 0, the normal permission flow decides). The one thing it adds there is
a note, and only once it is known that the repository HAS a plan: "DEGRADED, did not check this
commit". A guard that stays quiet when it is inert is how `guard_vault_writes.sh` shipped inert for
three releases.

## Latency

Added only to a command with `commit` in it. A commit in a repo with no plan costs one `git
rev-parse` and two `stat`s. A commit that is checked costs about six git calls, held to BUDGET
seconds in total (`gt_scope.DEADLINE`) inside the hook's own 10 s timeout.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

# Seconds of git work this guard allows itself, in total. The registration's `timeout` is 10.
BUDGET = 5.0

# Same list as gt_allin_commit.IN_PROGRESS (a test pins that they agree): a commit that concludes
# a merge or a rebase is not the author's own scope.
IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")

# git verbs that cannot change what a later `git commit` in the same command line holds. Anything
# else before a commit (`reset`, `restore`, `rm`, `mv`, `stash`, `checkout`, `merge`, ...) makes the
# index at hook time the wrong thing to judge, so that commit is not judged.
READ_ONLY_VERBS = frozenset((
    "status", "diff", "log", "show", "rev-parse", "rev-list", "ls-files", "ls-tree", "cat-file",
    "blame", "grep", "describe", "shortlog", "branch", "tag", "remote", "fetch", "push", "config",
    "symbolic-ref", "show-ref", "for-each-ref", "diff-index", "diff-tree", "merge-base", "version",
    "help", "reflog", "check-ignore", "name-rev"))

ADD_FLAGS = frozenset(("-A", "--all", "--no-all", "-u", "--update", "-f", "--force", "-N",
                       "--intent-to-add", "-v", "--verbose", "--ignore-errors",
                       "--ignore-removal", "--no-ignore-removal", "--sparse", "--renormalize",
                       "--refresh"))
# commit flags that take their value as the NEXT token (written `--flag=value` they do not)
COMMIT_VALUE_LONG = frozenset(("--message", "--file", "--reuse-message", "--reedit-message",
                               "--template", "--author", "--date", "--cleanup", "--fixup",
                               "--squash", "--trailer"))
COMMIT_VALUE_SHORT = "mFCct"
# commit flags whose effect depends on paths or a terminal: not guessed at
COMMIT_UNGUESSED_LONG = frozenset(("--patch", "--include", "--only", "--interactive"))
GLOB_CHARS = "*?[{}~"


class Skip(Exception):
    """This commit is not judged. No output."""


class Degraded(Exception):
    """The check could not finish; the commit goes ahead and the model is told."""


def no_objection():
    sys.exit(0)


def emit(payload):
    print(json.dumps(payload))
    sys.exit(0)


# ----------------------------------------------------------------------------- git ----

def _run_git(cwd, args, deadline, env=None):
    """-> (returncode, stdout text). Raises Degraded when the budget is spent or git hangs."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise Degraded("git work ran past its %.0f s budget" % BUDGET)
    # Popen, not subprocess.run: on Windows run() waits for the pipes after killing git, and a
    # process git started still holds them (gt_scope._run_git has the same fix, 2026-10-16).
    try:
        p = subprocess.Popen(["git", "--no-optional-locks", "-C", cwd] + list(args),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=dict(os.environ, **env) if env else None)
    except OSError as exc:
        raise Skip(str(exc))                           # no git on this machine: nothing to guard
    try:
        out, _err = p.communicate(timeout=left)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        raise Degraded("git %s did not finish within the %.0f s budget" % (args[0], BUDGET))
    return p.returncode, out.decode("utf-8", "replace")


def _kill_tree(p):
    """Kill git and everything it started; never wait on its pipes."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        p.kill()
    except (OSError, subprocess.SubprocessError):
        pass
    for fh in (p.stdout, p.stderr):
        try:
            fh.close()
        except (OSError, AttributeError):
            pass
    try:
        p.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def locate(directory, deadline, cache):
    """-> (toplevel, absolute git dir, index path) of the repository at `directory`, or None."""
    if directory in cache:
        return cache[directory]
    rc, out = _run_git(directory, ["rev-parse", "--show-toplevel", "--absolute-git-dir",
                                   "--git-path", "index"], deadline)
    lines = out.splitlines()
    found = None
    if rc == 0 and len(lines) >= 3:
        index = lines[2]
        found = (lines[0], lines[1], index if os.path.isabs(index)
                 else os.path.join(directory, index))
    cache[directory] = found
    return found


def plan_exists(root, gitdir):
    return (os.path.isfile(os.path.join(gitdir, "gt", "plan-current.md"))
            or os.path.isfile(os.path.join(root, ".claude", "gt-plan-current.md")))


# ------------------------------------------------------------------ the command line ----

def parse_add(argv):
    """-> (flags, pathspecs), or None when this add changes nothing (--dry-run).
    Raises Skip for anything whose effect cannot be reproduced exactly."""
    flags, paths, i, dry = [], [], 0, False
    while i < len(argv):
        t = argv[i]
        if t == "--":
            paths.extend(argv[i + 1:])
            break
        if t in ("-n", "--dry-run"):
            dry = True
        elif t in ADD_FLAGS or t.startswith("--chmod="):
            flags.append(t)
        elif re.match(r"^-[A-Za-z]{2,}$", t) and all("-" + c in ADD_FLAGS for c in t[1:]):
            flags.extend("-" + c for c in t[1:])
        elif t.startswith("-"):
            raise Skip("git add %s" % t)
        else:
            paths.append(t)
        i += 1
    for p in paths:
        if any(c in p for c in GLOB_CHARS) or "$" in p or "`" in p:
            raise Skip("a pathspec the shell may expand differently from git: %s" % p)
    return None if dry else (flags, paths)


def parse_commit(argv):
    """-> {"all": bool}, or None for --dry-run (no commit happens). Raises Skip for a shape whose
    contents depend on paths, a terminal, or something this will not guess at."""
    every, i = False, 0
    while i < len(argv):
        t = argv[i]
        if t == "--":
            if argv[i + 1:]:
                raise Skip("git commit -- <paths>")
            break
        if t.startswith("--"):
            name = t.split("=", 1)[0]
            if name == "--all":
                every = True
            elif name == "--dry-run":
                return None
            elif name in COMMIT_UNGUESSED_LONG or name.startswith("--pathspec"):
                raise Skip("git commit %s" % name)
            elif name in COMMIT_VALUE_LONG and "=" not in t:
                i += 1
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            for k, c in enumerate(t[1:], 1):
                if c == "a":
                    every = True
                elif c in "pio":
                    raise Skip("git commit -%s" % c)
                elif c in COMMIT_VALUE_SHORT:
                    if k == len(t) - 1:
                        i += 1                         # the value is the next token
                    break                              # otherwise it is attached to the cluster
                elif c in "uS":
                    break                              # an optional attached value
            i += 1
            continue
        raise Skip("git commit with a path operand")
    return {"all": every}


def strip_redirects(argv):
    """Drop `> file`, `2>&1`, `< in` from a command's words: they are not its operands. The lexer
    keeps a redirection operator as a word of its own, with the file descriptor before it."""
    out, i = [], 0
    while i < len(argv):
        t = argv[i]
        if t and set(t) <= set("<>&"):
            if out and out[-1].isdigit():
                out.pop()
            i += 2
            continue
        out.append(t)
        i += 1
    return out


def walk(command, cwd, fc):
    """-> [(kind, directory, argv)] in order, kind add | commit | other, for every git command.

    Raises fc.Uncertain when the command line cannot be analysed with certainty (a computed
    command, directory or `-C`, `eval`, `sh -c`, unbalanced quotes, an unquoted substitution)."""
    events, here = [], cwd
    for toks in fc._segments(fc.strip_heredocs(command)):
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks = toks[1:]                            # leading VAR=value
        if not toks:
            continue
        cmd = toks[0]
        if fc._dynamic(cmd) or cmd in ("eval", "exec", "source", "."):
            raise fc.Uncertain(cmd)
        if os.path.basename(cmd) in ("sh", "bash", "zsh", "dash", "ksh") and "-c" in toks[1:]:
            raise fc.Uncertain("nested shell")
        if cmd in ("cd", "pushd"):
            if len(toks) < 2 or toks[1] == "-" or fc._dynamic(toks[1]):
                raise fc.Uncertain("cd with no fixed target")
            here = fc._resolve(here, toks[1])
            continue
        if os.path.basename(cmd) != "git":
            continue
        i, target = 1, here
        while i < len(toks) and toks[i].startswith("-"):
            t = toks[i]
            if t in ("--help", "-h", "--version"):
                target = None
                break
            if t == "-C":
                if i + 1 >= len(toks) or fc._dynamic(toks[i + 1]):
                    raise fc.Uncertain("-C with no fixed path")
                target = fc._resolve(target, toks[i + 1])
                i += 2
                continue
            if t == "-c":
                i += 2
                continue
            if t.startswith(("--git-dir", "--work-tree", "--namespace")):
                raise fc.Uncertain(t)
            i += 1
        if target is None or i >= len(toks):
            continue
        verb = toks[i]
        if fc._dynamic(verb):
            raise fc.Uncertain(verb)
        argv = strip_redirects(toks[i + 1:])
        if "--help" in argv or "-h" in argv:
            continue                                   # reading the manual
        if verb in ("add", "commit"):
            events.append((verb, target, argv))
        elif verb not in READ_ONLY_VERBS:
            events.append(("other", target, [verb]))
    return events


class Job(object):
    """One commit to judge: where, and what the line stages before it."""

    def __init__(self, root, gitdir, index, adds, every):
        self.root, self.gitdir, self.index, self.adds, self.every = root, gitdir, index, adds, every


def jobs_for(command, cwd, deadline):
    """-> [Job] for each repository a `git commit` in the command line would commit to and that
    has a plan file. Raises Skip / fc.Uncertain when nothing can be judged."""
    import guard_foreign_checkout as fc
    try:
        events = walk(command, cwd, fc)
    except fc.Uncertain:
        raise Skip("uncertain command line")
    cache, jobs, seen, adds, tainted = {}, [], set(), {}, False
    for kind, directory, argv in events:
        if kind == "other":
            tainted = True                            # the index is not what it is now
            continue
        where = locate(directory, deadline, cache)
        if where is None:
            continue
        root, gitdir, index = where
        if kind == "add":
            try:
                parsed = parse_add(argv)
            except Skip:
                tainted = True
                continue
            if parsed is not None:
                adds.setdefault(root, []).append((directory, parsed))
            continue
        # a commit
        if tainted or root in seen:
            continue
        seen.add(root)
        try:
            parsed = parse_commit(argv)
        except Skip:
            continue
        if parsed is None:
            continue
        if not plan_exists(root, gitdir):
            continue
        if any(os.path.exists(os.path.join(gitdir, m)) for m in IN_PROGRESS):
            continue
        jobs.append(Job(root, gitdir, index, list(adds.get(root, [])), parsed["all"]))
    return jobs


# --------------------------------------------------------------------- the simulation ----

class Staged(object):
    """The index this commit will hold, as environment for git: the real one, or -- when the
    line stages something first -- a throwaway copy with that staging replayed on it."""

    def __init__(self, job, deadline):
        self.job, self.deadline, self.dir, self.env = job, deadline, None, {}

    def __enter__(self):
        job = self.job
        if not job.adds and not job.every:
            return self
        self.dir = tempfile.mkdtemp(prefix="gt-scope-")
        try:                                           # __exit__ does not run if this raises
            copy = os.path.join(self.dir, "index")
            if os.path.isfile(job.index):
                # copy2, not copyfile: git trusts a file whose size and (whole-second) times match
                # its entry unless the entry is no older than the INDEX FILE -- the racy-clean rule.
                # A copy stamped "now" is newer than every entry, loses that rule, and then misses a
                # same-size edit made within a second of the last `git add` (2 in 25 in a loop).
                shutil.copy2(job.index, copy)
            self.env = {"GIT_INDEX_FILE": copy}
            for directory, (flags, paths) in job.adds:
                args = ["add"] + flags + (["--"] + paths if paths else [])
                rc, _out = _run_git(directory, args, self.deadline, self.env)
                if rc != 0:
                    raise Skip("git add would fail, so the commit after it does not happen")
            if job.every:
                rc, _out = _run_git(job.root, ["add", "-u"], self.deadline, self.env)
                if rc != 0:
                    raise Skip("git add -u failed")
        except BaseException:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None
            raise
        return self

    def __exit__(self, *_exc):
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)
        return False


# ------------------------------------------------------------------------- the verdict ----

def _shown(path, root):
    try:
        if path and path.startswith(root.rstrip(os.sep) + os.sep):
            return os.path.relpath(path, root).replace(os.sep, "/")
    except Exception:                                  # noqa: BLE001
        pass
    return path


def refusal_text(res, report, root):
    plan = _shown(res.get("plan"), root) or ".git/gt/plan-current.md"
    # the report ends with its own one-paragraph fix; the numbered list below replaces it
    head = report.partition("\nscope_gate is ")[0]
    unjustified = [d for d in res["deviations"] if not d["justified"]]
    contract = any(d["kind"] == "contract" for d in unjustified)
    files = [d["subject"] for d in unjustified if d["kind"] == "file"]
    example = files[0] if files else (unjustified[0]["subject"] if unjustified else "path")
    if contract:
        first = ("  1. The scope block in the plan is not valid: correct it in %s, then have the user\n"
                 "     approve it again (`gt_scope.py approve --repo .`). It is not a deviation, so a\n"
                 "     `## Deviations` line does not fix it.\n" % plan)
    else:
        first = ("  1. Write the reason in the plan, then run the commit again. One line per UNJUSTIFIED\n"
                 "     item under `## Deviations` in %s:\n"
                 "         - `%s` -- why it was needed -- the simpler alternative rejected, and why\n"
                 "     Show the user the line before adding it: the written reason is what makes the\n"
                 "     extra change their call, in the open.\n" % (plan, example))
    return (
        "BLOCKED by the scope contract (scope_gate block): this commit holds a change the approved\n"
        "plan does not cover and gives no reason for.\n\n"
        "%s\n\n"
        "Do this instead:\n"
        "%s"
        "  2. Or take the change out of this commit:  git restore --staged <file>  (then revert it).\n"
        "  3. scope_gate is the user's setting, not the session's (gt_settings.py set scope_gate\n"
        "     warn|off, which needs their confirmation when gt unlock is on). Neither GT_TEST_GATE=off\n"
        "     nor .gt-no-test-gate applies: they are the test gate's, and this is a different gate.\n\n"
        "Why: \"don't over-engineer\" is a rule no hook can check, so the plan carries a contract and\n"
        "this checks it. A raw `git commit` used to go around /gt:gt-allin-commit, which already\n"
        "refuses this." % (head, first))


def context_text(res, report):
    if res["unjustified"]:
        lead = "scope contract (scope_gate %s): this commit holds changes outside the approved plan.\n\n" % res["gate"]
    else:
        lead = "scope contract: every change outside the approved plan has a written reason.\n\n"
    return lead + report


def degraded_text(why):
    return ("scope contract check is DEGRADED and did not check this commit: %s. This repository has "
            "an approved plan with a scope contract; run `gt_scope.py check --staged` by hand to see "
            "where the commit stands. The commit was not blocked (this guard fails open)." % why)


def judge(job, deadline):
    """-> ("deny" | "context", text) or None."""
    import gt_scope
    gate = gt_scope.gate_setting()
    if gate == "off":
        return None
    gt_scope.DEADLINE = deadline
    saved = os.environ.get("GIT_INDEX_FILE")
    try:
        with Staged(job, deadline) as staged:
            if staged.env:
                os.environ.update(staged.env)           # gt_scope's own git calls inherit it
            try:
                res = gt_scope.run_check(job.root, "staged", None, None, gate, False)
            except gt_scope.ScopeTimeout as exc:
                raise Degraded(str(exc))
    finally:
        if saved is None:
            os.environ.pop("GIT_INDEX_FILE", None)
        else:
            os.environ["GIT_INDEX_FILE"] = saved
    if res.get("failed"):
        raise Degraded(res.get("reason") or "git failed")
    if not res["applicable"] or not res["deviations"]:
        return None
    report = gt_scope.report_text(res)
    if res["refused"]:
        return "deny", refusal_text(res, report, job.root)
    return "context", context_text(res, report)



# 0.22.0: a repeat counter on this guard's refusals (gt_refusals.py, setting refusal_limit). Fails open:
# without the module beside the guard, or on any error, the deny text is exactly what it was.
sys.dont_write_bytecode = True          # the hooks folder is signed: importing a sibling must not leave a __pycache__ in it
try:
    import gt_refusals as _gtr
except Exception:                                            # noqa: BLE001
    _gtr = None


def _annotated(reason):
    try:
        return _gtr.annotate('guard_scope_contract', reason) if _gtr else reason
    except Exception:                                        # noqa: BLE001
        return reason


def _use_session(payload):
    try:
        if _gtr:
            _gtr.use_session(payload)
    except Exception:                                        # noqa: BLE001
        pass

def decide(raw):
    """-> the hook's output object, or None for no objection and nothing to add."""
    try:
        payload = json.loads(raw)
    except Exception:                                   # noqa: BLE001
        return None
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    _use_session(payload)
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "commit" not in command or "git" not in command:
        return None
    cwd = payload.get("cwd") or os.getcwd()
    deadline = time.monotonic() + BUDGET
    notes, denials = [], []
    try:
        jobs = jobs_for(command, cwd, deadline)
    except Skip:
        return None
    except Degraded:
        return None                                     # no plan known yet: nothing to degrade
    for job in jobs:
        try:
            verdict = judge(job, deadline)
        except Skip:
            continue
        except Degraded as exc:
            notes.append(degraded_text(str(exc)))
            continue
        except Exception as exc:                        # noqa: BLE001
            notes.append(degraded_text("%s: %s" % (type(exc).__name__, exc)))
            continue
        if verdict:
            (denials if verdict[0] == "deny" else notes).append(verdict[1])
    if denials:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": _annotated("\n\n".join(denials + notes))}}
    if notes:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "additionalContext": "\n\n".join(notes)}}
    return None


def main():
    try:
        out = decide(sys.stdin.read())
    except Exception:                                   # noqa: BLE001 - fail open, always
        out = None
    if out:
        emit(out)
    no_objection()


if __name__ == "__main__":
    main()
