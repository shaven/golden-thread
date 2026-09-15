"""gt-report-card as a gt 0.15.0 module: valid, and install.sh honours it end to end.

  * module.json is valid (gt_components.validate_module, and dev/plugins.py module-check
    when it exists); identity agrees with plugin.json and the version dir; its scripts
    list covers scripts/; its three hooks and two settings are the ones the card uses;
  * gt core no longer ships gt_report_card.py (replaces_core);
  * installed (default on): PreCompact, SessionEnd and SessionStart `surface --hook` are
    each wired once to the hooks-dir copy, which exists;
  * declined (--without report-card): none of those entries, no hooks-dir file;
  * report_card=off: the installed hook emits nothing and parks no notice;
  * on: a close-out candidate computed at PreCompact still surfaces at SessionStart.

The install tests fail while gt core still lists gt_report_card.py in HOOK_REGISTRATIONS
and HOOK_DIR_SCRIPTS: the core entries keep the hooks wired after --without.
"""
import json
import shlex
import shutil
import subprocess
import sys
import unittest

from _harness import Sandbox, REPO, GT, WIKI, SCRIPTS, load_module, latest_version_dir

MODULE = latest_version_dir(REPO / "golden-thread-report-card")
INSTALL = REPO / "install.sh"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
MARKET = "golden-thread-plugin"
SCRIPT = "gt_report_card.py"
EXPECTED_HOOKS = [("PreCompact", []), ("SessionEnd", []), ("SessionStart", ["surface", "--hook"])]


