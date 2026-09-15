"""gt_version_check.py: is the installed release still the newest one checked in?

Fixtures: a fake plugin source root holding version dirs (installable = N.N.N with
.claude-plugin/plugin.json) and a sandbox ~/.claude/plugins/installed_plugins.json.
"""
import json
import unittest

from _harness import Sandbox, SCRIPTS, load_module

TOOL = SCRIPTS / "gt_version_check.py"


class VersionCheck(Sandbox):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "Golden Thread" / "golden-thread-plugin"
        self.root.mkdir(parents=True)

    def release(self, version, sub="golden-thread", installable=True):
        d = self.root / sub / version
        (d / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        if installable:
            (d / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": "gt", "version": version}))
        return d

    def installed(self, gt=None, wiki=None):
        p = self.home / ".claude" / "plugins" / "installed_plugins.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        plugins = {}
        if gt:
            plugins["gt@golden-thread-plugin"] = [{"version": gt, "scope": "user"}]
        if wiki:
            plugins["gt-wiki@golden-thread-plugin"] = [{"version": wiki}]
        p.write_text(json.dumps({"version": 2, "plugins": plugins}))
        return p

    def check(self, *extra, root=None, **kw):
        p = self.py(TOOL, "check", root or self.root, *extra, **kw)
        self.assertOk(p, "version check must never fail a session start")
        return p.stdout

    # -- ordering ------------------------------------------------------------------
    def test_sorts_numerically_not_lexically(self):
        for v in ("0.9.4", "0.9.13", "0.10.0", "0.2.0"):
            self.release(v)
        self.installed(gt="0.9.13")
        out = self.check()
        self.assertIn("0.9.13 installed, 0.10.0 available", out)
        self.assertIn("install with:", out)
        self.assertIn(str(self.root), out)

    def test_available_ordering_in_process(self):
        for v in ("0.9.4", "0.10.0", "0.9.13", "1.0.0-rc1", "notes"):
            self.release(v)
        self.release("0.11.0", installable=False)
        import os
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)      # the module expands ~ at import
        try:
            m = load_module(TOOL, "gt_version_check_under_test")
        finally:
            os.environ["HOME"] = old_home
        self.assertEqual(m.available(str(self.root / "golden-thread")),
                         ["0.9.4", "0.9.13", "0.10.0"])
        self.assertGreater(m.parse("0.10.0"), m.parse("0.9.13"))
        self.assertIsNone(m.parse("0.9"))
        self.assertIsNone(m.parse(None))
        self.assertEqual(m.available(str(self.tmp / "nope")), [])

    def test_version_dir_without_plugin_json_is_not_offered(self):
        self.release("0.9.13")
        self.release("0.10.0", installable=False)          # WIP / archive
        self.installed(gt="0.9.13")
        out = self.check()
        self.assertIn("current", out)
        self.assertNotIn("0.10.0", out)

    # -- the three answers ---------------------------------------------------------
    def test_current(self):
        self.release("0.9.13")
        self.release("0.9.4")
        self.installed(gt="0.9.13")
        self.assertEqual(self.check().strip(),
                         "GOLDEN THREAD version: current — newest release installed.")

    def test_installed_ahead_never_recommends_install(self):
        self.release("0.9.13")
        self.installed(gt="0.10.1")
        out = self.check()
        self.assertIn("0.10.1 installed", out)
        self.assertIn("AHEAD", out)
        self.assertNotIn("install with", out,
                         "install.sh would DOWNGRADE an ahead release; never offer it")

    def test_both_plugins_reported_independently(self):
        self.release("0.9.13")
        self.release("0.9.14")
        self.release("0.3.0", sub="golden-thread-wiki")
        self.release("0.3.2", sub="golden-thread-wiki")
        self.installed(gt="0.9.14", wiki="0.3.0")
        out = self.check()
        self.assertRegex(out, r"gt-wiki\s+0\.3\.0 installed, 0\.3\.2 available")
        self.assertNotRegex(out, r"\bgt\s+0\.9\.14")

    # -- module install choices (0.14.0) ------------------------------------------
    def choices(self, **modules):
        p = self.home / ".claude" / "golden-thread" / "install-choices.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": 1, "choices": modules}))
        return p

    def _needs_choices(self):
        if "install_choices" not in TOOL.read_text():
            self.skipTest("gt release under test predates module install choices")

    def test_wiki_off_by_choice_is_reported_not_missing(self):
        self._needs_choices()
        self.release("0.14.0")
        self.release("0.2.0", sub="golden-thread-wiki")
        self.installed(gt="0.14.0")
        self.choices(wiki="off")
        out = self.check()
        self.assertIn("current", out)
        self.assertRegex(out, r"gt-wiki\s+not installed by choice")
        self.assertIn("--with wiki", out)
        self.assertNotIn("install with:", out)

    def test_wiki_off_but_still_installed_is_not_offered_an_upgrade(self):
        self._needs_choices()
        self.release("0.14.0")
        self.release("0.2.0", sub="golden-thread-wiki")
        self.installed(gt="0.14.0", wiki="0.1.3")
        self.choices(wiki="off")
        out = self.check()
        self.assertNotRegex(out, r"0\.1\.3 installed, 0\.2\.0 available")
        self.assertIn("has wiki off", out)
        self.assertNotIn("install with:", out)

    def test_wiki_on_by_choice_behaves_as_before(self):
        self._needs_choices()
        self.release("0.14.0")
        self.release("0.2.0", sub="golden-thread-wiki")
        self.installed(gt="0.14.0", wiki="0.1.3")
        self.choices(wiki="on", demo="off")
        out = self.check()
        self.assertRegex(out, r"gt-wiki\s+0\.1\.3 installed, 0\.2\.0 available")
        self.assertNotIn("by choice", out)

    def test_no_choices_file_wiki_absent_stays_silent_about_it(self):
        self.release("0.14.0")
        self.release("0.2.0", sub="golden-thread-wiki")
        self.installed(gt="0.14.0")
        out = self.check()
        self.assertIn("current", out)
        self.assertNotIn("gt-wiki", out)

    def module_release(self, dirname, plugin, name, version):
        d = self.root / dirname / version
        (d / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (d / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": plugin, "version": version}))
        (d / "module.json").write_text(json.dumps({"name": name, "plugin": plugin,
                                                   "version": version}))

    def test_every_module_is_version_checked_not_just_gt_wiki(self):
        """Until 0.15.0 only gt and gt-wiki were compared, so a stale gt-watch (or gt-demo)
        beside a newer source release was never mentioned."""
        self.release("0.15.0")
        self.module_release("golden-thread-watch", "gt-watch", "watch", "0.15.0")
        self.module_release("golden-thread-watch", "gt-watch", "watch", "0.15.1")
        self.module_release("golden-thread-farm", "gt-farm", "farm", "0.15.0")
        p = self.installed(gt="0.15.0")
        data = json.loads(p.read_text())
        data["plugins"]["gt-watch@golden-thread-plugin"] = [{"version": "0.15.0"}]
        p.write_text(json.dumps(data))
        out = self.check()
        self.assertRegex(out, r"gt-watch\s+0\.15\.0 installed, 0\.15\.1 available")
        self.assertNotIn("gt-farm", out, "a module that is not installed stays silent")
        self.choices(farm="off")
        self.assertRegex(self.check(), r"gt-farm\s+not installed by choice")

    # -- degraded inputs -----------------------------------------------------------
    def test_source_root_missing_is_said_out_loud(self):
        self.installed(gt="0.9.13")
        out = self.check(root=self.tmp / "not-synced")
        self.assertIn("plugin source not readable", out)

    def test_unreadable_install_record_is_not_reported_as_current(self):
        # With no installed_plugins.json (or an unparseable one) the check cannot
        # know what is installed. Printing "newest release installed" then is the
        # false calm the module docstring says it exists to end.
        self.release("0.9.13")
        self.release("0.10.0")
        out = self.check()
        self.assertNotIn("newest release installed", out,
                         "claimed current with no installed_plugins.json to read")
        p = self.home / ".claude" / "plugins" / "installed_plugins.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{garbage")
        out = self.check()
        self.assertNotIn("newest release installed", out,
                         "claimed current with an unparseable installed_plugins.json")

    def test_non_semver_installed_version_is_skipped_not_crashed(self):
        self.release("0.9.13")
        self.installed(gt="dev-build")
        self.check()                                         # exit 0 asserted inside

    def test_policy_off(self):
        self.config(vault_path=str(self.tmp), version_check="off")
        self.release("0.10.0")
        self.installed(gt="0.9.13")
        self.assertEqual(self.check(), "")

    def test_invalid_policy_falls_back_to_report(self):
        self.config(vault_path=str(self.tmp), version_check="auto")
        self.release("0.10.0")
        self.installed(gt="0.9.13")
        self.assertIn("0.10.0 available", self.check())

    def test_hook_json(self):
        self.release("0.10.0")
        self.installed(gt="0.9.13")
        d = json.loads(self.check("--hook"))
        self.assertIn("0.9.13 installed, 0.10.0 available", d["systemMessage"])
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(d["hookSpecificOutput"]["additionalContext"], d["systemMessage"])

    def test_usage(self):
        p = self.py(TOOL)
        self.assertEqual(p.returncode, 2)
        p = self.py(TOOL, "check", "--hook")
        self.assertEqual(p.returncode, 2)
        self.assertIn("need the plugin source root", p.stdout)


if __name__ == "__main__":
    unittest.main()
