"""gt_doctor.py -- one command for the whole health picture.

Contract:
  * Always names the release the other answers are RELATIVE TO. A clean report from a
    check pinned to the wrong version is the 2026-08-30 failure; the version must be
    on the page.
  * Exit 0 all clear, 1 needs attention, 2 a check could not run. "Could not check"
    is never reported as clean.
  * Every check survives its dependency being absent: a missing script, an unreadable
    config, no vault -> UNKNOWN with a reason, never a traceback.
  * --fix only re-wires hooks (adds entries, removes none). It never touches vault
    content.
"""
import json
import os
import shutil
import unittest

from _harness import Sandbox, SCRIPTS, GT


DOCTOR = SCRIPTS / "gt_doctor.py"


class DoctorBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env.pop("GT_VAULT", None)
        # Nothing on this machine should reach the real publish destination.
        self.env["GT_SRC"] = str(self.tmp / "no-such-gt-src")

    def doctor(self, *args, **kw):
        return self.py(DOCTOR, *args, env=kw.pop("env", self.env), **kw)

    def install_hook_scripts(self, names):
        for n in names:
            shutil.copy2(GT / "scripts" / n, self.hooks / n)


class DoctorRuns(DoctorBase):
    def test_it_runs_with_nothing_installed_at_all(self):
        """The empty machine must produce a report, not a traceback."""
        p = self.doctor()
        self.assertIn(p.returncode, (1, 2), "an empty machine is not 'all clear'")
        self.assertIn("doctor", p.stdout)
        self.assertNotIn("Traceback", p.stdout + p.stderr)

    def test_missing_checks_are_unknown_not_clean(self):
        p = self.doctor()
        self.assertNotIn("all clear", p.stdout,
                         "checks that could not run were reported as clear")
        self.assertEqual(p.returncode, 2,
                         "a check that could not run must exit 2, distinct from a "
                         "finding (1) and from clean (0)")

    def test_the_release_is_always_named(self):
        """A clean answer means nothing without the version it is relative to."""
        p = self.doctor()
        self.assertIn("release", p.stdout.splitlines()[0])

    def test_json_is_parseable_and_carries_every_check(self):
        p = self.doctor("--json")
        data = json.loads(p.stdout)
        self.assertIn("checks", data)
        self.assertIn("worst", data)
        names = {c["check"] for c in data["checks"]}
        self.assertIn("version", names)
        self.assertIn("vault", names)

    def test_only_runs_one_check(self):
        p = self.doctor("--json", "--only", "vault")
        data = json.loads(p.stdout)
        self.assertEqual([c["check"] for c in data["checks"]], ["vault"])

    def test_unreadable_config_does_not_crash(self):
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "vault-config.json").write_text("{ not json")
        p = self.doctor("--json", "--only", "vault")
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["checks"][0]["state"], "unknown")


class DoctorVaultCheck(DoctorBase):
    def test_unmigrated_decisions_are_reported(self):
        v = self.make_vault()
        proj = v / "Projects" / "demo"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "decisions.md").write_text("# D\n\n## ADR-1: one\n")
        p = self.doctor("--json", "--vault", str(v), "--only", "vault")
        row = json.loads(p.stdout)["checks"][0]
        self.assertEqual(row["state"], "warn")
        self.assertIn("demo", row["detail"])
        self.assertIn("migrat", row["fix"], "the finding must say how to fix it")

    def test_migrated_vault_is_clean(self):
        v = self.make_vault()
        spool = v / "Projects" / "golden-thread" / "spool"
        (spool / "log").mkdir(parents=True, exist_ok=True)
        (spool / "log" / "0000-baseline.md").write_text("x\n")
        p = self.doctor("--json", "--vault", str(v), "--only", "vault")
        self.assertEqual(json.loads(p.stdout)["checks"][0]["state"], "ok")

    def test_a_configured_vault_that_does_not_exist_is_a_failure(self):
        p = self.doctor("--json", "--vault", str(self.tmp / "ghost"), "--only", "vault")
        row = json.loads(p.stdout)["checks"][0]
        self.assertEqual(row["state"], "fail")
        self.assertIn("does not exist", row["summary"])


class DoctorGtSrcCheck(DoctorBase):
    def dest(self, **files):
        d = self.tmp / "gt-src"
        d.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (d / name).write_text(body)
        self.env["GT_SRC"] = str(d)
        return d

    def test_no_destination_is_not_a_problem(self):
        """Most machines never publish; the check must be silent there."""
        p = self.doctor("--json", "--only", "gt-src")
        self.assertEqual(json.loads(p.stdout)["checks"][0]["state"], "ok")

    def test_foreign_top_level_directory_is_named(self):
        """The 2026-09-11 shape: a flat scripts/ nobody's publisher wrote."""
        d = self.dest(**{"SOURCE.json": json.dumps({"commit": "abc123", "gt": "0.12.0"})})
        (d / "scripts").mkdir()
        (d / "scripts" / "stray.py").write_text("x")
        p = self.doctor("--json", "--only", "gt-src")
        row = json.loads(p.stdout)["checks"][0]
        self.assertEqual(row["state"], "warn")
        self.assertIn("scripts", row["detail"])

    def test_destination_without_provenance_is_flagged(self):
        self.dest(**{"README.md": "x"})
        row = json.loads(self.doctor("--json", "--only", "gt-src").stdout)["checks"][0]
        self.assertEqual(row["state"], "warn")
        self.assertIn("SOURCE.json", row["summary"])

    def test_clean_destination_reports_its_provenance(self):
        self.dest(**{"SOURCE.json": json.dumps({"commit": "deadbeefcafe", "gt": "0.12.0"})})
        row = json.loads(self.doctor("--json", "--only", "gt-src").stdout)["checks"][0]
        self.assertEqual(row["state"], "ok")
        self.assertIn("deadbeef", row["summary"], "say WHICH commit is published")


class DoctorWorkersAndLint(DoctorBase):
    def test_worker_check_is_reported_when_installed(self):
        self.install_hook_scripts(["gt_workers.py"])
        row = [c for c in json.loads(self.doctor("--json").stdout)["checks"]
               if c["check"] == "workers"][0]
        self.assertIn(row["state"], ("ok", "warn"))

    def test_lint_findings_are_summarised_not_dumped(self):
        self.install_hook_scripts(["gt_lint.py", "gt_paths.py"])
        v = self.make_vault()
        row = [c for c in json.loads(
            self.doctor("--json", "--vault", str(v)).stdout)["checks"]
            if c["check"] == "lint"][0]
        self.assertLess(len(row["summary"]), 200,
                        "the lint row is a summary; /gt:gt-lint gives the detail")


if __name__ == "__main__":
    unittest.main()