class ReportCardModuleJson(unittest.TestCase):
    def setUp(self):
        self.mod = json.loads((MODULE / "module.json").read_text(encoding="utf-8"))
        self.plugin = json.loads((MODULE / ".claude-plugin" / "plugin.json").read_text())

    def test_validates(self):
        gc = load_module(SCRIPTS / "gt_components.py", "gt_components_for_rc_module")
        data, reasons = gc.validate_module(str(MODULE))
        self.assertIsNotNone(data, "no module.json")
        self.assertEqual(reasons, [])

    def test_identity(self):
        self.assertEqual(self.mod["schema"], 1)
        self.assertEqual(self.mod["name"], "report-card")
        self.assertEqual(self.mod["plugin"], "gt-report-card")
        self.assertEqual(self.plugin["name"], "gt-report-card")
        self.assertEqual(self.mod["version"], self.plugin["version"])
        self.assertEqual(self.mod["version"], MODULE.name)
        self.assertEqual(self.mod["default"], "on")

    def test_contents(self):
        self.assertEqual(self.mod["scripts"], [SCRIPT])
        self.assertEqual(self.mod["hookdir_scripts"], [SCRIPT])
        shipped = sorted(p.name for p in (MODULE / "scripts").iterdir()
                         if p.name != "__pycache__" and not p.name.startswith("."))
        self.assertEqual(shipped, [SCRIPT])
        self.assertEqual([(h["event"], h["args"]) for h in self.mod["hooks"]], EXPECTED_HOOKS)
        for h in self.mod["hooks"]:
            self.assertEqual((h["script"], h["kind"]), (SCRIPT, "reporter"))
        keys = {s["key"]: s for s in self.mod["settings"]}
        self.assertEqual(set(keys), {"report_card", "closeout_check"})
        self.assertEqual(keys["report_card"]["default"], "minimal")
        self.assertEqual(keys["report_card"]["values"], ["off", "minimal", "full"])
        self.assertEqual(keys["closeout_check"]["default"], "ask")
        self.assertEqual(keys["closeout_check"]["values"], ["off", "ask"])
        self.assertEqual(self.mod.get("skills", []), [])

    def test_core_no_longer_ships_it(self):
        self.assertIn("scripts/%s" % SCRIPT, self.mod["replaces_core"])
        self.assertFalse((GT / "scripts" / SCRIPT).exists(),
                         "gt core still ships %s beside the module" % SCRIPT)

    def test_dev_module_check_when_available(self):
        tool = REPO / "dev" / "plugins.py"
        usage = subprocess.run([sys.executable, str(tool)], capture_output=True, text=True)
        if "module-check" not in (usage.stdout + usage.stderr):
            self.skipTest("dev/plugins.py has no module-check yet")
        p = subprocess.run([sys.executable, str(tool), "module-check", str(MODULE)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class ReportCardInstalled(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / MARKET
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        for src, dirname in ((GT, "golden-thread"), (WIKI, "golden-thread-wiki"),
                             (MODULE, "golden-thread-report-card")):
            shutil.copytree(src, self.repo / dirname / src.name, ignore=IGNORE)
        gt = self.repo / "golden-thread" / GT.name
        self.assertOk(self.py(gt / "scripts" / "gt_components.py", "manifest", gt))

    @property
    def hooks_dir(self):
        return self.home / ".claude" / "golden-thread" / "hooks"

    def install(self, *args):
        p = self.sh(self.repo / "install.sh", *args, timeout=300)
        self.assertOk(p, "install.sh failed")
        return p

    def entries(self):
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        return [(ev, h.get("command", "")) for ev, blocks in s.get("hooks", {}).items()
                for b in blocks for h in b.get("hooks", [])]

    def card_entries(self):
        return [(ev, shlex.split(c)) for ev, c in self.entries() if SCRIPT in c]

    def setting(self, **kv):
        p = self.home / ".claude" / "vault-config.json"
        d = json.loads(p.read_text())
        d.update(kv)
        p.write_text(json.dumps(d))

    def run_entry(self, event, stdin=""):
        cmds = [argv for ev, argv in self.card_entries() if ev == event]
        self.assertEqual(len(cmds), 1, self.card_entries())
        p = self.run_cmd(cmds[0], input=stdin)
        self.assertOk(p, "the %s report-card hook failed" % event)
        return p

    def test_on_by_default_wires_the_three_hooks_to_the_hooks_dir(self):
        p = self.install("--no-vault")
        self.assertRegex(p.stdout, r"Modules: .*report-card on")
        self.assertTrue((self.hooks_dir / SCRIPT).is_file())
        got = sorted((ev, argv[2:]) for ev, argv in self.card_entries())
        self.assertEqual(got, sorted(EXPECTED_HOOKS))
        for _ev, argv in self.card_entries():
            self.assertEqual(argv[1], str(self.hooks_dir / SCRIPT))

    def test_declined_leaves_no_hook_and_no_hookdir_file(self):
        self.install("--no-vault")
        p = self.install("--no-vault", "--without", "report-card")
        self.assertIn("report-card off", p.stdout)
        self.assertEqual(self.card_entries(), [], "report card still wired after --without")
        self.assertFalse((self.hooks_dir / SCRIPT).exists(), "hooks-dir copy left behind")
        self.assertFalse((self.home / ".claude" / "plugins" / "cache" / MARKET
                          / "gt-report-card").exists())

    def test_declined_on_a_first_install(self):
        self.install("--no-vault", "--without=report-card")
        self.assertEqual(self.card_entries(), [])
        self.assertFalse((self.hooks_dir / SCRIPT).exists())

    def _vault_with_finished_project(self):
        vault = self.tmp / "vault"
        self.install("--vault", vault)
        d = vault / "Projects" / "done-proj"
        d.mkdir(parents=True)
        (d / "README.md").write_text(
            "---\ntype: project\nslug: done-proj\nstage: active\n---\n\n# Done\n\n"
            "## Tasks\n\n- [x] one\n- [x] two\n\n## Notes\n")
        if (vault / ".git").exists():
            self.run_cmd(["git", "-C", vault, "add", "-A"])
            self.run_cmd(["git", "-C", vault, "commit", "-q", "-m", "fixture"])
        return vault

    def test_off_emits_nothing(self):
        self._vault_with_finished_project()
        self.setting(report_card="off", closeout_check="ask")
        hook = json.dumps({"session_id": "s-off", "hook_event_name": "PreCompact"})
        self.assertEqual(self.run_entry("PreCompact", hook).stdout, "")
        self.assertEqual(self.run_entry("SessionEnd", hook).stdout, "")
        self.assertFalse((self.home / ".claude" / "golden-thread" / "notices"
                          / "report-card.md").exists())
        self.assertEqual(self.run_entry("SessionStart").stdout, "")

    def test_closeout_notice_surfaces_at_session_start(self):
        self._vault_with_finished_project()
        self.setting(report_card="minimal", closeout_check="ask")
        hook = json.dumps({"session_id": "s-on", "hook_event_name": "PreCompact"})
        self.assertIn("ASK THE USER", self.run_entry("PreCompact", hook).stdout)
        out = self.run_entry("SessionStart").stdout
        d = json.loads(out)
        self.assertIn("report card", d["systemMessage"])
        ctx = d["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ASK THE USER", ctx)
        self.assertIn("done-proj", ctx)
        self.assertIn("session s-on", ctx)
        self.assertEqual(self.run_entry("SessionStart").stdout, "", "surfaced twice")


if __name__ == "__main__":
    unittest.main()
