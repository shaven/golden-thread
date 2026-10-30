#!/usr/bin/env python3
"""gt_farm_workspace.py -- a sandboxed scratch copy of a repo and the tools an external coding agent may use on it
(gt 0.22.0, gt-farm 0.21.3, agent farming phase B).

    gt_farm_workspace.py check          # which sandbox backend is usable here, in plain words

    as a module:
      make_workspace(src_dir, dest_dir, *, max_bytes=200_000_000) -> {"workspace", "files", "skipped", "bytes"}
      list_dir(workspace, args)   args {"path": "."}                       -> str
      read_file(workspace, args)  args {"path", "offset": 0}               -> str
      write_file(workspace, args, read_only=())   args {"path", "content"} -> str
      run_command(workspace, args, *, timeout=30.0, max_out=20000, allow_unisolated=False, env=None) -> str
                                  args {"command"}
      take_patch(workspace) -> str          unified diff of everything changed since the baseline
      changed_files(workspace) -> [str]     paths changed since the baseline
      fresh_copy(workspace, dest, *, patch=None, with_patch=True) -> {"workspace", "patch", "files"}
                                  the baseline commit in a NEW folder, plus the workspace's patch applied; discard_copy(dest)

WHAT IT GUARANTEES. The agent works on a COPY. The copy leaves out links, .git, .claude, .ssh, .aws, .gnupg, .env
files, keys, certificates, credentials*, node_modules and caches, and the copy is refused outright for a tree that is,
contains or lies inside ~/.claude or the vault. Every path an agent names is resolved with realpath and must stay
inside the workspace (../, absolute paths and links that point out are refused). Every tool's result is capped and
returned as a string; a refusal is a string beginning "error:", never an exception. write_file refuses .git and
the paths the caller marks read-only. take_patch and changed_files leave out caches (EXTRA_SKIP_DIRS such as
__pycache__, and *.pyc / *.pyo): running the tests is not a change. When a command fails with exit 126 or "Operation
not permitted" (typically an interpreter under the home folder, such as a virtualenv's python, which the sandbox
cannot read), the result ends with HOME_INTERPRETER_HINT: use a system or package-manager Python, or install the
dependencies into one the sandbox can read. run_command goes through gt_sandbox_launch.run with write_paths = the
workspace and a private temp folder: no network, no reads of the home folder or other temp folders, no writes
anywhere else. When no isolating backend exists it REFUSES to run unless allow_unisolated=True is passed.

THE BASELINE. make_workspace commits the copy in a git directory kept OUTSIDE the workspace (`<workspace>.gt-git`),
so the agent, which can write the workspace, cannot touch the repository the host later runs `git diff` against
(a planted hook or config in a .git inside the workspace would otherwise run on the host). The commit is tagged
gt-baseline. The agent therefore sees no .git and cannot commit.

Exit codes (check): 0 an isolating backend is usable | 3 none is (commands would run unisolated) | 2 usage or gt core not found.
"""
import fnmatch
import os
import io
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

GT_VERSION = "0.21.3"            # the gt-farm module version
HERE = os.path.dirname(os.path.abspath(__file__))

MAX_TOOL_OUT = 40000             # chars any tool returns
MAX_READ_BYTES = 2000000         # a file larger than this is not read
MAX_WRITE_BYTES = 1000000        # a file larger than this is not written
MAX_PATCH_BYTES = 4000000
EXTRA_SKIP_DIRS = ("node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv",
                   ".tox", ".cache", ".gradle", ".next", ".parcel-cache")
FALLBACK_SKIP_DIRS = (".git", ".claude", ".ssh", ".aws", ".gnupg")
FALLBACK_SKIP_FILES = (".env", ".env.*", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.pem", "*.key",
                       "*.p12", "*.pfx", "credentials*", ".netrc", ".npmrc", ".pypirc", "*.kdbx")
ALWAYS_READ_ONLY = (".git", ".gt-git")
BASELINE_TAG = "gt-baseline"


# Said back to the model (and so to the person reading the run) when the sandbox could not start a program.
HOME_INTERPRETER_HINT = (
    "[hint] The sandbox could not run that program. This usually means it lives under your home folder (for example "
    "a virtualenv's python), which sandboxed commands may not read. Use a Python from the system or a package manager "
    "(for example /usr/bin/python3), or install the project's dependencies into one the sandbox can read.")


class WorkspaceError(Exception):
    """The workspace could not be made, or a baseline operation failed."""


