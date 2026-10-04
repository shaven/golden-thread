"""tests/prun.py execution (0.18.1): load-aware workers, --affected, --hosts, and the scoped
receipt tests/run.sh records from --affected.

Pinned (requests 2026-10-01-fast-build-loop-and-execution-optimizer and
2026-09-28-gt-bench-measured-execution-profile):

  * under a synthetic high load prun starts FEWER workers than the ceiling, and says why; on an
    idle machine it starts the ceiling;
  * the Governor shrinks the pool when units run well over their baseline and regrows it when
    they recover;
  * --affected maps a changed script to the test modules that name it; an unmapped code change
    or a harness change selects the full suite; a docs change selects nothing;
  * tests/run.sh --affected records a SCOPED receipt naming the changed files, which the receipt
    check accepts only when asked for scoped receipts;
  * --hosts splits units across runners, prints each failure under the host that ran it,
    reports an unreachable runner as skipped and never counts it as passing.

Every case runs in a throwaway git repo shaped like this one (golden-thread-plugin/tests,
golden-thread/<release>/scripts), so nothing here depends on this repository's history.
"""
import json
import re
import shlex
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, GT, REPO, PYTHON, load_module
from _fakes import install_fake

PRUN = REPO / "tests" / "prun.py"
FAKE_SSH = """#!/usr/bin/env bash
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; *) break ;; esac; done
host="$1"; shift
[ "$host" = unreachable ] && { echo "ssh: connect to host unreachable: refused" >&2; exit 255; }
exec bash -c "$*"
"""
TESTS = {
    "test_alpha.py": "import unittest\n# covers alpha_tool.py\n"
                     "class One(unittest.TestCase):\n    def test_a(self): pass\n"
                     "class Two(unittest.TestCase):\n    def test_b(self): pass\n"
                     "class Three(unittest.TestCase):\n    def test_c(self): pass\n"
                     "class Four(unittest.TestCase):\n    def test_d(self): pass\n",
    "test_beta.py": "import unittest\n# covers beta_tool\n"
                    "class Bad(unittest.TestCase):\n"
                    "    def test_bad(self): self.fail('boom from beta')\n",
}


class FakeRepo(Sandbox):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "fake"
        self.plugin = self.root / "golden-thread-plugin"
        tests = self.plugin / "tests"
        tests.mkdir(parents=True)
        shutil.copy2(PRUN, tests / "prun.py")
        shutil.copy2(REPO / "tests" / "run.sh", tests / "run.sh")
        for name, text in TESTS.items():
            (tests / name).write_text(text)
        self.rel = self.plugin / "golden-thread" / GT.name
        # The release's scripts, minus the ones this fixture does not run. gt_metrics.py is left
        # out: the secrets gate flags one keyword argument in it as an assigned credential (a false
        # positive awaiting the owner's baseline decision), and run.sh's gates scan this tree.
        shutil.copytree(GT, self.rel, ignore=shutil.ignore_patterns("__pycache__",
                                                                     "gt_metrics.py"))
        (self.rel / "scripts" / "alpha_tool.py").write_text("x = 1\n")
        (self.rel / "scripts" / "beta_tool.py").write_text("y = 1\n")
        base = REPO / "tests" / "secrets-baseline.json"
        if base.is_file():
            shutil.copy2(base, tests / "secrets-baseline.json")
        # And the code-scan baseline (0.20.1: the owner's accepted findings in the vendored
        # qrcodegen.py), at the path run.sh's code gate reads: <plugin>/.gt/code-baseline.json.
        cbase = REPO / ".gt" / "code-baseline.json"
        if cbase.is_file():
            (self.plugin / ".gt").mkdir()
            shutil.copy2(cbase, self.plugin / ".gt" / "code-baseline.json")
        self.git_init(self.root)
        self.run_cmd(["git", "-C", self.root, "checkout", "-q", "-b", "feature"])
        self.env.update({"GT_TEST_VERSION": GT.name, "PYTHONDONTWRITEBYTECODE": "1"})

    def prun(self, *args, env=None, timeout=300):
        e = dict(self.env)
        e.update(env or {})
        return self.run_cmd([PYTHON, "tests/prun.py", *args], env=e, cwd=self.plugin,
                            timeout=timeout)


