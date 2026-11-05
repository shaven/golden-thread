#!/usr/bin/env python3
"""gt_disposable -- run blocked work in a disposable VM or container (0.22.0, phase 94).

A command or test run that a gate refuses on this machine can finish somewhere that is thrown away:
the work tree is COPIED into a fresh tart VM, docker or podman container (or, on Linux with nothing
else, a bwrap namespace), the command runs there, its exit code and output come back, and the guest is
destroyed. No guard is loosened. Not to be confused with gt_sandbox.py, which is sandbox MODE for
Claude's own tools.

    gt_disposable.py backends [--json]
    gt_disposable.py plan [--tree D] [--backend B] [--image I] [--network] [--max-mb N] [--name N] [--json] -- COMMAND...
    gt_disposable.py run [--tree D] [--backend B] [--image I] [--network] [--max-mb N] [--name N] [--timeout S] [--max-out N] [--dry-run] [--json] -- COMMAND...

`backends` lists tart, docker, podman and bwrap: present (on PATH) and usable (a cheap probe answers).
`plan` names the target and prints every command it would run; it runs nothing. `run --dry-run` is the
same. `run` executes. Exit codes: the command's own exit code on a run; 2 usage; 3 refused (a folder it
must not copy, no usable backend, a tree over the size cap); 124 timed out; 125 the guest could not be
set up.

WHAT IT NEVER DOES. It never mounts anything: no bind mount, no shared folder, no volume. The tree goes
in as a tar stream on stdin (bwrap: a private copy). It refuses a tree that is, contains or lies inside
~/.claude or the vault, and it skips, inside any tree, links, .git, .claude, .ssh, .aws, .gnupg, .env
files, keys, certificates and files named credentials*. The host's environment is not passed to the
guest. The guest has no network unless --network is given (tart cannot be cut off from here, and says so
in the plan). --network is recorded in the plan and honoured by the docker and podman commands; a real bwrap
run replaces the plan's argv with `_default_bwrap_cmd`, which is `--unshare-all` and never shares the network,
whatever the plan recorded. Teardown runs on success, failure, timeout and a failed boot; a teardown step that fails is
reported, not hidden.

NOT LIVE-VERIFIED (2026-10-25): no real tart VM, docker or podman container or bwrap run was made while
building this; the tests inject a recording runner. The exact `tart exec` / `docker cp -` behaviour and
the default images are from the tools' documentation.
"""
import argparse
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))

ORDER = ("tart", "docker", "podman", "bwrap")
DEFAULT_IMAGE = {"docker": "python:3.12-slim", "podman": "python:3.12-slim",
                 "tart": "ghcr.io/cirruslabs/macos-sequoia-base:latest"}
PROBE = {"docker": ["info"], "podman": ["info"], "tart": ["list"]}
SKIP_DIRS = (".git", ".claude", ".ssh", ".aws", ".gnupg")
SKIP_FILES = (".env", ".env.*", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.pem", "*.key",
              "*.p12", "*.pfx", "credentials*", ".netrc", ".npmrc", ".pypirc", "*.kdbx")
NEVER_COPIED = sorted(set(SKIP_DIRS) | set(SKIP_FILES) | {"symbolic links", "~/.claude", "the vault"})
DEFAULT_MAX_MB = 200


class TooBig(Exception):
    pass


# ------------------------------------------------------------------------------------ running
class SubRunner:
    """The real runner: argv only (no shell string), a time limit, the process group killed on timeout."""

    def run(self, cmd, stdin=None, timeout=60.0):
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=(os.name != "nt"))
        except OSError as exc:
            return {"rc": 127, "out": b"", "err": ("could not start: %s" % exc.__class__.__name__).encode(),
                    "timed_out": False}
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
            return {"rc": proc.returncode, "out": out, "err": err, "timed_out": False}
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:                                # noqa: BLE001
                out, err = b"", b""
            return {"rc": -9, "out": out, "err": err, "timed_out": True}

    def start(self, cmd):
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)