# ------------------------------------------------------------------------------------ gt core
def _core(name):
    """A gt core module by name, or None. Looks where the other farm scripts look."""
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import gt_farm_calibration as cal
        mod = cal.core_module(name)
        if mod is not None:
            return mod
    except Exception:                                        # noqa: BLE001 - a missing piece is a state
        pass
    cands = [os.environ.get("GT_CORE_SCRIPTS")]
    root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    base = os.path.join(root, "golden-thread")
    try:
        cands += [os.path.join(base, v, "scripts") for v in sorted(os.listdir(base), reverse=True)]
    except OSError:
        pass
    for c in cands:
        if c and os.path.isfile(os.path.join(c, name + ".py")):
            if c not in sys.path:
                sys.path.append(c)
            try:
                return __import__(name)
            except Exception:                                # noqa: BLE001
                return None
    return None


def _inside(p, root):
    p, root = os.path.realpath(str(p)), os.path.realpath(str(root))
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


# ------------------------------------------------------------------------------------ the copy
def _tree_refusal(src):
    disp = _core("gt_disposable")
    vault = disp._vault() if disp is not None and hasattr(disp, "_vault") else None
    if disp is not None and hasattr(disp, "refusal"):
        why = disp.refusal(src, vault=vault)
        v = vault or os.environ.get("GT_VAULT")
        if not why and v and _inside(v, src):
            return "%s contains the vault" % src
        return why
    home_claude = os.path.join(os.path.expanduser("~"), ".claude")
    if not os.path.isdir(src):
        return "%s is not a folder" % src
    if _inside(home_claude, src) or _inside(src, home_claude):
        return "%s is or lies inside ~/.claude" % src
    v = os.environ.get("GT_VAULT")
    if v and (_inside(v, src) or _inside(src, v)):
        return "%s is or contains the vault" % src
    return ""


def _skip_sets():
    disp = _core("gt_disposable")
    dirs = tuple(getattr(disp, "SKIP_DIRS", FALLBACK_SKIP_DIRS)) if disp is not None else FALLBACK_SKIP_DIRS
    files = tuple(getattr(disp, "SKIP_FILES", FALLBACK_SKIP_FILES)) if disp is not None else FALLBACK_SKIP_FILES
    return set(dirs) | set(EXTRA_SKIP_DIRS), files


def _gitdir(workspace):
    return os.path.realpath(str(workspace)).rstrip(os.sep) + ".gt-git"


def _git(workspace, *args, check=True, input_=None):
    ws = os.path.realpath(str(workspace))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_DIR": _gitdir(ws), "GIT_WORK_TREE": ws,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
           "HOME": tempfile.gettempdir(), "LC_ALL": "C"}
    for k in ("SystemRoot", "TMP", "TEMP"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    cmd = ["git", "-c", "user.name=gt-farm", "-c", "user.email=gt-farm@localhost", "-c", "commit.gpgsign=false",
           "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false",
           "-c", "core.quotepath=off"] + list(args)
    try:
        p = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, timeout=120, input=input_)
    except FileNotFoundError:
        raise WorkspaceError("git was not found")
    except subprocess.TimeoutExpired:
        raise WorkspaceError("git timed out")
    if check and p.returncode != 0:
        raise WorkspaceError("git %s failed: %s" % (args[0], p.stderr.decode("utf-8", "replace").strip()[:300]))
    return p.stdout


