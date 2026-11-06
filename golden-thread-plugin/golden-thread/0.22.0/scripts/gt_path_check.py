#!/usr/bin/env python3
"""gt_path_check.py -- does this path exist? Stat it before you say so (0.21.4).

    gt_path_check.py <path> [<path>...] [--json] [--vault V]

WHY. An agent states a path from memory or inference ("design.md isn't there", "run sed on
~/Projects/x/design.md") and hands the user a command against a path nobody stat'd. The path
was right, or wrong, and either way the round trip cost a correction. This is the one cheap
check: it stats each path and says EXISTS or MISSING, with the resolved absolute form, so a
path claim can carry its verification state (Core rule 10's discipline, for paths).

WHAT IT REPORTS, per path: EXISTS, MISSING or UNKNOWN; the resolved absolute path (~ expanded; a
relative path resolved against the CURRENT FOLDER -- against the vault only with --vault, or when
the current folder is inside the vault in ~/.claude/vault-config.json -- and the output says which
base it used); kind (file, dir, or symlink with its target's kind); size and mtime; and flags:
outside-vault (when a vault is known), outside-git-work-tree. A MISSING path also lists near
misses: the same name in another letter case, the same name one folder up, and the moved-folder
shape (~/Projects against ~/Documents/Projects).

MISSING means the filesystem said so (ENOENT / ENOTDIR). A permission error (EACCES / EPERM, a
chmod-000 folder, a macOS privacy-protected folder) is UNKNOWN, naming the folder that could not
be entered: this shell cannot see whether the path is there, which is not the same as absent.
Until the 0.21.4 review both were MISSING, and a locked folder read as a missing file.

WHAT IT NEVER DOES. It never reads a file's contents and never lists a folder: it calls os.stat
(os.lstat) on named candidate paths and nothing else, so a near miss never names an entry the
caller could not already have asked about. A credential, key or .env path (gt's never list) gets
only whether it exists -- no size, time, kind, flags or near misses.

The same function backs the broker's `path_stat` tool (gt_io_broker.py), which is how a session
whose shell cannot see a folder checks a path; under gt sandbox mode the vault MCP's
vault_report path_check runs this script for vault paths.

Exit: 0 every path exists | 1 any path is missing | 2 usage | 3 any path is UNKNOWN (this shell
cannot see it; checked before 1, since an unanswered path makes the whole answer incomplete)
"""
import argparse
import errno
import json
import os
import stat as _stat
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MAX_NEAR = 6
# Folders a project folder is often moved between (~/Projects <-> ~/Documents/Projects).
MOVE_PARENTS = ("Documents", "Desktop")
# Only these errors mean "not there". Anything else (EACCES, EPERM, ELOOP...) means "cannot tell".
ABSENT_ERRNOS = (errno.ENOENT, errno.ENOTDIR)
BASE_WORDS = {"cwd": "the current folder", "vault": "the vault"}


def _posix(p):
    return p.replace("\\", "/") if isinstance(p, str) else p


def _home():
    return os.path.expanduser("~")


def never_reason(real, h=None):
    """-> why this path is a credential/key/.env (existence only), else None. Both the path as
    written and what it resolves to are judged. Fails closed: if the policy cannot load, a name
    that looks like one is still treated as one."""
    try:
        import gt_io_policy as pol
        return pol.never_reason(real, h) or pol.never_reason(os.path.realpath(real), h)
    except Exception:                                   # noqa: BLE001
        low = os.path.basename(real).lower()
        if low.startswith(".env") or low.endswith((".pem", ".key", ".p12", ".pfx")) \
                or "credential" in low or low.startswith("id_"):
            return "it looks like a credential or key file"
        return None


