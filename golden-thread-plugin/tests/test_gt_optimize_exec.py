"""/gt:gt-optimize --execution (gt_optimize_exec.py): how work runs, from what was measured.

Pinned (requests 2026-10-01-fast-build-loop-and-execution-optimizer and
2026-10-01-execution-metrics-and-process-optimizer, test plans):

  * on fixture metrics with a serial bottleneck, a redundant re-run and a slow step, it reports
    all three -- for gt (labelled gt-development) AND for a second, non-gt project (labelled
    project) -- each with a measured cost and an expected saving, ranked by cost;
  * a full suite run several times a day is reported where a scoped run would do;
  * slow and install-heavy test units are read from the runner's own timings;
  * an unmeasured parallel profile is reported as a default never revisited;
  * a hand-written step script is reported as a recipe candidate;
  * --apply on an accepted finding makes the change and records a marker with its rollback;
    reporting writes nothing; `gt_optimize.py --execution` reaches the same tool.
"""
import json
import time
import unittest

from _harness import Sandbox, SCRIPTS

TOOL = SCRIPTS / "gt_optimize_exec.py"
METRICS = SCRIPTS / "gt_metrics.py"


class ExecBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        (self.vault / "Projects" / "app").mkdir(parents=True)
        self.config(vault_path=str(self.vault))
        self.t0 = time.time() - 86400 * 3

    def rec(self, project, process, duration, start, **kw):
        args = ["record", "--vault", self.vault, "--project", project, "--process", process,
                "--duration", duration, "--exit", kw.get("exit", 0), "--start", start]
        for k in ("units", "parallel"):
            if k in kw:
                args += ["--" + k, kw[k]]
        if kw.get("scope"):
            args += ["--scope", kw["scope"]]
        if kw.get("args"):
            args += ["--args"] + kw["args"]
        self.assertOk(self.py(METRICS, *[str(a) for a in args]))

    def fixture(self, project):
        h = 3600
        # serial bottleneck: 12 units, one at a time, ~120 s
        for i in range(3):
            self.rec(project, "build:compile", 120 + i, self.t0 + i * h, units=12, parallel=1)
        # redundant re-run: same commit/settings/args twice within a day
        for i in range(2):
            self.rec(project, "data:export", 50, self.t0 + 5 * h + i * 60, args=["--all"])
        # slow step: one release step takes most of the pipeline
        for i in range(3):
            self.rec(project, "release:tests", 300, self.t0 + 10 * h + i * h)
            self.rec(project, "release:lint", 20, self.t0 + 10 * h + i * h + 400)
            self.rec(project, "release:push", 5, self.t0 + 10 * h + i * h + 500)

    def report(self, *extra):
        p = self.py(TOOL, "--vault", self.vault, "--json", *extra)
        self.assertIn(p.returncode, (0, 1), p.stderr)
        return json.loads(p.stdout)["findings"]


class Findings(ExecBase):
    def test_gt_and_a_second_project_each_get_all_three(self):
        self.fixture("golden-thread")
        self.fixture("app")
        found = self.report()
        for project, scope in (("golden-thread", "gt-development"), ("app", "project")):
            mine = [f for f in found if f["project"] == project]
            kinds = {f["kind"] for f in mine}
            self.assertTrue({"serial-bottleneck", "redundant-rerun", "slow-step"} <= kinds,
                            (project, kinds))
            for f in mine:
                self.assertEqual(f["scope"], scope)
            serial = next(f for f in mine if f["kind"] == "serial-bottleneck")
            self.assertEqual(serial["cost_s"], 121.0)
            self.assertGreater(serial["saving_s"], 0)
            slow = next(f for f in mine if f["kind"] == "slow-step")
            self.assertEqual(slow["process"], "release:tests")
        costs = [f["cost_s"] for f in found]
        self.assertEqual(costs, sorted(costs, reverse=True), "not ranked by measured cost")

    def test_full_suite_several_times_a_day(self):
        day = time.mktime(time.localtime(self.t0)[:3] + (10, 0, 0, 0, 0, -1))
        for i in range(4):
            self.rec("app", "tests:suite", 600, day + i * 1800, scope="full", units=50,
                     parallel=8, args=["run-%d" % i])
        found = [f for f in self.report("--project", "app") if f["kind"] == "full-when-scoped"]
        self.assertEqual(len(found), 1)
        self.assertGreater(found[0]["saving_s"], 0)
        self.assertEqual(found[0]["apply"][1:], ["set", "scoped_receipts", "on"])

    def test_slow_and_install_heavy_units_from_the_runner_timings(self):
        t = self.home / ".claude" / "golden-thread" / "prun-timings.json"
        t.parent.mkdir(parents=True, exist_ok=True)
        units = {"test_a.A": 2, "test_b.B": 3, "test_c.C": 2, "test_d.D": 2,
                 "test_install.InstallTest": 120, "test_install_modules.M": 90}
        t.write_text(json.dumps({str(self.tmp / "somerepo"): units}))
        found = self.report()
        slow = [f for f in found if f["kind"] == "slow-step" and "unit" in f["process"]]
        self.assertEqual({f["process"] for f in slow},
                         {"tests:unit:test_install.InstallTest", "tests:unit:test_install_modules.M"})
        self.assertTrue(any(f["kind"] == "repeated-install" for f in found))

    def test_unmeasured_profile_and_recipe_candidate(self):
        found = self.report()
        self.assertTrue(any(f["process"] == "settings:parallel_profile" for f in found))
        repo = self.tmp / "repo"
        repo.mkdir()
        body = "#!/bin/sh\n" + "".join('echo "== %d. step"\n' % i for i in range(1, 5)) + \
               "true\n" * 90
        (repo / "deploy.sh").write_text(body)
        found = self.report("--repo", repo, "--project", "app")
        self.assertTrue(any(f["kind"] == "recipe-candidate" and f["process"] == "script:deploy.sh"
                            for f in found))


