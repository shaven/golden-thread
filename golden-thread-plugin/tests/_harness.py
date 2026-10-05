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

# gt unlock (0.20.1): the suite must NEVER raise a real prompt -- no code dialog, no Touch ID
# sheet, no Windows Hello prompt, no browser -- and never leave an unlock daemon behind. On
# 2026-10-03 a test run put a real "enter 6-digit code" dialog on the owner's screen. Every
# test process, and every process a test starts (Sandbox.env re-adds both below), carries:
#   GT_UNLOCK_NO_UI=1     gt_unlock_factors refuses real UI (fail closed: "unavailable")
#   GT_UNLOCK_NO_START=1  gt_unlock_client never auto-starts a daemon; a test that needs a
#                         real daemon starts and stops it itself, and asserts it is gone
UNLOCK_TEST_ENV = {"GT_UNLOCK_NO_UI": "1", "GT_UNLOCK_NO_START": "1"}
os.environ.update(UNLOCK_TEST_ENV)
# gt_schedule (0.20.1): launchd's domain is gui/<uid> and Task Scheduler's is the user's,
# whatever HOME says, so a test must never change either. GT_TEST_SANDBOX makes gt_schedule
# refuse every scheduler write and never treat any HOME as the real user's. Same reach as
# UNLOCK_TEST_ENV: every test process, and every process a test starts (Sandbox.env).
SANDBOX_TEST_ENV = {"GT_TEST_SANDBOX": "1"}
os.environ.update(SANDBOX_TEST_ENV)
# gt-lotr OAuth (0.3.0): a test must NEVER open a real browser (2026-10-04: a manual run against a
# fake authorization server sent the owner's Chrome to a dead loopback port). Every test process,
# and every process a test starts (Sandbox.env), refuses to call webbrowser.open.
LOTR_TEST_ENV = {"GT_LOTR_NO_BROWSER": "1"}
os.environ.update(LOTR_TEST_ENV)
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
# The interpreter every test runs a script with. On native Windows `python3` on PATH is the
# Microsoft Store stub (python.org installs python.exe and py.exe, never python3.exe), so the
# suite uses the interpreter running it -- install.sh's rule, which refuses anything under
# ...\WindowsApps\ (0.20.1). macOS and Linux are unchanged.
IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    if "\\windowsapps\\" in sys.executable.lower():
        raise RuntimeError("the suite is running under a Microsoft Store Python (%s), which gt "
                           "does not support; run it with a python.org Python"
                           % sys.executable)
    PYTHON = sys.executable
else:
    PYTHON = shutil.which("python3") or "python3"
posix_only = unittest.skipIf(IS_WINDOWS, "POSIX-only")

if IS_WINDOWS:
    # In-process tests sandbox the home the POSIX way -- os.environ["HOME"] = <sandbox>, then
    # load_module() -- and Windows Python's expanduser reads USERPROFILE instead, so the module
    # under test would read the developer's real ~/.claude. In THIS process only, "~" follows
    # HOME when it is set (subprocesses get USERPROFILE from Sandbox.env). Python 3.12's pathlib
    # expands "~" through os.path.expanduser, so Path.home() follows too.
    import ntpath as _ntpath
    _real_expanduser = _ntpath.expanduser

    def _expanduser(path):
        p = os.fspath(path)
        home = os.environ.get("HOME")
        if home and isinstance(p, str) and (p == "~" or p.startswith(("~/", "~\\"))):
            return home + p[1:]
        return _real_expanduser(path)
    _ntpath.expanduser = _expanduser

# How gt writes a .py hook COMMAND on this platform (gt_components.hook_python): `python3 -B`
# everywhere but native Windows, where `python3` is the Store stub and the command names the
# interpreter by absolute "/" path, in UTF-8 mode.
# On macOS (0.20.1) an install that recorded an interpreter for hooks (the one that passed the
# write probe, in <home>/.claude/golden-thread/python) names it instead: py_hook_prefix(home).
PY_HOOK_PREFIX = ([Path(PYTHON).as_posix(), "-X", "utf8", "-B"] if IS_WINDOWS
                  else ["python3", "-B"])