def _bwrap_ok():
    sys.path.insert(0, HERE)
    try:
        import gt_sandbox_launch
        return gt_sandbox_launch._bwrap_usable()
    except Exception:                                        # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------- backends
def detect(which=shutil.which, runner=None, bwrap_ok=_bwrap_ok, probe=True):
    """One row per backend, in preference order: {name, present, usable}. `probe` False never runs
    anything (the doctor row), so usable then only means present."""
    runner = runner or SubRunner()
    rows = []
    for name in ORDER:
        if name == "bwrap":
            present = bool(which("bwrap")) and sys.platform.startswith("linux")
            usable = present and (bwrap_ok() if probe else True)
        else:
            present = bool(which(name))
            usable = present
            if present and probe:
                try:
                    usable = runner.run([name] + PROBE[name], timeout=15.0)["rc"] == 0
                except Exception:                            # noqa: BLE001
                    usable = False
        rows.append({"name": name, "present": present, "usable": usable})
    return rows


def choose(rows, want="auto"):
    """The backend to use: the requested one if usable, else (auto) the first usable; None if none."""
    for r in rows:
        if r["usable"] and want in ("auto", r["name"]):
            return r["name"]
    return None


def doctor_row(rows):
    have = ["%s (%s)" % (r["name"], "usable" if r["usable"] else "installed, not answering")
            for r in rows if r["present"]]
    if any(r["usable"] for r in rows):
        return "note", "blocked work can run in a disposable guest: " + ", ".join(have), ""
    return ("note", "no disposable backend here" + ((": " + ", ".join(have)) if have else "")
            + "; blocked work can only run on this machine",
            "install tart (macOS), docker or podman, then `gt_disposable.py backends`")


# ----------------------------------------------------------------------------------- the tree
def _vault():
    v = os.environ.get("GT_VAULT")
    if not v:
        try:
            v = json.loads((Path.home() / ".claude" / "vault-config.json").read_text()).get("vault_path")
        except Exception:                                    # noqa: BLE001
            v = None
    return Path(v) if v else None


def _inside(p, root):
    p, root = os.path.realpath(str(p)), os.path.realpath(str(root))
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def refusal(tree, claude_dir=None, vault=None):
    """Why this tree must not be copied out, or "" when it may."""
    claude_dir = Path(claude_dir) if claude_dir else Path.home() / ".claude"
    tree = Path(tree)
    if not tree.is_dir():
        return "%s is not a folder" % tree
    if _inside(claude_dir, tree):
        return "%s is or contains ~/.claude (credentials, settings)" % tree
    if _inside(tree, claude_dir):
        return "%s lies inside ~/.claude" % tree
    if vault and _inside(tree, vault):
        return "%s is or lies inside the vault" % tree
    return ""


def _default_exclude():
    ex = [str(Path.home() / ".claude")]
    v = _vault()
    if v:
        ex.append(str(v))
    return ex


def collect(tree, exclude=None, max_bytes=None):
    """(files, skipped): files is [(absolute, relative)]; links, secret-shaped names and excluded
    paths are skipped and named. Raises TooBig past max_bytes."""
    tree = Path(tree)
    exclude = _default_exclude() if exclude is None else exclude
    files, skipped, total = [], [], 0
    for dirpath, dirs, names in os.walk(str(tree), followlinks=False):
        keep = []
        for d in sorted(dirs):
            full = os.path.join(dirpath, d)
            rel = os.path.relpath(full, str(tree)).replace(os.sep, "/")
            if os.path.islink(full):
                skipped.append(rel)
            elif d in SKIP_DIRS or any(_inside(full, e) for e in exclude):
                skipped.append(rel + "/")
            else:
                keep.append(d)
        dirs[:] = keep
        for n in sorted(names):
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, str(tree)).replace(os.sep, "/")
            if os.path.islink(full) or not os.path.isfile(full):
                skipped.append(rel)
            elif any(fnmatch.fnmatch(n, p) for p in SKIP_FILES) or any(_inside(full, e) for e in exclude):
                skipped.append(rel)
            else:
                total += os.path.getsize(full)
                if max_bytes is not None and total > max_bytes:
                    raise TooBig("the tree is over %d bytes" % max_bytes)
                files.append((full, rel))
    return files, skipped


