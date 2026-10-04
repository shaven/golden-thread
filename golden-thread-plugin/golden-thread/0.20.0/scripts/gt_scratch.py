#!/usr/bin/env python3
"""gt_scratch.py -- a private scratch folder per pipeline run and unit, OUTSIDE the vault (0.20.0).

    gt_scratch.py root [--json]                         where scratch folders live
    gt_scratch.py path <run> --stage S --unit U [--json] create (0700) and print one unit's folder
    gt_scratch.py list [--json]                         every run with scratch left, and its size
    gt_scratch.py cleanup <run> [--json]                remove one run's scratch
    gt_scratch.py check [--vault V] [--json]            leaks: scratch of a run that is finished or
                                                        gone (exit 1), never removed by `check`

WHY. A pipeline stage agent (ingest extract / classify / reconcile / draft, promote verify /
generalize / place, gt-work's extract-session) used to have nowhere of its own for intermediate
files: the run's spool folder is per RUN, not per agent, and it is INSIDE the vault -- the wrong
place for extracted raw material, and a place gt sandbox mode denies every write to. Each unit of
a stage whose agent CAN write -- a tool in gt_agent_spec.WRITE_TOOLS, decided from the stage
spec's tool list; today only verify, which has a shell -- now gets its own folder. A read-only
stage gets none, and its prompt never mentions one (owner, 2026-10-04):

    POSIX    ~/.gt-scratch/<run>/<stage>-<unit>/     every level created 0700, owner only
    Windows  %LOCALAPPDATA%\\gt-scratch\\<run>\\<stage>-<unit>\\   inheritance removed, the
             current user alone granted full control (icacls)

GT_SCRATCH_ROOT overrides the root (tests; a machine whose home is on a slow share). The folder's
path goes into the agent's rendered prompt as the ONLY place it may put intermediate files; only
the agent's RESULT comes back, through the packet, exactly as before. Nothing gt does reads the
folder back.

CLEANUP. gt_ingest_pipeline.py removes a run's scratch when the run finishes (`draft` writes its
queue, `promote-plan` reaches the owner) and on `cleanup <run>` for an abandoned one; `check`
lists scratch left for a run that is finished or no longer exists, and gt_doctor reports it.

SAFETY. The root and every folder under it must be a real directory owned by this user: a symlink
or a folder someone else owns is refused rather than followed or chmod-ed. Run, stage and unit
names are reduced to [A-Za-z0-9._-] before they become path parts.

Exit: 0 ok | 1 refused, or (check) a leak | 2 usage.
"""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys

OK, PROBLEM, USAGE = 0, 1, 2
IS_WINDOWS = os.name == "nt"
ROOT_NAME = ".gt-scratch"
WIN_ROOT_NAME = "gt-scratch"
RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


class ScratchError(Exception):
    pass


def root(h=None):
    """The scratch root. Never inside the vault: it is under the user's home (POSIX) or local
    app data (Windows)."""
    env = os.environ.get("GT_SCRATCH_ROOT")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if IS_WINDOWS and not h:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"),
                                                              "AppData", "Local")
        return os.path.join(base, WIN_ROOT_NAME)
    return os.path.join(os.path.abspath(h or os.path.expanduser("~")), ROOT_NAME)


def slug(part):
    """gt_ingest_pipeline.unit_slug, exactly: a unit name as one safe path part."""
    if part in (".", ""):
        return "_root"
    s = re.sub(r"[^A-Za-z0-9._-]+", "__", str(part).strip("/"))
    s = s[:120] or "_unit"
    return "_" + s if s in (".", "..") or s.startswith("..") else s


def _current_sid():
    """The current user's SID from `whoami /user` ("box\\u","S-1-5-21-..."), or None. A SID,
    not DOMAIN\\name: over ssh USERDOMAIN can read WORKGROUP, which icacls cannot map."""
    try:
        r = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"(S-1-[0-9-]+)"', r.stdout or "")
    return m.group(1) if r.returncode == 0 and m else None


def _owner_only_windows(path):
    """Remove inherited ACEs and grant the current user alone full control."""
    sid = _current_sid()
    who = ("*" + sid) if sid else (os.environ.get("USERNAME") or "")
    if not who:
        raise ScratchError("cannot restrict %s: neither the user's SID nor USERNAME is known"
                           % path)
    r = subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "%s:(OI)(CI)F" % who],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ScratchError("icacls could not restrict %s: %s"
                           % (path, (r.stderr or r.stdout).strip()[:200]))


def _private_dir(path):
    """Create `path` (one level) owner-only, or check an existing one is a real directory of
    ours and tighten it to 0700. Raises ScratchError otherwise."""
    try:
        os.mkdir(path, 0o700)
        created = True
    except FileExistsError:
        created = False
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ScratchError("%s is not a real directory (a symlink or a file); refusing it" % path)
    if IS_WINDOWS:
        if created:
            _owner_only_windows(path)
        return path
    if st.st_uid != os.getuid():
        raise ScratchError("%s is owned by uid %d, not this user; refusing it" % (path, st.st_uid))
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)              # mkdir's mode is masked by the umask
    return path


