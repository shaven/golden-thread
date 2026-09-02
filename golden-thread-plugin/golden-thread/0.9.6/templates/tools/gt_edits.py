#!/usr/bin/env python3
"""Per-edit attribution: which SESSION, on which MACHINE, changed which file.

## Why this exists

`git blame` already answers "who changed this line, and when" — but every session
commits as the same human, from the same account, so git cannot distinguish two
Claude Code sessions working the vault at once. That is exactly the dimension the
2026-08-28 near-miss needed and did not have: two sessions wrote `log.md`,
`TASKS.md` and `research.md` within minutes, and nothing recorded which was which.

## Where the record lives, and why it is not a tracked file

The active ledger is `<repo>/.git/gt-edits.jsonl`.

Inside `.git` deliberately:

  * it is per-working-tree by construction, so two machines sharing this vault
    through Dropbox never write the same ledger and never conflict over it;
  * it can never be committed, so it cannot itself become the shared file that
    two sessions clobber — which would be a fine joke at this system's expense;
  * it needs no `.gitignore` entry that a future `git add -f` could defeat.

## The permanent record is the commit message

A `prepare-commit-msg` hook renders the ledger into `Session-Edit:` trailers on the
commit, and `post-commit` truncates it. Git history is then the immutable store:
messages are content-addressed, already replicated by every clone, and need no
second file to be reconciled into. Nothing here is the permanent record; the commit
is. This module only has to survive until the next commit.

## What it cannot see

Writes that do not go through `safe_write.write()` — a shell redirect, `sed -i`, a
heredoc — are invisible here, the same blind spot `guard_session_claims.sh` has and
for the same reason: detecting them means parsing arbitrary shell. Treat the ledger
as near-complete, never as proof that nothing else was touched.
"""
import json
import os
import re
import socket
import subprocess
import time

LEDGER_NAME = "gt-edits.jsonl"
_TRAILER = "Session-Edit"


def host():
    """Short machine name, e.g. 'SHMacBook-Pro-M4-Max'.

    The hostname already carries the hardware designation on these machines
    (M4-Max vs M1), which is what makes two of the user's laptops tellable apart,
    so it is kept whole; only the mDNS '.local' suffix is dropped.
    """
    h = socket.gethostname()
    return h[:-6] if h.endswith(".local") else h


def session_id():
    """Same resolution order as gt_session.py, deliberately duplicated.

    Importing gt_session here would make every write depend on it being present
    and importable. This function is three lines of env lookup; a hard dependency
    for that is a worse trade than the duplication.
    """
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
        if os.environ.get(var):
            return os.environ[var].strip()
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    for val in os.environ.values():
        m = uuid_re.search(val)
        if m:
            return m.group(0)
    return None