def tar_bytes(files, prefix):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for full, rel in files:
            try:
                ti = tf.gettarinfo(full, arcname="%s/%s" % (prefix, rel))
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                with open(full, "rb") as fh:
                    tf.addfile(ti, fh)
            except OSError:
                continue
    return buf.getvalue()


# ------------------------------------------------------------------------------------- plan
def make_plan(backend, tree, command, image=None, network=False, name=None, exclude=None, max_bytes=None):
    name = name or "gt-disp-" + uuid.uuid4().hex[:8]
    files, skipped = collect(tree, exclude, max_bytes)
    image = (image or DEFAULT_IMAGE.get(backend)) if backend != "bwrap" else None
    command = list(command)
    if backend in ("docker", "podman"):
        create = [backend, "create", "--name", name, "-w", "/work", "--cap-drop", "ALL",
                  "--security-opt", "no-new-privileges", "--pids-limit", "512"]
        if not network:
            create += ["--network", "none"]
        steps = [("create", create + [image] + command),
                 ("copy", [backend, "cp", "-", "%s:/" % name]),
                 ("run", [backend, "start", "-a", name])]
        teardown = [("remove", [backend, "rm", "-f", name])]
    elif backend == "tart":
        steps = [("clone", ["tart", "clone", image, name]),
                 ("boot", ["tart", "run", name, "--no-graphics"]),
                 ("boot", ["tart", "ip", name, "--wait", "120"]),
                 ("copy", ["tart", "exec", "-i", name, "sh", "-c", "tar -x -C /tmp"]),
                 ("run", ["tart", "exec", name, "sh", "-c", 'cd /tmp/work && exec "$0" "$@"'] + command)]
        teardown = [("stop", ["tart", "stop", name]), ("delete", ["tart", "delete", name])]
    elif backend == "bwrap":
        steps = [("run", ["bwrap", "--unshare-all"] + (["--share-net"] if network else [])
                  + ["--bind", "<stage>", "/work", "--chdir", "/work", "--"] + command)]
        teardown = [("remove-stage", ["rm", "-rf", "<stage>"])]
    else:
        raise ValueError("unknown backend %r" % backend)
    return {"name": name, "backend": backend, "image": image, "command": command, "tree": str(tree),
            "files": len(files), "skipped": skipped, "network": "open" if network or backend == "tart" else "none",
            "steps": [{"label": l, "argv": a} for l, a in steps],
            "teardown": [{"label": l, "argv": a} for l, a in teardown],
            "never_copied": NEVER_COPIED, "mounts": []}


# --------------------------------------------------------------------------------- execution
def _default_bwrap_cmd(argv, stage):
    sys.path.insert(0, HERE)
    import gt_sandbox_launch
    base = gt_sandbox_launch._bwrap_cmd(shutil.which("bwrap"), [], [])[:-1]    # drop the trailing "--"
    return base + ["--bind", stage, "/work", "--chdir", "/work", "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
                   "--"] + list(argv)


def _fail(res, label, r):
    res["rc"] = r["rc"] if r["rc"] not in (0, None) and r["rc"] > 0 else 125
    res["err"] = r["err"]
    res["error"] = "%s failed (rc %s): %s" % (label, r["rc"], r["err"].decode("utf-8", "replace")[:200].strip())


