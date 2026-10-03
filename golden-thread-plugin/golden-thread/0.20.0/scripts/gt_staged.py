#!/usr/bin/env python3
"""gt_staged.py -- what a commit will actually contain, materialised so a scanner can read it.

ONE copy, imported by every tool that gates a commit. gt_secrets.py had this logic first and
gt_scan_code.py needed the same thing; copying it would have been the third duplicated-fixture
defect of 2026-09-27 (the install fixture that skipped its manifest step, the scan fixture that
hard-coded its member list, and this). A commit gate that reads the index slightly differently
from another commit gate is a gate that disagrees with itself.

THE INDEX, NOT THE WORKING TREE, and the difference is the whole reason a commit gate exists:
`git add` something, then edit the file again, and the worktree and the commit differ. A gate
reading the worktree waves through what the commit carries, and blocks on what it does not.

DELETIONS ARE EXCLUDED. A commit that REMOVES a bad line must never be blocked by the line it
removes, or the only way to fix a finding is blocked by the finding.

THE MATERIALISED FILES ARE THE CALLER'S TO DELETE. They hold the staged content -- for
gt_secrets that is literally the credential being protected -- so `scan_staged` is a context
manager and cleanup is not optional.
"""
from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path


def staged_paths(repo: Path):
    """-> [rel, ...] added, copied or modified in the index; None if `repo` is not a git repo."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return [r for r in proc.stdout.split("\0") if r]


def materialise(repo: Path, rels, dest: Path):
    """Write each staged BLOB into `dest` under its own relative path. -> the paths written.

    Writing them out rather than scanning in memory means the SAME scope rules and the SAME
    per-file logic run for a commit as for a tree scan, so a gate cannot drift from the scanner
    it claims to be.
    """
    written = []
    for rel in rels:
        proc = subprocess.run(["git", "-C", str(repo), "show", ":" + rel], capture_output=True)
        if proc.returncode != 0:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(proc.stdout)
        written.append(rel)
    return written


@contextlib.contextmanager
def scan_staged(repo: Path, prefix="gt-staged-"):
    """Yield (root, rels): a temp tree holding the staged content, removed on the way out.

    root is None when `repo` is not a git repository -- "there is no index here" is not "there
    is nothing to find", and the caller must say so rather than reporting clean. rels is empty
    when nothing is staged, which IS clean: there is no content to carry a finding.

    mkdtemp gives 0700, and the tree is removed on every exit path including an exception. A
    scanner that leaves the content it was inspecting in a shared /tmp has become a way for
    that content to leak.
    """
    rels = staged_paths(repo)
    if rels is None:
        yield None, []
        return
    if not rels:
        yield None, []
        return
    tmp = tempfile.mkdtemp(prefix=prefix)
    try:
        yield Path(tmp), materialise(repo, rels, Path(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def is_a_repo(repo: Path) -> bool:
    return staged_paths(repo) is not None
