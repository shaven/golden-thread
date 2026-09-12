#!/usr/bin/env python3
"""What is in the publish destination that the publisher did not put there?

    foreign_files.py <dest> <staged-tree>     # one path per line, empty = none

## Why this is separate from the sync

`rsync --delete` removes a foreign file either way. The point is to SAY so first, and
by name. On 2026-09-11 a flat, 0.9.13-era `scripts/` and `templates/` appeared in
gt-src after a publish, written by something other than `sync-gt-src.sh`. The next
sync deleted them inside forty lines of itemised rsync output, where nothing
distinguished "a file this publisher replaced" from "a file nobody here wrote".

A deletion nobody can see is how a second writer stays invisible. Only a human can
tell whether a foreign file is someone's work or a stale copy, so this names them and
stops; the sync's existing backup is what makes the removal recoverable.

Exit 0 always: this reports, it never decides.
"""
import pathlib
import sys

SKIP = {".DS_Store"}


def foreign(dest: pathlib.Path, stage: pathlib.Path):
    if not dest.is_dir():
        return []
    staged = {str(f.relative_to(stage)) for f in stage.rglob("*") if f.is_file()}
    staged.add("SOURCE.json")           # written into the destination, not the stage
    out = []
    for f in sorted(dest.rglob("*")):
        if not f.is_file() or f.name in SKIP:
            continue
        rel = str(f.relative_to(dest))
        if rel not in staged:
            out.append(rel)
    return out


def main(argv):
    if len(argv) != 2:
        print("usage: foreign_files.py <dest> <staged-tree>", file=sys.stderr)
        return 2
    for rel in foreign(pathlib.Path(argv[0]), pathlib.Path(argv[1])):
        print(rel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