def py_hook_prefix(home=None):
    """The interpreter part of a .py hook command for an install under `home`."""
    if IS_WINDOWS or sys.platform != "darwin" or home is None:
        return list(PY_HOOK_PREFIX)
    rec = Path(home) / ".claude" / "golden-thread" / "python"
    try:
        py = rec.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return list(PY_HOOK_PREFIX)
    return [py, "-B"] if py and os.path.isabs(py) and os.access(py, os.X_OK) \
        else list(PY_HOOK_PREFIX)


def py_hook_command(script, *args, home=None):
    """The settings.json command gt writes for a .py hook at `script`, quoted as gt quotes it.
    `home`: the sandbox HOME the install ran under (macOS names its recorded interpreter)."""
    import shlex
    path = str(script).replace("\\", "/") if IS_WINDOWS else str(script)
    return " ".join(shlex.quote(x) for x in py_hook_prefix(home) + [path] + list(args))


# gt-lotr 0.3.0 runs on native Windows (its local door is gt core's named pipe, gt_ipc), so the
# blanket LOTR_POSIX_ONLY skip is gone (0.20.1). A lotr test that still cannot run there skips
# with its specific reason: WIN_MODE_BITS below, or the unix-socket-mechanics reason in the test.


def _has_tzdb():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/Chicago")
        return True
    except Exception:
        return False


# Priority-window rules name IANA zones. macOS and Linux ship the database; native Windows has
# none unless the `tzdata` package is installed, and gt_tasks then says so on stderr (0.20.1).
HAS_TZDB = _has_tzdb()
needs_tzdb = unittest.skipUnless(HAS_TZDB, "no IANA time-zone database on this machine (native "
                                           "Windows without the tzdata package); gt_tasks "
                                           "says so when a window rule needs it")


# Reasons a test cannot run on native Windows that are about the TEST's mechanics or a POSIX
# facility, never about a gt defect (owner, 2026-10-03: every skip names a genuinely POSIX-only
# reason; a product bug is fixed, not skipped). Use these, or a specific reason of the same kind.
# A fake external program (gh, ps, git, ssh ...) is NOT such a reason: write it with
# tests/_fakes.install_fake(), which makes the same `#!` script runnable on native Windows
# (0.20.1; it replaced WIN_FAKE_EXE, which skipped 31 tests there).
WIN_MODE_BITS = ("asserts POSIX permission bits (chmod/st_mode); Windows has no mode bits -- "
                 "st_mode always reads 0o666 or 0o444")
WIN_CHMOD_FAULT = ("injects a failure with chmod (an unreadable file or unwritable directory); "
                   "Windows has no permission bits, so chmod cannot make that failure happen")
WIN_LAUNCHD = "macOS launchd / plist behaviour; Windows uses Task Scheduler (tested separately)"
WIN_CRON = "crontab is POSIX; Windows has no cron"
WIN_OSASCRIPT = "macOS osascript notification channel"
WIN_FILENAME = ("needs a file name Windows forbids (control characters, or \\ : * ? \" < > |)")


def skip_on_windows(reason):
    """Mark a test that cannot run on native Windows, with the reason (counted in the report)."""
    return unittest.skipIf(IS_WINDOWS, "POSIX-only: " + reason)
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


def rmtree(path):
    """shutil.rmtree that also clears read-only files: Windows refuses to delete them, and git
    writes its objects read-only, so every sandbox with a repo would otherwise stay in TEMP."""
    import stat

    def retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(str(path), onexc=retry)
    else:
        shutil.rmtree(str(path), onerror=retry)


