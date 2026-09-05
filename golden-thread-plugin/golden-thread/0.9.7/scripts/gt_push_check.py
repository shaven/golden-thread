#!/usr/bin/env python3
"""Golden Thread push check: are the vault's commits still only on this machine?
Notify only.

## The gap this closes

The vault is the single source of truth for AI memory across every project, and it
is a git repo precisely so that truth survives one disk and reaches the other
machines. A commit that never leaves is not backed up, is invisible to a session on
another host, and -- worst -- *looks* finished. `gt-work` reports success, the tree
goes clean, and the record stops there.

On 2026-09-02 the vault was found **16 commits ahead of origin**, the oldest dating
back weeks. Nothing had failed. Every session had committed correctly and none had
pushed, and no check anywhere asked. The same session had just spent hours
reconciling two plugin repos that had drifted for four releases because nothing
carried work between them -- the identical shape, one layer down, in the vault's own
history.

## Why this is separate from the other probes

  gt_components   installed files    vs  one version's source     (contents)
  gt_version      installed version  vs  newest version present   (selection)
  gt_push         local commits      vs  the remote               (distribution)

The first two ask whether this machine is running the right thing. This asks whether
anything else can ever see what this machine produced.

## Why notify-only, with no `auto`

Pushing is an outward-facing act. It publishes to a remote that other people or
machines read, it can be rejected, and it can require credentials or a merge. Doing
it unattended at SessionStart would mean a hook deciding to publish -- and a push
that surprises its author is worse than a delay that annoys them.

An unpushed commit is also sometimes correct: work deliberately held back, a rebase
in progress, a branch not meant to leave. So this reports and stops.

## No upstream is reported louder, not quieter

A branch with no upstream configured cannot be behind, so a naive count returns zero
and the check reports clean. That is the same false calm as a component check aimed
at the wrong version: the commits are not merely unpushed, they have nowhere to go.
It is called out explicitly rather than folded into the healthy path.
"""
import json
import os
import shutil
import subprocess
import sys
import time

VAULT_CONFIG = os.path.expanduser("~/.claude/vault-config.json")


def _registry_get(name, fallback):
    """Prefer the shared settings registry so defaults live in ONE place.
    Same reasoning as gt_components._registry_get: a second copy of a default is a
    default that drifts."""
    try:
        import gt_settings
        v = gt_settings.get(name)
        if v:
            return v
    except Exception:
        pass
    return fallback


GIT_TIMEOUT = 5   # seconds per call. See _git.


def _git(repo, *args):
    """-> (ok, stdout, why). Never raises: a probe must not break SessionStart.

    `why` is "" on success, else "missing" (no git binary), "timeout", or
    "failed". Bounded by GIT_TIMEOUT: without one, every call here was limited only
    by Claude Code's 60 s hook timeout, and a pack file Dropbox had evicted to an
    online-only placeholder, or a stale index.lock, would block the whole session
    start for a minute and then report nothing at all.
    """
    if shutil.which("git") is None:
        return False, "", "missing"
    try:
        p = subprocess.run(("git", "-C", repo) + args, capture_output=True,
                           text=True, timeout=GIT_TIMEOUT)
        return p.returncode == 0, (p.stdout or "").strip(), "" if p.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception:
        return False, "", "failed"


def _vault_path():
    """Same resolution order as gt_paths.find_vault: GT_VAULT first, then the
    recorded vault-config.json. 0.9.6 read only the config file, so with the
    documented override exported every other hook watched one repo while this
    one reported on another -- or on none."""
    env = os.environ.get("GT_VAULT")
    if env and os.path.isdir(env):
        return env
    try:
        with open(VAULT_CONFIG) as fh:
            return json.load(fh).get("vault_path") or ""
    except Exception:
        return ""


