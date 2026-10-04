"""Fake external programs (gh, ps, git, ssh ...) that run on macOS, Linux AND native Windows.

A test fakes a program by writing a `#!` shell script first on PATH. POSIX runs it as it is,
and nothing here changes that: on macOS and Linux install_fake() writes the script and chmods
it, exactly what the tests did inline before (0.20.1).

Native Windows cannot run that script, for two separate reasons, and each needs its own bridge:

* shutil.which("gh") honours PATHEXT, so it never matches an extensionless file. Without a
  match the product decides gh is not installed and the fake is never consulted. A sibling
  `gh.cmd` gives which() something to find.

* subprocess.run(["gh", ...]) does NOT honour PATHEXT. CreateProcess appends ".exe" to a bare
  name and nothing else, so even with gh.cmd beside it the call fails with WinError 2
  (verified on gt-win11, Python 3.12.10). The real gh is gh.exe, so this is not a product
  defect -- only the fake's form differs. Rather than change product code or ship a launcher
  .exe, a `usercustomize` module on the children's PYTHONPATH wraps subprocess.Popen in every
  Python the test starts: when the program a list-form call names resolves, through PATH the
  way POSIX execvp would, to a REGISTERED fake, the call becomes [bash, script, args...].
  Everything else -- real programs, shell=True, an explicit executable= -- passes through
  untouched, and a real <name>.exe earlier on PATH still wins, as it would for the real call.
  usercustomize rather than sitecustomize because Sandbox.pin_hostname already owns
  sitecustomize on the same PYTHONPATH, and only the first module of a name is imported.

The bridge goes straight to bash, never through the .cmd: cmd.exe re-parses %* and mangles
quotes, braces and `&`, which a GraphQL query or a commit message is full of. A bash or
`sh` caller (publish.sh, a hook wrapper) needs neither bridge -- Git Bash runs a `#!` script
found on PATH natively.
"""
import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Imported by every Python child on Windows (see the module docstring). Stdlib only; must never
# raise, or every child process the test starts would die at startup.
_USERCUSTOMIZE = r'''
import os
if os.name == "nt" and os.environ.get("GT_TEST_FAKES") and os.environ.get("GT_TEST_FAKE_BASH"):
    import subprocess

    def _key(p):
        return os.path.normcase(os.path.abspath(p))

    _FAKES = {_key(p) for p in os.environ["GT_TEST_FAKES"].split(os.pathsep) if p}
    _BASH = os.environ["GT_TEST_FAKE_BASH"]

    def _path_of(env):
        if env is None:
            return os.environ.get("PATH", "")
        for k, v in env.items():          # a passed env is a plain dict: "Path" or "PATH"
            if k.upper() == "PATH":
                return v
        return ""

    def _fake_for(prog, env):
        """The registered fake `prog` would run, or None for a real program."""
        if os.path.dirname(prog):         # a path: a fake itself, or which()'s <fake>.cmd
            stem, ext = os.path.splitext(prog)
            for cand in (prog, stem if ext.lower() == ".cmd" else None):
                if cand and _key(cand) in _FAKES:
                    return cand
            return None
        if os.path.splitext(prog)[1]:     # "gh.exe": the caller named a real program
            return None
        for d in _path_of(env).split(os.pathsep):
            if not d:
                continue
            cand = os.path.join(d, prog)
            if _key(cand) in _FAKES:
                return cand
            if os.path.isfile(cand + ".exe"):
                return None               # the real program comes first on PATH
        return None

    _init = subprocess.Popen.__init__

    def __init__(self, args, *a, **kw):
        try:
            if (isinstance(args, (list, tuple)) and args and not kw.get("shell")
                    and kw.get("executable") is None and isinstance(args[0], (str, os.PathLike))):
                fake = _fake_for(os.fspath(args[0]), kw.get("env"))
                if fake:
                    args = [_BASH, fake] + list(args[1:])
                    env = dict(kw["env"] if kw.get("env") is not None else os.environ)
                    # Hand the fake its arguments verbatim, as POSIX would: no msys path
                    # rewriting of a "/..." argument, no globbing of a "?" or "*" in one.
                    env["MSYS_NO_PATHCONV"] = "1"
                    env["MSYS"] = (env.get("MSYS", "") + " noglob").strip()
                    kw["env"] = env
        except Exception:
            pass
        _init(self, args, *a, **kw)

    subprocess.Popen.__init__ = __init__
'''


def git_bash():
    """Git for Windows' bash.exe, or None. Never C:\\Windows\\System32\\bash.exe (that is WSL)."""
    cand = shutil.which("bash")
    if cand and "\\system32\\" not in cand.lower():
        return cand
    git = shutil.which("git")
    if git:
        for root in list(Path(git).resolve().parents)[:3]:   # Git\cmd\git.exe, Git\mingw64\bin
            for rel in ("bin/bash.exe", "usr/bin/bash.exe"):
                if (root / rel).is_file():
                    return str(root / rel)
    return None


def _prepend(env, key, value):
    parts = [p for p in env.get(key, "").split(os.pathsep) if p and p != value]
    env[key] = os.pathsep.join([value] + parts)


def install_fake(test, path, body):
    """Write a fake program at `path` (a `#!` script body) that this platform can run.

    `test` is the Sandbox: on Windows its env gains the bridge (see the module docstring), and
    a machine without Git Bash skips the test with the reason instead of failing it. Returns the
    path. Rewriting a fake later (a test that changes its behaviour) is install_fake again."""
    path = Path(path)
    # Bytes, not write_text: on Windows text mode writes CRLF, and bash reads `cat 'x'\r`.
    path.write_bytes(body.encode("utf-8"))
    path.chmod(0o755)
    if not IS_WINDOWS:
        return path
    bash = git_bash()
    if not bash:
        test.skipTest("POSIX-only here: the fake %s is a #! script and this Windows machine "
                      "has no Git Bash to run it" % path.name)
    env = test.env
    site = path.parent / "_gt_fakes_site"
    site.mkdir(exist_ok=True)
    (site / "usercustomize.py").write_text(_USERCUSTOMIZE, encoding="utf-8")
    _prepend(env, "PYTHONPATH", str(site))
    _prepend(env, "GT_TEST_FAKES", str(path))
    env["GT_TEST_FAKE_BASH"] = bash
    # For shutil.which(), and for anything that launches the fake through cmd or PATHEXT.
    path.with_name(path.name + ".cmd").write_bytes(
        ('@echo off\r\nsetlocal\r\nset MSYS_NO_PATHCONV=1\r\n"%s" "%s" %%*\r\n'
         % (bash, path)).encode("utf-8"))
    return path


def remove_fake(path):
    """Take a fake off PATH again (both the script and its Windows .cmd)."""
    path = Path(path)
    for p in (path, path.with_name(path.name + ".cmd")):
        if p.exists():
            p.unlink()
