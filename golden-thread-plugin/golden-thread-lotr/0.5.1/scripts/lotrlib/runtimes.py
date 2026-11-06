"""The programs a stdio MCP server needs on this machine, checked BEFORE it is launched (0.5.0).

A template names what its command runs on (`connect.runtime`): node (npx), uv (uvx), docker, a
plain binary, or python. Without this check a missing `npx` surfaces as a launch failure deep in a
connection test; here it is one plain sentence that says how to install the thing. Nothing is
installed and nothing is run except an optional `docker info` (a status question, 8 s).
"""
import os
import shutil
import subprocess

from .errors import GatewayError

RUNTIMES = ("node", "uv", "docker", "binary", "python")

# runtime -> the program names a command may start with, and the one to look for
_FAMILY = {
    "node": ("npx", "node", "pnpm", "bunx", "yarn"),
    "uv": ("uvx", "uv"),
    "docker": ("docker", "podman"),
    "python": ("python", "python3", "pipx"),
}
_NEED = {"node": "npx", "uv": "uvx", "docker": "docker", "python": "python3"}

INSTALL = {
    "node": ("Node.js (it provides npx) is not installed. Install it from https://nodejs.org, or "
             "with `brew install node` on a Mac, `winget install OpenJS.NodeJS.LTS` on Windows, "
             "or your Linux package manager; then open a new terminal."),
    "uv": ("uv (it provides uvx) is not installed. Install it from "
           "https://docs.astral.sh/uv/getting-started/installation/ or with `brew install uv`; "
           "then open a new terminal."),
    "docker": ("Docker is not installed. Install Docker Desktop (https://docs.docker.com/get-docker/) "
               "or, on Linux, Docker Engine; then start it."),
    "python": ("Python 3 is not installed. Install it from https://www.python.org/downloads/ or "
               "with your package manager."),
}


def _base(prog):
    b = os.path.basename(prog).lower()
    for ext in (".exe", ".cmd", ".bat"):
        if b.endswith(ext):
            b = b[: -len(ext)]
    return b


_IS_WINDOWS = os.name == "nt"


def _store_stub_dir(d):
    return "\\windowsapps\\" in d.lower().replace("/", "\\").rstrip("\\") + "\\"


def which(prog, path=None):
    """shutil.which, except on native Windows for a bare python/python3: there `python3` (and
    often `python`) on PATH is the Microsoft Store stub under ...\\WindowsApps\\, which exits 9009
    instead of running anything, and python.org installs python.exe, never python3.exe. So the
    first python3 or python on PATH outside WindowsApps is taken (install.sh's rule, 0.20.1)."""
    b = _base(prog)
    if not _IS_WINDOWS or b not in ("python", "python3") or os.path.dirname(prog):
        return shutil.which(prog, path=path)
    dirs = (path if path is not None else os.environ.get("PATH") or os.defpath).split(os.pathsep)
    clean = os.pathsep.join(d for d in dirs if d and not _store_stub_dir(d))
    for name in (prog, "python" if b == "python3" else "python3"):
        found = shutil.which(name, path=clean) if clean else None
        if found:
            return found
    return None


def infer(command):
    """The runtime a command line implies, or 'binary'."""
    first = _base(command[0]) if command else ""
    for rt, names in _FAMILY.items():
        if first in names:
            return rt
    return "binary"


def required_program(runtime, command):
    """The program that must exist: the command's own first word if it belongs to the runtime
    (or the runtime is 'binary'), else the runtime's usual program."""
    first = command[0] if command else ""
    if runtime == "binary" or _base(first) in _FAMILY.get(runtime, ()):
        return first
    return _NEED[runtime]


def consistent(runtime, command):
    """True when `runtime` is believable for `command` (a 'node' template starting with 'docker'
    is a mistake the checker should name)."""
    if runtime == "binary":
        return True
    return infer(command) == runtime


def check(runtime, command, *, deep=False, path=None):
    """-> {'ok': bool, 'runtime', 'program', 'found', 'say', 'fix'}. Never raises."""
    rt = runtime or infer(command)
    prog = required_program(rt, command)
    found = prog if (os.path.isabs(prog) and os.access(prog, os.X_OK)) else which(prog, path=path)
    out = {"ok": bool(found), "runtime": rt, "program": prog, "found": found}
    if not found:
        out["say"] = f"This server needs {prog!r}, which is not on this machine."
        out["fix"] = INSTALL.get(rt) or (
            f"Install the program {prog!r} (the template's command starts with it) and make sure it "
            "is on your PATH, or use its absolute path.")
        return out
    if deep and rt == "docker":
        try:
            r = subprocess.run([found, "info"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=8, check=False)
            if r.returncode != 0:
                out.update(ok=False, say="Docker is installed but is not running.",
                           fix="Start Docker Desktop (or the docker service), wait until it says "
                               "it is running, then run this again.")
        except (OSError, subprocess.SubprocessError):
            out.update(ok=False, say="Docker is installed but did not answer.",
                       fix="Start Docker Desktop (or the docker service), then run this again.")
    return out


def require(runtime, command, **kw):
    r = check(runtime, command, **kw)
    if not r["ok"]:
        raise GatewayError("runtime_missing", r["say"], hints=[r["fix"]])
    return r
