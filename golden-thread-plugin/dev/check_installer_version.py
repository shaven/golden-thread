#!/usr/bin/env python3
"""`install.sh` is not covered by the version number. This makes changing it require one.

    dev/check_installer_version.py [plugin-root]

Exit 0 = the unversioned tooling has not changed since the newest release directory was
cut. Exit 1 = it has, and the release needs a bump so the change has a name.

## The incident

2026-09-12. gt 0.12.2 was published, then an installer bug was fixed — the wiring call
was gated on the vault being a git repo — and the fix was committed WITHOUT a version
bump. Two different published states then both called themselves "0.12.2": one whose
installer wires the enforcement hooks and one whose installer does not. The plugin
payload was byte-identical in both, which is exactly why nothing noticed: `MANIFEST.json`
covers `hooks/`, `scripts/` and `templates/` inside a version directory, and `install.sh`
sits at the repo root, outside all of it.

"Which version wires correctly?" had no answer. That is the same class of failure as a
hardcoded VERSION constant or a component check pinned to the wrong release: a name that
does not identify what you have.

## What counts as unversioned tooling

`install.sh` above all — it is the file a user actually runs, and a bug in it reaches
everyone regardless of which plugin payload ships. `selftest.sh` counts too: it is what
someone runs to decide whether an install is healthy.

## How it decides, without keeping a second record

Git already knows. The newest version directory was ADDED in some commit; if a tracked
change to `install.sh` exists after that commit — or sits uncommitted right now — then
the installer has moved on from the release that names it.

Recording a hash inside the version directory instead would be worse: regenerating the
manifest after editing the installer would quietly bless the edit, which is the very
move that caused this.
"""
import os
import subprocess
import sys
from pathlib import Path

WATCHED = ("install.sh", "selftest.sh")


def git(root, *args):
    try:
        p = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)


def newest_version(root):
    best = None
    for d in (root / "golden-thread").glob("*/"):
        name = d.name.rstrip("/")
        if not (d / ".claude-plugin" / "plugin.json").is_file():
            continue
        try:
            key = tuple(int(x) for x in name.split("."))
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, name)
    return best[1] if best else None


def main(argv):
    root = Path(argv[0] if argv else ".").resolve()
    if not (root / "install.sh").is_file():
        print("  no install.sh at %s" % root)
        return 2

    version = newest_version(root)
    if not version:
        print("  no installable version directory found")
        return 2

    rc, _ = git(root, "rev-parse", "--git-dir")
    if rc != 0:
        # Not a checkout (a published copy, a zip). Nothing to compare against, and
        # saying "clean" would be a lie, so say what happened instead.
        print("  not a git checkout — cannot tell whether %s changed since %s was cut"
              % ("/".join(WATCHED), version))
        return 0

    marker = "golden-thread/%s/.claude-plugin/plugin.json" % version
    rc, out = git(root, "log", "--diff-filter=A", "--format=%H", "--", marker)
    if rc != 0 or not out:
        print("  cannot locate the commit that added %s — treating as unverifiable" % version)
        return 0
    cut = out.splitlines()[-1]

    problems = []
    for name in WATCHED:
        if not (root / name).is_file():
            continue
        rc, commits = git(root, "log", "--format=%h %s", "%s..HEAD" % cut, "--", name)
        if rc == 0 and commits:
            problems.append("%s changed in %d commit(s) since %s was cut:\n      %s"
                            % (name, len(commits.splitlines()), version,
                               "\n      ".join(commits.splitlines()[:5])))
        rc, dirty = git(root, "status", "--porcelain", "--", name)
        if rc == 0 and dirty:
            problems.append("%s has uncommitted changes and %s is already cut"
                            % (name, version))

    if problems:
        for p in problems:
            print("  " + p)
        print("  The installer is what a user RUNS, and it is not covered by "
              "MANIFEST.json.")
        print("  Cut a new version directory so this change has a name:")
        print("    cp -R golden-thread/%s golden-thread/<next>" % version)
        print("    # bump .claude-plugin/plugin.json, then regenerate the manifest")
        print("installer version: %d problem(s)" % len(problems))
        return 1

    print("installer version: %s unchanged since %s was cut"
          % (", ".join(WATCHED), version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