class Apply(ExecBase):
    def test_reporting_writes_nothing_and_apply_records_a_marker(self):
        self.fixture("golden-thread")
        before = self.home.joinpath(".claude", "vault-config.json").read_text()
        found = self.report()
        self.assertEqual(self.home.joinpath(".claude", "vault-config.json").read_text(), before)
        f = next(x for x in found if x["process"] == "settings:parallel_profile")
        p = self.py(TOOL, "--vault", self.vault, "--apply", f["id"], "--dry-run")
        self.assertOk(p)
        self.assertIn("would run: ", p.stdout)
        tmpdir = next((x for x in found if x["process"] == "settings:test_tmpdir"), None)
        target = tmpdir or next(x for x in found if x["kind"] == "serial-bottleneck")
        if target["apply"] is None:
            p = self.py(TOOL, "--vault", self.vault, "--apply", target["id"])
            self.assertEqual(p.returncode, 2)
            return
        p = self.py(TOOL, "--vault", self.vault, "--apply", target["id"])
        self.assertOk(p)
        rows = []
        for fpath in (self.vault / "Projects" / "golden-thread" / "metrics").glob("*.jsonl"):
            rows += [json.loads(l) for l in fpath.read_text().splitlines() if l.strip()]
        marks = [r for r in rows if r.get("type") == "change"]
        self.assertEqual(len(marks), 1)
        self.assertTrue(marks[0]["rollback"])

    def test_serial_tests_finding_applies_a_setting(self):
        for i in range(3):
            self.rec("golden-thread", "tests:suite", 200, self.t0 + i * 3600, units=20,
                     parallel=1)
        self.config(vault_path=str(self.vault), parallel_work="off")
        f = next(x for x in self.report() if x["kind"] == "serial-bottleneck")
        p = self.py(TOOL, "--vault", self.vault, "--apply", f["id"])
        self.assertOk(p)
        cfg = json.loads(self.home.joinpath(".claude", "vault-config.json").read_text())
        self.assertEqual(cfg["parallel_work"], "on")

    def test_gt_optimize_execution_flag_delegates(self):
        self.fixture("app")
        p = self.py(SCRIPTS / "gt_optimize.py", "--execution", "--vault", self.vault, "--json")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertTrue(json.loads(p.stdout)["findings"])

    def test_execution_is_an_opt_in_aggregator_member(self):
        """`--only execution` runs it as the third member; a bare run does not include it."""
        self.fixture("app")
        p = self.py(SCRIPTS / "gt_optimize.py", "--vault", self.vault, "--only", "execution",
                    "--json")
        self.assertEqual(p.returncode, 1, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual((["execution"], ["execution"]), (data["asked"], data["ran"]))
        self.assertTrue(data["members"]["execution"]["findings"])
        p = self.py(SCRIPTS / "gt_optimize.py", "--vault", self.vault, "--only", "vault",
                    "--json")
        self.assertNotIn("execution", json.loads(p.stdout)["members"])


if __name__ == "__main__":
    unittest.main()