class LoadAware(FakeRepo):
    def test_high_load_starts_fewer_workers_idle_starts_the_ceiling(self):
        hot = json.dumps({"load1": 64, "cores": 16, "mem_free": 0.5, "others": 0})
        p = self.prun("-j", "8", "test_alpha", env={"GT_LOAD_OVERRIDE": hot})
        self.assertOk(p)
        self.assertIn("load-aware: starting 1 of 8 worker(s)", p.stdout)
        self.assertIn("load 64.0 on 16 cores", p.stdout)
        self.assertIn("1 worker(s)", p.stdout)
        idle = json.dumps({"load1": 0, "cores": 16, "mem_free": 0.9, "others": 0})
        p = self.prun("-j", "8", "test_alpha", env={"GT_LOAD_OVERRIDE": idle})
        self.assertOk(p)
        self.assertNotIn("load-aware: starting", p.stdout)
        self.assertIn("4 worker(s)", p.stdout)
        busy = json.dumps({"load1": 0, "cores": 16, "mem_free": 0.9, "others": 3})
        p = self.prun("-j", "8", "test_alpha", env={"GT_LOAD_OVERRIDE": busy})
        self.assertIn("3 other parallel run(s)", p.stdout)

    def test_governor_backs_off_and_recovers(self):
        g = load_module(SCRIPTS / "gt_load.py", "gt_load_for_tests")
        gov = g.Governor(8, 8, {"u%d" % i: 10.0 for i in range(20)})
        for i in range(3):
            gov.finished("u%d" % i, 30.0)
        self.assertEqual(gov.limit, 6, gov.events)
        for i in range(3, 6):
            gov.finished("u%d" % i, 30.0)
        self.assertEqual(gov.limit, 4)
        for i in range(6, 9):
            gov.finished("u%d" % i, 10.0)
        self.assertEqual(gov.limit, 5, gov.events)
        self.assertTrue(any("back off" in e for e in gov.events))
        n, why = g.recommend(16, r={"load1": 2, "cores": 16, "mem_free": 0.05, "others": 0,
                                     "own": 0})
        self.assertIn("memory pressure", why)
        self.assertLessEqual(n, 8)


class Affected(FakeRepo):
    def affected(self):
        p = self.prun("--print-affected")
        self.assertOk(p)
        return json.loads(p.stdout)

    def test_mapping(self):
        (self.rel / "scripts" / "alpha_tool.py").write_text("x = 2\n")
        a = self.affected()
        self.assertEqual((a["modules"], a["full"]), (["test_alpha"], False))
        (self.root / "NOTES.md").write_text("docs only\n")
        self.assertEqual(self.affected()["modules"], ["test_alpha"])
        (self.rel / "scripts" / "unmapped_thing.py").write_text("z = 1\n")
        a = self.affected()
        self.assertTrue(a["full"])
        self.assertIn("no test names", " ".join(a["reasons"]))

    def test_harness_change_is_the_full_suite(self):
        (self.plugin / "tests" / "prun.py").write_text(
            (self.plugin / "tests" / "prun.py").read_text() + "\n# touched\n")
        a = self.affected()
        self.assertTrue(a["full"])

    def test_run_sh_affected_records_a_scoped_receipt(self):
        (self.rel / "scripts" / "alpha_tool.py").write_text("x = 3\n")
        e = dict(self.env)
        p = self.run_cmd(["bash", "tests/run.sh", "--affected", "-j", "2"], env=e,
                         cwd=self.plugin, timeout=600)
        self.assertOk(p, "run.sh --affected")
        self.assertIn("scoped receipt recorded", p.stdout)
        rec = self.rel / "scripts" / "gt_test_receipt.py"
        rel = "golden-thread-plugin/golden-thread/%s/scripts/alpha_tool.py" % GT.name
        q = self.py(rec, "check", "--repo", self.root, "--files", rel)
        self.assertEqual(q.returncode, 1, "a scoped receipt must not satisfy a full check")
        q = self.py(rec, "check", "--repo", self.root, "--files", rel, "--allow-scoped")
        self.assertOk(q, "the scoped receipt does not cover the file it ran for")
        q = self.py(rec, "check", "--repo", self.root, "--files",
                    "golden-thread-plugin/golden-thread/%s/scripts/beta_tool.py" % GT.name,
                    "--allow-scoped")
        self.assertEqual(q.returncode, 1, "a scoped receipt covered a file it did not run for")


