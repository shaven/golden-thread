#!/usr/bin/env python3
"""gt_check.py -- the validation host: modules plug deterministic CHECKERS into one place.

    gt_check.py list [--for PATH ...] [--plugin-root R] [--json]
    gt_check.py run [PATH ...] [--staged] [--repo DIR] [--plugin-root R] [--json] [--no-receipt]
                    [--vault V] [--dry-run]
    gt_check.py run --event commit-msg --message-file F [--repo DIR] [--plugin-root R] [--json]

WHAT A CHECKER IS. A module declares checkers in its module.json (`checkers`): an id, the
script that runs it, what it applies to (`globs`, `mime`, `events`), the external tools it
needs (`requires_tools`), a `timeout`, and -- optionally -- `fixes`, the files it may
PROPOSE changes to (gt_apply.py makes them; a checker never writes). gt is the host; only
the checkers are modular, because the Core-rule enforcement may never be module-owned.

THE PROTOCOL, versioned so a sandboxed extension can later return the same thing:

    stdin   {"schema": 1, "checker": ID, "event": null|"pre-commit"|"commit-msg",
             "root": SNAPSHOT_DIR, "files": [REL, ...], "message_file": PATH|null}
    stdout  {"schema": 1, "verdict": "pass"|"fail"|"cannot-check", "reason": TEXT?,
             "findings": [{"file": REL, "line": N|null, "rule": ID, "message": TEXT}],
             "proposals": [{"file": REL, "diff": UNIFIED_DIFF, "finding": RULE_ID,
                            "reason": TEXT}]?}

The checker runs in a READ-ONLY SNAPSHOT: its files are copied to a temp dir outside the
repo and the vault, and the checker's cwd is that dir. Every byte of the snapshot is hashed
before and after the run; a checker that changes, adds or removes anything there has
violated the contract, and its verdict is `fail` whatever it printed. That is what makes "a
checker modified a file" detectable without false alarms from other live sessions editing
the real tree at the same moment.

`cannot-check` NEVER COUNTS AS A PASS. A required tool that is not on PATH, a timeout (the
checker's whole process group is killed), a crash, or output that is not the result shape
above is `cannot-check`, with the reason, and the run exits non-zero.

PARALLEL within the machine's worker budget: `gt_settings.parallel_jobs` decides, so
`parallel_max 1` and `parallel_work off` both run one checker at a time.

RECEIPT. `run` records one row in ~/.claude/golden-thread/check-runs.jsonl (via
gt_test_receipt.py, the test-run receipt's sibling): every file, the sha256 of the content
each checker saw, and each checker's verdict. With the `commit_checks` setting on, the commit
guard refuses a commit whose staged content no passing receipt covers. `--staged` checks the
INDEX, not the working tree, so the receipt hashes are exactly what the commit will carry.

FIX PROPOSALS a checker returns are handed to gt_apply.py, which honours `addon_fixes`.

Exit: 0 every applicable checker passed | 1 a checker failed or could not check | 2 usage |
3 nothing applicable -- no checker ran, which is never the same as a clean check.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import hashlib
import importlib.util
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SCHEMA = 1
VERDICTS = ("pass", "fail", "cannot-check")
DEFAULT_TIMEOUT = 30
MAX_OUTPUT = 1 << 20
EVENTS = ("pre-commit", "commit-msg")
VIOLATION_RULE = "gt-check/read-only-violation"
OK, FAILED, USAGE, NOTHING = 0, 1, 2, 3


# ------------------------------------------------------------------ helpers ----

def _load(name):
    """Import a sibling script (this dir first, then the installed hooks dir)."""
    for d in (HERE, Path.home() / ".claude" / "golden-thread" / "hooks"):
        p = d / ("%s.py" % name)
        if p.is_file():
            spec = importlib.util.spec_from_file_location("gt_check_" + name, str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


def clean(text, limit=300):
    """Checker output is untrusted: no control characters, no ANSI, bounded length."""
    if not isinstance(text, str):
        text = str(text)
    out = "".join(ch if (ch.isprintable() and unicodedata.category(ch) not in ("Cf", "Co", "Cn"))
                  else " " for ch in text)
    return out if len(out) <= limit else out[:limit - 1] + "…"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str | None:
    try:
        with open(path, "rb") as fh:
            return sha256_bytes(fh.read())
    except OSError:
        return None


def tree_state(root: Path) -> dict:
    """{rel: sha256 or 'dir'} for everything under root -- the snapshot fingerprint."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.relpath(os.path.join(dirpath, d), root).replace(os.sep, "/")] = "dir"
        for f in filenames:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                out[os.path.relpath(p, root).replace(os.sep, "/")] = "link:" + os.readlink(p)
            else:
                out[os.path.relpath(p, root).replace(os.sep, "/")] = sha256_file(p)
    return out


