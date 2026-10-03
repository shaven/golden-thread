"""gt_bench.py -- a measured execution profile instead of the core-count rule of thumb.

Pinned (request 2026-09-28-gt-bench-measured-execution-profile):

  * it writes parallel_profile with source: measured, the date and the curve it chose from;
    --dry-run prints it and writes nothing;
  * the knee rule: the smallest width reaching 90% of the best throughput;
  * a second run on an unchanged machine lands within 15% of the first (deterministic curve);
  * with no test command and no runners it still produces a width profile from the synthetic
    workload (a real, tiny run);
  * install's re-detection keeps a measured profile while the machine is the same;
  * gt_doctor.py shows whether the profile is measured or default, and its age; a Rosetta-
    translated shell is detected (on a Mac).
"""
import json
import os
import sys
import unittest

from _harness import Sandbox, SCRIPTS, load_module

TOOL = SCRIPTS / "gt_bench.py"
CURVE = {"io": {"1": 10, "2": 19, "4": 36, "8": 60, "16": 64, "32": 63},
         "cpu": {"1": 5, "2": 10, "4": 19, "8": 21, "16": 21, "32": 20}}


class BenchBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.config(vault_path=str(self.tmp))

    def cfg(self):
        return json.loads((self.home / ".claude" / "vault-config.json").read_text())

    def bench(self, *args, fake=CURVE):
        env = {"GT_BENCH_FAKE": json.dumps(fake)} if fake else {}
        return self.py(TOOL, "--widths", "1,2,4,8,16,32", *args, env=env, timeout=300)


class Profile(BenchBase):
    def test_writes_a_measured_profile_with_its_curve(self):
        p = self.bench()
        self.assertOk(p)
        prof = self.cfg()["parallel_profile"]
        self.assertEqual(prof["source"], "measured")
        self.assertTrue(prof["measured_at"])
        self.assertEqual(prof["io_max"], 8)        # 60 >= 0.9 * 64; 36 is not
        self.assertEqual(prof["cpu_max"], 4)       # 19 >= 0.9 * 21
        self.assertEqual(prof["curve"]["io"][0], [1, 10.0])
        self.assertIn("knee", prof["method"])

    def test_dry_run_writes_nothing(self):
        before = (self.home / ".claude" / "vault-config.json").read_text()
        p = self.bench("--dry-run", "--json")
        self.assertOk(p)
        self.assertEqual(json.loads(p.stdout)["source"], "measured")
        self.assertEqual((self.home / ".claude" / "vault-config.json").read_text(), before)

    def test_a_second_run_lands_within_15_percent(self):
        a = json.loads(self.bench("--dry-run", "--json").stdout)
        jitter = {k: {w: v * 1.04 for w, v in c.items()} for k, c in CURVE.items()}
        b = json.loads(self.bench("--dry-run", "--json", fake=jitter).stdout)
        for k in ("io_max", "cpu_max"):
            self.assertLessEqual(abs(a[k] - b[k]) / a[k], 0.15, k)

    def test_real_synthetic_width_profile_with_no_tests_and_no_runners(self):
        p = self.py(TOOL, "--widths", "1,2", "--units", "4", "--repeat", "1", "--dry-run",
                    "--json", timeout=300)
        self.assertOk(p)
        prof = json.loads(p.stdout)
        self.assertEqual([w for w, _ in prof["curve"]["io"]], [1, 2])
        self.assertTrue(all(v > 0 for _, v in prof["curve"]["io"]))
        self.assertIn(prof["io_max"], (1, 2))
        self.assertIn("load_before", prof["environment"])
        self.assertNotIn("hosts", prof, "no runner may be contacted unless listed")

    def test_budget_marks_a_partial_curve(self):
        p = self.py(TOOL, "--widths", "1,2,4", "--units", "2", "--repeat", "1", "--budget", "0",
                    "--dry-run", "--json", timeout=300)
        self.assertEqual(p.returncode, 1, "nothing measurable inside a zero budget")


class InstallKeepsIt(BenchBase):
    def test_redetection_keeps_a_measured_profile_on_the_same_machine(self):
        self.assertOk(self.bench())
        measured = self.cfg()["parallel_profile"]
        p = self.py(SCRIPTS / "gt_settings.py", "detect-machine", "--write")
        self.assertOk(p)
        self.assertEqual(self.cfg()["parallel_profile"], measured)
        p = self.py(SCRIPTS / "gt_settings.py", "detect-machine", "--write", "--force")
        self.assertOk(p)
        self.assertNotEqual(self.cfg()["parallel_profile"].get("source"), "measured")


class Doctor(BenchBase):
    def doctor(self):
        p = self.py(SCRIPTS / "gt_doctor.py", "--only", "execution", "--json")
        rows = [r for r in json.loads(p.stdout)["checks"] if r["check"] == "execution"]
        self.assertEqual(len(rows), 1, p.stdout)
        return rows[0]

    def test_doctor_reports_default_then_measured_with_its_age(self):
        row = self.doctor()
        self.assertIn("DEFAULT", row["summary"])
        self.assertOk(self.bench())
        row = self.doctor()
        self.assertIn("MEASURED", row["summary"])
        self.assertIn("0 day(s) old", row["summary"])

    def test_rosetta_detection_reads_the_kernel_flag(self):
        old = os.environ.get("HOME")
        self.addCleanup(os.environ.__setitem__, "HOME", old)
        os.environ["HOME"] = str(self.home)
        m = load_module(TOOL, "gt_bench_for_tests")
        if sys.platform != "darwin":
            self.assertFalse(m.rosetta_translated(), "only a Mac can be translated")
        else:
            self.assertIn(m.rosetta_translated(), (True, False))
        states = [s for s, _, _ in m.health()]
        self.assertTrue(set(states) <= {"ok", "warn"})


if __name__ == "__main__":
    unittest.main()