def execute(plan, tree, runner=None, exclude=None, timeout=600.0, max_out=1048576, max_bytes=None,
            bwrap_cmd=None):
    """Run a plan: copy in, run, copy nothing back but exit code and output, teardown always."""
    runner = runner or SubRunner()
    be = plan["backend"]
    res = {"rc": 0, "out": b"", "err": b"", "backend": be, "name": plan["name"], "isolated": True,
           "timed_out": False, "truncated": False, "torn_down": False, "notes": [], "error": ""}
    started, stage = [], None
    try:
        files, _ = collect(tree, exclude, max_bytes)
        steps = plan["steps"]
        if be == "bwrap":
            stage = tempfile.mkdtemp(prefix="gt-disposable-")
            for full, rel in files:
                dest = os.path.join(stage, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(full, dest)
            steps = [{"label": "run", "argv": (bwrap_cmd or _default_bwrap_cmd)(plan["command"], stage)}]
        for s in steps:
            label, argv = s["label"], list(s["argv"])
            if argv[:2] == ["tart", "run"]:
                started.append(runner.start(argv))
                continue
            stdin = tar_bytes(files, "work") if label == "copy" else None
            r = runner.run(argv, stdin=stdin, timeout=timeout if label == "run" else
                           (3600.0 if label == "clone" else 300.0))
            if label == "run":
                res["rc"], res["timed_out"] = r["rc"], r.get("timed_out", False)
                res["out"], res["err"] = r["out"][:max_out], r["err"][:8192]
                res["truncated"] = len(r["out"]) > max_out
                if res["timed_out"]:
                    res["rc"], res["error"] = 124, "the command timed out after %ss" % timeout
            elif r["rc"] != 0:
                _fail(res, label, r)
                break
    except TooBig as exc:
        res["rc"], res["error"] = 3, str(exc)
    except Exception as exc:                                 # noqa: BLE001
        res["rc"], res["error"] = 125, "%s raised %s" % (plan["name"], exc.__class__.__name__)
    finally:
        ok = True
        for t in plan["teardown"]:
            argv = list(t["argv"])
            if be == "bwrap":
                continue                                     # the stage is removed below
            try:
                r = runner.run(argv, timeout=300.0)
                if r["rc"] != 0:
                    ok = False
                    res["notes"].append("teardown step %s failed (rc %s)" % (t["label"], r["rc"]))
            except Exception as exc:                         # noqa: BLE001
                ok = False
                res["notes"].append("teardown step %s raised %s" % (t["label"], exc.__class__.__name__))
            if argv[:2] == ["tart", "stop"]:
                for p in started:
                    try:
                        p.terminate()
                    except Exception:                        # noqa: BLE001
                        pass
        if stage:
            shutil.rmtree(stage, ignore_errors=True)
            if os.path.exists(stage):
                ok = False
                res["notes"].append("teardown step remove-stage failed")
        res["torn_down"] = ok
    return res


# ------------------------------------------------------------------------------------- CLI
def _common(p, run=False):
    p.add_argument("--tree", default=os.getcwd(), help="the work tree to copy in (default: the current folder)")
    p.add_argument("--backend", default="auto", choices=("auto",) + ORDER,
                   help="where to run: auto picks the first usable of tart, docker, podman, bwrap")
    p.add_argument("--image", help="container image or tart VM image (default: python:3.12-slim for "
                                   "docker and podman, a cirruslabs macOS base for tart)")
    p.add_argument("--network", action="store_true", help="give the guest a network (default: none)")
    p.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB,
                   help="refuse a tree larger than this many MB (default %d)" % DEFAULT_MAX_MB)
    p.add_argument("--name", help="name for the guest (default: gt-disp- and eight random characters)")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    if run:
        p.add_argument("--timeout", type=float, default=600.0,
                       help="seconds the command may run before it is killed (default 600)")
        p.add_argument("--max-out", type=int, default=1048576,
                       help="bytes of output returned (default 1048576)")
        p.add_argument("--dry-run", action="store_true", help="print the plan and run nothing")
    p.add_argument("command", nargs="*", help="the command to run in the guest, after --")


def _emit(obj, as_json, text):
    print(json.dumps(obj, indent=2, default=lambda b: b.decode("utf-8", "replace")) if as_json else text)