def ensure_root(h=None):
    r = root(h)
    parent = os.path.dirname(r)
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    return _private_dir(r)


def run_dir(run, h=None):
    if not RUN_RE.match(run or ""):
        raise ScratchError("bad run id %r" % (run,))
    return os.path.join(root(h), run)


def unit_dir(run, stage, unit, h=None, create=True):
    """-> the private folder for one unit of one stage of a run, created 0700 (every level)."""
    rd = run_dir(run, h)
    d = os.path.join(rd, "%s-%s" % (slug(stage), slug(unit)))
    if create:
        ensure_root(h)
        _private_dir(rd)
        _private_dir(d)
    return d


def _size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(base, f)).st_size
            except OSError:
                pass
    return total


def runs(h=None):
    """-> [{run, path, units, bytes}] for every run with a scratch folder."""
    r = root(h)
    out = []
    try:
        names = sorted(os.listdir(r))
    except OSError:
        return out
    for n in names:
        p = os.path.join(r, n)
        if not RUN_RE.match(n) or os.path.islink(p) or not os.path.isdir(p):
            continue
        try:
            units = sorted(os.listdir(p))
        except OSError:
            units = []
        out.append({"run": n, "path": p, "units": units, "bytes": _size(p)})
    return out


def _rmtree(path):
    def retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(p)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def cleanup(run, h=None):
    """Remove a run's scratch. -> True when something was removed. Never follows a symlink."""
    rd = run_dir(run, h)
    if os.path.islink(rd):
        os.unlink(rd)
        return True
    if not os.path.isdir(rd):
        return False
    _rmtree(rd)
    return not os.path.exists(rd)


SPOOL = ("Projects", "golden-thread", "spool", "pipeline")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def run_finished(vault, run):
    """True when a pipeline run is over -- or gone -- so scratch still there is a leak. Read
    from the run's spool files directly (gt_ingest_pipeline is not installed beside gt_doctor):
    an ingest is over once `draft` wrote drafted.json, a promote once plan.json waits on no
    stage."""
    d = os.path.join(vault, *SPOOL, run)
    meta = _read_json(os.path.join(d, "run.json"))
    if not isinstance(meta, dict):
        return True
    if meta.get("pipeline") == "ingest":
        return os.path.isfile(os.path.join(d, "drafted.json"))
    plan = _read_json(os.path.join(d, "plan.json"))
    return isinstance(plan, dict) and not plan.get("waiting")


def leaks(vault, h=None):
    """-> runs() entries whose run is finished or gone."""
    return [r for r in runs(h) if run_finished(vault, r["run"])]


def _configured_vault():
    """GT_VAULT, else vault_path in ~/.claude/vault-config.json (Core rule 2: named, never
    guessed)."""
    v = os.environ.get("GT_VAULT")
    if v:
        return v
    cfg = _read_json(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"))
    return (cfg or {}).get("vault_path") if isinstance(cfg, dict) else None


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def _emit(a, data, lines):
    if getattr(a, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(description="private scratch folders for gt's stage agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("root")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("path")
    p.add_argument("run")
    p.add_argument("--stage", required=True)
    p.add_argument("--unit", required=True)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("cleanup")
    p.add_argument("run")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("--vault")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "root":
            _emit(a, {"root": root()}, [root()])
        elif a.cmd == "path":
            d = unit_dir(a.run, a.stage, a.unit)
            _emit(a, {"path": d}, [d])
        elif a.cmd == "list":
            rs = runs()
            _emit(a, {"root": root(), "runs": rs},
                  ["%s  %d unit folder(s), %s" % (r["run"], len(r["units"]), _human(r["bytes"]))
                   for r in rs] or ["no scratch under %s" % root()])
        elif a.cmd == "cleanup":
            gone = cleanup(a.run)
            _emit(a, {"run": a.run, "removed": gone},
                  ["removed the scratch of %s" % a.run if gone else
                   "no scratch for %s" % a.run])
        elif a.cmd == "check":
            vault = a.vault or _configured_vault()
            if not vault:
                print("gt_scratch: no vault (pass --vault)", file=sys.stderr)
                return USAGE
            bad = leaks(vault)
            _emit(a, {"root": root(), "leaks": bad},
                  ["LEAK %s: %d unit folder(s), %s -- the run is finished or gone; remove it "
                   "with gt_ingest_pipeline.py cleanup %s" % (r["run"], len(r["units"]),
                                                             _human(r["bytes"]), r["run"])
                   for r in bad] or ["no scratch left behind under %s" % root()])
            return PROBLEM if bad else OK
    except ScratchError as exc:
        print("gt_scratch: %s" % exc, file=sys.stderr)
        return PROBLEM
    return OK


if __name__ == "__main__":
    sys.exit(main())
