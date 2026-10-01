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
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# dev/ (release tooling) is NOT published: gt-src and the repository it fills carry the tests but
# not dev/ (owner ruling, 2026-10-01). A test that reads dev/ is a dev-only test; mark it (class or
# method) with @needs_dev so the published suite skips it, with the reason, instead of failing.
# tests/test_sync_gt_src.py asserts every test module that reaches into dev/ uses this.
HAS_DEV = (REPO / "dev").is_dir()
needs_dev = unittest.skipUnless(HAS_DEV, "dev-only: needs dev/, which the published tree "
                                         "does not carry")


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


def _shipped_core_rules_paths():
    """Read the canonical core-rules location from the RELEASE UNDER TEST.

    Imported rather than spelled, deliberately. Before 0.17.0 about 35 occurrences of
    "Projects/golden-thread/core-rules" were hardcoded across a dozen test files, so
    moving the default meant editing every one and nothing prevented a 36th appearing. A
    test that spells the default cannot fail when the default changes -- it goes on
    asserting the old world, correctly and uselessly.
    """
    sys.path.insert(0, str(GT / "scripts"))
    import gt_paths          # deliberately NOT guarded -- see below
    return gt_paths.CORE_RULES_DEFAULT, gt_paths.CORE_RULES_LEGACY


# No try/except here, on purpose. The first version of this helper wrapped the import and
# fell back to spelled literals; `sys` was not imported in this module, so it NameError'd
# into the fallback on every run and nobody noticed -- because the fallback strings were
# identical to the shipped ones. A silent failure masked by a coincidence, in the helper
# written to stop tests hardcoding the path. If gt_paths cannot be imported, the harness
# should fail loudly at import: every test here depends on the release under test.
CORE_RULES, CORE_RULES_LEGACY = _shipped_core_rules_paths()


def core_rules_dir(vault):
    """Where this release puts a vault's Core rules. Use in fixtures and assertions."""
    return Path(vault) / CORE_RULES


def legacy_core_rules_dir(vault):
    """The pre-0.17.0 location -- only for tests ABOUT the migration."""
    return Path(vault) / CORE_RULES_LEGACY


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


def path_without_astgrep(path=None):
    """PATH with every directory that holds an `ast-grep` binary removed.

    A test of the ABSENT case must not see the machine's own install. Found 2026-09-29: the
    suite passed on the owner's Mac only because ast-grep had never been installed there; the
    hour it was, three test classes that build their PATH from os.environ started failing. Only
    `ast-grep` is looked for, never `sg`: on Linux /usr/bin/sg is the unrelated setgid tool, and
    dropping /usr/bin would break everything else the test runs."""
    dirs = (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep)
    keep = [d for d in dirs if d and not os.access(os.path.join(d, "ast-grep"), os.X_OK)]
    return os.pathsep.join(keep) or "/usr/bin:/bin"


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
        # Point the pre-commit credential gate at the release UNDER TEST. Without this the
        # gate resolves nothing in a throwaway HOME (there is no plugin cache there), and
        # since it fails closed -- correctly -- every `git commit` in every sandbox refuses.
        # Seeding a fake cache under the sandbox HOME was the alternative and was rejected:
        # test_install asserts exactly what install.sh puts in that directory, including
        # that stale caches are pruned, so a pre-seeded entry would corrupt those tests.
        env["GT_SECRETS_BIN"] = str(SCRIPTS / "gt_secrets.py")
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
    def pin_hostname(self, name):
        """Make socket.gethostname() return `name` in every Python this test starts.

        A test that samples the real hostname at import and compares it against a value a
        subprocess resolves later is racing the network: on 2026-09-28 DHCP renamed the
        machine mid-run and two suites failed in parallel for that reason alone. Pin the
        value instead -- a sitecustomize.py on PYTHONPATH, which every python3 child
        (hooks and git hooks included) imports at startup. Call again to RENAME the
        machine between two commands, which is how the rename defect is reproduced.
        Returns the pinned name so a test can assert against exactly what the tool saw.
        """
        shim = self.tmp / "hostname-shim"
        shim.mkdir(exist_ok=True)
        (shim / "sitecustomize.py").write_text(
            "import os, socket\n"
            "if os.environ.get('GT_TEST_HOSTNAME'):\n"
            "    socket.gethostname = lambda: os.environ['GT_TEST_HOSTNAME']\n",
            encoding="utf-8")
        self.env["PYTHONPATH"] = os.pathsep.join(
            [str(shim)] + [p for p in self.env.get("PYTHONPATH", "").split(os.pathsep)
                           if p and p != str(shim)])
        self.env["GT_TEST_HOSTNAME"] = name
        return name

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