def _git_dir(start):
    """The .git DIRECTORY for the repo containing `start`, or None.

    Uses --git-dir rather than --show-toplevel so this keeps working inside a
    worktree or a submodule, where .git is a file pointing elsewhere.
    """
    try:
        d = start if os.path.isdir(start) else os.path.dirname(start)
        out = subprocess.run(["git", "-C", d, "rev-parse", "--absolute-git-dir"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


def _repo_root(git_dir):
    r = os.path.dirname(git_dir)
    return r if os.path.basename(git_dir) == ".git" else None


def _task_for(sid, repo_root):
    """The session's registered task line, used as the 'why' on each entry."""
    if not (sid and repo_root):
        return None
    d = os.path.join(repo_root, "Projects", "golden-thread", "sessions")
    if not os.path.isdir(d):
        return None
    hits = sorted((f for f in os.listdir(d) if f.startswith(sid + "_")), reverse=True)
    for name in hits:
        try:
            with open(os.path.join(d, name)) as fh:
                for line in fh:
                    if line.startswith("task:"):
                        return line.split(":", 1)[1].strip() or None
                    if line.startswith("# "):
                        break
        except Exception:
            continue
    return None


def ledger_path(target):
    g = _git_dir(target)
    return os.path.join(g, LEDGER_NAME) if g else None


def record(target, strategy="atomic"):
    """Append one attribution line. Never raises — logging must not break a write."""
    try:
        target = os.path.abspath(target)
        g = _git_dir(target)
        if not g:
            return None
        root = _repo_root(g)
        rel = os.path.relpath(target, root) if root else target
        # A write INTO .git is bookkeeping (this ledger, hook scratch), not content.
        if rel.startswith(".git" + os.sep) or rel == ".git":
            return None
        sid = session_id()
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "path": rel,
                 "session": (sid or "unknown")[:8],
                 "host": host(),
                 "task": _task_for(sid, root),
                 "strategy": strategy}
        with open(os.path.join(g, LEDGER_NAME), "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry
    except Exception:
        return None


def load(git_dir):
    p = os.path.join(git_dir, LEDGER_NAME)
    if not os.path.exists(p):
        return []
    rows = []
    try:
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return rows


def trailers(git_dir, staged=None):
    """-> list of 'Session-Edit: <path> · <session> · <host> · <task>' lines.

    One line per (path, session, host), newest task wins. Restricted to `staged`
    when given: a ledger entry for a file that is not in THIS commit belongs to a
    later one, and claiming it here would make the message a lie.
    """
    seen = {}
    for r in load(git_dir):
        path = r.get("path")
        if not path:
            continue
        if staged is not None and path not in staged:
            continue
        seen[(path, r.get("session"), r.get("host"))] = r
    out = []
    for (path, sess, hostname), r in sorted(seen.items()):
        bits = [path, sess or "unknown", hostname or "unknown"]
        if r.get("task"):
            bits.append(r["task"])
        out.append("%s: %s" % (_TRAILER, " · ".join(bits)))
    return out


def clear(git_dir, keep_unstaged=None):
    """Truncate the ledger after a successful commit.

    `keep_unstaged` retains entries for files that were NOT part of the commit, so
    an edit made but not yet committed is still attributed when it eventually is.
    Dropping them would silently lose attribution for anything staged later.
    """
    p = os.path.join(git_dir, LEDGER_NAME)
    if not os.path.exists(p):
        return 0
    rows = load(git_dir)
    keep = [r for r in rows if keep_unstaged and r.get("path") in keep_unstaged]
    try:
        with open(p, "w") as fh:
            for r in keep:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        return 0
    return len(rows) - len(keep)


def _staged(git_dir):
    root = _repo_root(git_dir) or "."
    try:
        out = subprocess.run(["git", "-C", root, "diff", "--cached", "--name-only"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return {l for l in out.stdout.splitlines() if l.strip()}
    except Exception:
        pass
    return set()


def apply_to_message(msg_path, git_dir, staged=None):
    """Append missing Session-Edit: trailers to a commit message file.

    Done in Python rather than the hook's shell because the shell version needed a
    flag to survive a `while read` loop, which runs in a subshell after a pipe --
    the flag silently reset every iteration and the blank line never got written.

    Idempotent: a trailer already present is not added again, so an amend or a
    retried commit cannot stack duplicates.
    """
    try:
        with open(msg_path) as fh:
            body = fh.read()
    except Exception:
        return 0
    lines = trailers(git_dir, staged)
    if not lines:
        return 0
    # Ignore commented lines when checking for duplicates: git strips them, so a
    # trailer only "present" inside the commented diff is not really there.
    live = "\n".join(l for l in body.splitlines() if not l.startswith("#"))
    new = [l for l in lines if l not in live]
    if not new:
        return 0
    stripped = body.rstrip("\n")
    # Trailers are only parsed as trailers when they sit in the final paragraph.
    sep = "\n\n" if stripped and not stripped.endswith("\n") else "\n"
    try:
        with open(msg_path, "w") as fh:
            fh.write(stripped + sep + "\n".join(new) + "\n")
    except Exception:
        return 0
    return len(new)


def main():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    g = _git_dir(os.getcwd())
    if not g:
        print("not a git repository")
        return 1
    if cmd == "show":
        rows = load(g)
        print("%d pending edit(s) in %s" % (len(rows), os.path.join(g, LEDGER_NAME)))
        for r in rows:
            print("  %s  %-45s %s@%s  %s" % (r.get("ts", "?"), r.get("path", "?"),
                                             r.get("session", "?"), r.get("host", "?"),
                                             r.get("task") or ""))
    elif cmd == "trailers":
        for t in trailers(g, _staged(g) or None):
            print(t)
    elif cmd == "apply-msg":
        if len(sys.argv) < 3:
            print("usage: gt_edits.py apply-msg <commit-msg-file>")
            return 2
        print(apply_to_message(sys.argv[2], g, _staged(g) or None))
    elif cmd == "clear":
        # Keep anything not in this commit: an edit made but not yet committed
        # must stay attributed for whenever it IS committed.
        n = clear(g, keep_unstaged=None if _staged(g) else set())
        print("cleared %d entr(ies)" % n)
    elif cmd == "commit-clear":
        # post-commit: the just-committed paths are gone from the index, so
        # anything still modified in the tree is what must be retained.
        root = _repo_root(g) or "."
        keep = set()
        try:
            out = subprocess.run(["git", "-C", root, "diff", "--name-only"],
                                 capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                keep = {l for l in out.stdout.splitlines() if l.strip()}
        except Exception:
            pass
        print(clear(g, keep_unstaged=keep))
    else:
        print("usage: gt_edits.py [show|trailers|apply-msg <f>|clear|commit-clear]")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