def _plan_text(p, dry):
    lines = ["%s: %s on %s%s" % ("would run" if dry else "plan", " ".join(p["command"]), p["backend"],
                                 (" (image %s)" % p["image"]) if p["image"] else ""),
             "tree %s: %d file(s) copied in, %d skipped, no mounts; network %s" % (
                 p["tree"], p["files"], len(p["skipped"]), p["network"])]
    lines += ["  %s: %s" % (s["label"], " ".join(s["argv"])) for s in p["steps"]]
    lines += ["  teardown %s: %s" % (s["label"], " ".join(s["argv"])) for s in p["teardown"]]
    return "\n".join(lines)


def main(argv=None, runner=None, which=shutil.which):
    ap = argparse.ArgumentParser(
        prog="gt_disposable.py",
        description="Run blocked work in a disposable VM or container: the tree is copied in (never "
                    "mounted), the command runs, exit code and output come back, the guest is destroyed.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{backends,plan,run}")
    pb = sub.add_parser("backends", help="list tart, docker, podman and bwrap: present and usable")
    pb.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    _common(sub.add_parser("plan", help="name the target and what would run; runs nothing"))
    _common(sub.add_parser("run", help="copy the tree in, run the command, tear down"), run=True)
    a = ap.parse_args(argv)
    as_json = getattr(a, "json", False)
    runner = runner or SubRunner()
    rows = detect(which=which, runner=runner, probe=(a.cmd != "plan"))
    if a.cmd == "backends":
        _emit(rows, as_json, "\n".join("%-7s present=%s usable=%s" % (r["name"], r["present"], r["usable"])
                                       for r in rows))
        return 0
    command = list(a.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        ap.error("give the command to run after --")
    tree = Path(a.tree).resolve()
    why = refusal(tree, vault=_vault())
    if why:
        print("gt_disposable: refused: " + why, file=sys.stderr)
        return 3
    be = a.backend
    if be == "auto" or a.cmd == "run" and not getattr(a, "dry_run", False):
        be = choose(rows, a.backend)
    if be is None:
        print("gt_disposable: no usable backend%s (see `gt_disposable.py backends`)" % (
            "" if a.backend == "auto" else " named " + a.backend), file=sys.stderr)
        return 3
    if a.name and not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,40}", a.name):
        print("gt_disposable: --name must be 3 to 41 lowercase letters, digits or hyphens", file=sys.stderr)
        return 2
    try:
        plan = make_plan(be, tree, command, image=a.image, network=a.network, name=a.name,
                         max_bytes=a.max_mb * 1024 * 1024)
    except TooBig as exc:
        print("gt_disposable: refused: %s" % exc, file=sys.stderr)
        return 3
    dry = a.cmd == "plan" or a.dry_run
    if dry:
        plan["dry_run"] = True
        plan["available"] = {r["name"]: r["usable"] for r in rows}
        _emit(plan, as_json, _plan_text(plan, True))
        return 0
    res = execute(plan, tree, runner=runner, timeout=a.timeout, max_out=a.max_out,
                  max_bytes=a.max_mb * 1024 * 1024)
    if as_json:
        print(json.dumps(res, indent=2, default=lambda b: b.decode("utf-8", "replace")))
    else:
        sys.stdout.write(res["out"].decode("utf-8", "replace"))
        sys.stderr.write(res["err"].decode("utf-8", "replace"))
        print("gt_disposable: %s, exit %s%s, %s%s" % (
            res["backend"], res["rc"], ", " + res["error"] if res["error"] else "",
            "torn down" if res["torn_down"] else "TEARDOWN INCOMPLETE: " + "; ".join(res["notes"]),
            ", output truncated" if res["truncated"] else ""), file=sys.stderr)
    if not res["torn_down"] and res["rc"] == 0:
        return 125
    return res["rc"] if 0 <= res["rc"] <= 255 else 125


if __name__ == "__main__":
    sys.exit(main())
