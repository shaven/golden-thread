"""gt_metrics.py -- execution rows for every repeated process, per project, with baselines.

Pinned (request 2026-10-01-execution-metrics-and-process-optimizer):

  * `record` writes one row (duration, exit, units, parallelism, tokens, hashed args, machine,
    commit) into the vault project's metrics/ folder, or the local store for a project the vault
    does not have; `--dry-run` writes nothing;
  * rows are recorded from a release-pipeline step, from tests/prun.py and from a gt skill run
    (gt_allin.py);
  * no row ever holds a raw argument or a credential-shaped value; `execution_metrics off`
    records nothing at all;
  * `report` is per project and cross-project, with a rolling median/p90 baseline; a run outside
    it is a REGRESSION naming what changed since;
  * `peers` reports a much cheaper like process in another project, with what it does
    differently;
  * `mark` + `verify`: a change that beat its baseline is measured as such; one that did not is
    reported, with the rollback.
"""
import json
import os
import time
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, REPO, PYTHON

TOOL = SCRIPTS / "gt_metrics.py"
SECRET = "sk-live-" + "A1b2C3d4E5f6G7h8I9j0K1l2"          # credential-shaped, built at run time


class MetricsBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        (self.vault / "Projects" / "app").mkdir(parents=True)
        self.config(vault_path=str(self.vault))

    def m(self, *args, **kw):
        return self.py(TOOL, *[str(a) for a in args], **kw)

    def rows(self, project):
        out = []
        for d in (self.vault / "Projects" / project / "metrics",
                  self.home / ".claude" / "golden-thread" / "metrics" / project):
            for f in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
                out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return out

    def seed(self, project, process, durations, start=None, **fields):
        start = start or time.time() - 86400 * 10
        for i, d in enumerate(durations):
            args = ["record", "--vault", self.vault, "--project", project, "--process", process,
                    "--duration", d, "--exit", fields.get("exit", 0),
                    "--start", start + i * 3600]
            for k in ("units", "parallel", "tokens"):
                if k in fields:
                    args += ["--" + k, fields[k]]
            if fields.get("scope"):
                args += ["--scope", fields["scope"]]
            for t in fields.get("traits", ()):
                args += ["--trait", t]
            self.assertOk(self.m(*args))


class Record(MetricsBase):
    def test_a_row_has_every_field_and_lands_in_the_project(self):
        p = self.m("record", "--vault", self.vault, "--project", "app", "--process",
                   "tests:suite", "--duration", "12.5", "--exit", "0", "--units", "40",
                   "--parallel", "8", "--tokens", "1200", "--scope", "full", "--args",
                   "-j", "8", "--token=" + SECRET)
        self.assertOk(p)
        rows = self.rows("app")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        for k in ("project", "process", "kind", "start", "duration", "exit", "units",
                  "parallel", "retries", "reruns", "tokens", "machine", "args_hash",
                  "config_hash", "scope", "traits"):
            self.assertIn(k, r)
        self.assertEqual((r["kind"], r["units"], r["parallel"], r["tokens"]),
                         ("tests", 40, 8, 1200))
        raw = "".join(f.read_text() for f in (self.vault / "Projects" / "app" / "metrics")
                      .glob("*.jsonl"))
        self.assertNotIn(SECRET, raw, "a raw argument reached the store")
        self.assertNotIn("--token", raw)
        self.assertEqual(len(r["args_hash"]), 16)

    def test_a_credential_shaped_process_or_trait_is_stored_only_as_a_hash(self):
        self.assertOk(self.m("record", "--vault", self.vault, "--project", "app", "--process",
                             "deploy:" + SECRET, "--duration", "1", "--exit", "0",
                             "--trait", "password=hunter2"))
        raw = json.dumps(self.rows("app"))
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("hunter2", raw)
        self.assertIn("hash:", raw)

    def test_unknown_project_goes_to_the_local_store_and_dry_run_writes_nothing(self):
        self.assertOk(self.m("record", "--vault", self.vault, "--project", "elsewhere",
                             "--process", "build:x", "--duration", "1", "--exit", "0"))
        self.assertEqual(len(list((self.home / ".claude" / "golden-thread" / "metrics" /
                                   "elsewhere").glob("*.jsonl"))), 1)
        p = self.m("record", "--vault", self.vault, "--project", "app", "--process", "x:y",
                   "--duration", "1", "--exit", "0", "--dry-run")
        self.assertOk(p)
        self.assertEqual(self.rows("app"), [])

    def test_off_records_nothing(self):
        self.config(vault_path=str(self.vault), execution_metrics="off")
        p = self.m("record", "--vault", self.vault, "--project", "app", "--process", "x:y",
                   "--duration", "1", "--exit", "0")
        self.assertOk(p)
        self.assertIn("off", p.stdout)
        p = self.m("time", "--vault", self.vault, "--project", "app", "--process", "x:z", "--",
                   "true")
        self.assertOk(p)
        self.assertEqual(self.rows("app"), [])


