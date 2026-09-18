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
from pathlib import Path

from _harness import Sandbox, SCRIPTS, GT, ENFORCEMENT_HOOKS


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


STUB = '''#!/usr/bin/env python3
"""Stand-in for gt_components.py: replays a recorded report for one subcommand.

gt_components' `check` is ADVISORY -- it prints drift and exits 0 on purpose, because
it also runs as a SessionStart hook. That combination is the thing gt_doctor has to
read correctly, and only a stub lets a test pin an exact (output, exit code) pair.
"""
import json, sys, os
spec = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "stub-spec.json")))
cmd = (sys.argv[1] if len(sys.argv) > 1 else "")
out, rc = spec.get(cmd, ["", 2])
if out:
    print(out)
sys.exit(rc)
'''


class ComponentBase(DoctorBase):
    """A plugin root with one release, and a scripted gt_components in the hooks dir."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "plugin"
        vd = self.root / "golden-thread" / "0.16.4" / ".claude-plugin"
        vd.mkdir(parents=True)
        (vd / "plugin.json").write_text('{"name": "gt", "version": "0.16.4"}')

    def stub(self, **spec):
        """stub(check=("text", 0), wiring=("text", 1)) -> gt_components.py in hooks/."""
        (self.hooks / "gt_components.py").write_text(STUB)
        (self.hooks / "stub-spec.json").write_text(
            json.dumps({k: list(v) for k, v in spec.items()}))

    def row(self, which):
        p = self.doctor("--json", "--only", which, "--plugin-root", str(self.root))
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        data = json.loads(p.stdout)
        return data["checks"][0], p


class DoctorComponentsCheck(ComponentBase):
    """The component check reads gt_components' REPORT, not its exit code.

    `gt_components.py check` ends with `return 0  # never a failing exit: advisory
    only`. Deciding from that exit code made every drift row invisible: a wall of
    `missing packs/core/*.pack.json` exited 0 and was rendered as "installed files
    match <ver>" with the drift text thrown away. Found when `version` said 0.16.3 was
    installed while `components` said the files matched 0.16.4 -- and the files on disk
    agreed with `version`.
    """

    DRIFT = ("GOLDEN THREAD components drifted from 0.16.4:\n"
             "  missing   packs/core/classify.common.pack.json\n"
             "  stale     scripts/gt_log.py\n"
             "  apply with: python3 gt_components.py apply 0.16.4")

    def test_drift_that_exits_zero_is_still_drift(self):
        self.stub(check=(self.DRIFT, 0))
        row, p = self.row("components")
        self.assertEqual(row["state"], "warn",
                         "drift printed by an advisory check was read as clean: " + str(row))
        self.assertIn("packs/core/classify.common.pack.json", row["detail"],
                      "the drift text gt_components printed was discarded")
        self.assertIn("scripts/gt_log.py", row["detail"])
        self.assertIn("0.16.4", row["summary"], "say WHICH release the drift is against")
        self.assertEqual(p.returncode, 1)

    def test_actionable_drift_offers_apply(self):
        self.stub(check=(self.DRIFT, 0))
        row, _ = self.row("components")
        self.assertIn("apply", row["fix"])
        self.assertIn("0.16.4", row["fix"])

    def test_blocked_drift_is_reported_but_apply_is_not_offered(self):
        """gt_components: differs/ahead/no-manifest are never auto-applied — that would
        revert work that exists only on this machine."""
        self.stub(check=("GOLDEN THREAD components drifted from 0.16.4:\n"
                         "  differs   scripts/gt_paths.py  (installed copy differs from the "
                         "plugin source and which is newer could NOT be established)\n"
                         "  ahead     scripts/gt_log.py  (installed is NEWER)\n"
                         "  no-manifest MANIFEST.json", 0))
        row, _ = self.row("components")
        self.assertEqual(row["state"], "warn")
        self.assertIn("gt_paths.py", row["detail"])
        self.assertIn("gt_log.py", row["detail"])
        self.assertNotIn("apply", row["fix"],
                         "apply would revert work that exists only here")

    def test_extra_alone_is_benign_and_stays_ok(self):
        """Module-installed scripts (gt_report_card.py, gt_watch.py) are `extra` on a
        healthy install. Warning on those warns forever."""
        self.stub(check=("GOLDEN THREAD components drifted from 0.16.4:\n"
                         "  extra     gt_report_card.py  (installed, absent from plugin source)\n"
                         "  extra     gt_watch.py  (installed, absent from plugin source)\n"
                         "  `differs`/`ahead`/`extra` are never auto-applied", 0))
        row, p = self.row("components")
        self.assertEqual(row["state"], "ok",
                         "an install-extra file must not raise the state: " + str(row))
        self.assertEqual(p.returncode, 0)
        self.assertIn("gt_report_card.py", row["detail"], "still SAY what is extra")

    def test_extra_beside_real_drift_does_not_hide_it(self):
        self.stub(check=("GOLDEN THREAD components drifted from 0.16.4:\n"
                         "  missing   hooks/guard_vault_writes.py\n"
                         "  extra     gt_watch.py  (installed, absent from plugin source)", 0))
        row, _ = self.row("components")
        self.assertEqual(row["state"], "warn")
        self.assertIn("guard_vault_writes.py", row["detail"])

    def test_clean_is_clean(self):
        self.stub(check=("GOLDEN THREAD components: clean — installed matches 0.16.4, "
                         "all 12 hooks wired.", 0))
        row, p = self.row("components")
        self.assertEqual(row["state"], "ok")
        self.assertIn("0.16.4", row["summary"])
        self.assertEqual(p.returncode, 0)

    def test_a_silent_component_check_is_not_clean(self):
        """The drift policy `off` prints nothing. Silence is never 'the files match'."""
        self.stub(check=("", 0))
        row, p = self.row("components")
        self.assertEqual(row["state"], "unknown", str(row))
        self.assertEqual(p.returncode, 2)

    def test_unreadable_report_is_not_clean(self):
        self.stub(check=("some future format nobody here can parse", 0))
        row, _ = self.row("components")
        self.assertEqual(row["state"], "unknown", str(row))

    def test_against_the_real_gt_components(self):
        """End to end, no stub: a hooks dir with nothing in it but gt_components itself
        IS drift against a real release, and the real check exits 0 saying so."""
        self.install_hook_scripts(["gt_components.py"])
        p = self.doctor("--json", "--only", "components",
                        "--plugin-root", str(Path(GT).parent.parent))
        row = json.loads(p.stdout)["checks"][0]
        self.assertEqual(row["state"], "warn",
                         "real drift from the real gt_components read as clean: " + str(row))
        self.assertIn("missing", row["detail"])


class DoctorWiringCheck(ComponentBase):
    def test_badpath_is_not_every_declared_hook_is_wired(self):
        """gt_components emits `badpath` beside `unwired`: wired, but a path argument
        does not exist on this machine. Dropping those rows is the 2026-08-30 shape —
        a check pinned to a path that does not exist, reporting clean."""
        self.stub(wiring=("badpath  inject_core_rules.sh        UserPromptSubmit  "
                          "(vault /gone/vault does not exist)", 1))
        row, p = self.row("wiring")
        self.assertNotEqual(row["state"], "ok",
                            "a hook pinned to a missing path was reported wired: " + str(row))
        self.assertNotIn("every declared hook is wired", row["summary"])
        self.assertIn("inject_core_rules.sh", row["detail"])
        self.assertIn("/gone/vault", row["detail"], "say WHICH path is missing")
        self.assertEqual(p.returncode, 1)

    def test_unwired_is_still_reported(self):
        self.stub(wiring=("unwired  guard_vault_writes.sh       PreToolUse  (install.sh)", 1))
        row, _ = self.row("wiring")
        self.assertEqual(row["state"], "fail")
        self.assertIn("guard_vault_writes.sh", row["detail"])

    def test_both_kinds_are_counted_separately(self):
        self.stub(wiring=("unwired  guard_vault_writes.sh  PreToolUse  (install.sh)\n"
                          "badpath  inject_core_rules.sh   UserPromptSubmit  (no /gone)", 1))
        row, _ = self.row("wiring")
        self.assertEqual(row["state"], "fail")
        self.assertIn("1 not wired", row["summary"])
        self.assertIn("path that does not exist", row["summary"])

    def test_all_wired_is_ok(self):
        self.stub(wiring=("all 12 declared hooks are wired", 0))
        row, p = self.row("wiring")
        self.assertEqual(row["state"], "ok")
        self.assertEqual(p.returncode, 0)

    def test_a_usage_error_is_not_all_wired(self):
        """`wiring` exits 2 on a usage error, printing no rows at all — which used to
        read as 'no unwired rows', i.e. clean."""
        self.stub(wiring=("need a version dir", 2))
        row, p = self.row("wiring")
        self.assertEqual(row["state"], "unknown", str(row))
        self.assertEqual(p.returncode, 2)


class DoctorCoreRulesCheck(DoctorBase):
    """`core-rules`: does each rule that CLAIMS enforcement have ITS OWN mechanism wired?

    gt_lint's core-unenforced only asks whether SOME hook sits on the event, so a Stop
    entry belonging to anything at all made every validated rule look enforced. Nothing
    in the system verified the mechanism itself until this check.
    """

    MECHANISMS = {"reminder": ("UserPromptSubmit", "inject_core_rules.sh"),
                  "validated": ("Stop", "validate_response.sh")}

    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.rules = self.vault / "Projects" / "golden-thread" / "core-rules"
        self.rules.mkdir(parents=True)
        (self.rules / "core_rule_priority_model.md").write_text(
            "---\nname: core_rule_priority_model\nmetadata:\n  level: core\n"
            "  enforcement: reminder\n---\n\nThe model.\n")
        for name in ("inject_core_rules.sh", "validate_response.sh"):
            (self.hooks / name).write_text("#!/bin/sh\n")

    def rule(self, name, enforcement="validated"):
        (self.rules / (name + ".md")).write_text(
            "---\nname: %s\nmetadata:\n  node_type: memory\n  level: core\n"
            "  enforcement: %s\n---\n\nThe rule.\n" % (name, enforcement))

    def wire(self, *pairs):
        hooks = {}
        for event, command in pairs:
            hooks.setdefault(event, []).append(
                {"hooks": [{"type": "command", "command": command, "timeout": 10}]})
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}))

    def hook(self, name):
        return str(self.hooks / name)

    def wire_everything(self):
        self.wire(("UserPromptSubmit", self.hook("inject_core_rules.sh")),
                  ("Stop", self.hook("validate_response.sh")))

    def row(self):
        p = self.doctor("--json", "--vault", str(self.vault), "--only", "core-rules")
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual([c["check"] for c in data["checks"]], ["core-rules"])
        return data["checks"][0], p

    def test_the_check_exists(self):
        """The docstring advertised `core rules` while --only core-rules was a usage
        error and no check_core_rules existed."""
        p = self.doctor("--only", "core-rules", "--vault", str(self.vault))
        self.assertNotIn("invalid choice", p.stderr)
        self.assertIn("core-rules", p.stdout)

    def test_every_mechanism_wired_is_ok(self):
        self.rule("core_timestamp_every_message", "validated")
        self.rule("core_global_memory_scope", "reminder")
        self.wire_everything()
        row, p = self.row()
        self.assertEqual(row["state"], "ok", str(row))
        self.assertIn("3 Core rule(s)", row["summary"])
        self.assertEqual(p.returncode, 0)

    def test_a_rule_whose_mechanism_is_missing_is_a_failure(self):
        self.rule("core_timestamp_every_message", "validated")
        self.wire(("UserPromptSubmit", self.hook("inject_core_rules.sh")))
        row, p = self.row()
        self.assertEqual(row["state"], "fail", str(row))
        self.assertIn("core_timestamp_every_message.md", row["detail"])
        self.assertIn("Stop", row["detail"])
        self.assertIn("install-core-rules", row["fix"])
        self.assertEqual(p.returncode, 1)

    def test_a_foreign_hook_on_the_event_does_not_count_as_enforcement(self):
        """The gap this check closes: 'some Stop hook is wired' is not 'this rule's
        mechanism runs'."""
        self.rule("core_timestamp_every_message", "validated")
        self.wire(("UserPromptSubmit", self.hook("inject_core_rules.sh")),
                  ("Stop", "/opt/someone-elses/notify.sh"))
        row, _ = self.row()
        self.assertEqual(row["state"], "fail", str(row))
        self.assertIn("a different mechanism", row["detail"])

    def test_a_hook_wired_to_a_script_that_does_not_exist_is_a_failure(self):
        self.rule("core_timestamp_every_message", "validated")
        self.wire_everything()
        (self.hooks / "validate_response.sh").unlink()
        row, _ = self.row()
        self.assertEqual(row["state"], "fail", str(row))
        self.assertIn("does not exist", row["detail"])

    def test_a_core_rule_declaring_no_enforcement_is_a_failure(self):
        (self.rules / "core_nothing.md").write_text(
            "---\nname: core_nothing\nmetadata:\n  level: core\n---\n\nStored only.\n")
        self.wire_everything()
        row, _ = self.row()
        self.assertEqual(row["state"], "fail", str(row))
        self.assertIn("NO enforcement", row["detail"])

    def test_arguments_after_the_hook_path_still_count(self):
        self.rule("core_timestamp_every_message", "validated")
        self.wire(("UserPromptSubmit", self.hook("inject_core_rules.sh")),
                  ("Stop", self.hook("validate_response.sh") + " --quiet"))
        row, _ = self.row()
        self.assertEqual(row["state"], "ok", str(row))

    def test_unreadable_settings_is_unknown_not_enforced(self):
        self.rule("core_timestamp_every_message", "validated")
        (self.home / ".claude" / "settings.json").write_text("{ not json")
        row, p = self.row()
        self.assertEqual(row["state"], "unknown", str(row))
        self.assertEqual(p.returncode, 2)

    def test_a_vault_without_core_rules_is_skipped_not_clean(self):
        other = self.tmp / "plain"
        other.mkdir()
        self.wire_everything()
        p = self.doctor("--json", "--vault", str(other), "--only", "core-rules")
        row = json.loads(p.stdout)["checks"][0]
        self.assertEqual(row["state"], "skipped", str(row))

    def test_it_matches_a_real_vault_wired_by_vault_init(self):
        """No fixtures: the rules vault_init ships, wired by the installer that owns
        them, must come back enforced."""
        # Every enforcement hook has to exist before vault_init will wire any of them.
        for name in ENFORCEMENT_HOOKS:
            (self.hooks / name).write_text("#!/bin/sh\n")
        v = self.make_vault()
        proc = self.py(SCRIPTS / "vault_init.py", "install-core-rules", "--vault", v)
        self.assertOk(proc, "install-core-rules failed while building the fixture")
        p = self.doctor("--json", "--vault", str(v), "--only", "core-rules")
        row = json.loads(p.stdout)["checks"][0]
        self.assertEqual(row["state"], "ok", str(row))
        self.assertIn("Core rule(s)", row["summary"])


