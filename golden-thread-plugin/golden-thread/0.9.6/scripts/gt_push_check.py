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


def _git(repo, *args):
    """-> (ok, stdout). Never raises: a probe must not break SessionStart."""
    try:
        p = subprocess.run(("git", "-C", repo) + args, capture_output=True, text=True)
        return p.returncode == 0, (p.stdout or "").strip()
    except Exception:
        return False, ""


def _vault_path():
    try:
        with open(VAULT_CONFIG) as fh:
            return json.load(fh).get("vault_path") or ""
    except Exception:
        return ""


def report(vault=None):
    vault = vault or _vault_path()
    if not vault or not os.path.isdir(os.path.join(vault, ".git")):
        # Not an error: plenty of vaults are not repos. Say so rather than going
        # quiet, so "no vault repo" is distinguishable from "check never ran".
        print("GOLDEN THREAD push: no vault git repo — nothing to check.")
        return 0

    ok, branch = _git(vault, "rev-parse", "--abbrev-ref", "HEAD")
    if not ok or not branch:
        print("GOLDEN THREAD push: could not read the vault's branch — skipped.")
        return 0
    if branch == "HEAD":
        print("GOLDEN THREAD push: vault is in detached HEAD — commit to a branch "
              "before this can mean anything.")
        return 1

    has_upstream, upstream = _git(vault, "rev-parse", "--abbrev-ref",
                                  "--symbolic-full-name", "@{u}")
    if not has_upstream:
        ok, total = _git(vault, "rev-list", "--count", "HEAD")
        n = total if ok else "?"
        print("GOLDEN THREAD push: vault branch '%s' has NO UPSTREAM — its %s "
              "commit(s) have nowhere to go." % (branch, n))
        print("  set one with: git -C \"%s\" push -u origin %s" % (vault, branch))
        return 1

    ok, count = _git(vault, "rev-list", "--count", "%s..HEAD" % upstream)
    if not ok:
        print("GOLDEN THREAD push: could not compare vault against %s — skipped."
              % upstream)
        return 0
    n = int(count or 0)
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
    ok, stamps = _git(vault, "log", "--format=%ct", "%s..HEAD" % upstream)
    if ok and stamps:
        try:
            oldest = int(stamps.split("\n")[-1])
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
    Falls back to printing if the settings module cannot be imported, because a
    degraded delivery path must never swallow the report entirely."""
    try:
        import gt_settings
    except Exception:
        return fn(*a, **kw)
    r, text = gt_settings.capture(fn, *a, **kw)
    gt_settings.emit(text)
    return r


def main():
    args = sys.argv[1:]
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
