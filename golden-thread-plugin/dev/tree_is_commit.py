#!/usr/bin/env python3
"""tree_is_commit.py -- is the working tree the tests ran on the same tree the commit holds?

    python3 dev/tree_is_commit.py [REPO] [--exclude PATH ...] [--warn PATH ...]
        # exit 0 same | 1 differs (each item named) | 2 usage

    --exclude PATH   a subtree that is never published (a release older than the previous one)
    --warn PATH      a subtree that publishes but can no longer be changed (the PREVIOUS release,
                     already released): a difference there is reported, not refused
    --published      derive both from dev/plugins.py, exactly as dev/sync-gt-src.sh publishes:
                     newest release of each plugin must match, the previous one warns, older
                     releases are excluded. What publish.sh and sync-gt-src.sh both run.

WHY (2026-09-29). gt 0.17.3 passed a full gate of 1,988 tests and still would not install from
gt-src: `packs/community/` was an EMPTY directory. It existed in the working tree every test
installed from, and in nothing made through git -- git tracks files, never directories -- so the
commit, gt-src and every clone lacked it. The publisher's only working-tree check was "git status
is clean", and git status cannot see an empty directory, so it reported clean. Every later check
compared a copy with the COMMIT; nothing compared the commit with the tree that was TESTED.

This compares them directly, without asking git's opinion of the working tree:

  * a DIRECTORY in the working tree with no committed file beneath it -- git will drop it;
  * a FILE in the working tree that the commit does not hold and .gitignore does not exclude
    (untracked work that was tested and would not ship).

Ignored paths (`__pycache__`, build zips) are expected to differ and are left out, and so is
`.git` itself. Run it AFTER committing and BEFORE publishing: that is the moment "what was
tested" and "what ships" must be proven equal.
"""
import os
import subprocess
import sys


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--warn", action="append", default=[])
    ap.add_argument("--published", action="store_true")
    a = ap.parse_args(argv)
    under = lambda p, roots: any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)
    try:
        root = git(a.repo, "rev-parse", "--show-toplevel").strip()
        committed = set(git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD").split("\0")) - {""}
        untracked = [p for p in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if p]
        ignored_dirs = {p.rstrip("/") for p in git(root, "ls-files", "--others", "--ignored",
                                                   "--exclude-standard", "--directory", "-z").split("\0") if p}
    except (subprocess.CalledProcessError, OSError) as exc:
        print("tree_is_commit: could not read the repository: %s" % exc, file=sys.stderr)
        return 2
    if a.published:
        plugin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        prefix = git(plugin, "rev-parse", "--show-prefix").strip()
        run = lambda *x: subprocess.run([sys.executable, os.path.join(plugin, "dev", "plugins.py"), *x],
                                        capture_output=True, text=True, check=True).stdout
        for line in run("list").splitlines():
            d, newest = line.split()[:2]
            keep = run("releases", d, "2").split()
            for v in sorted(os.listdir(os.path.join(plugin, d))):
                if not os.path.isfile(os.path.join(plugin, d, v, ".claude-plugin", "plugin.json")):
                    continue
                if v == newest:
                    continue
                (a.warn if v in keep else a.exclude).append(prefix + d + "/" + v)
    has_file = set()
    for f in committed:
        d = os.path.dirname(f)
        while d:
            has_file.add(d)
            d = os.path.dirname(d)
    dropped = []
    for dirpath, dirnames, _files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        # (0.19.3) git names ignored dirs with "/"; normpath gives "\\" on Windows, so an
        # ignored __pycache__ there read as a dropped directory. Compared in git's form.
        dirnames[:] = [d for d in dirnames if d != ".git"
                       and os.path.normpath(os.path.join(rel, d)).replace(os.sep, "/").lstrip("./")
                       not in ignored_dirs]
        if rel != "." and rel.replace(os.sep, "/") not in has_file:
            dropped.append(rel.replace(os.sep, "/"))
    found = ["directory with no committed file (git drops it): %s/" % d for d in sorted(dropped)]
    found += ["file tested here but not in the commit: %s" % f for f in sorted(untracked)]
    path_of = lambda line: line.split(": ", 1)[1].rstrip("/")
    found = [f for f in found if not under(path_of(f), a.exclude)]
    warned = [f for f in found if under(path_of(f), a.warn)]
    problems = [f for f in found if f not in warned]
    for w in warned:
        print("WARNING (already released, cannot be changed): " + w)
    if problems:
        print("WORKING TREE != COMMIT -- what was tested is not what would ship:")
        for p in problems[:40]:
            print("  " + p)
        if len(problems) > 40:
            print("  ... and %d more" % (len(problems) - 40))
        return 1
    print("working tree matches HEAD: %d committed files, no empty or untracked directory"
          % len(committed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