class DoctorExitContract(DoctorBase):
    def test_a_check_that_could_not_run_is_named_even_when_a_failure_wins_the_exit(self):
        """Report.worst ranks FAIL above UNKNOWN, so a run with both exits 1 — which
        reads as 'everything was checked and something is wrong'. The report must say
        otherwise, in the footer and in the JSON."""
        p = self.doctor("--vault", str(self.tmp / "ghost"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("could not run", p.stdout.strip().splitlines()[-1])
        j = json.loads(self.doctor("--json", "--vault", str(self.tmp / "ghost")).stdout)
        self.assertTrue(j["unrunnable"], "the JSON must name the unrunnable checks")
        self.assertEqual(j["worst"], "fail")


class DoctorUnderlyingCheckCrashes(DoctorBase):
    """No probe here fails its exit code — they are all advisory — so a crash comes back
    as garbage on stdout with rc 0 or 2. None of it may be read as clean."""

    def crasher(self, name):
        (self.hooks / name).write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            "sys.stderr.write('Traceback (most recent call last):\\nBoom\\n')\n"
            "sys.exit(2)\n")

    def test_a_crashed_linter_is_not_a_healthy_vault(self):
        self.crasher("gt_lint.py")
        v = self.tmp / "vault"     # any directory: the linter is the thing under test
        v.mkdir()
        row = [c for c in json.loads(self.doctor("--json", "--vault", str(v)).stdout)
               ["checks"] if c["check"] == "lint"][0]
        self.assertEqual(row["state"], "unknown", str(row))

    def test_a_crashed_worker_check_is_not_no_workers(self):
        self.crasher("gt_workers.py")
        row = [c for c in json.loads(self.doctor("--json").stdout)["checks"]
               if c["check"] == "workers"][0]
        self.assertEqual(row["state"], "unknown", str(row))

    def test_a_crashed_push_check_is_not_in_sync(self):
        self.crasher("gt_push_check.py")
        row = [c for c in json.loads(self.doctor("--json").stdout)["checks"]
               if c["check"] == "push"][0]
        self.assertEqual(row["state"], "unknown", str(row))


if __name__ == "__main__":
    unittest.main()