def make_workspace(src_dir, dest_dir, *, max_bytes=200_000_000):
    """Copy `src_dir` to `dest_dir` (new or empty) without secrets, links, .git, node_modules or caches, and commit
    the copy as the baseline. Raises WorkspaceError on a refusal. -> {workspace, files, skipped, bytes}"""
    src = os.path.realpath(str(src_dir))
    why = _tree_refusal(src)
    if why:
        raise WorkspaceError("refused: " + why)
    dest = os.path.abspath(str(dest_dir))
    if _inside(dest, src):
        raise WorkspaceError("refused: the workspace may not lie inside the source tree")
    if os.path.exists(dest) and (not os.path.isdir(dest) or os.listdir(dest)):
        raise WorkspaceError("refused: %s exists and is not an empty folder" % dest)
    skip_dirs, skip_files = _skip_sets()
    extra = [os.path.join(os.path.expanduser("~"), ".claude")]
    disp = _core("gt_disposable")
    if disp is not None and hasattr(disp, "_vault") and disp._vault():
        extra.append(str(disp._vault()))
    elif os.environ.get("GT_VAULT"):
        extra.append(os.environ["GT_VAULT"])
    files, skipped, total = [], [], 0
    for dirpath, dirs, names in os.walk(src, followlinks=False):
        keep = []
        for d in sorted(dirs):
            full = os.path.join(dirpath, d)
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            if os.path.islink(full) or d in skip_dirs or any(_inside(full, e) for e in extra):
                skipped.append(rel + "/")
            else:
                keep.append(d)
        dirs[:] = keep
        for n in sorted(names):
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            if os.path.islink(full) or not os.path.isfile(full) or any(fnmatch.fnmatch(n, p) for p in skip_files):
                skipped.append(rel)
                continue
            total += os.path.getsize(full)
            if total > max_bytes:
                raise WorkspaceError("refused: the tree is over %d bytes" % max_bytes)
            files.append((full, rel))
    os.makedirs(dest, exist_ok=True)
    for full, rel in files:
        target = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(full, target)
    gd = _gitdir(dest)
    if os.path.exists(gd):
        shutil.rmtree(gd)
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "--allow-empty", "-m", "gt-farm baseline")
    _git(dest, "tag", BASELINE_TAG)
    return {"workspace": os.path.realpath(dest), "files": len(files), "skipped": skipped, "bytes": total}


def _cache_excludes():
    """Git pathspecs that leave out the cache folders the copy never took (EXTRA_SKIP_DIRS, at any depth) and compiled
    Python files. A run that merely executes the tests leaves a __pycache__ behind; it is not a change."""
    out = [":(exclude,glob)**/%s/**" % d for d in EXTRA_SKIP_DIRS]
    out += [":(exclude,glob)**/*.pyc", ":(exclude,glob)**/*.pyo"]
    return out


def take_patch(workspace):
    """The unified diff of everything changed (added, edited, deleted) since the baseline; "" when nothing. Caches
    (EXTRA_SKIP_DIRS such as __pycache__, and *.pyc / *.pyo) are not changes and never appear."""
    _git(workspace, "add", "-A", "--", ".", *_cache_excludes())
    out = _git(workspace, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", BASELINE_TAG, "--", ".",
               *_cache_excludes())
    return out[:MAX_PATCH_BYTES].decode("utf-8", "replace")


def changed_files(workspace):
    """Paths changed since the baseline, caches left out as in take_patch."""
    _git(workspace, "add", "-A", "--", ".", *_cache_excludes())
    out = _git(workspace, "diff", "--cached", "--name-only", "-z", "--no-renames", BASELINE_TAG, "--", ".",
               *_cache_excludes())
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def rmtree(path):
    """Remove a folder tree and never raise. A read-only file (git writes its objects read-only, and Windows will not
    delete one) is made writable and tried once more."""
    def retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)
    except OSError:
        pass


def discard_copy(dest):
    """Remove a fresh copy and the git directory kept beside it."""
    d = os.path.abspath(str(dest))
    rmtree(d)
    rmtree(d.rstrip(os.sep) + ".gt-git")


def _unpack_baseline(tar_bytes, dest):
    """Write the files of a `git archive` tar into `dest`. Only plain files and folders, only inside `dest`."""
    root = os.path.realpath(dest)
    n = 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
        for m in tf:
            if m.name == "pax_global_header" or m.name in (".", "./"):
                continue
            rel = m.name.replace("\\", "/").strip("/")
            segs = [x for x in rel.split("/") if x]
            if os.path.isabs(m.name) or ".." in segs or ".git" in segs:
                raise WorkspaceError("the baseline holds a path that leaves the folder: %s" % m.name)
            target = os.path.join(root, *segs)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
            elif m.isreg():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as fh:
                    shutil.copyfileobj(tf.extractfile(m), fh)
                os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)
                n += 1
            else:
                raise WorkspaceError("the baseline holds something that is not a plain file: %s" % m.name)
    return n