class Sandbox(unittest.TestCase):
    """Base class: a temp dir with an empty HOME and an environment pointing at it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gt-test-"))
        # A cleanup, not only tearDown: tearDown never runs when setUp raises -- a skipTest()
        # in a subclass's setUp included -- and every such test leaked its whole sandbox
        # (0.20.1; tests/prun.py now fails a unit that leaves anything in TMPDIR).
        self.addCleanup(self._remove_sandbox, self.tmp)
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "GT_"))}
        env.update(GIT_ID)
        env.update(UNLOCK_TEST_ENV)            # no real prompt, no stray daemon (0.20.1)
        env.update(SANDBOX_TEST_ENV)           # never the real launchd/schtasks (0.20.1)
        env.update(LOTR_TEST_ENV)              # never a real browser (0.3.0 OAuth)
        env["HOME"] = str(self.home)
        if IS_WINDOWS:
            # Windows Python's expanduser reads USERPROFILE, not HOME: without this every tool a
            # test runs would read the developer's real ~/.claude (0.20.1).
            env["USERPROFILE"] = str(self.home)
            env["PYTHONUTF8"] = "1"
            # gt_scratch's Windows root is %LOCALAPPDATA%, which a sandbox HOME does not move:
            # without this every pipeline test would write into the real local app data.
            env["GT_SCRATCH_ROOT"] = str(self.home / "AppData" / "Local" / "gt-scratch")
            # What an installed Windows machine has (install.sh's python3 shim, first on PATH in
            # Git Bash): a `python3` that bash -- hook wrappers, vault git hooks, pipeline steps,
            # gt_demo.sh -- resolves to the real interpreter instead of the Store stub.
            shim = self.tmp / "python3-shim"
            shim.mkdir()
            (shim / "python3").write_bytes(
                ('#!/usr/bin/env bash\nexport PYTHONUTF8=1\n"%s" "$@" | tr -d \'\\r\'\n'
                 'exit "${PIPESTATUS[0]}"\n' % Path(PYTHON).as_posix()).encode("utf-8"))
            env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
        # Point the pre-commit credential gate at the release UNDER TEST. Without this the
        # gate resolves nothing in a throwaway HOME (there is no plugin cache there), and
        # since it fails closed -- correctly -- every `git commit` in every sandbox refuses.
        # Seeding a fake cache under the sandbox HOME was the alternative and was rejected:
        # test_install asserts exactly what install.sh puts in that directory, including
        # that stale caches are pruned, so a pre-seeded entry would corrupt those tests.
        env["GT_SECRETS_BIN"] = str(SCRIPTS / "gt_secrets.py")
        self.env = env

    # A class that shares ONE sandbox across its tests (test_gt_doctor_postinstall's
    # InstalledMachine) sets this and removes the sandbox itself, in tearDownClass.
    KEEP_SANDBOX_AFTER_TEST = False

    def _remove_sandbox(self, tmp):
        if not self.KEEP_SANDBOX_AFTER_TEST:
            rmtree(tmp)

    def tearDown(self):
        pass                    # the sandbox is removed by the cleanup setUp registered

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


# ---- the cached install (0.18.1) ---------------------------------------------------------
#
# A full install into a throwaway HOME is the slowest thing this suite does, and most classes
# that need "an installed machine" build the SAME one. cached_sandbox() builds it once per run
# and copies it into each test's tmp, rewriting the one path that differs (the sandbox root) in
# every text file. prun.py points GT_TEST_INSTALL_CACHE at a directory it removes at the end of
# the run, and the units of one run share it across processes under a file lock; with no
# GT_TEST_INSTALL_CACHE the cache lives for this process only. A class whose install depends on
# something it did first (a PRE_INSTALL hook) must not use it -- the key names only the build.
# tests/test_cached_install.py pins that the copy is equivalent to a fresh install: the same
# files, the same hook registrations, and the release gate passes on it.

_PROCESS_CACHE = None


def _cache_root():
    global _PROCESS_CACHE
    env = os.environ.get("GT_TEST_INSTALL_CACHE")
    if env:
        Path(env).mkdir(parents=True, exist_ok=True)
        return Path(env)
    if _PROCESS_CACHE is None:
        import atexit
        _PROCESS_CACHE = Path(tempfile.mkdtemp(prefix="gt-install-cache-"))
        atexit.register(shutil.rmtree, str(_PROCESS_CACHE), True)
    return _PROCESS_CACHE


def source_fingerprint(*dirs):
    """Cheap identity of the sources an install reads: every file's path, size and mtime."""
    import hashlib
    h = hashlib.sha256()
    for d in [REPO / "install.sh", *dirs]:
        d = Path(d)
        if d.is_file():
            st = d.stat()
            h.update(("%s %d %d\n" % (d, st.st_size, st.st_mtime_ns)).encode())
            continue
        for root, subdirs, files in os.walk(str(d)):
            subdirs[:] = sorted(x for x in subdirs if x != "__pycache__")
            for f in sorted(files):
                p = os.path.join(root, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                h.update(("%s %d %d\n" % (p, st.st_size, st.st_mtime_ns)).encode())
    return h.hexdigest()[:16]


def _path_variants(old, new):
    """[(old form, new form)], longest first. On Windows a path is written three ways -- native
    "C:\\x", "/"-separated (hook commands, git) and JSON-escaped "C:\\\\x" -- and each form
    must be rewritten as itself (0.20.1)."""
    pairs = {(old, new), (os.path.realpath(old), new)}
    if IS_WINDOWS:
        for o, n in list(pairs):
            pairs.add((o.replace("\\", "/"), n.replace("\\", "/")))
            pairs.add((o.replace("\\", "\\\\"), n.replace("\\", "\\\\")))
            if len(o) > 2 and o[1] == ":" and len(n) > 2 and n[1] == ":":   # Git Bash: /c/...
                pairs.add(("/" + o[0].lower() + o[2:].replace("\\", "/"),
                           "/" + n[0].lower() + n[2:].replace("\\", "/")))
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def _rewrite_paths(root: Path, old: str, new: str):
    """Replace the cached sandbox's root path with this one in every UTF-8 text file."""
    pairs = _path_variants(old, new)
    olds = {o for o, _ in pairs}
    for dirpath, dirs, files in os.walk(str(root)):
        for f in files:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                continue
            try:
                with open(p, "rb") as fh:
                    data = fh.read()
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not any(o in text for o in olds):
                continue
            for o, n in pairs:
                text = text.replace(o, n)
            st = os.stat(p)
            with open(p, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))


