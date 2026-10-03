"""tests/_harness.cached_sandbox: one real install per run, copied into each test's sandbox.

The claim it must keep (request 2026-10-01-fast-build-loop-and-execution-optimizer, test plan:
"the cached-install fixture is equivalent to a fresh install (same files, same hooks)"):

  * the second sandbox is served from the cache, not rebuilt;
  * it holds the same files as a FRESH install made at its own path -- same relative paths
    under ~/.claude and the vault, byte-identical installed hooks, the same hook registrations
    in settings.json once each sandbox's own root is normalised away;
  * no path of the sandbox the cache was built in survives in any text file;
  * the release's own gate, gt_doctor.py post-install, passes on the copy.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from _harness import Sandbox, cached_sandbox, source_fingerprint
import test_install as _ti   # module import only: its TestCases must not be collected here

SKIP_PARTS = {"__pycache__", ".git", "logs"}


class _SandboxBase(Sandbox):
    def runTest(self):                      # a bare sandbox, built and torn down by hand
        pass


def _build(case):
    repo = _ti.build_repo_fixture(case, case.tmp / "src" / "golden-thread-plugin")
    case.make_vault()
    q = case.sh(repo / "install.sh", timeout=600)
    return {"returncode": q.returncode, "stdout": q.stdout, "stderr": q.stderr}


def _files(root: Path):
    out = set()
    for p in root.rglob("*"):
        if p.is_file() and not (SKIP_PARTS & set(p.relative_to(root).parts)):
            out.add(p.relative_to(root).as_posix())
    return out


def _hooks(case):
    data = json.loads((case.home / ".claude" / "settings.json").read_text())
    # Path(...).as_posix(): hook commands carry the "C:/..." form on Windows (identical to
    # str() on POSIX, so a no-op there)
    return json.loads(json.dumps(data.get("hooks", {})).replace(str(case.tmp), "<ROOT>")
                      .replace(os.path.realpath(str(case.tmp)), "<ROOT>")
                      .replace(Path(case.tmp).as_posix(), "<ROOT>"))


class CachedInstallIsAFreshInstall(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="gt-cache-test-")
        self.old = os.environ.get("GT_TEST_INSTALL_CACHE")
        os.environ["GT_TEST_INSTALL_CACHE"] = self.cache
        os.environ.pop("GT_TEST_NO_INSTALL_CACHE", None)
        self.cases = []

    def tearDown(self):
        for c in self.cases:
            c.tearDown()
        shutil.rmtree(self.cache, ignore_errors=True)
        if self.old is None:
            os.environ.pop("GT_TEST_INSTALL_CACHE", None)
        else:
            os.environ["GT_TEST_INSTALL_CACHE"] = self.old

    def case(self):
        c = _SandboxBase()
        c.setUp()
        c.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.cases.append(c)
        return c

    def test_copy_equals_a_fresh_install_and_passes_the_gate(self):
        key = "equivalence|%s" % source_fingerprint(_ti.GT, _ti.WIKI)
        a, b, fresh = self.case(), self.case(), self.case()
        ra, how_a = cached_sandbox(a, key, _build)
        self.assertEqual(how_a, "built")
        self.assertEqual(ra["returncode"], 0, ra["stdout"][-3000:] + ra["stderr"][-2000:])
        rb, how_b = cached_sandbox(b, key, _build)
        self.assertEqual(how_b, "cached", "the second sandbox was rebuilt, not copied")
        self.assertEqual(rb["returncode"], 0)
        self.assertNotIn(str(a.tmp), rb["stdout"], "the cached result still names the old root")
        rf = _build(fresh)
        self.assertEqual(rf["returncode"], 0, rf["stdout"][-3000:])

        for sub in ("home/.claude", "vault"):
            self.assertEqual(_files(b.tmp / sub), _files(fresh.tmp / sub),
                             "the cached %s differs from a fresh install's" % sub)
        hooks_b = b.home / ".claude" / "golden-thread" / "hooks"
        hooks_f = fresh.home / ".claude" / "golden-thread" / "hooks"
        for rel in _files(hooks_f):
            self.assertEqual((hooks_b / rel).read_bytes(), (hooks_f / rel).read_bytes(), rel)
        self.assertEqual(_hooks(b), _hooks(fresh), "hook registrations differ")

        leaked = []
        for p in b.tmp.rglob("*"):
            if p.is_file() and not p.is_symlink() and ".git" not in p.parts:
                try:
                    text = p.read_text(encoding="utf-8")
                    if str(a.tmp) in text or Path(a.tmp).as_posix() in text:
                        leaked.append(str(p.relative_to(b.tmp)))
                except (UnicodeDecodeError, OSError):
                    pass
        self.assertEqual(leaked, [], "files still name the sandbox the cache was built in")

        hooks = b.home / ".claude" / "golden-thread" / "hooks"
        p = b.py(hooks / "gt_doctor.py", "post-install", "--vault", b.tmp / "vault",
                 timeout=300)
        self.assertEqual(p.returncode, 0, "the gate fails on the cached copy:\n" + p.stdout[-4000:])

    def test_the_off_switch_rebuilds(self):
        os.environ["GT_TEST_NO_INSTALL_CACHE"] = "1"
        try:
            calls = []
            c = self.case()
            r, how = cached_sandbox(c, "off-switch", lambda case: calls.append(1) or {"ok": 1})
            r2, how2 = cached_sandbox(self.case(), "off-switch",
                                      lambda case: calls.append(1) or {"ok": 1})
        finally:
            os.environ.pop("GT_TEST_NO_INSTALL_CACHE", None)
        self.assertEqual((how, how2, len(calls)), ("built", "built", 2))


if __name__ == "__main__":
    unittest.main()