# ------------------------------------------------------------------ discovery ----

def plugin_root(explicit=None, home=None):
    if explicit:
        return str(explicit)
    if os.environ.get("GT_PLUGIN_ROOT"):
        return os.environ["GT_PLUGIN_ROOT"]
    gc = _load("gt_components")
    return gc.plugin_root_from_settings(home) if gc else None


def _manifest_row(version_dir, rel):
    try:
        with open(os.path.join(version_dir, "MANIFEST.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    row = (data.get("files") or {}).get(rel) if isinstance(data, dict) else None
    if isinstance(row, dict):
        return row.get("sha256")
    return row if isinstance(row, str) else None


def first_party(version_dir, script_rel) -> bool:
    """A checker is FIRST-PARTY when its script is byte-for-byte what the release's MANIFEST
    says it shipped. Anything else -- edited after install, added by hand, a future
    extension -- is not, and gt_apply.py caps it at `propose`."""
    want = _manifest_row(version_dir, script_rel)
    return bool(want) and want == sha256_file(os.path.join(version_dir, script_rel))


def installed_checkers(root=None, home=None):
    """-> [checker dict] for every checker of every module that is ON. Never raises."""
    gc = _load("gt_components")
    root = plugin_root(root, home)
    if gc is None or not root:
        return []
    try:
        mods = gc.active_modules(root, home)
    except Exception:
        return []
    out = []
    for name, vd, data in mods:
        for c in data.get("checkers") or []:
            if not isinstance(c, dict) or not c.get("id") or not c.get("script"):
                continue
            rel = "scripts/" + c["script"]
            tools = list(c.get("requires_tools") or [])
            out.append({
                "key": "%s/%s" % (name, c["id"]), "module": name, "id": c["id"],
                "version_dir": vd, "script": os.path.join(vd, rel),
                "globs": list(c.get("globs") or []), "mime": list(c.get("mime") or []),
                "events": list(c.get("events") or []), "requires_tools": tools,
                "missing_tools": [t for t in tools if not shutil.which(t)],
                "timeout": int(c.get("timeout") or DEFAULT_TIMEOUT),
                "fixes": list(c.get("fixes") or []), "rules": list(c.get("rules") or []),
                "summary": c.get("summary") or "",
                "first_party": first_party(vd, rel),
            })
    return sorted(out, key=lambda c: c["key"])


def applies(checker, rel, abspath=None) -> bool:
    """Does this checker apply to this file? Globs match the relative path or the basename;
    a MIME type matches the guessed type of the name."""
    base = os.path.basename(rel)
    for g in checker["globs"]:
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(base, g):
            return True
    if checker["mime"]:
        mt = mimetypes.guess_type(abspath or rel)[0]
        if mt and mt in checker["mime"]:
            return True
    return False


def for_files(checkers, rels, event=None):
    """-> [(checker, [rel, ...])] for a file run. A checker bound only to commit-msg never
    runs on files; with event pre-commit, a checker with events must list it."""
    plan = []
    for c in checkers:
        if not (c["globs"] or c["mime"]):
            continue
        if event == "pre-commit" and c["events"] and "pre-commit" not in c["events"]:
            continue
        hit = [r for r in rels if applies(c, r)]
        if hit:
            plan.append((c, hit))
    return plan


# ------------------------------------------------------------------ running ----

def _command(script):
    if script.endswith(".py"):
        return [sys.executable, script]
    if script.endswith(".sh"):
        return ["bash", script]
    return [script]


def validate_result(data, rels):
    """-> (result, None) or (None, why) for a checker's parsed stdout."""
    if not isinstance(data, dict):
        return None, "output is not a JSON object"
    if data.get("schema") != SCHEMA:
        return None, "schema is %r, the host reads %d" % (data.get("schema"), SCHEMA)
    v = data.get("verdict")
    if v not in VERDICTS:
        return None, "verdict %r is not one of %s" % (v, "/".join(VERDICTS))
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        return None, "findings is not a list"
    out = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            return None, "findings[%d] is not an object" % i
        for k in ("file", "rule", "message"):
            if not isinstance(f.get(k), str):
                return None, "findings[%d].%s is missing or not a string" % (i, k)
        line = f.get("line")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
            return None, "findings[%d].line is not a whole number" % i
        out.append({"file": f["file"], "line": line, "rule": clean(f["rule"], 80),
                    "message": clean(f["message"])})
    props = data.get("proposals", [])
    if not isinstance(props, list):
        return None, "proposals is not a list"
    for i, p in enumerate(props):
        if not (isinstance(p, dict) and isinstance(p.get("file"), str)
                and isinstance(p.get("diff"), str)):
            return None, "proposals[%d] needs a string file and diff" % i
    return {"verdict": v, "reason": clean(data.get("reason") or "", 200), "findings": out,
            "proposals": props}, None


def run_one(checker, rels, source, event=None, message_file=None):
    """Run one checker over `rels` (content read from `source(rel)` -> bytes).

    -> {checker, verdict, reason, findings, proposals, files: {rel: sha256}, elapsed,
        started, ended}
    """
    res = {"checker": checker["key"], "module": checker["module"], "verdict": "cannot-check",
           "reason": "", "findings": [], "proposals": [], "files": {}, "started": time.time()}
    if checker["missing_tools"]:
        res["reason"] = "required tool(s) not on PATH: %s" % ", ".join(checker["missing_tools"])
        res["ended"] = time.time()
        return res
    snap = Path(tempfile.mkdtemp(prefix="gt-check-"))
    try:
        for rel in rels:
            data = source(rel)
            if data is None:
                continue
            dst = snap / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            res["files"][rel] = sha256_bytes(data)
        msg = None
        if message_file:
            msg = snap / ".gt-commit-msg"
            try:
                msg.write_bytes(Path(message_file).read_bytes())
            except OSError as exc:
                res["reason"] = "message file unreadable (%s)" % exc.__class__.__name__
                return res
        before = tree_state(snap)
        req = {"schema": SCHEMA, "checker": checker["id"], "event": event, "root": str(snap),
               "files": sorted(res["files"]), "message_file": str(msg) if msg else None}
        try:
            proc = subprocess.Popen(_command(checker["script"]), cwd=str(snap),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True,
                                    env=dict(os.environ, GT_CHECK_ROOT=str(snap)))
        except OSError as exc:
            res["reason"] = "could not start (%s)" % exc.__class__.__name__
            return res
        try:
            out, err = proc.communicate(json.dumps(req).encode("utf-8"),
                                        timeout=checker["timeout"])
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    # No process groups on Windows (os.killpg does not exist there): kill the
                    # checker's whole tree, or a child holding the pipes open hangs us (0.20.1).
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                                   capture_output=True, timeout=30)
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                proc.kill()
            proc.communicate()
            res["reason"] = "timed out after %ds and was killed" % checker["timeout"]
            return res
        after = tree_state(snap)
        try:
            data = json.loads(out[:MAX_OUTPUT].decode("utf-8", "replace"))
        except ValueError:
            data = None
        if data is None:
            errl = (err or b"").decode("utf-8", "replace").strip().splitlines()
            tail = clean(errl[-1], 160) if errl else ""
            res["reason"] = ("output is not the result shape (not JSON; exit %d%s)"
                             % (proc.returncode, (": " + tail) if tail else ""))
        else:
            parsed, why = validate_result(data, rels)
            if why:
                res["reason"] = "output is not the result shape: %s" % why
            else:
                res.update(parsed)
        if after != before:
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            res["verdict"] = "fail"
            res["reason"] = "read-only violation: the checker changed its input"
            res["proposals"] = []
            res["findings"].append({"file": changed[0] if changed else "", "line": None,
                                    "rule": VIOLATION_RULE,
                                    "message": "checker modified %d path(s) it was only "
                                               "allowed to read: %s"
                                               % (len(changed), clean(", ".join(changed[:5]), 200))})
        return res
    finally:
        res["ended"] = time.time()
        shutil.rmtree(snap, ignore_errors=True)


def run_all(plan, source, event=None, message_file=None):
    """Run every (checker, rels) concurrently within the worker budget. Order preserved."""
    if not plan:
        return []
    gs = _load("gt_settings")
    try:
        workers = gs.parallel_jobs(len(plan), io_bound=True) if gs else 1
    except Exception:
        workers = 1
    if workers <= 1:
        return [run_one(c, r, source, event, message_file) for c, r in plan]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(run_one, c, r, source, event, message_file) for c, r in plan]
        return [f.result() for f in futs]


