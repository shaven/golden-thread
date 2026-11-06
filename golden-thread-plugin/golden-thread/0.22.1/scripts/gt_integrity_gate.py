#!/usr/bin/env python3
"""gt_integrity_gate -- the check every hook makes about its own files before it runs
(0.21.0, `paranoid.signed_manifest`; request 2026-10-08-installed-code-integrity).

HOT PATH, so deliberately tiny: standard library only, nothing imported at module level but
os/sys/json, and with the group off the whole cost is ONE stat() of
~/.claude/golden-thread/integrity.json (absent = nothing to check, return).

WHAT IT CHECKS. install.sh (via gt_integrity.py) records every installed code file's SHA-256 in
integrity.json (signed with the unlock key when unlock is enrolled, else pinned -- weaker, and
said so) and, after the OS lock is applied, a stamp of each hooks-dir file in
integrity-stamps.json:

    [sha256, inode, size, mtime_ns, ctime_ns]

A hook calls check() with its own path. The files checked are that entry and the gt modules it
imports (a static list the sealing step worked out, in the stamps file). Each file is compared by
its stamp: a handful of stat() calls, no hashing -- microseconds. ctime is in the stamp because
nothing can set it: a write, a chmod or a rename always moves it, so a file edited and then
`touch -d`-ed back to its old mtime still shows. Only a stamp that DIFFERS costs a hash, and a
file that was merely touched (same bytes) then passes.

ON A MISMATCH
  guard   (kind "guard": the PreToolUse hooks)  FAILS CLOSED: a `deny` that names the file and
          the way out. The one deliberate exception to "a gt hook fails open", and only when the
          group is on.
  other   (kind "reporter")  fails LOUD: a systemMessage naming the file, and the hook does not
          run (code that does not match the record is not run to find out what it does).
Either way the process exits 0 with the JSON on stdout; it never prints "allow". A gate that
itself cannot run (no interpreter, a broken module) fails open like every gt hook -- the OS lock
and `gt_integrity.py verify` are the other layers.

DESIGN LIMIT, stated plainly: this detects drift from the RECORD. An attacker who can rewrite the
record can bless a change; with the OS lock on that needs the unlock first (presence), and in
signed mode `gt_integrity.py verify` (run by the doctor and at session start) checks the record's
signature, which the per-hook path does not (it would cost milliseconds per hook).
"""
import json
import os
import sys

NAME = "integrity.json"
SEALED = "paranoid-sealed.json"
STAMPS = "integrity-stamps.json"
ROUTE = ("python3 ~/.claude/golden-thread/hooks/gt_integrity.py verify   (what changed); "
         "after a real upgrade, `gt_integrity.py unlock --for-upgrade` then re-run install.sh")


def _gt_home():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread")


def active(gt=None):
    """Is there a sealed record to check against? One stat() normally. The sealed marker counts too:
    paranoid was turned on, so a MISSING integrity.json is tampering, not "nothing to check" (0.22.0,
    validation F3: deleting that one file made every hook run unchecked)."""
    gt = gt or _gt_home()
    return os.path.exists(os.path.join(gt, NAME)) or os.path.exists(os.path.join(gt, SEALED))


def _stamp(st):
    return [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _sha(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _names(stamps, rel):
    """The entry and everything it reaches: the modules, then their bytecode. The stamps file holds the
    DIRECT import graph (linear in the module count); the closure is walked here, over only the modules
    this entry reaches. A stamps file from before 0.22.0 holds the closure itself ("deps")."""
    graph = stamps.get("graph")
    if not isinstance(graph, dict):
        return [rel] + [d for d in (stamps.get("deps") or {}).get(rel, []) if d != rel]
    edges, pycs = graph.get("edges") or {}, graph.get("pycs") or {}
    seen, todo = {rel}, [rel]
    while todo:
        for d in edges.get(todo.pop(), ()):
            if d not in seen:
                seen.add(d)
                todo.append(d)
    return [rel] + sorted(d for d in seen if d != rel) + sorted(x for d in seen for x in pycs.get(d, []))


def verify_entry(entry, gt=None):
    """-> a list of 'file: what is wrong' for the entry file and the modules it imports ([] = clean)."""
    gt = gt or _gt_home()
    if not os.path.exists(os.path.join(gt, NAME)):
        return ["%s: missing (paranoid is on, so the record was deleted, not never written)" % NAME]
    try:
        with open(os.path.join(gt, STAMPS), encoding="utf-8") as fh:
            stamps = json.load(fh)
    except (OSError, ValueError):
        return ["%s: the integrity stamps are missing or unreadable (a sealed record exists "
                "without them)" % STAMPS]
    files = stamps.get("files") or {}
    rel = os.path.relpath(os.path.abspath(entry), gt).replace(os.sep, "/")
    names = _names(stamps, rel)
    if rel not in files:
        return ["%s: not in the sealed manifest (a hook file that was added after the seal)"
                % os.path.basename(entry)]
    problems = []
    for n in names:
        rec = files.get(n)
        if rec is None:
            problems.append("%s: not in the sealed manifest" % os.path.basename(n))
            continue
        path = os.path.join(gt, n)
        try:
            st = os.lstat(path)
        except OSError:
            problems.append("%s: missing" % os.path.basename(n))
            continue
        if rec[1:] == _stamp(st):
            continue
        try:
            same = _sha(path) == rec[0]
        except OSError:
            problems.append("%s: unreadable" % os.path.basename(n))
            continue
        if not same:
            problems.append("%s: changed since it was sealed (its fingerprint differs from the "
                            "manifest)" % os.path.basename(n))
    return problems


def _refusal(kind, entry, problems):
    more = "" if len(problems) <= 4 else " (+%d more)" % (len(problems) - 4)
    body = ("INTEGRITY CHECK FAILED (gt paranoid.signed_manifest) for %s: %s%s.\n"
            "  %s\n  What to do: %s"
            % (os.path.basename(entry), "; ".join(problems[:4]), more,
               "This guard did not run, and the action it guards is blocked until the record "
               "matches again." if kind == "guard"
               else "This hook did not run.", ROUTE))
    if kind == "guard":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": body}}
    return {"systemMessage": "gt: " + body.replace("\n  ", " ")}


def check(entry, kind="reporter", when=True, gt=None):
    """Called at the top of a hook. Returns when all is well (or the group is off); otherwise
    prints the refusal and exits 0. `when=False` is for a script that is only SOMETIMES a hook
    (gt_components.py check --hook vs gt_components.py apply, which is the repair route)."""
    if not when:
        return
    gt = gt or _gt_home()
    if not active(gt):
        return
    try:
        problems = verify_entry(entry, gt)
    except Exception:                                      # noqa: BLE001 - fail open like every gt hook
        return
    if not problems:
        return
    sys.stdout.write(json.dumps(_refusal(kind, entry, problems)) + "\n")
    sys.stdout.flush()
    raise SystemExit(0)


def main(argv=None):
    """Shell wrappers: `gt_integrity_gate.py check <guard|reporter> <hook file>` -> exit 0 clean,
    3 after printing a refusal (the wrapper then exits 0 without running the hook)."""
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) >= 3 and a[0] == "check":
        gt = _gt_home()
        if not active(gt):
            return 0
        try:
            problems = verify_entry(a[2], gt)
        except Exception:                                  # noqa: BLE001
            return 0
        if not problems:
            return 0
        sys.stdout.write(json.dumps(_refusal(a[1], a[2], problems)) + "\n")
        return 3
    sys.stderr.write("usage: gt_integrity_gate.py check <guard|reporter> <hook file>\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