class Hosts(FakeRepo):
    def fake_ssh(self):
        """GT_PRUN_SSH for a local stand-in for ssh. prun shlex-splits the value, so it is the
        "/" form of the path, quoted: a Windows path's backslashes would be eaten (0.20.1)."""
        ssh = install_fake(self, self.tmp / "fake-ssh", FAKE_SSH)
        return shlex.quote(ssh.as_posix())

    def test_split_attribute_and_skip_unreachable(self):
        ssh = self.fake_ssh()
        p = self.prun("--hosts", "good,unreachable,local", "-j", "2",
                      env={"GT_PRUN_SSH": ssh, "GT_TEST_LOAD_AWARE": "0"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        out = p.stdout
        self.assertIn("SKIPPED runner unreachable", out)
        self.assertIn("NOT counted as passing", out)
        self.assertIn("runners skipped as unreachable: unreachable", out)
        ran = [l for l in out.splitlines() if re.match(r"^(ok  |FAIL) \S+\s+[\d.]+s", l)]
        self.assertEqual(len(ran), 5, out)
        hosts = {l.rsplit("[", 1)[1].rstrip("]") for l in ran}
        self.assertTrue(hosts <= {"good", "local"} and hosts, hosts)
        self.assertRegex(out, r"FAILED UNIT: test_beta\.Bad  \[host (good|local)\]")
        self.assertIn("boom from beta", out)

    def test_no_reachable_runner_is_a_failure(self):
        ssh = self.fake_ssh()
        p = self.prun("--hosts", "unreachable", "test_alpha", env={"GT_PRUN_SSH": ssh})
        self.assertEqual(p.returncode, 1)
        self.assertIn("no runner reachable", p.stdout)


class LeakCheck(FakeRepo):
    """0.20.1: a unit that leaves anything in its TMPDIR fails, naming what it left. Before it,
    test_gt_demote left ~118 MB per test and claudebox2's /tmp filled to 97%, all green."""

    def test_a_leaked_temp_dir_fails_its_unit_and_nothing_is_left(self):
        tests = self.plugin / "tests"
        (tests / "test_leaky.py").write_text(
            "import tempfile, unittest\n"
            "class Leaky(unittest.TestCase):\n"
            "    def test_leaks(self): tempfile.mkdtemp(prefix='gt-leak-')\n")
        (tests / "test_tidy.py").write_text(
            "import shutil, tempfile, unittest\n"
            "class Tidy(unittest.TestCase):\n"
            "    def test_cleans(self):\n"
            "        d = tempfile.mkdtemp(prefix='gt-tidy-')\n"
            "        self.addCleanup(shutil.rmtree, d)\n")
        tmp = self.tmp / "t"
        tmp.mkdir()
        p = self.prun("test_leaky", "test_tidy", env={"TMPDIR": str(tmp)})
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("FAIL test_leaky.Leaky", p.stdout)
        self.assertIn("ok   test_tidy.Tidy", p.stdout)
        self.assertRegex(p.stdout, r"LEAKED TEMP: test_leaky\.Leaky left 1 entry .*\n  gt-leak-")
        self.assertNotIn("gt-tidy-", p.stdout)
        self.assertEqual(list(tmp.iterdir()), [], "the run left its own temp dirs behind")

    def test_the_check_can_be_turned_off(self):
        (self.plugin / "tests" / "test_leaky.py").write_text(
            "import tempfile, unittest\n"
            "class Leaky(unittest.TestCase):\n"
            "    def test_leaks(self): tempfile.mkdtemp(prefix='gt-leak-')\n")
        tmp = self.tmp / "t"
        tmp.mkdir()
        p = self.prun("test_leaky", env={"TMPDIR": str(tmp), "GT_TEST_LEAK_CHECK": "0"})
        self.assertOk(p)
        self.assertNotIn("LEAKED TEMP", p.stdout)


if __name__ == "__main__":
    unittest.main()