def report(vault=None):
    vault = vault or _vault_path()
    if not vault or not os.path.isdir(vault):
        print("GOLDEN THREAD push: no vault path configured — nothing to check.")
        return 0

    # Ask git, never the filesystem: `.git` is a FILE for a worktree, a submodule
    # or a --separate-git-dir checkout, and `os.path.isdir(.git)` reported all of
    # those as "no vault git repo" -- the false calm this module exists to end.
    is_repo, _, why = _git(vault, "rev-parse", "--git-dir")
    if not is_repo:
        if why == "missing":
            print("GOLDEN THREAD push: git is not installed — skipped.")
        elif why == "timeout":
            print("GOLDEN THREAD push: git did not answer within %ds (evicted pack "
                  "file? stale index.lock?) — skipped." % GIT_TIMEOUT)
        else:
            # Not an error: plenty of vaults are not repos. Say so rather than going
            # quiet, so "no vault repo" is distinguishable from "check never ran".
            print("GOLDEN THREAD push: no vault git repo — nothing to check.")
        return 0

    ok, branch, _ = _git(vault, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok or not branch:
        print("GOLDEN THREAD push: could not read the vault's branch — skipped.")
        return 0
    if branch == "HEAD":
        print("GOLDEN THREAD push: vault is in detached HEAD — commit to a branch "
              "before this can mean anything.")
        return 1

    has_upstream, upstream, _ = _git(vault, "rev-parse", "--abbrev-ref",
                                     "--symbolic-full-name", "@{u}")
    if not has_upstream:
        # Three different situations used to share one message and one piece of
        # advice (`push -u origin <branch>`), which was wrong for two of them.
        _, total, _ = _git(vault, "rev-list", "--count", "HEAD")
        n = total or "?"
        _, remotes, _ = _git(vault, "remote")
        remotes = remotes.split()
        _, merge, _ = _git(vault, "config", "--get", "branch.%s.merge" % branch)
        _, remote, _ = _git(vault, "config", "--get", "branch.%s.remote" % branch)
        if merge:
            target = "%s/%s" % (remote or "?", merge.replace("refs/heads/", ""))
            print("GOLDEN THREAD push: vault branch '%s' is configured to track %s, "
                  "but that ref does not exist locally — the remote branch was "
                  "deleted, or never fetched. %s commit(s) unaccounted for."
                  % (branch, target, n))
            print("  fetch first: git -C \"%s\" fetch %s" % (vault, remote or "--all"))
            print("  then, if it is really gone: git -C \"%s\" push -u %s %s"
                  % (vault, remote or "origin", branch))
            return 1
        if not remotes:
            print("GOLDEN THREAD push: vault branch '%s' has NO REMOTE — its %s "
                  "commit(s) exist on this disk only." % (branch, n))
            print("  add one with: git -C \"%s\" remote add origin <url>" % vault)
            print("  then:         git -C \"%s\" push -u origin %s" % (vault, branch))
            return 1
        remote = "origin" if "origin" in remotes else remotes[0]
        print("GOLDEN THREAD push: vault branch '%s' has NO UPSTREAM — its %s "
              "commit(s) have nowhere to go." % (branch, n))
        print("  set one with: git -C \"%s\" push -u %s %s" % (vault, remote, branch))
        return 1

    # One call answers both "how many" and "how old": the count is the number of
    # stamps. 0.9.6 spent a separate `rev-list --count` on the same range.
    ok, stamps, why = _git(vault, "log", "--format=%ct", "%s..HEAD" % upstream)
    if not ok:
        print("GOLDEN THREAD push: could not compare vault against %s (%s) — skipped."
              % (upstream, why))
        return 0
    lines = [x for x in stamps.splitlines() if x.strip()]
    n = len(lines)
    if n == 0:
        # Said out loud rather than exiting silently: at SessionStart a mute check
        # cannot be told apart from one that never ran. Same reasoning as
        # gt_components, gt_version_check and gt_workers.
        print("GOLDEN THREAD push: vault in sync with %s." % upstream)
        return 0

    # The count alone understates it. Fifteen commits made this morning is a busy
    # session; fifteen spanning weeks is a habit that has stopped working, and only
    # the age of the OLDEST one tells them apart.
    age = ""
    try:
        oldest = int(lines[-1])
        days = (time.time() - oldest) / 86400.0
        age = (", oldest %.0f days old" % days) if days >= 1 else ", all from today"
    except Exception:
        pass

    print("GOLDEN THREAD push: vault is %d commit(s) AHEAD of %s%s — committed here "
          "only." % (n, upstream, age))
    print("  push with: git -C \"%s\" push" % vault)
    return n


def _emit(fn, *a, **kw):
    """Run a reporting function and deliver its output to the user as well as the
    model. See gt_settings.emit -- as a SessionStart hook, plain stdout reaches the
    model only, so a healthy check was invisible to the person it was reassuring.

    Degrades to a plain print, never to silence: if gt_settings cannot be imported
    OR is an older copy without capture/emit (a stale file in the hooks dir looked
    exactly like this on 2026-09-05 and would have crashed all four SessionStart
    checks while the component check reported clean), the report is printed
    directly. fn runs exactly once on every path."""
    try:
        import gt_settings
        capture, emit = gt_settings.capture, gt_settings.emit
    except Exception:
        return fn(*a, **kw)
    r, text = capture(fn, *a, **kw)
    try:
        emit(text)
    except Exception:
        print(text)
    return r


def main():
    args = [x for x in sys.argv[1:] if x != "--hook"]   # see gt_settings.hook_args
    if not args or args[0] != "check":
        print(__doc__.strip().splitlines()[0])
        print("usage: gt_push_check.py check [vault-path]")
        return 2
    if _registry_get("push_check", "report") == "off":
        return 0
    vault = args[1] if len(args) > 1 else None
    _emit(report, vault)
    return 0      # never a failing exit: advisory only, like the other probes


if __name__ == "__main__":
    raise SystemExit(main())