def fresh_copy(workspace, dest, *, patch=None, with_patch=True):
    """Make `dest` (new or empty) from the BASELINE COMMIT of `workspace` -- not from the folder the agent worked in, so
    nothing the agent left running or planted there is in it -- and, when with_patch, apply the agent's patch on top
    (`patch`, or take_patch(workspace) when None). `dest` gets a baseline of its own, so changed_files / take_patch /
    run_command work on it. Raises WorkspaceError (nothing usable is left) when the baseline cannot be read, the patch
    makes a link or a mode change, or `git apply` refuses it. -> {workspace, patch, files}"""
    d = os.path.abspath(str(dest))
    if os.path.exists(d) and (not os.path.isdir(d) or os.listdir(d)):
        raise WorkspaceError("refused: %s exists and is not an empty folder" % d)
    text = ""
    if with_patch:
        text = take_patch(workspace) if patch is None else patch
    try:
        os.makedirs(d, exist_ok=True)
        tar_bytes = _git(workspace, "archive", "--format=tar", BASELINE_TAG)
        n = _unpack_baseline(tar_bytes, d)
        gd = _gitdir(d)
        if os.path.exists(gd):
            shutil.rmtree(gd)
        _git(d, "init", "-q")
        _git(d, "add", "-A")
        _git(d, "commit", "-q", "--allow-empty", "-m", "gt-farm baseline")
        _git(d, "tag", BASELINE_TAG)
        if with_patch and text.strip():
            if re.search(r"^(old|new) mode |^(new|deleted) file mode (100755|120000)|^index [0-9a-f]+\.\.[0-9a-f]+ 120000", text, re.M):
                raise WorkspaceError("the patch makes a link or changes a file mode")
            _git(d, "apply", "--whitespace=nowarn", "-", input_=text.encode("utf-8"))
    except (WorkspaceError, OSError, tarfile.TarError) as e:
        discard_copy(d)
        if isinstance(e, WorkspaceError):
            raise
        raise WorkspaceError("the fresh copy could not be made: %s" % e)
    return {"workspace": os.path.realpath(d), "patch": text, "files": n}


# ------------------------------------------------------------------------------------ the tools
def _cap(text, limit=MAX_TOOL_OUT):
    if len(text) > limit:
        return text[:limit] + "\n[truncated: %d more characters]" % (len(text) - limit)
    return text