def vault_from_config():
    try:
        with open(os.path.join(_home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            v = json.load(fh).get("vault_path")
        return os.path.abspath(os.path.expanduser(v)) if isinstance(v, str) and v else None
    except (OSError, ValueError, AttributeError):
        return None


def relative_base(vault=None, explicit=False, cwd=None):
    """-> (folder, "cwd"|"vault"): what a relative path is resolved against. The current folder,
    unless the vault was named explicitly (--vault) or the current folder is inside the vault.
    Before the 0.21.4 review the configured vault always came first, so a relative path typed in
    a code repo was looked up in the vault."""
    cwd = cwd or os.getcwd()
    if vault and (explicit or _inside(cwd, vault) or _inside(os.path.realpath(cwd),
                                                             os.path.realpath(vault))):
        return vault, "vault"
    return cwd, "cwd"


def resolve(path, vault=None, base=None):
    """-> (absolute path, what it was resolved against). No filesystem access. `base` is a
    (folder, label) pair from relative_base(); without one, a given `vault` is the base (the
    explicit --vault form), else the current folder."""
    p = path.strip()
    if p == "~" or p.startswith(("~/", "~\\")):
        return os.path.normpath(os.path.expanduser(p)), "home"
    if os.path.isabs(p):
        return os.path.normpath(p), "absolute"
    if base is None:
        base = (vault, "vault") if vault else (os.getcwd(), "cwd")
    return os.path.normpath(os.path.join(base[0], p)), base[1]


def _inside(p, base):
    p, base = os.path.normcase(os.path.normpath(p)), os.path.normcase(os.path.normpath(base))
    return p == base or p.startswith(base.rstrip(os.sep) + os.sep)


def _in_git_work_tree(p):
    """Walk up with stat only: is there a .git above (or at) this path?"""
    d = p
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return True
        up = os.path.dirname(d)
        if up == d:
            return False
        d = up


def _kind(lst, p):
    if _stat.S_ISLNK(lst.st_mode):
        try:
            tk = "dir" if _stat.S_ISDIR(os.stat(p).st_mode) else "file"
        except OSError as e:
            tk = "broken" if e.errno in ABSENT_ERRNOS else "unknown"
        return "symlink", tk
    return ("dir" if _stat.S_ISDIR(lst.st_mode) else "file"), None


def _variants(name):
    seen, out = {name}, []
    for v in (name.lower(), name.upper(), name.capitalize(), name.title()):
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def near_misses(real, home=None, never=None):
    """Candidate paths that DO exist, found by os.lstat on guessed names only (never a listing)."""
    home = home or _home()
    never = never or never_reason
    parent, base = os.path.split(real)
    cands = []
    for v in _variants(base):                               # same folder, another letter case
        cands.append(os.path.join(parent, v))
    gp = os.path.dirname(parent)
    if gp and gp != parent:                                 # the same name one folder up
        cands.append(os.path.join(gp, base))
    pb = os.path.basename(parent)
    for v in _variants(pb):                                 # the folder's own letter case
        cands.append(os.path.join(gp, v, base))
    if _inside(real, home) and real != home:                # moved folder: ~/X <-> ~/Documents/X
        parts = os.path.relpath(real, home).replace(os.sep, "/").split("/")
        if parts[0] in MOVE_PARENTS and len(parts) > 1:
            cands.append(os.path.join(home, *parts[1:]))
        else:
            for m in MOVE_PARENTS:
                cands.append(os.path.join(home, m, *parts))
    out, seen = [], {os.path.normcase(real)}
    for c in cands:
        n = os.path.normcase(os.path.normpath(c))
        if n in seen:
            continue
        seen.add(n)
        if never(c):
            continue                                        # not even a stat of a credential name
        try:
            lst = os.lstat(c)
        except OSError:
            continue
        k, _t = _kind(lst, c)
        out.append({"path": _posix(os.path.normpath(c)), "kind": k})
        if len(out) >= MAX_NEAR:
            break
    return out


def _blocked_at(real):
    """-> the folder this process cannot enter on the way to `real`: the deepest ancestor that
    lstat still answers for (its contents are what the permission error hid). Stat only."""
    d = os.path.dirname(real)
    while True:
        try:
            os.lstat(d)
            return d
        except OSError:
            up = os.path.dirname(d)
            if up == d:
                return d
            d = up


def _unknown(res, real, err):
    why = os.strerror(err.errno) if err.errno else type(err).__name__
    res.update(exists=None, status="UNKNOWN", errno=errno.errorcode.get(err.errno, str(err.errno)))
    res["note"] = "cannot see %s: %s" % (_posix(_blocked_at(real)), why.lower())
    return res


def check(path, vault=None, with_near=True, never=None, base=None):
    """-> the report for one path. Only os.lstat / os.stat run; contents are never read.
    status: EXISTS, MISSING (ENOENT/ENOTDIR only) or UNKNOWN (any other error, e.g. EACCES /
    EPERM: this process cannot see whether it is there; exists is None)."""
    real, against = resolve(path, vault, base)
    never = never or never_reason
    res = {"input": path, "path": _posix(real), "resolved_against": against}
    if against in BASE_WORDS:
        res["base"] = _posix(os.path.normpath(base[0] if base else (vault or os.getcwd())))
    if never(real):
        res["credential"] = True
        res["note"] = "credential, key or .env path: existence only, nothing else is reported"
        try:
            os.lstat(real)
            res["exists"] = True
        except OSError as e:
            if e.errno not in ABSENT_ERRNOS:
                return _unknown(res, real, e)
            res["exists"] = False
        res["status"] = "EXISTS" if res["exists"] else "MISSING"
        return res
    try:
        lst = os.lstat(real)
    except OSError as e:
        if e.errno not in ABSENT_ERRNOS:
            return _unknown(res, real, e)
        lst = None
    exists = lst is not None
    flags = []
    if vault and not _inside(real, vault):
        flags.append("outside-vault")
    if not _in_git_work_tree(real):
        flags.append("outside-git-work-tree")
    res["flags"] = flags
    if exists:
        k, tk = _kind(lst, real)
        res.update(kind=k, size=lst.st_size, mtime=int(lst.st_mtime))
        res["mtime_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(lst.st_mtime))
        if tk:
            res["target_kind"] = tk
            try:
                res["target"] = _posix(os.readlink(real))
            except OSError:
                pass
            if tk == "broken":
                exists = False
                res["note"] = "a symlink whose target is missing"
            elif tk == "unknown":
                res.update(exists=None, status="UNKNOWN",
                           note="a symlink whose target cannot be seen (permission denied)")
                return res
    res["exists"] = exists
    res["status"] = "EXISTS" if exists else "MISSING"
    if not exists and with_near:
        res["near_misses"] = near_misses(real, never=never)
    return res


CLI_ALTERNATIVE = ("this shell cannot see it: check it with the path_stat MCP tool, or "
                   "vault_report path_check for a vault path")


def _text(r):
    line = "%-7s %s" % (r["status"], r["path"])
    if r["resolved_against"] in BASE_WORDS:
        line += "  <- relative to %s %s" % (BASE_WORDS[r["resolved_against"]], r.get("base", ""))
    if r["status"] == "UNKNOWN":
        return line + "  (%s; %s)" % (r.get("note", "cannot see it"), CLI_ALTERNATIVE)
    if r.get("credential"):
        return line + "  (credential path: existence only)"
    if r.get("kind"):
        line += "  [%s%s, %d bytes, %s]" % (
            r["kind"], ("->" + r["target_kind"]) if r.get("target_kind") else "", r["size"],
            r["mtime_iso"])
    if r.get("flags"):
        line += "  {%s}" % ", ".join(r["flags"])
    if r.get("note"):
        line += "  (%s)" % r["note"]
    for n in r.get("near_misses") or []:
        line += "\n          near miss: %s (%s)" % (n["path"], n["kind"])
    return line


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_path_check.py", description="Stat paths: EXISTS or "
                                 "MISSING, never reads contents or lists a folder.")
    ap.add_argument("paths", nargs="+", metavar="path")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    ap.add_argument("--vault", help="resolve relative paths against this vault (default: the "
                                    "current folder; the vault in ~/.claude/vault-config.json "
                                    "only when the current folder is inside it)")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    vault = os.path.abspath(os.path.expanduser(a.vault)) if a.vault else vault_from_config()
    base = relative_base(vault, explicit=bool(a.vault))
    results = [check(p, vault, base=base) for p in a.paths if p.strip()]
    if not results:
        sys.stderr.write("gt_path_check.py: give at least one path\n")
        return 2
    unknown = [r for r in results if r["status"] == "UNKNOWN"]
    missing = [r for r in results if r["status"] == "MISSING"]
    if a.json:
        print(json.dumps({"ok": not (missing or unknown), "vault": _posix(vault) if vault else None,
                          "relative_base": {"path": _posix(base[0]), "against": base[1]},
                          "results": results}, indent=1, ensure_ascii=False))
    else:
        for r in results:
            print(_text(r))
    return 3 if unknown else 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
