"""install.sh installs, removes and remembers MODULES (0.14.0).

A module is a plugin beside gt whose version dir carries module.json (contract:
Projects/golden-thread/design-addons.md, module contract 0.14.0). Contracts pinned here:
  * a module that defaults on is installed, registered, enabled, its hookdir_scripts
    copied and its hooks wired like gt's own;
  * --without NAME removes every trace (cache, marketplace entry and dir,
    installed_plugins, enabledPlugins, hook entries, hooks-dir files), backs up first,
    prints each removal and records the choice; a plain re-install keeps it off;
    --with NAME brings it back and records that;
  * an unknown name and --with/--without gt are refused with the module list and
    nothing changed;
  * a module whose requires_gt does not admit the gt being installed is skipped with a
    message, installed nowhere, its recorded choice left alone;
  * --list-modules prints name, plugin, version, state and why, and changes nothing;
  * turning a module off never touches a user's own hook, file or another plugin;
  * a stale same-version gt cache is replaced, so gt stops carrying files it no longer ships;
  * a 0.13.0 home with install_demo=no in vault-config.json ends with the demo module off,
    equal to a fresh install with --without demo.
"""
import json
import re
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, REPO, GT, WIKI, latest_version_dir

INSTALL = REPO / "install.sh"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
MARKET = "golden-thread-plugin"
try:
    DEMO = latest_version_dir(REPO / "golden-thread-demo")
except (RuntimeError, OSError):
    DEMO = None


def module_json(name, plugin, version, **over):
    d = {"schema": 1, "name": name, "plugin": plugin, "version": version,
         "requires_gt": ">=0.14.0,<0.15.0", "summary": "fixture module %s" % name,
         "default": "on", "skills": [], "scripts": [], "templates": [], "hooks": [],
         "hookdir_scripts": [], "settings": [], "requires_modules": [], "replaces_core": []}
    d.update(over)
    return d


class ModuleBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / MARKET
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        shutil.copytree(GT, self.repo / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        self.assertOk(self.py(self.gt_src / "scripts" / "gt_components.py", "manifest",
                              self.gt_src))
        # beacon: a reporter hook and a hookdir script
        self.beacon = self.fixture("golden-thread-beacon", "gt-beacon", "1.0.0", module_json(
            "beacon", "gt-beacon", "1.0.0", skills=["beacon-hello"],
            scripts=["beacon_report.py", "beacon_tool.py"],
            hooks=[{"event": "SessionStart", "script": "beacon_report.py", "args": [],
                    "kind": "reporter"}],
            hookdir_scripts=["beacon_tool.py"]))
        (self.beacon / "skills" / "beacon-hello").mkdir(parents=True)
        (self.beacon / "skills" / "beacon-hello" / "SKILL.md").write_text(
            "---\nname: beacon-hello\ndescription: Say hello from the beacon fixture.\n---\n")
        (self.beacon / "scripts").mkdir()
        (self.beacon / "scripts" / "beacon_report.py").write_text("print('beacon')\n")
        (self.beacon / "scripts" / "beacon_tool.py").write_text("print('tool')\n")

    def fixture(self, dirname, plugin, version, mj):
        v = self.repo / dirname / version
        (v / ".claude-plugin").mkdir(parents=True)
        (v / ".claude-plugin" / "plugin.json").write_text(json.dumps(
            {"name": plugin, "version": version, "description": "Fixture %s." % plugin}))
        (v / "module.json").write_text(json.dumps(mj, indent=2))
        return v

    # -- helpers ------------------------------------------------------------------
    @property
    def gt_src(self):
        return self.repo / "golden-thread" / GT.name

    @property
    def claude(self):
        return self.home / ".claude"

    @property
    def hooks_dir(self):
        return self.claude / "golden-thread" / "hooks"

    def install(self, *args, ok=True):
        if not any(a in ("--vault", "--no-vault", "--list-modules") for a in args):
            args = (*args, "--no-vault")
        p = self.sh(self.repo / "install.sh", *args, timeout=300)
        if ok:
            self.assertOk(p, "install.sh failed")
        return p

    def jload(self, rel, default=None):
        p = self.claude / rel
        return json.loads(p.read_text()) if p.exists() else default

    def commands(self):
        s = self.jload("settings.json", {})
        return [(ev, h.get("command", "")) for ev, blocks in s.get("hooks", {}).items()
                for b in blocks for h in b.get("hooks", [])]

    def choices(self):
        return (self.jload("golden-thread/install-choices.json", {}) or {}).get("choices", {})

    def snapshot(self):
        out = {}
        for f in sorted(self.home.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                out[str(f.relative_to(self.home))] = f.read_bytes()
        return out

    def assert_on(self, plugin, skill, hook_script=None, hookdir=()):
        key = "%s@%s" % (plugin, MARKET)
        cache = self.claude / "plugins" / "cache" / MARKET / plugin
        self.assertTrue(list(cache.glob("*/skills/%s/SKILL.md" % skill)), "not in the cache")
        market = self.claude / "plugins" / "marketplaces" / MARKET
        self.assertTrue((market / "plugins" / plugin / "skills" / skill / "SKILL.md").is_file())
        mp = json.loads((market / ".claude-plugin" / "marketplace.json").read_text())
        self.assertIn(plugin, [e["name"] for e in mp["plugins"]])
        self.assertIn(key, self.jload("plugins/installed_plugins.json")["plugins"])
        self.assertIs(self.jload("settings.json")["enabledPlugins"].get(key), True)
        for f in hookdir:
            self.assertTrue((self.hooks_dir / f).is_file(), f)
        if hook_script:
            hits = [c for ev, c in self.commands()
                    if ev == "SessionStart" and c.endswith(str(self.hooks_dir / hook_script))]
            self.assertEqual(len(hits), 1, self.commands())

    def assert_absent(self, plugin, scripts=()):
        key = "%s@%s" % (plugin, MARKET)
        self.assertFalse((self.claude / "plugins" / "cache" / MARKET / plugin).exists(),
                         "cache left behind")
        market = self.claude / "plugins" / "marketplaces" / MARKET
        self.assertFalse((market / "plugins" / plugin).exists(), "marketplace dir left behind")
        mp = json.loads((market / ".claude-plugin" / "marketplace.json").read_text())
        self.assertNotIn(plugin, [e["name"] for e in mp["plugins"]])
        self.assertNotIn(key, self.jload("plugins/installed_plugins.json")["plugins"])
        self.assertNotIn(key, self.jload("settings.json").get("enabledPlugins", {}))
        for s in scripts:
            self.assertFalse((self.hooks_dir / s).exists(), "%s left in the hooks dir" % s)
            self.assertFalse([c for _e, c in self.commands() if str(self.hooks_dir / s) in c],
                             "%s still wired" % s)


class OnAndOff(ModuleBase):
    def test_a_default_on_module_is_installed_enabled_and_its_hook_wired(self):
        p = self.install()
        self.assert_on("gt-beacon", "beacon-hello", "beacon_report.py",
                       ("beacon_report.py", "beacon_tool.py"))
        self.assertIn("Verified hook wiring", p.stdout)
        self.assertIn("/gt-beacon:beacon-hello", p.stdout)
        self.assertRegex(p.stdout, r"Modules: .*beacon on")
        w = self.run_cmd(["python3", self.gt_src / "scripts" / "gt_components.py", "wiring",
                          self.gt_src, "--owner", "install.sh"])
        self.assertOk(w, "the wiring check disagrees with what install.sh wired")

    def test_without_removes_everything_records_and_stays_off(self):
        self.install()
        p = self.install("--without", "beacon")
        self.assert_absent("gt-beacon", ("beacon_report.py", "beacon_tool.py"))
        self.assertEqual(self.choices().get("beacon"), "off")
        for line in ("Recorded module choice: beacon off", "removed cache",
                     "removed marketplace dir", "removed marketplace entry gt-beacon",
                     "removed installed_plugins.json entry gt-beacon@",
                     "removed settings.json enabledPlugins gt-beacon@",
                     "removed hooks-dir file beacon_tool.py",
                     "Module beacon is off: removed hook entry SessionStart/beacon_report.py"):
            self.assertIn(line, p.stdout)
        self.assertIn("beacon off (by your choice)", p.stdout)
        self.assertNotIn("/gt-beacon:", p.stdout)
        backups = self.claude / "golden-thread" / "backups"
        self.assertTrue(list(backups.glob("settings.json.*.module-off")))
        self.assertTrue(list(backups.glob("installed_plugins.json.*.module-off")))
        self.assertTrue(list(backups.glob("hooks-module-off.*/beacon_tool.py")))
        self.assertTrue(list(backups.glob("settings.json.*.pre-prune")),
                        "hook entries removed without a settings backup")
        self.assertIn("Verified hook wiring", p.stdout, "an off module's hook reported unwired")

        p = self.install()                                    # plain: stays off
        self.assert_absent("gt-beacon", ("beacon_report.py", "beacon_tool.py"))
        self.assertNotIn("Recorded module choice", p.stdout)
        self.assertNotIn("removed cache", p.stdout, "nothing left to remove, nothing printed")

        p = self.install("--with", "beacon")                  # and back
        self.assert_on("gt-beacon", "beacon-hello", "beacon_report.py",
                       ("beacon_report.py", "beacon_tool.py"))
        self.assertEqual(self.choices().get("beacon"), "on")

    def test_without_on_a_first_install_installs_nothing_of_it(self):
        self.install("--without=beacon")
        self.assert_absent("gt-beacon", ("beacon_report.py", "beacon_tool.py"))
        self.assertEqual(self.choices().get("beacon"), "off")

    def test_refusals_change_nothing(self):
        self.install()
        before = self.snapshot()
        for args, needle in ((("--without", "gt"), "gt is the core plugin"),
                             (("--with", "gt"), "gt is the core plugin"),
                             (("--with", "nope"), "Unknown module 'nope'"),
                             (("--with", "beacon", "--without", "beacon"), "Both --with")):
            with self.subTest(args=args):
                p = self.install(*args, ok=False)
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(needle, p.stdout)
                if "Both" not in needle:
                    self.assertIn("Modules: beacon, wiki", p.stdout)
                self.assertEqual(self.snapshot(), before, "a refused install changed the home")

    def test_list_modules_changes_nothing(self):
        self.install()
        before = self.snapshot()
        p = self.install("--list-modules", "--without", "beacon")
        self.assertOk(p)
        self.assertRegex(p.stdout, r"beacon\s+gt-beacon\s+1\.0\.0\s+off\s+--without this run")
        self.assertRegex(p.stdout, r"wiki\s+gt-wiki\s+%s\s+on\s+module default"
                         % re.escape(WIKI.name))
        self.assertNotIn("Installing", p.stdout)
        self.assertEqual(self.snapshot(), before)
        self.install("--without", "beacon")
        p = self.install("--list-modules")
        self.assertRegex(p.stdout, r"beacon\s+gt-beacon\s+1\.0\.0\s+off\s+your recorded choice")

    def test_list_modules_on_an_empty_home_creates_nothing(self):
        p = self.install("--list-modules")
        self.assertOk(p)
        self.assertEqual(list((self.claude).iterdir()), [])


class RequiresGt(ModuleBase):
    def setUp(self):
        super().setUp()
        self.fixture("golden-thread-future", "gt-future", "1.0.0",
                     module_json("future", "gt-future", "1.0.0", requires_gt=">=9.0.0"))

    def test_a_module_that_does_not_admit_this_gt_is_skipped(self):
        p = self.install()
        self.assertIn("Skipping module future (gt-future 1.0.0): its requires_gt does not "
                      "admit gt %s" % GT.name, p.stdout)
        self.assertFalse((self.claude / "plugins" / "cache" / MARKET / "gt-future").exists())
        self.assertNotIn("gt-future@%s" % MARKET, self.jload("settings.json")["enabledPlugins"])
        self.assertNotIn("future", self.choices())
        self.assertIn("future off (needs gt >=9.0.0)", p.stdout)
        self.assert_on("gt-beacon", "beacon-hello", "beacon_report.py")
        lm = self.install("--list-modules")
        self.assertRegex(lm.stdout, r"future\s+gt-future\s+1\.0\.0\s+off\s+skipped: needs gt >=9\.0\.0")

    def test_a_recorded_choice_is_left_alone_when_skipped(self):
        self.install("--with", "future")
        self.assertEqual(self.choices().get("future"), "on")
        self.assertFalse((self.claude / "plugins" / "cache" / MARKET / "gt-future").exists())
        self.install()
        self.assertEqual(self.choices().get("future"), "on")


class OffTouchesNothingElse(ModuleBase):
    def test_user_hooks_files_and_other_plugins_survive(self):
        self.install()
        s = self.claude / "settings.json"
        d = json.loads(s.read_text())
        d["hooks"]["SessionStart"].append({"hooks": [
            {"type": "command", "command": "echo users-own"},
            {"type": "command", "command": "python3 /opt/me/beacon_report.py"}]})
        d["enabledPlugins"]["other@elsewhere"] = True
        d["theme"] = "dark"
        s.write_text(json.dumps(d, indent=2))
        inst = self.claude / "plugins" / "installed_plugins.json"
        di = json.loads(inst.read_text())
        di["plugins"]["other@elsewhere"] = [{"scope": "user", "installPath": "/x"}]
        inst.write_text(json.dumps(di, indent=2))
        other_cache = self.claude / "plugins" / "cache" / "elsewhere" / "other" / "1.0.0"
        other_cache.mkdir(parents=True)
        (other_cache / "keep.txt").write_text("x")
        mine = self.hooks_dir / "my_note.sh"
        mine.write_text("#!/bin/sh\n")

        self.install("--without", "beacon")
        self.assert_absent("gt-beacon", ("beacon_report.py", "beacon_tool.py"))
        d = json.loads(s.read_text())
        cmds = [h["command"] for b in d["hooks"]["SessionStart"] for h in b["hooks"]]
        self.assertIn("echo users-own", cmds)
        self.assertIn("python3 /opt/me/beacon_report.py", cmds)
        self.assertTrue(d["enabledPlugins"]["other@elsewhere"])
        self.assertEqual(d["theme"], "dark")
        self.assertIn("other@elsewhere", json.loads(inst.read_text())["plugins"])
        self.assertTrue((other_cache / "keep.txt").is_file())
        self.assertTrue(mine.is_file())
        self.assert_on("gt-wiki", "gt-wiki")


class GtCacheConverges(ModuleBase):
    def test_a_stale_same_version_gt_cache_loses_files_gt_no_longer_ships(self):
        stale = self.claude / "plugins" / "cache" / MARKET / "gt" / GT.name
        (stale / "skills" / "gt-demo").mkdir(parents=True)
        (stale / "skills" / "gt-demo" / "SKILL.md").write_text("old demo\n")
        (stale / "scripts").mkdir()
        (stale / "scripts" / "gt_demo.sh").write_text("old\n")
        self.install()
        self.assertFalse((stale / "skills" / "gt-demo").exists())
        self.assertFalse((stale / "scripts" / "gt_demo.sh").exists())
        self.assertTrue((stale / "scripts" / "gt_components.py").is_file())


OLD_0_13 = ("2c5ca48", "0.13.0", "0.1.3")


@unittest.skipIf(DEMO is None, "golden-thread-demo module not in this tree")
class DemoChoiceFromA013Home(Sandbox):
    """0.13.0 honoured install_demo=no by stripping the demo out of gt. After 0.14.0 the
    demo is module `demo`; the machine migration records the setting and install.sh must
    end with the module off -- the same state as a fresh 0.14.0 install --without demo."""

    def setUp(self):
        super().setUp()
        self.new = self.tmp / "new" / MARKET
        self.new.mkdir(parents=True)
        shutil.copy2(INSTALL, self.new / "install.sh")
        for src, dirname in ((GT, "golden-thread"), (WIKI, "golden-thread-wiki"),
                             (DEMO, "golden-thread-demo")):
            shutil.copytree(src, self.new / dirname / src.name, ignore=IGNORE)
        gt = self.new / "golden-thread" / GT.name
        self.assertOk(self.py(gt / "scripts" / "gt_components.py", "manifest", gt))

    def old_repo(self):
        commit, gt, wiki = OLD_0_13
        if not shutil.which("git"):
            self.skipTest("git not installed")
        dest = self.tmp / "old"
        dest.mkdir()
        arch = self.run_cmd(["git", "-C", REPO.parent, "archive", "--format=tar",
                             "-o", self.tmp / "old.tar", commit,
                             "%s/install.sh" % MARKET, "%s/golden-thread/%s" % (MARKET, gt),
                             "%s/golden-thread-wiki/%s" % (MARKET, wiki)])
        if arch.returncode != 0:
            self.skipTest("0.13.0 not in git history here: %s" % arch.stderr.strip())
        self.assertOk(self.run_cmd(["tar", "-xf", self.tmp / "old.tar", "-C", dest]))
        return dest / MARKET

    def run_install(self, repo, home, *args):
        p = self.run_cmd(["bash", repo / "install.sh", *args], env={"HOME": str(home)},
                         timeout=300)
        self.assertOk(p, "install.sh (%s) failed" % repo)
        return p

    def state(self, home):
        c = home / ".claude"
        cache = c / "plugins" / "cache" / MARKET
        return {
            "cache": sorted(str(f.relative_to(cache)) for f in cache.rglob("*")
                            if f.is_file() and "__pycache__" not in f.parts),
            "enabled": json.loads((c / "settings.json").read_text())["enabledPlugins"],
            "installed": sorted(json.loads((c / "plugins" / "installed_plugins.json")
                                           .read_text())["plugins"]),
            "market": sorted(p.name for p in (c / "plugins" / "marketplaces" / MARKET
                                              / "plugins").iterdir()),
            "choices": json.loads((c / "golden-thread" / "install-choices.json")
                                  .read_text())["choices"],
        }

    def test_install_demo_no_ends_with_the_demo_module_off(self):
        old = self.old_repo()
        vault = self.tmp / "vault-up"
        self.run_install(old, self.home, "--vault", vault)
        cfg = self.home / ".claude" / "vault-config.json"
        data = json.loads(cfg.read_text())
        data["install_demo"] = "no"
        cfg.write_text(json.dumps(data, indent=2))
        p = self.run_install(old, self.home)          # 0.13.0 honours it: demo stripped from gt
        self.assertIn("install_demo=no", p.stdout, "fixture: 0.13.0 did not see the setting")
        self.assertFalse(list((self.home / ".claude" / "plugins" / "cache" / MARKET)
                              .glob("gt/*/skills/gt-demo")), "fixture: demo not stripped")
        p = self.run_install(self.new, self.home)
        self.assertIn("Machine migrations changed a module choice", p.stdout)
        self.assertIn("demo off (by your choice)", p.stdout)
        up = self.state(self.home)
        self.assertEqual(up["choices"].get("demo"), "off")
        self.assertNotIn("gt-demo@%s" % MARKET, up["enabled"])
        self.assertFalse([f for f in up["cache"] if f.startswith("gt-demo/") or "gt-demo" in f or "demo-pizzabot" in f or "gt_demo" in f], up["cache"])

        fresh = self.tmp / "home-fresh"
        (fresh / ".claude").mkdir(parents=True)
        self.run_install(self.new, fresh, "--vault", self.tmp / "vault-fresh",
                         "--without", "demo")
        self.assertEqual(up, self.state(fresh), "upgrade differs from a fresh --without demo")

    def test_without_the_setting_the_demo_module_is_on_after_upgrade(self):
        old = self.old_repo()
        self.run_install(old, self.home, "--vault", self.tmp / "v")
        p = self.run_install(self.new, self.home)
        self.assertNotIn("Machine migrations changed a module choice", p.stdout)
        enabled = json.loads((self.home / ".claude" / "settings.json").read_text())["enabledPlugins"]
        self.assertIs(enabled.get("gt-demo@%s" % MARKET), True)
        cache = self.home / ".claude" / "plugins" / "cache" / MARKET
        self.assertTrue(list(cache.glob("gt-demo/*/skills/gt-demo/SKILL.md")))
        self.assertFalse(list(cache.glob("gt/*/skills/gt-demo")), "gt still carries the demo")


if __name__ == "__main__":
    unittest.main()