def _cap_ends(text, limit, head=2048):
    """`text` when it fits; otherwise its start, a marker and its END, within `limit` characters. A command's last lines
    (a test runner's summary) matter most, so unlike _cap this keeps them."""
    if len(text) <= limit:
        return text
    marker = "\n[... %d characters omitted ...]\n" % (len(text) - limit)
    room = limit - len(marker)
    if room < 8:
        return text[:limit]
    h = min(head, room // 2)
    return text[:h] + marker + text[len(text) - (room - h):]


def _resolve(workspace, rel):
    """(real path, "") when `rel` stays inside the workspace after links are followed, else (None, why)."""
    if not isinstance(rel, str) or not rel.strip():
        return None, "error: a path is required"
    if "\0" in rel:
        return None, "error: bad path"
    root = os.path.realpath(str(workspace))
    cand = rel if os.path.isabs(rel) else os.path.join(root, rel)
    real = os.path.realpath(cand)
    if not _inside(real, root):
        return None, "error: %s is outside the workspace" % rel
    return real, ""


def _rel(workspace, real):
    r = os.path.relpath(real, os.path.realpath(str(workspace))).replace(os.sep, "/")
    return "." if r == "." else r


def list_dir(workspace, args):
    real, why = _resolve(workspace, (args or {}).get("path", "."))
    if why:
        return why
    if not os.path.isdir(real):
        return "error: %s is not a folder" % (args or {}).get("path", ".")
    rows = []
    for n in sorted(os.listdir(real)):
        full = os.path.join(real, n)
        if os.path.islink(full):
            rows.append(n + "@")
        elif os.path.isdir(full):
            rows.append(n + "/")
        else:
            rows.append("%s  (%d bytes)" % (n, os.path.getsize(full)))
    return _cap("\n".join(rows) if rows else "(empty)")


def read_file(workspace, args):
    args = args or {}
    real, why = _resolve(workspace, args.get("path"))
    if why:
        return why
    if not os.path.isfile(real):
        return "error: %s is not a file" % args.get("path")
    size = os.path.getsize(real)
    if size > MAX_READ_BYTES:
        return "error: %s is %d bytes, over the %d byte limit" % (args.get("path"), size, MAX_READ_BYTES)
    with open(real, "rb") as fh:
        data = fh.read()
    if b"\0" in data[:8192]:
        return "error: %s looks binary and is not shown" % args.get("path")
    text = data.decode("utf-8", "replace")
    try:
        off = max(0, int(args.get("offset", 0)))
    except (TypeError, ValueError):
        off = 0
    if off:
        text = text[off:]
    if len(text) > MAX_TOOL_OUT:
        return text[:MAX_TOOL_OUT] + "\n[truncated: %d more characters; read again with offset=%d]" % (
            len(text) - MAX_TOOL_OUT, off + MAX_TOOL_OUT)
    return text


def _denied(rel, read_only):
    parts = rel.split("/")
    if any(p in ALWAYS_READ_ONLY for p in parts):
        return True
    for pat in read_only or ():
        pat = pat.replace(os.sep, "/").strip("/")
        if rel == pat or rel.startswith(pat + "/") or fnmatch.fnmatch(rel, pat):
            return True
    return False


def write_file(workspace, args, read_only=()):
    args = args or {}
    real, why = _resolve(workspace, args.get("path"))
    if why:
        return why
    content = args.get("content")
    if not isinstance(content, str):
        return "error: content must be text"
    rel = _rel(workspace, real)
    if rel == "." or _denied(rel, read_only):
        return "error: %s is read-only" % args.get("path")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        return "error: %d bytes is over the %d byte limit" % (len(data), MAX_WRITE_BYTES)
    if os.path.isdir(real):
        return "error: %s is a folder" % args.get("path")
    os.makedirs(os.path.dirname(real), exist_ok=True)
    # a parent folder may have been a link made after the check above; resolve again before writing
    if not _inside(os.path.realpath(os.path.dirname(real)), workspace):
        return "error: %s is outside the workspace" % args.get("path")
    with open(real, "wb") as fh:
        fh.write(data)
    return "wrote %d bytes to %s" % (len(data), rel)


def _launcher():
    return _core("gt_sandbox_launch")


def run_command(workspace, args, *, timeout=30.0, max_out=20000, allow_unisolated=False, env=None):
    """Run `args["command"]` through the platform shell inside the sandbox, working folder = the workspace. Refuses to
    run at all when the backend does not isolate, unless allow_unisolated=True."""
    cmd = (args or {}).get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return "error: a command is required"
    launch = _launcher()
    if launch is None:
        return "error: refused: the gt sandbox launcher was not found, so nothing was run"
    ws = os.path.realpath(str(workspace))
    backend = launch.available_backends()[0]
    if backend == "none" and not allow_unisolated:
        return ("error: refused: no isolating sandbox backend is usable on this machine, so the command was NOT run "
                "(pass allow_unisolated=True to run it without isolation)")
    tmp = tempfile.mkdtemp(prefix="gt-farm-tmp-")
    try:
        real_tmp = os.path.realpath(tmp)
        e = {"TMPDIR": real_tmp, "TEMP": real_tmp, "TMP": real_tmp, "HOME": real_tmp}
        e.update(env or {})
        if backend == "none":
            r = launch.run([cmd], timeout=timeout, max_out=max_out, backend="none", shell=True, cwd=ws, env=e)
        else:
            r = launch.run([cmd], timeout=timeout, max_out=max_out, backend=backend, shell=True, cwd=ws, env=e,
                           write_paths=[ws, real_tmp])
    finally:
        rmtree(tmp)
    if r.get("reason"):
        return "error: refused: " + r["reason"]
    out = r["out"].decode("utf-8", "replace")
    err = r["err"].decode("utf-8", "replace")
    notes = []
    if r["timed_out"]:
        notes.append("timed out after %ss" % timeout)
    if r["truncated"]:
        notes.append("output truncated")
    if not r["isolated"]:
        notes.append("NOT isolated")
    text = "exit %s%s\n%s" % (r["rc"], (" (" + ", ".join(notes) + ")") if notes else "", out)
    if err:
        text += "\n[stderr]\n" + err
    text = _cap_ends(text, max_out)
    if r["rc"] == 126 or "Operation not permitted" in out or "Operation not permitted" in err:
        text += "\n" + HOME_INTERPRETER_HINT
    return text


# ------------------------------------------------------------------------------------ CLI
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["check"]:
        print(__doc__.split("Exit codes")[0].strip())
        return 2
    launch = _launcher()
    if launch is None:
        print("gt core was not found, so the sandbox launcher could not be checked.")
        return 2
    backend = launch.available_backends()[0]
    if backend == "none":
        print("No sandbox backend is usable here. Agent commands would run with no isolation, so they are refused "
              "unless you allow that explicitly.")
        return 3
    print("Sandbox backend usable here: %s. Agent commands run with no network, no reads outside the workspace "
          "and no writes outside it." % backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
