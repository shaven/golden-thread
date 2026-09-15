"""gt-watch as a gt 0.15.0 module: watch moved out of gt core into its own plugin.

  * module.json is valid (gt_components.validate_module and dev/plugins.py module-check),
    agrees with .claude-plugin/plugin.json, the version directory and the tree;
  * everything it lists in replaces_core is in the module and no longer in gt;
  * the SessionStart reporter, its hook-dir copy and the `watch` setting are declared;
  * install.sh installs it by default and `--without watch` leaves no skill, script or hook;
  * the installed hook is silent while the `watch` setting is off (the default).
"""
import json
import re
import shutil
import subprocess
import sys
import unittest

from _harness import Sandbox, GT, WIKI, REPO, PYTHON, load_module, latest_version_dir

WATCH = latest_version_dir(REPO / "golden-thread-watch")
INSTALL = REPO / "install.sh"
MARKET = "golden-thread-plugin"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
KEYS = {"schema", "name", "plugin", "version", "requires_gt", "summary", "default", "demo",
        "skills", "scripts", "templates", "hooks", "hookdir_scripts", "settings",
        "requires_modules", "replaces_core"}
MOVED = ("skills/gt-watch", "scripts/gt_watch.py", "templates/watch.md")


class WatchModuleJson(unittest.TestCase):
    def setUp(self):
        self.mod = json.loads((WATCH / "module.json").read_text(encoding="utf-8"))
        self.plugin = json.loads((WATCH / ".claude-plugin" / "plugin.json").read_text())

    def test_validates(self):
        sys.path.insert(0, str(GT / "scripts"))
        try:
            gc = load_module(GT / "scripts" / "gt_components.py", "gtc_watch_module")
        finally:
            sys.path.pop(0)
        data, reasons = gc.validate_module(str(WATCH))
        self.assertEqual(reasons, [], "module.json does not validate")
        self.assertEqual(set(data) - KEYS, set())

    def test_identity_agrees_with_plugin_json_and_dir(self):
        self.assertEqual((self.mod["name"], self.mod["plugin"]), ("watch", "gt-watch"))
        self.assertEqual(self.plugin["name"], "gt-watch")
        self.assertEqual(self.mod["version"], self.plugin["version"])
        self.assertEqual(self.mod["version"], WATCH.name)
        self.assertEqual(self.mod["default"], "on")
        for clause in self.mod["requires_gt"].split(","):
            self.assertRegex(clause.strip(), r"^(>=|<=|==|<|>)\d+\.\d+\.\d+$")

    def test_listed_entries_cover_the_tree(self):
        for group in ("skills", "scripts", "templates"):
            shipped = sorted(p.name for p in (WATCH / group).iterdir()
                             if p.name != "__pycache__" and not p.name.startswith("."))
            self.assertEqual(sorted(self.mod[group]), shipped,
                             "module.json %s disagrees with %s/" % (group, group))

    def test_hook_hookdir_script_and_setting_declared(self):
        self.assertEqual(self.mod["hooks"], [{"event": "SessionStart", "script": "gt_watch.py",
                                              "args": ["--hook"], "kind": "reporter"}])
        self.assertEqual(self.mod["hookdir_scripts"], ["gt_watch.py"])
        self.assertEqual(len(self.mod["settings"]), 1)
        s = self.mod["settings"][0]
        self.assertEqual({k: s[k] for k in ("key", "default", "values")},
                         {"key": "watch", "default": "off", "values": ["off", "report"]})
        # The long explanation moved with the setting (optional `detail`, 0.15.0).
        self.assertTrue(s["summary"].strip())
        self.assertIn("report", s.get("detail", ""))

    def test_replaces_core_moved_out_of_gt(self):
        self.assertEqual(sorted(self.mod["replaces_core"]), sorted(MOVED))
        for rel in MOVED:
            self.assertTrue((WATCH / rel).exists(), rel)
            self.assertFalse((GT / rel).exists(), "gt %s still ships %s" % (GT.name, rel))

    def test_demo_act_is_well_formed_and_uses_the_module_scripts(self):
        self.assertEqual(self.mod["demo"], "demo/act.md")
        act = (WATCH / "demo" / "act.md").read_text()
        self.assertRegex(act, r"(?m)^## (?!Act )\S")
        for key in ("narration:", "do:", "point:"):
            self.assertRegex(act, r"(?m)^%s " % key)
        self.assertIn("the gt-watch skill", act)
        self.assertNotIn("<core>/gt_watch.py", act)
        for name in re.findall(r"<module:watch>/([A-Za-z0-9_.-]+)", act):
            self.assertTrue((WATCH / "scripts" / name).is_file(), name)

    def test_dev_module_and_manifest_check(self):
        for cmd in ("module-check", "manifest-check"):
            p = subprocess.run([PYTHON, str(REPO / "dev" / "plugins.py"), cmd, str(WATCH)],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class WatchModuleInstall(Sandbox):
    """install.sh against a throwaway HOME and a copy of the source tree."""

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / MARKET
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        shutil.copytree(GT, self.repo / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        shutil.copytree(WATCH, self.repo / "golden-thread-watch" / WATCH.name, ignore=IGNORE)
        gt_src = self.repo / "golden-thread" / GT.name
        self.assertOk(self.py(gt_src / "scripts" / "gt_components.py", "manifest", gt_src))
        self.claude = self.home / ".claude"
        self.hooks = self.claude / "golden-thread" / "hooks"

    def install(self, *args):
        p = self.sh(self.repo / "install.sh", *args, "--no-vault", timeout=300)
        self.assertOk(p, "install.sh failed")
        return p

    def commands(self):
        s = json.loads((self.claude / "settings.json").read_text())
        return [h.get("command", "") for blocks in s.get("hooks", {}).values()
                for b in blocks for h in b.get("hooks", [])]

    def test_on_by_default_then_without_leaves_no_skill_script_or_hook(self):
        self.install()
        cache = self.claude / "plugins" / "cache" / MARKET
        self.assertTrue(list(cache.glob("gt-watch/*/skills/gt-watch/SKILL.md")))
        self.assertTrue((self.hooks / "gt_watch.py").is_file())
        self.assertEqual(len([c for c in self.commands() if "gt_watch.py" in c]), 1, self.commands())

        self.install("--without", "watch")
        self.assertFalse((cache / "gt-watch").exists(), "module cache left behind")
        self.assertFalse(list(cache.glob("gt/*/skills/gt-watch")), "gt still carries the skill")
        self.assertFalse(list(cache.glob("*/*/scripts/gt_watch.py")), "a plugin still ships gt_watch.py")
        market = self.claude / "plugins" / "marketplaces" / MARKET / "plugins"
        self.assertFalse(list(market.glob("*/skills/gt-watch")), "marketplace still carries the skill")
        enabled = json.loads((self.claude / "settings.json").read_text()).get("enabledPlugins", {})
        self.assertNotIn("gt-watch@" + MARKET, enabled)
        self.assertFalse((self.hooks / "gt_watch.py").exists(), "gt_watch.py left in the hooks dir")
        self.assertEqual([c for c in self.commands() if "gt_watch.py" in c], [], "hook still wired")

    def test_installed_hook_is_silent_while_the_setting_is_off(self):
        self.install()
        vault = self.tmp / "vault"
        (vault / "Projects" / "golden-thread" / "watches").mkdir(parents=True)
        env = {"GT_VAULT": str(vault), "GT_WATCH_STATE": str(self.tmp / "state")}
        p = self.py(self.hooks / "gt_watch.py", "--hook", env=env)
        self.assertOk(p)
        self.assertEqual(p.stdout, "", "watch defaults to off and must then say nothing")
        cfg = self.claude / "vault-config.json"
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
        data["watch"] = "report"
        cfg.write_text(json.dumps(data))
        p = self.py(self.hooks / "gt_watch.py", "--hook", env=env)
        self.assertOk(p)
        self.assertIn("GOLDEN THREAD watch", p.stdout, "switched on, the hook reports")


if __name__ == "__main__":
    unittest.main()