# ------------------------------------------------------------------ sources ----

def git_root(path):
    try:
        p = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=15)
        if p.returncode == 0:
            return os.path.realpath(p.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def staged_files(root):
    """-> [rel] added/copied/modified in the index (deletions carry no content to check)."""
    p = subprocess.run(["git", "-C", root, "diff", "--cached", "--name-only",
                        "--diff-filter=ACM", "-z"], capture_output=True, text=True)
    return [r for r in p.stdout.split("\0") if r] if p.returncode == 0 else []


def staged_reader(root):
    def read(rel):
        p = subprocess.run(["git", "-C", root, "show", ":" + rel], capture_output=True)
        return p.stdout if p.returncode == 0 else None
    return read


def tree_reader(root):
    def read(rel):
        p = rel if os.path.isabs(rel) else os.path.join(root, rel)
        try:
            with open(p, "rb") as fh:
                return fh.read()
        except OSError:
            return None
    return read


def resolve_paths(paths, root):
    """User paths -> [rel to root]. A directory expands to its files (no .git)."""
    out = []
    for p in paths:
        a = os.path.realpath(p)
        cands = []
        if os.path.isdir(a):
            for dp, dns, fns in os.walk(a):
                dns[:] = [d for d in dns if d not in (".git", "__pycache__", "node_modules")]
                cands += [os.path.join(dp, f) for f in sorted(fns)]
        else:
            cands = [a]
        for c in cands:
            rel = os.path.relpath(c, root).replace(os.sep, "/")
            out.append(c if rel.startswith("..") else rel)
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ commands ----

def cmd_list(a):
    checkers = installed_checkers(a.plugin_root)
    if a.paths:
        root = os.path.realpath(a.repo or os.getcwd())
        rels = resolve_paths(a.paths, git_root(root) or root)
        checkers = [c for c in checkers if any(applies(c, r) for r in rels)]
    if a.json:
        print(json.dumps({"checkers": [{k: v for k, v in c.items() if k != "version_dir"}
                                       for c in checkers]}, indent=2))
        return OK
    if not checkers:
        print("no installed checker%s. Checkers come from modules (module.json `checkers`); "
              "`./install.sh --list-modules` shows which modules are on."
              % (" applies to those files" if a.paths else ""))
        return OK
    for c in checkers:
        to = []
        if c["globs"]:
            to.append("globs " + " ".join(c["globs"]))
        if c["mime"]:
            to.append("mime " + " ".join(c["mime"]))
        if c["events"]:
            to.append("events " + " ".join(c["events"]))
        tools = ("tools ok" if not c["missing_tools"]
                 else "MISSING TOOL(S): %s" % ", ".join(c["missing_tools"]))
        if not c["requires_tools"]:
            tools = "no tools required"
        print("%-28s module %-14s %s; %s%s" % (c["key"], c["module"], "; ".join(to), tools,
                                             "" if c["first_party"] else "; not first-party"))
    print("%d checker(s) installed" % len(checkers))
    return OK


def summarise(results):
    ran = len(results)
    bad = [r for r in results if r["verdict"] != "pass"]
    return ran, bad


def print_report(results, as_json, extra=None):
    if as_json:
        print(json.dumps(dict({"results": results}, **(extra or {})), indent=2))
        return
    for r in results:
        mark = {"pass": "PASS", "fail": "FAIL", "cannot-check": "CANNOT-CHECK"}[r["verdict"]]
        print("%-13s %-28s %d file(s)%s" % (mark, r["checker"], len(r["files"]),
                                           ("  -- " + r["reason"]) if r["reason"] else ""))
        for f in r["findings"]:
            loc = f["file"] + (":%d" % f["line"] if isinstance(f.get("line"), int) else "")
            print("    %s  %s  %s" % (clean(loc, 120), f["rule"], f["message"]))


def cmd_run(a):
    if a.event == "commit-msg" and not a.message_file:
        print("--event commit-msg needs --message-file F", file=sys.stderr)
        return USAGE
    base = os.path.realpath(a.repo or os.getcwd())
    root = git_root(base) or base
    checkers = installed_checkers(a.plugin_root)
    staged = a.staged or a.event == "pre-commit"
    if a.event == "commit-msg":
        plan = [(c, []) for c in checkers if "commit-msg" in c["events"]]
        rels, source = [], tree_reader(root)
    else:
        if staged:
            if not git_root(base):
                print("--staged needs a git repository (%s is not one)" % base, file=sys.stderr)
                return USAGE
            rels, source = staged_files(root), staged_reader(root)
        else:
            if not a.paths:
                print("name the files to check, or pass --staged", file=sys.stderr)
                return USAGE
            rels, source = resolve_paths(a.paths, root), tree_reader(root)
        plan = for_files(checkers, rels, a.event)
    if a.dry_run:
        for c, r in plan:
            print("would run %-28s on %d file(s)" % (c["key"], len(r) if r else 0))
        print("%d checker(s) would run; nothing written" % len(plan))
        return OK if plan else NOTHING
    results = run_all(plan, source, a.event, a.message_file)
    ran, bad = summarise(results)
    ok = ran > 0 and not bad

    # The receipt names every file considered -- including those no checker applies to, so
    # the commit guard can tell "checked, nothing applied" from "never checked".
    if not a.no_receipt and a.event != "commit-msg":
        rcpt = _load("gt_test_receipt")
        if rcpt is not None:
            files = {}
            for rel in rels:
                data = source(rel)
                if data is None:
                    continue
                files[rel] = {"sha256": sha256_bytes(data), "checkers": {}}
            for r in results:
                for rel in r["files"]:
                    files.setdefault(rel, {"sha256": r["files"][rel], "checkers": {}})
                    if files[rel]["sha256"] == r["files"][rel]:
                        files[rel]["checkers"][r["checker"]] = r["verdict"]
            try:
                rcpt.record_check(root, files, ok=not bad, event=a.event or
                                  ("pre-commit" if staged else "files"))
            except Exception as exc:                       # noqa: BLE001
                print("gt_check: receipt NOT recorded (%s)" % exc, file=sys.stderr)

    extra = {}
    props = [(r, p) for r in results for p in r.get("proposals") or []]
    if props:
        ap_mod = _load("gt_apply")
        if ap_mod is None:
            print("gt_check: %d fix proposal(s) ignored: gt_apply.py is not installed"
                  % len(props), file=sys.stderr)
        else:
            by_key = {c["key"]: c for c in checkers}
            extra["proposals"] = ap_mod.intake(results, by_key, root, staged=staged,
                                               vault=a.vault, as_json=a.json,
                                               plugin_root=plugin_root(a.plugin_root))
    print_report(results, a.json, extra)
    if not a.json:
        if not plan:
            print("no installed checker applies -- nothing was checked")
        else:
            print("%d checker(s) ran: %d passed, %d failed, %d could not check"
                  % (ran, sum(r["verdict"] == "pass" for r in results),
                     sum(r["verdict"] == "fail" for r in results),
                     sum(r["verdict"] == "cannot-check" for r in results)))
    if not plan:
        return NOTHING
    return OK if ok else FAILED


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_check.py", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--plugin-root", help="plugin source (default: read from settings.json)")
    common.add_argument("--repo", help="the repository or directory (default: cwd)")
    common.add_argument("--json", action="store_true")
    common.add_argument("--vault", help="vault for fix proposals (default: $GT_VAULT, config)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", parents=[common], help="installed checkers")
    ls.add_argument("--for", dest="paths", nargs="+", default=[],
                    help="only checkers that apply to these files")
    rn = sub.add_parser("run", parents=[common], help="run the checkers that apply")
    rn.add_argument("paths", nargs="*")
    rn.add_argument("--staged", action="store_true", help="check the index (what a commit carries)")
    rn.add_argument("--event", choices=EVENTS)
    rn.add_argument("--message-file", help="the commit message file (--event commit-msg)")
    rn.add_argument("--no-receipt", action="store_true", help="do not record a receipt")
    rn.add_argument("--dry-run", action="store_true", help="say what would run; run nothing")
    a = ap.parse_args(argv)
    return cmd_list(a) if a.cmd == "list" else cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())
