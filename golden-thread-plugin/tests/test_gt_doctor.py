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
        # No publish destination unless a test configures one.
        self.env.pop("GT_SRC", None)

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


class DoctorModulesCheck(DoctorBase):
    """`modules`: on/off and why, version, requires_gt, and an ON module's plugin state.

    OFF by choice is never drift; an ON module whose plugin is not registered or not
    enabled is. The check only READS installed_plugins.json and settings.json.
    """

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "Golden Thread" / "plugin"
        gt = self.root / "golden-thread" / "0.14.0" / ".claude-plugin"
        gt.mkdir(parents=True)
        (gt / "plugin.json").write_text('{"name": "gt", "version": "0.14.0"}')
        self.module("demo", "on", ">=0.14.0,<0.15.0")
        self.module("wiki", "on", ">=0.14.0")
        self.module("far", "on", ">=0.20.0")
        self.module("quiet", "off", ">=0.14.0")

    def module(self, name, default, requires):
        vd = self.root / ("golden-thread-" + name) / "1.0.0"
        (vd / ".claude-plugin").mkdir(parents=True)
        (vd / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "gt-" + name, "version": "1.0.0"}))
        (vd / "module.json").write_text(json.dumps({
            "schema": 1, "name": name, "plugin": "gt-" + name, "version": "1.0.0",
            "requires_gt": requires, "summary": "fixture", "default": default}))

    def register(self, installed=(), enabled=()):
        pl = self.home / ".claude" / "plugins"
        pl.mkdir(parents=True, exist_ok=True)
        (pl / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
            "gt-%s@golden-thread-plugin" % n: [{"version": "1.0.0"}] for n in installed}}))
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {
            "gt-%s@golden-thread-plugin" % n: True for n in enabled}}))

    def row(self):
        p = self.doctor("--json", "--only", "modules", "--plugin-root", str(self.root))
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual([c["check"] for c in data["checks"]], ["modules"])
        return data["checks"][0]

    def line(self, row, name):
        return next(l for l in row["detail"].splitlines() if l.startswith(name + " "))

    def test_healthy_modules_and_off_by_choice_is_not_drift(self):
        choices = self.home / ".claude" / "golden-thread" / "install-choices.json"
        choices.parent.mkdir(parents=True, exist_ok=True)
        choices.write_text(json.dumps({"version": 1, "choices": {"wiki": "off"}}))
        self.register(installed=("demo",), enabled=("demo",))
        row = self.row()
        self.assertEqual(row["state"], "ok", row)
        self.assertIn("4 module(s): 1 on, 3 off", row["summary"])
        demo = self.line(row, "demo")
        self.assertIn("on", demo)
        self.assertIn("1.0.0", demo)
        self.assertIn("admits gt 0.14.0", demo)
        self.assertIn("installed and enabled", demo)
        self.assertIn("not installed by choice (recorded choice)", self.line(row, "wiki"))
        self.assertIn("not installed by choice (module default)", self.line(row, "quiet"))
        far = self.line(row, "far")
        self.assertIn("does NOT admit gt 0.14.0", far)
        self.assertIn("requires gt >=0.20.0", far)

    def test_on_module_whose_plugin_is_missing_or_disabled_needs_attention(self):
        self.register(installed=("demo",), enabled=())
        row = self.row()
        self.assertEqual(row["state"], "warn")
        self.assertIn("not enabled in settings.json", self.line(row, "demo"))
        wiki = self.line(row, "wiki")
        self.assertIn("not in installed_plugins.json", wiki)
        self.assertIn("install.sh", row["fix"])

    def test_off_module_still_installed_is_reported(self):
        self.register(installed=("demo", "wiki", "quiet"), enabled=("demo", "wiki"))
        row = self.row()
        self.assertEqual(row["state"], "warn")
        self.assertIn("still installed", self.line(row, "quiet"))

    def test_the_check_only_reads(self):
        self.register(installed=("demo",), enabled=("demo",))
        files = [self.home / ".claude" / "plugins" / "installed_plugins.json",
                 self.home / ".claude" / "settings.json"]
        before = [f.read_bytes() for f in files]
        self.row()
        self.assertEqual([f.read_bytes() for f in files], before)
        self.assertFalse((self.home / ".claude" / "golden-thread" /
                          "install-choices.json").exists())

    def test_no_plugin_root_is_unknown(self):
        p = self.doctor("--json", "--only", "modules")
        self.assertEqual(json.loads(p.stdout)["checks"][0]["state"], "unknown")


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

    def test_no_destination_configured_is_skipped_not_failed(self):
        """Most machines never publish: no $GT_SRC and no gt_src key -> skipped, exit 0,
        and no guessed path anywhere in the report."""
        p = self.doctor("--json", "--only", "gt-src")
        data = json.loads(p.stdout)
        row = data["checks"][0]
        self.assertEqual(row["state"], "skipped")
        self.assertIn("not configured", row["summary"])
        self.assertEqual(p.returncode, 0, "an unconfigured destination is not a finding")
        self.assertNotIn("/", row["summary"] + row["detail"] + row["fix"],
                         "the skipped row must not name a guessed destination path")
        human = self.doctor("--only", "gt-src")
        self.assertIn("not configured", human.stdout)
        self.assertNotIn("FAIL", human.stdout)

    def test_gt_src_from_vault_config_is_checked(self):
        d = self.tmp / "configured-dest"
        d.mkdir()
        (d / "SOURCE.json").write_text(json.dumps({"commit": "0123456789ab", "gt": "0.13.0"}))
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "vault-config.json").write_text(json.dumps({"gt_src": str(d)}))
        row = json.loads(self.doctor("--json", "--only", "gt-src").stdout)["checks"][0]
        self.assertEqual(row["state"], "ok")
        self.assertIn("012345678", row["summary"])

    def test_configured_destination_that_is_missing_is_a_warning(self):
        self.env["GT_SRC"] = str(self.tmp / "ghost-dest")
        row = json.loads(self.doctor("--json", "--only", "gt-src").stdout)["checks"][0]
        self.assertEqual(row["state"], "warn")
        self.assertIn("does not exist", row["summary"])

    def test_foreign_top_level_directory_is_named(self):
        """The 2026-09-11 shape: a flat scripts/ nobody's publisher wrote."""
        d = self.dest(**{"SOURCE.json": json.dumps({"commit": "abc123", "gt": "0.12.0"})})
        (d / "scripts").mkdir()
        (d / "scripts" / "stray.py").write_text("x")
        p = self.doctor("--json", "--only", "gt-src")
        row = json.loads(p.stdout)["checks"][0]
        self.assertEqual(row["state"], "warn")
        self.assertIn("scripts", row["detail"])

    def test_every_published_plugin_dir_is_expected_and_a_conflict_copy_is_not(self):
        """0.15.0: the expected set was a literal list from before modules, so it flagged
        golden-thread-demo and would flag every module dir after a publish."""
        d = self.dest(**{"SOURCE.json": json.dumps({"commit": "abc123", "gt": "0.15.0"}),
                         "install-SomeHost.sh": "x"})
        for plugin, ver in (("golden-thread", "0.15.0"), ("golden-thread-demo", "0.15.0"),
                            ("golden-thread-watch", "0.15.0"), ("golden-thread-flow", "0.15.0")):
            (d / plugin / ver / ".claude-plugin").mkdir(parents=True)
            (d / plugin / ver / ".claude-plugin" / "plugin.json").write_text("{}")
        (d / "golden-thread-lookalike").mkdir()           # no release inside: not a plugin
        row = json.loads(self.doctor("--json", "--only", "gt-src").stdout)["checks"][0]
        self.assertEqual(row["state"], "warn")
        for published in ("golden-thread-demo", "golden-thread-watch", "golden-thread-flow"):
            self.assertNotIn(published, row["detail"].replace("golden-thread-lookalike", ""))
        self.assertIn("install-SomeHost.sh", row["detail"])
        self.assertIn("golden-thread-lookalike", row["detail"])

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