class Instrumented(MetricsBase):
    def test_a_release_pipeline_step_records_a_row(self):
        repo = self.git_init(self.tmp / "repo")
        self.run_cmd(["git", "-C", repo, "checkout", "-q", "-b", "f"])
        pl = SCRIPTS / "gt_pipeline.py"
        self.assertOk(self.py(pl, "init", "--repo", repo, "--project", "app"))
        (repo / ".gt-no-test-gate").write_text("")
        env = dict(self.env, GT_PIPELINE_PY=str(pl), GT_METRICS_PY=str(TOOL),
                   GT_METRICS_PROJECT="app", GT_VAULT=str(self.vault))
        self.run_cmd(["bash", repo / "release.sh", "--until", "allin"], env=env, cwd=repo)
        procs = {r["process"] for r in self.rows("app")}
        self.assertIn("release:tests", procs)
        self.assertIn("release", procs)

    def test_a_gt_skill_run_records_a_row(self):
        env = dict(self.env, GT_METRICS_PROJECT="app", GT_VAULT=str(self.vault))
        empty = self.tmp / "empty"
        empty.mkdir()
        self.run_cmd([PYTHON, SCRIPTS / "gt_allin.py", "--only", "secrets", "--repo", empty],
                     env=env, timeout=300)
        procs = [r for r in self.rows("app") if r["process"] == "skill:gt-allin"]
        self.assertEqual(len(procs), 1)
        self.assertEqual(procs[0]["units"], 1)

    def test_the_test_runner_records_a_row(self):
        env = dict(self.env, GT_METRICS_PROJECT="app", GT_VAULT=str(self.vault),
                   GT_TEST_VERSION=SCRIPTS.parent.name)
        p = self.run_cmd([PYTHON, REPO / "tests" / "prun.py", "-j", "1",
                          "test_gt_recipe.Refusals"], env=env, cwd=REPO / "tests", timeout=300)
        self.assertOk(p)
        rows = [r for r in self.rows("app") if r["process"] == "tests:suite"]
        self.assertEqual(len(rows), 1, p.stdout)
        self.assertEqual((rows[0]["units"], rows[0]["exit"]), (1, 0))


class Baselines(MetricsBase):
    def test_regression_names_what_changed(self):
        self.seed("app", "tests:suite", [10, 11, 10, 12, 10, 11], units=40, parallel=8)
        self.seed("app", "tests:suite", [30], start=time.time(), units=40, parallel=1)
        p = self.m("report", "--vault", self.vault, "--project", "app", "--json")
        self.assertEqual(p.returncode, 1, p.stdout)
        s = json.loads(p.stdout)["processes"][0]
        self.assertEqual(s["baseline"]["duration"]["median"], 10.5)
        self.assertIsNotNone(s["baseline"]["duration"]["p90"])
        self.assertIn("parallelism 8 -> 1", "; ".join(s["regression"]["changed"]))
        p = self.m("report", "--vault", self.vault)
        self.assertIn("REGRESSION", p.stdout)

    def test_cross_project_report_and_peers(self):
        self.seed("app", "tests:suite", [100, 100, 100], units=10, parallel=1)
        self.seed("golden-thread", "tests:suite", [20, 20, 20], units=10, parallel=8,
                  traits=["cached-install"])
        p = self.m("report", "--vault", self.vault, "--json")
        projects = {s["project"] for s in json.loads(p.stdout)["processes"]}
        self.assertEqual(projects, {"app", "golden-thread"})
        p = self.m("peers", "--vault", self.vault, "--json")
        peers = json.loads(p.stdout)
        self.assertEqual(len(peers), 1)
        self.assertEqual((peers[0]["project"], peers[0]["peer_project"]), ("app", "golden-thread"))
        self.assertTrue(any("8-wide" in d for d in peers[0]["differences"]))
        self.assertTrue(any("cached-install" in d for d in peers[0]["differences"]))

    def test_verify_beat_and_did_not_beat(self):
        t0 = time.time() - 86400 * 5
        h = 3600
        self.seed("app", "build:x", [100, 100, 100], start=t0)
        self.assertOk(self.m("mark", "--vault", self.vault, "--project", "app", "--process",
                             "build:x", "--change", "cache deps", "--start", t0 + 4 * h,
                             "--rollback", "gt_settings.py set x off"))
        self.seed("app", "build:x", [60, 60, 60], start=t0 + 5 * h)
        p = self.m("verify", "--vault", self.vault, "--project", "app", "--process", "build:x",
                   "--json")
        self.assertOk(p)
        r = json.loads(p.stdout)
        self.assertEqual((r["verdict"], r["saved_per_run"]), ("beat", 40.0))
        self.assertOk(self.m("mark", "--vault", self.vault, "--project", "app", "--process",
                             "build:x", "--change", "more workers", "--start", t0 + 10 * h,
                             "--rollback", "gt_settings.py set parallel_max auto"))
        self.seed("app", "build:x", [90, 95, 90], start=t0 + 11 * h)
        p = self.m("verify", "--vault", self.vault, "--project", "app", "--process", "build:x")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("DID-NOT-BEAT", p.stdout)
        self.assertIn("Roll back: gt_settings.py set parallel_max auto", p.stdout)

if __name__ == "__main__":
    unittest.main()