def cached_sandbox(case, key, build):
    """Make case.tmp hold what `build(case)` would build there; build it at most once per run.

    `build(case)` must build ONLY under case.tmp and return a JSON-serialisable dict (e.g. the
    install's returncode/stdout/stderr). Returns (that dict, "built" | "cached")."""
    import hashlib
    if os.environ.get("GT_TEST_NO_INSTALL_CACHE") == "1":     # measure the uncached cost
        return build(case), "built"
    root = _cache_root()
    slot = root / hashlib.sha256(key.encode()).hexdigest()[:20]
    lock = open(str(slot) + ".lock", "w")
    try:
        _lock(lock)
        meta = slot / "meta.json"
        if meta.is_file():
            m = json.loads(meta.read_text(encoding="utf-8"))
            shutil.copytree(str(slot / "tmp"), str(case.tmp), symlinks=True, dirs_exist_ok=True)
            _rewrite_paths(case.tmp, m["tmp"], str(case.tmp))
            raw = json.dumps(m["result"])
            for o, n in _path_variants(m["tmp"], str(case.tmp)):
                raw = raw.replace(json.dumps(o)[1:-1], json.dumps(n)[1:-1])
            return json.loads(raw), "cached"
        result = build(case)
        tmp_copy = slot / "tmp"
        if tmp_copy.exists():
            shutil.rmtree(str(tmp_copy))
        shutil.copytree(str(case.tmp), str(tmp_copy), symlinks=True)
        meta.write_text(json.dumps({"tmp": str(case.tmp), "result": result}), encoding="utf-8")
        return result, "built"
    finally:
        _unlock(lock)
        lock.close()


def _lock(fh):
    """An exclusive lock across processes: flock on POSIX, msvcrt on Windows (no fcntl there)."""
    if IS_WINDOWS:
        import msvcrt
        import time
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:              # LK_LOCK gives up after ~10 s; keep waiting
                time.sleep(0.5)
    import fcntl
    fcntl.flock(fh, fcntl.LOCK_EX)


def _unlock(fh):
    if IS_WINDOWS:
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(fh, fcntl.LOCK_UN)
