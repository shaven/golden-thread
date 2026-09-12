#!/usr/bin/env python3
"""Deny a `git commit` of CODE whose tests nobody has seen pass.

## Why this is a guard and not an intention

Every other step of this project's release discipline is mechanical -- the gate, the
manifest, the wiring check -- and the one step that decides whether any of them ran was
a habit. On 2026-09-12 a release was committed and pushed, and the manifest turned out
to be stale; the gate would have caught it, and the gate had been run before the last
few edits rather than after. Nothing was wrong with the discipline. The discipline had
no mechanism.

## What it denies

A Bash command containing `git commit` where:

  * the repo has code staged (not only docs), AND
  * the repo has a discoverable way to run tests, AND
  * there is no PASSING receipt newer than every staged file
    (see gt_test_receipt.py -- a run writes one, editing a file invalidates it).

## The three escapes, in order of bluntness

  1. `.gt-no-test-gate` in the repo root -- this repo is exempt, permanently and
     visibly. A vault of notes and a scratch repo have no tests to run and should not
     be arguing with a gate about it. Per-repo, because the exemption is a property of
     the repo, not of whoever is committing.
  2. `GT_TEST_GATE=off` in front of the command -- this one commit, said out loud.
  3. `gt_settings.py set test_gate off` -- this machine, until changed.

## FAIL OPEN, always

Unparseable payload, unparseable shell, no git, not a repo, any exception: allow. This
sits in front of every Bash call in every session. A gate that blocks work it merely
does not understand is a gate someone switches off, and then it guards nothing --
which is how `guard_vault_writes.sh` shipped inert for three releases while every
check reported clean.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

ALLOW = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                "permissionDecision": "allow"}}

# Files whose change cannot break a test. A commit touching only these is waved
# through: demanding a test run for a typo fix in a README is how a gate earns the
# reputation that gets it disabled.
DOC_EXT = {".md", ".markdown", ".rst", ".txt", ".html", ".htm", ".pdf", ".png", ".jpg",
           ".jpeg", ".gif", ".svg", ".ico", ".webp", ".csv", ".json5"}
DOC_NAMES = {"LICENSE", "NOTICE", "CHANGELOG", "AUTHORS", "CODEOWNERS", ".gitignore"}

# How this repo runs its tests. Discovery, not configuration: a repo with no visible
# test entry point is not asked to have one.
TEST_ENTRY_POINTS = (
    ("tests/run.sh", "tests/run.sh"),
    ("dev/release-check.sh", "dev/release-check.sh"),
    ("pytest.ini", "pytest"),
    ("tox.ini", "pytest"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
)
OPT_OUT = ".gt-no-test-gate"


def out(payload):
    print(json.dumps(payload))
    sys.exit(0)


def allow(context=None):
    if context:
        p = dict(ALLOW)
        p["hookSpecificOutput"] = dict(ALLOW["hookSpecificOutput"])
        p["hookSpecificOutput"]["additionalContext"] = context
        out(p)
    out(ALLOW)


def deny(reason):
    out({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": reason}})


def git(root, *args, timeout=15):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                           timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def strip_heredocs(command):
    """Drop heredoc BODIES -- data being written, not commands being run.

    The implementation is guard_vault_writes.strip_heredocs, imported rather than
    copied: it is line-based and already proven against the 2026-09-12 case where a
    guard fired on a command that merely QUOTED a tool invocation. A second copy here
    would be a second copy to drift. If the import fails the raw command is inspected,
    which can only produce a false deny on a heredoc that quotes `git commit` -- and
    the test suite pins that case.
    """
    try:
        from guard_vault_writes import strip_heredocs as impl
        return impl(command)
    except Exception:
        return command


def is_commit(command):
    """A real `git commit`, not the words 'git commit' inside quoted text.

    Heredoc bodies and message strings are data. `--amend` counts: amending a commit
    with untested code produces exactly the same untested commit.
    """
    stripped = strip_heredocs(command)
    for seg in re.split(r"&&|\|\||[;\n|]", stripped):
        toks = seg.split()
        if not toks:
            continue
        try:
            gi = next(i for i, t in enumerate(toks) if os.path.basename(t) == "git")
        except StopIteration:
            continue
        after = toks[gi + 1:]
        if "--help" in after or "-h" in after:
            continue                       # reading the manual is not committing
        rest = [t for t in after if not t.startswith("-")]
        # `git -C path commit`: the first non-flag token after -C is the path.
        if "-C" in after:
            rest = rest[1:]
        if rest and rest[0] == "commit":
            return True
    return False


def changed_files(root):
    """Absolute paths of what this commit would carry.

    Staged first. `git commit -a` stages nothing up front, so fall back to the modified
    working tree, which is what -a would pick up.
    """
    names = [n for n in git(root, "diff", "--cached", "--name-only").splitlines() if n]
    if not names:
        names = [l[3:].strip().strip('"') for l in git(root, "status", "--porcelain").splitlines()
                 if l[:2] not in ("??",) and len(l) > 3]
    return [os.path.join(root, n) for n in names if n]


def is_doc(path):
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    return ext.lower() in DOC_EXT or stem in DOC_NAMES or base in DOC_NAMES


def entry_point(root):
    for marker, how in TEST_ENTRY_POINTS:
        if os.path.exists(os.path.join(root, marker)):
            return how
    pkg = os.path.join(root, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg) as fh:
                if (json.load(fh).get("scripts") or {}).get("test"):
                    return "npm test"
        except Exception:
            pass
    mk = os.path.join(root, "Makefile")
    if os.path.isfile(mk):
        try:
            with open(mk) as fh:
                if re.search(r"^test:", fh.read(), re.M):
                    return "make test"
        except Exception:
            pass
    return None


def mode():
    """off | warn | block, resolving `auto` against what the repo actually has."""
    if os.environ.get("GT_TEST_GATE", "").strip().lower() == "off":
        return "off"
    try:
        import gt_settings
        return gt_settings.get("test_gate") or "auto"
    except Exception:
        return "auto"


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        allow()
    if payload.get("tool_name") != "Bash":
        allow()
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "commit" not in command or not is_commit(command):
        allow()

    setting = mode()
    if setting == "off":
        allow()

    cwd = payload.get("cwd") or os.getcwd()
    root = git(cwd, "rev-parse", "--show-toplevel").strip()
    if not root:
        allow()                                   # not a repo: nothing to gate
    if os.path.exists(os.path.join(root, OPT_OUT)):
        allow()                                   # this repo is exempt, on purpose

    files = changed_files(root)
    if not files:
        allow()
    code = [f for f in files if not is_doc(f)]
    if not code:
        allow()                                   # docs-only commit

    how = entry_point(root)
    if not how and setting == "auto":
        allow()                                   # no tests to run; do not pretend

    try:
        import gt_test_receipt
        ok, receipt, stale = gt_test_receipt.covers(root, code)
    except Exception as exc:
        # Fail open, but SAY SO. gt_test_receipt.py is installed into this directory by
        # install.sh; if it cannot be imported the gate is inert, and an inert guard
        # that stays quiet is the exact failure this project keeps finding
        # (guard_vault_writes.sh shipped inert through three releases while every check
        # reported clean). Same principle as inject_core_rules.sh: degradation is
        # announced, never silent.
        allow("core_test_before_commit is DEGRADED and did not check this commit: "
              "%s: %s. gt_test_receipt.py should sit beside this hook in "
              "~/.claude/golden-thread/hooks/ -- re-run install.sh, and say that the "
              "gate is not currently enforcing." % (type(exc).__name__, exc))
    if ok:
        allow()

    how = how or "this project's tests"
    if stale:
        why = ("%s was changed after the last passing run (%s). A receipt only covers "
               "files older than itself." % (os.path.relpath(stale, root),
                                             receipt.get("at_human", "?")))
    else:
        why = "No passing test run is recorded for this repo."
    n = len(code)
    reason = (
        "BLOCKED by Core rule core_test_before_commit.\n\n"
        "  %d code file(s) staged, and their tests have not been seen to pass.\n"
        "  %s\n\n"
        "Do this instead:\n"
        "  1. Run them:      %s\n"
        "     then record it: gt_test_receipt.py record --repo . --what \"%s\" --ok\n"
        "     (tests/run.sh and dev/release-check.sh record their own receipts.)\n"
        "  2. This repo has no tests?   touch %s   -- exempt, visibly, for everyone.\n"
        "  3. This one commit only:     GT_TEST_GATE=off git commit ...\n\n"
        "Why: a release was committed on 2026-09-12 with a stale MANIFEST.json. The gate\n"
        "that would have caught it existed and had been run before the last edits rather\n"
        "than after. The discipline was fine; it had no mechanism." %
        (n, why, how, how, OPT_OUT))

    if setting == "warn":
        allow("core_test_before_commit (warn mode): " + reason.replace("BLOCKED", "WARNING"))
    deny(reason)


if __name__ == "__main__":
    main()
