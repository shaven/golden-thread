"""Shared helpers for the Golden Thread test harness.

Every test runs against a THROWAWAY home directory and, where it needs one, a
throwaway vault. Nothing here may touch the real ~/.claude, the real vault, or the
network. Tests find the plugin by layout, not by version number, so they keep
working across version bumps and run unchanged from Projects2/gt-src.

Conventions (read before adding a test file):

* One file per tool: tests/test_<tool>.py. Test BEHAVIOUR through the tool's CLI
  (subprocess) wherever it has one. Several scripts read HOME at import time, so an
  in-process import would read the developer's real config; if you must import,
  use `load_module()` after setting os.environ["HOME"] inside the test.
* Standard library only, Python 3.8+.
* A test that needs a tool the machine lacks (git, gh, weasyprint) skips with the
  reason; it never fails for an environment gap.
* A test that finds a real defect should fail with a message saying what the tool
  did wrong. Do not weaken an assertion to make a buggy tool pass.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _version_key(name):
    return tuple(int(x) for x in name.split("."))


def latest_version_dir(root: Path) -> Path:
    """The newest installable version directory, the way install.sh picks it."""
    cands = [d for d in root.iterdir()
             if d.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+", d.name)
             and (d / ".claude-plugin" / "plugin.json").is_file()]
    if not cands:
        raise RuntimeError(f"no installable version directory under {root}")
    return max(cands, key=lambda d: _version_key(d.name))


# GT_TEST_VERSION pins the gt release under test (e.g. a release being built beside the
# current one); otherwise the newest installable directory is tested, as install.sh picks it.
GT = (REPO / "golden-thread" / os.environ["GT_TEST_VERSION"]) if os.environ.get("GT_TEST_VERSION") \
    else latest_version_dir(REPO / "golden-thread")
WIKI = latest_version_dir(REPO / "golden-thread-wiki")


def next_minor(version):
    """0.16.0 -> 0.17.0. Used to derive the requires_gt range a module must declare."""
    major, minor, _patch = (int(x) for x in version.split("."))
    return "%d.%d.0" % (major, minor + 1)


def gt_requires_range(gt_version):
    """The exact requires_gt a module tracking this gt release must carry."""
    return ">=%s,<%s" % (gt_version, next_minor(gt_version))


def _module_dir(dirname):
    """The newest release of an optional module, or None when this tree does not ship it."""
    try:
        return latest_version_dir(REPO / dirname)
    except (RuntimeError, OSError):
        return None


# Modules extracted from gt in 0.14.0 (demo) and 0.15.0 (watch, report card, farm).
DEMO = _module_dir("golden-thread-demo")
WATCH = _module_dir("golden-thread-watch")
REPORT_CARD = _module_dir("golden-thread-report-card")
FARM = _module_dir("golden-thread-farm")
FLOW = _module_dir("golden-thread-flow")
SCRIPTS = GT / "scripts"
HOOKS = GT / "hooks"
TEMPLATES = GT / "templates"
TOOLS = TEMPLATES / "tools"
WIKI_SCRIPTS = WIKI / "scripts"
PYTHON = shutil.which("python3") or "python3"
GIT_ID = {"GIT_AUTHOR_NAME": "gt-test", "GIT_AUTHOR_EMAIL": "gt-test@example.invalid",
          "GIT_COMMITTER_NAME": "gt-test", "GIT_COMMITTER_EMAIL": "gt-test@example.invalid"}


def load_module(path: Path, name: str = None):
    """Import a script by path. Set HOME in os.environ BEFORE calling if it reads config."""
    spec = importlib.util.spec_from_file_location(name or path.stem, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def enforcement_hooks():
    """The Core-rule hook scripts, READ from gt_components rather than listed here.

    Six test files used to carry their own copy of ("inject_core_rules.sh",
    "validate_response.sh", "guard_session_claims.sh"). Adding a fourth hook in
    0.12.0 broke nineteen tests at once, not one of which was about that hook --
    the same drift MANIFEST.json exists to prevent, one level down. So derive the
    list from the single declaration, the way install.sh already does.
    """
    mod = load_module(SCRIPTS / "gt_components.py", "gt_components_for_tests")
    return tuple(dict.fromkeys(r["script"] for r in mod.HOOK_REGISTRATIONS
                               if r["owner"].startswith("vault_init.py")))


ENFORCEMENT_HOOKS = enforcement_hooks()


class Sandbox(unittest.TestCase):
    """Base class: a temp dir with an empty HOME and an environment pointing at it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gt-test-"))
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "GT_"))}
        env.update(GIT_ID)
        env["HOME"] = str(self.home)
        self.env = env

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- running things ---------------------------------------------------------
    def run_cmd(self, args, input=None, cwd=None, env=None, timeout=120):
        e = dict(self.env)
        if env:
            e.update(env)
        return subprocess.run([str(a) for a in args], input=input, cwd=cwd, env=e,
                              capture_output=True, text=True, timeout=timeout)

    def py(self, script: Path, *args, **kw):
        return self.run_cmd([PYTHON, script, *args], **kw)

    def sh(self, script: Path, *args, **kw):
        return self.run_cmd(["bash", script, *args], **kw)

    def assertOk(self, proc, msg=""):
        self.assertEqual(proc.returncode, 0,
                         f"{msg}\nexit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

    # -- fixtures ---------------------------------------------------------------
    def config(self, **values):
        """Write ~/.claude/vault-config.json in the sandbox home."""
        p = self.home / ".claude" / "vault-config.json"
        p.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
        return p

    def make_vault(self, name="vault", domain="Test"):
        """A fresh Golden Thread vault, scaffolded the way /gt:gt-init does it."""
        v = self.tmp / name
        proc = self.py(SCRIPTS / "vault_init.py", "fresh", "--vault", v, "--domain", domain)
        self.assertOk(proc, "vault_init.py fresh failed while building a fixture")
        return v

    def git_init(self, path: Path, commit=True):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        path.mkdir(parents=True, exist_ok=True)
        self.run_cmd(["git", "init", "-q", "-b", "main", path])
        if commit:
            self.run_cmd(["git", "-C", path, "add", "-A"])
            self.run_cmd(["git", "-C", path, "commit", "-q", "--allow-empty", "-m", "init"])
        return path
