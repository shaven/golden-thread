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
  * 0.15.0, the real watch / report-card / farm modules (the first with hooks, hookdir
    scripts and settings):
      - --without watch / --without report-card removes each hook entry and hook-dir file,
        and watch's crontab line (tagged "# gt-watch", under THIS home) -- every other
        crontab line kept, the crontab backed up; a watch/report_card setting survives;
      - a 0.14.0 home + vault upgraded by one install equals a fresh 0.15.0 install with
        the same module choices (hook entries, hook-dir files, caches, enabledPlugins,
        installed plugins, marketplace) -- farm kept ON for the upgrader, OFF by default
        for a fresh install; also with --without watch --without report-card;
      - --with watch after --without watch puts back exactly the crontab lines that were
        removed (kept mode 600 under module-cron/ while off), once;
      - rolling back with ./install.sh 0.14.0 leaves no module plugin enabled beside a gt
        that still carries the same skills, and installs the newest gt-wiki / gt-demo
        release whose requires_gt admits 0.14.0 instead of skipping them;
      - rolling back to a gt from before modules (0.13.0) installs the gt-wiki release it
        shipped with (0.1.3, no module.json) and honours install_demo=no / a demo choice
        by leaving the demo out of that gt, as its own installer did;
      - an upgrade prints "Moved: /gt:X → /<plugin>:X" once for each skill the previous gt
        had and the new one moved to a module; a fresh install and a re-install print none.
  Every crontab call goes to a stub on PATH; the real crontab is never touched.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import unittest
from pathlib import Path

from _harness import (Sandbox, REPO, GT, WIKI, WATCH, REPORT_CARD, FARM, latest_version_dir)

INSTALL = REPO / "install.sh"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
MARKET = "golden-thread-plugin"
try:
    DEMO = latest_version_dir(REPO / "golden-thread-demo")
except (RuntimeError, OSError):
    DEMO = None


def _next_minor(v):
    """The fixture modules admit the gt under test: >=0.14.0,<(its next minor)."""
    major, minor = (int(x) for x in v.split(".")[:2])
    return "%d.%d.0" % (major, minor + 1)


def module_json(name, plugin, version, **over):
    d = {"schema": 1, "name": name, "plugin": plugin, "version": version,
         "requires_gt": ">=0.14.0,<%s" % _next_minor(GT.name), "summary": "fixture module %s" % name,
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
        # `"gt_demo" in f` also matched gt_demote.py, an unrelated scanner (2026-09-16).
        # Match the module's own paths, not any filename containing those letters.
        demo = [f for f in up["cache"]
                if f.startswith("gt-demo/") or "gt-demo" in f or "demo-pizzabot" in f
                or os.path.basename(f).startswith("gt_demo.")]
        self.assertFalse(demo, up["cache"])

        fresh = self.tmp / "home-fresh"
        (fresh / ".claude").mkdir(parents=True)
        self.run_install(self.new, fresh, "--vault", self.tmp / "vault-fresh",
                         "--without", "demo")
        # 0.15.0: an upgrader from a gt that shipped /gt:gt-farm also gets farm=on recorded
        # (machine migration farm-kept-for-upgraders). This tree carries no farm module, so
        # the choice is all that differs; the demo convergence is what is under test here.
        fresh_state = self.state(fresh)
        if up["choices"].get("farm") == "on" and "farm" not in fresh_state["choices"]:
            up["choices"].pop("farm")
        self.assertEqual(up, fresh_state, "upgrade differs from a fresh --without demo")

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


# -- 0.15.0: the real watch, report-card and farm modules ------------------------------------

CRONTAB_STUB = """#!/bin/sh
# test stand-in: the table lives in $CRONTAB_FILE; every call is logged
echo "$*" >> "$CRONTAB_FILE.calls"
if [ "$1" = "-l" ]; then
  [ -f "$CRONTAB_FILE" ] || { echo "no crontab for $USER" >&2; exit 1; }
  cat "$CRONTAB_FILE"; exit 0
fi
if [ "$1" = "-" ]; then cat > "$CRONTAB_FILE"; exit 0; fi
exit 2
"""
OLD_0_14 = ("12e40c8", "0.14.0", "0.2.0", "0.14.0")      # commit, gt, gt-wiki, gt-demo
REAL_MODULES = [m for m in (WATCH, REPORT_CARD, FARM) if m is not None]


class RealModulesBase(Sandbox):
    """A copy of the real tree (gt, wiki, and every module) and a crontab stub on PATH."""

    def setUp(self):
        super().setUp()
        if WATCH is None or REPORT_CARD is None or FARM is None:
            self.skipTest("the 0.15.0 watch / report-card / farm modules are not in this tree")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "crontab").write_text(CRONTAB_STUB)
        (self.bin / "crontab").chmod(0o755)
        self.crontab = self.tmp / "crontab.txt"
        self.env.update({"PATH": "%s:%s" % (self.bin, os.environ.get("PATH", "")),
                         "CRONTAB_FILE": str(self.crontab)})
        self.new = self.tmp / "new" / MARKET
        self.copy_tree(self.new)

    def copy_tree(self, dest, gt=GT):
        dest.mkdir(parents=True)
        shutil.copy2(INSTALL, dest / "install.sh")
        shutil.copytree(gt, dest / "golden-thread" / gt.name, ignore=IGNORE)
        self.assertOk(self.py(dest / "golden-thread" / gt.name / "scripts" / "gt_components.py",
                              "manifest", dest / "golden-thread" / gt.name))
        for src in [WIKI] + [m for m in (_demo(),) if m is not None] + REAL_MODULES:
            shutil.copytree(src, dest / src.parent.name / src.name, ignore=IGNORE)
        return dest

    def run_install(self, repo, home, *args, ok=True):
        if not any(a in ("--vault", "--no-vault") for a in args) and \
                not (home / ".claude" / "vault-config.json").exists():
            args = (*args, "--no-vault")
        p = self.run_cmd(["bash", repo / "install.sh", *args], env={"HOME": str(home)},
                         timeout=400)
        if ok:
            self.assertOk(p, "install.sh (%s %s) failed" % (repo, " ".join(map(str, args))))
        return p

    @staticmethod
    def claude(home):
        return home / ".claude"

    def commands(self, home):
        s = json.loads((self.claude(home) / "settings.json").read_text())
        return [(ev, h.get("command", "")) for ev, blocks in s.get("hooks", {}).items()
                for b in blocks for h in b.get("hooks", [])]

    def state(self, home, vault=None):
        c = self.claude(home)

        def norm(text):
            if vault is not None:
                text = text.replace(str(vault), "<VAULT>")
            return text.replace(str(home), "<HOME>")
        entries = sorted((ev, norm(cmd)) for ev, cmd in self.commands(home)
                         if "golden-thread" in cmd)
        hooks = c / "golden-thread" / "hooks"
        hook_files = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                      for f in hooks.iterdir() if f.is_file() and f.name != "user_note.sh"}
        cache = c / "plugins" / "cache" / MARKET
        return {
            "entries": entries, "hook_files": hook_files,
            "cache": sorted(str(f.relative_to(cache)) for f in cache.rglob("*")
                            if f.is_file() and "__pycache__" not in f.parts),
            "enabled": {k: v for k, v in json.loads((c / "settings.json").read_text())
                        .get("enabledPlugins", {}).items() if k.endswith("@" + MARKET)},
            "installed": sorted(k for k in json.loads(
                (c / "plugins" / "installed_plugins.json").read_text())["plugins"]
                if k.endswith("@" + MARKET)),
            "market": sorted(p.name for p in (c / "plugins" / "marketplaces" / MARKET
                                              / "plugins").iterdir()),
        }

    def choices(self, home):
        p = self.claude(home) / "golden-thread" / "install-choices.json"
        return json.loads(p.read_text())["choices"] if p.exists() else {}


def _demo():
    try:
        return latest_version_dir(REPO / "golden-thread-demo")
    except (RuntimeError, OSError):
        return None


class RealModulesOff(RealModulesBase):
    def test_without_watch_removes_hook_file_and_only_its_own_crontab_line(self):
        home = self.home
        self.run_install(self.new, home)
        hooks = self.claude(home) / "golden-thread" / "hooks"
        self.assertTrue((hooks / "gt_watch.py").is_file())
        self.assertEqual([c for e, c in self.commands(home) if "gt_watch.py" in c],
                         ["python3 %s --hook" % (hooks / "gt_watch.py")])
        # the real tool writes the tagged line, from the installed copy as the skill says
        self.crontab.write_text("0 5 * * * /usr/bin/true # users-own\n")
        cron = self.run_cmd(["python3", hooks / "gt_watch.py", "install-cron", "--every", "1h"],
                            env={"HOME": str(home)})
        self.assertOk(cron, "fixture: gt_watch.py install-cron")
        self.assertIn(str(hooks / "gt_watch.py"), self.crontab.read_text())
        # lines that are not this home's install: another home's watch, a look-alike tag
        with open(self.crontab, "a") as fh:
            fh.write("15 * * * * /opt/other/.claude/golden-thread/hooks/gt_watch.py fetch "
                     "# gt-watch\n0 6 * * * /bin/echo keep # gt-watch-extra\n")
        self.config(vault_path=str(self.tmp), watch="report")
        p = self.run_install(self.new, home, "--without", "watch", "--no-vault")
        self.assertIn("Module watch is off: removed hook entry SessionStart/gt_watch.py", p.stdout)
        self.assertIn("removed hooks-dir file gt_watch.py", p.stdout)
        self.assertIn("removed 1 crontab line(s) tagged '# gt-watch'", p.stdout)
        self.assertFalse((hooks / "gt_watch.py").exists())
        self.assertFalse([c for _e, c in self.commands(home) if "gt_watch.py" in c])
        left = self.crontab.read_text()
        self.assertNotIn(str(hooks / "gt_watch.py"), left)
        for keep in ("# users-own", "/opt/other/.claude/golden-thread/hooks/gt_watch.py",
                     "# gt-watch-extra"):
            self.assertIn(keep, left, "a crontab line that was not this install's was removed")
        baks = list((self.claude(home) / "golden-thread" / "backups").glob("crontab.*.module-off"))
        self.assertEqual(len(baks), 1)
        self.assertIn(str(hooks / "gt_watch.py"), baks[0].read_text())
        self.assertEqual(json.loads((self.claude(home) / "vault-config.json").read_text())["watch"],
                         "report", "a module going off rewrote its setting")
        self.assertEqual(self.choices(home).get("watch"), "off")
        self.assertIn("Verified hook wiring", p.stdout)
        # a plain re-install asks crontab nothing more and changes nothing
        calls = (self.tmp / "crontab.txt.calls").read_text()
        self.run_install(self.new, home, "--no-vault")
        self.assertEqual((self.tmp / "crontab.txt.calls").read_text(), calls)
        # what was removed is kept, privately, for the module coming back -- and only that
        kept = self.claude(home) / "golden-thread" / "module-cron" / "watch.lines"
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)
        removed = [l for l in baks[0].read_text().splitlines() if l not in left.splitlines()]
        self.assertEqual(kept.read_text().splitlines(), removed)
        self.assertEqual(len(removed), 1)
        # still off, so the plain re-install above put nothing back
        self.assertNotIn(str(hooks / "gt_watch.py"), self.crontab.read_text())
        # --with brings the hook back AND exactly the removed line (0.15.0; it used not to)
        p = self.run_install(self.new, home, "--with", "watch", "--no-vault")
        self.assertTrue((hooks / "gt_watch.py").is_file())
        self.assertEqual(len([c for _e, c in self.commands(home) if "gt_watch.py" in c]), 1)
        self.assertIn("Module watch is on — restored 1 crontab line(s)", p.stdout)
        now = self.crontab.read_text().splitlines()
        self.assertEqual([l for l in now if l == removed[0]], removed, "line not restored once")
        for keep in ("# users-own", "/opt/other/.claude/golden-thread/hooks/gt_watch.py",
                     "# gt-watch-extra"):
            self.assertEqual(len([l for l in now if keep in l]), 1, keep)
        self.assertFalse(kept.exists(), "state file left after the lines went back")
        self.assertEqual(len(list((self.claude(home) / "golden-thread" / "backups")
                                  .glob("crontab.*.module-on"))), 1)
        # and again changes nothing
        before = self.crontab.read_text()
        self.run_install(self.new, home, "--no-vault")
        self.assertEqual(self.crontab.read_text(), before)

    def test_without_report_card_removes_its_three_entries_and_file(self):
        home = self.home
        self.run_install(self.new, home)
        hooks = self.claude(home) / "golden-thread" / "hooks"
        card = [(e, c) for e, c in self.commands(home) if "gt_report_card.py" in c]
        self.assertEqual(sorted(e for e, _c in card), ["PreCompact", "SessionEnd", "SessionStart"])
        p = self.run_install(self.new, home, "--without", "report-card")
        self.assertFalse((hooks / "gt_report_card.py").exists())
        self.assertFalse([c for _e, c in self.commands(home) if "gt_report_card.py" in c])
        for ev in ("PreCompact", "SessionEnd", "SessionStart"):
            self.assertIn("Module report-card is off: removed hook entry %s/gt_report_card.py"
                          % ev, p.stdout)
        self.assertNotIn("crontab", p.stdout)
        self.assertIn("Verified hook wiring", p.stdout)
        s = json.loads((self.claude(home) / "settings.json").read_text())
        self.assertNotIn("PreCompact", s["hooks"], "an event emptied by the removal should go")
        self.assertNotIn("gt-report-card@%s" % MARKET, s["enabledPlugins"])

    def test_a_fresh_install_leaves_farm_off_and_records_nothing(self):
        p = self.run_install(self.new, self.home)
        st = self.state(self.home)
        self.assertNotIn("gt-farm@%s" % MARKET, st["enabled"])
        self.assertFalse([f for f in st["cache"] if f.startswith("gt-farm/")])
        self.assertIn("farm off (off by default)", p.stdout)
        self.assertNotIn("farm", self.choices(self.home))
        self.assertIn("gt-watch@%s" % MARKET, st["enabled"])
        self.assertIn("gt-report-card@%s" % MARKET, st["enabled"])
        self.assertNotIn("Machine migrations changed a module choice", p.stdout)


class UpgradeFrom014Converges(RealModulesBase):
    """R1 for the 0.15.0 extraction: one install over 0.14.0 equals a fresh 0.15.0 install
    with the same module choices, and no feature the user had is silently lost."""

    def old_repo(self):
        commit, gt, wiki, demo = OLD_0_14
        if not shutil.which("git"):
            self.skipTest("git not installed")
        dest = self.tmp / "old"
        dest.mkdir()
        arch = self.run_cmd(["git", "-C", REPO.parent, "archive", "--format=tar",
                             "-o", self.tmp / "old.tar", commit,
                             "%s/install.sh" % MARKET, "%s/golden-thread/%s" % (MARKET, gt),
                             "%s/golden-thread-wiki/%s" % (MARKET, wiki),
                             "%s/golden-thread-demo/%s" % (MARKET, demo)])
        if arch.returncode != 0:
            self.skipTest("0.14.0 not in git history here: %s" % arch.stderr.strip())
        self.assertOk(self.run_cmd(["tar", "-xf", self.tmp / "old.tar", "-C", dest]))
        return dest / MARKET

    def upgrade_and_fresh(self, *flags):
        old = self.old_repo()
        home_up, vault_up = self.home, self.tmp / "vault-up"
        self.run_install(old, home_up, "--vault", vault_up)
        c = self.claude(home_up)
        self.assertTrue(list((c / "plugins" / "cache" / MARKET).glob("gt/0.14.0/skills/gt-farm")),
                        "fixture: 0.14.0 did not ship /gt:gt-farm")
        s = c / "settings.json"
        data = json.loads(s.read_text())
        data["hooks"].setdefault("Stop", []).append(
            {"hooks": [{"type": "command", "command": "echo users-own-hook"}]})
        s.write_text(json.dumps(data, indent=2))
        (c / "golden-thread" / "hooks" / "user_note.sh").write_text("#!/bin/sh\n")
        up_out = self.run_install(self.new, home_up, *flags)

        home_fresh, vault_fresh = self.tmp / "home-fresh", self.tmp / "vault-fresh"
        (home_fresh / ".claude").mkdir(parents=True)
        self.run_install(self.new, home_fresh, "--vault", vault_fresh, "--with", "farm", *flags)
        return up_out, (home_up, vault_up), (home_fresh, vault_fresh)

    def assert_converged(self, up, fresh):
        a, b = self.state(*up), self.state(*fresh)
        for key in ("entries", "hook_files", "cache", "enabled", "installed", "market"):
            self.assertEqual(a[key], b[key], "upgrade differs from a fresh install: %s" % key)
        self.assertEqual(self.choices(up[0]), self.choices(fresh[0]))
        c = self.claude(up[0])
        self.assertTrue((c / "golden-thread" / "hooks" / "user_note.sh").is_file())
        stop = [cmd for ev, cmd in self.commands(up[0]) if ev == "Stop"]
        self.assertIn("echo users-own-hook", stop)
        return a

    def test_default_choices(self):
        out, up, fresh = self.upgrade_and_fresh()
        self.assertIn("applied farm-kept-for-upgraders", out.stdout)
        self.assertIn("Machine migrations changed a module choice", out.stdout)
        self.assertEqual(self.choices(up[0]), {"farm": "on"})
        st = self.assert_converged(up, fresh)
        for plugin in ("gt-watch", "gt-report-card", "gt-farm", "gt-wiki", "gt-demo"):
            self.assertIs(st["enabled"].get("%s@%s" % (plugin, MARKET)), True, plugin)
        self.assertIn("gt_watch.py", st["hook_files"])
        self.assertIn("gt_report_card.py", st["hook_files"])
        gt_cache = [f for f in st["cache"] if f.startswith("gt/")]
        for gone in ("gt-farm", "gt-watch", "gt_watch.py", "gt_report_card.py", "watch.md"):
            self.assertFalse([f for f in gt_cache if gone in f], "gt still carries %s" % gone)
        self.assertTrue([f for f in st["cache"] if f.startswith("gt-farm/") and
                         f.endswith("skills/gt-farm/SKILL.md")])
        self.assertTrue([f for f in st["cache"] if f.startswith("gt-watch/") and
                         f.endswith("templates/watch.md")])
        self.assertNotIn("Unknown hook entr", out.stdout, "an upgrade left an unknown hook entry")
        unknown = re.findall(r"unknown file\(s\), not shipped by this release, left in place:\n"
                             r"((?:  .+\n)+)", out.stdout)
        self.assertTrue(unknown and all(u.split() == ["user_note.sh"] for u in unknown),
                        "an upgrade left an unknown hook-dir file: %s" % unknown)
        # the release's own checks agree with what the upgrade left
        src = self.new / "golden-thread" / GT.name
        w = self.run_cmd(["python3", src / "scripts" / "gt_components.py", "wiring", src],
                         env={"HOME": str(up[0])})
        self.assertOk(w, "wiring check after the upgrade")
        (self.claude(up[0]) / "golden-thread" / "hooks" / "user_note.sh").unlink()
        chk = self.run_cmd(["python3", src / "scripts" / "gt_components.py", "check", src],
                           env={"HOME": str(up[0])})
        self.assertIn("components: clean", chk.stdout)

    def test_watch_and_report_card_declined_on_upgrade(self):
        out, up, fresh = self.upgrade_and_fresh("--without", "watch", "--without", "report-card")
        st = self.assert_converged(up, fresh)
        self.assertNotIn("gt_watch.py", st["hook_files"])
        self.assertNotIn("gt_report_card.py", st["hook_files"])
        self.assertFalse([e for e in st["entries"] if "gt_watch" in e[1] or "gt_report_card" in e[1]])
        self.assertEqual(self.choices(up[0]), {"farm": "on", "watch": "off", "report-card": "off"})
        self.assertTrue(list((self.claude(up[0]) / "golden-thread" / "backups")
                             .glob("hooks-module-off.*/gt_watch.py")),
                        "the 0.14.0 gt_watch.py was removed without a backup")


class RollbackTo014(RealModulesBase):
    def test_rollback_leaves_no_module_beside_a_gt_that_has_the_same_skills(self):
        old_gt = REPO / "golden-thread" / "0.14.0"
        if not (old_gt / ".claude-plugin" / "plugin.json").is_file():
            self.skipTest("golden-thread/0.14.0 not in this tree")
        shutil.copytree(old_gt, self.new / "golden-thread" / "0.14.0", ignore=IGNORE)
        self.run_install(self.new, self.home, "--with", "farm")
        p = self.run_install(self.new, self.home, "0.14.0")
        c = self.claude(self.home)
        enabled = json.loads((c / "settings.json").read_text())["enabledPlugins"]
        cache = c / "plugins" / "cache" / MARKET
        for plugin in ("gt-watch", "gt-report-card", "gt-farm"):
            self.assertNotIn("%s@%s" % (plugin, MARKET), enabled, plugin)
            self.assertFalse((cache / plugin).exists(), plugin)
        skills = {}
        for key in enabled:
            name = key.split("@")[0]
            for sk in cache.glob("%s/*/skills/*/SKILL.md" % name):
                skills.setdefault(sk.parent.name, []).append(name)
        dups = {k: v for k, v in skills.items() if len(v) > 1}
        self.assertEqual(dups, {}, "duplicate skill names after a rollback")
        self.assertIn("gt-farm", skills, "the rolled-back gt lost /gt:gt-farm")
        hooks = c / "golden-thread" / "hooks"
        self.assertTrue((hooks / "gt_watch.py").is_file(), "0.14.0's own gt_watch.py was removed")
        cmds = self.commands(self.home)
        self.assertEqual(len([x for x in cmds if "gt_watch.py" in x[1]]), 1)
        self.assertEqual(len([x for x in cmds if "gt_report_card.py" in x[1]]), 3)
        self.assertEqual(self.choices(self.home).get("farm"), "on",
                         "a rollback must not rewrite the recorded choice")
        self.assertFalse((self.tmp / "crontab.txt").exists())

    def test_rollback_installs_the_newest_wiki_and_demo_that_admit_the_old_gt(self):
        old_gt = REPO / "golden-thread" / "0.14.0"
        olds = {"gt-wiki": REPO / "golden-thread-wiki" / "0.2.0",
                "gt-demo": REPO / "golden-thread-demo" / "0.14.0"}
        if not (old_gt / ".claude-plugin" / "plugin.json").is_file() or \
                not all((v / "module.json").is_file() for v in olds.values()):
            self.skipTest("gt 0.14.0 / gt-wiki 0.2.0 / gt-demo 0.14.0 not in this tree")
        shutil.copytree(old_gt, self.new / "golden-thread" / "0.14.0", ignore=IGNORE)
        c = self.claude(self.home)
        cache = c / "plugins" / "cache" / MARKET

        def enabled():
            return {k.split("@")[0] for k, v in json.loads((c / "settings.json").read_text())
                    ["enabledPlugins"].items() if v and k.endswith("@" + MARKET)}

        # without the older releases in the tree nothing admits 0.14.0: skipped, as before
        self.run_install(self.new, self.home)
        p = self.run_install(self.new, self.home, "0.14.0")
        self.assertIn("Skipping module wiki", p.stdout)
        self.assertNotIn("gt-wiki", enabled())
        self.assertNotIn("Chose gt-wiki", p.stdout)

        # with them, each falls back to its newest release that accepts the pinned gt
        for src in olds.values():
            shutil.copytree(src, self.new / src.parent.name / src.name, ignore=IGNORE)
        p = self.run_install(self.new, self.home, "0.14.0")
        self.assertNotIn("Skipping module wiki", p.stdout)
        self.assertNotIn("Skipping module demo", p.stdout)
        for plugin, src in olds.items():
            self.assertIn("Chose %s %s, not the newest" % (plugin, src.name), p.stdout)
            self.assertIn(plugin, enabled())
            self.assertEqual(sorted(d.name for d in (cache / plugin).iterdir()), [src.name],
                             "%s: the chosen release's cache must be the one kept" % plugin)
        # back to the newest gt: newest everything, nothing chosen
        p = self.run_install(self.new, self.home)
        self.assertNotIn("Chose ", p.stdout)
        self.assertEqual(sorted(d.name for d in (cache / "gt-wiki").iterdir()), [WIKI.name])


class RollbackToPreModuleGt(RealModulesBase):
    """gt 0.13.0 predates module.json: its tree shipped gt-wiki 0.1.3 (no module.json) and
    the demo inside gt, honouring install_demo=no by stripping it. A rollback to it from a
    newer tree must end where 0.13.0's own installer did (0.15.0)."""

    OLD_GT, OLD_WIKI = "0.13.0", "0.1.3"

    def setUp(self):
        super().setUp()
        self.old_gt = REPO / "golden-thread" / self.OLD_GT
        self.old_wiki = REPO / "golden-thread-wiki" / self.OLD_WIKI
        if not (self.old_gt / ".claude-plugin" / "plugin.json").is_file() or \
                not (self.old_wiki / ".claude-plugin" / "plugin.json").is_file():
            self.skipTest("gt %s / gt-wiki %s not in this tree" % (self.OLD_GT, self.OLD_WIKI))
        shutil.copytree(self.old_gt, self.new / "golden-thread" / self.OLD_GT, ignore=IGNORE)

    def enabled(self):
        s = json.loads((self.claude(self.home) / "settings.json").read_text())
        return {k.split("@")[0] for k, v in s["enabledPlugins"].items()
                if v and k.endswith("@" + MARKET)}

    def gt_skills(self, where):
        return {d.name for d in where.glob("skills/*") if (d / "SKILL.md").is_file()}

    def test_rollback_keeps_the_wiki_that_gt_shipped_with(self):
        cache = self.claude(self.home) / "plugins" / "cache" / MARKET
        self.run_install(self.new, self.home)
        # without the old wiki release in the tree nothing fits: the newest is skipped
        p = self.run_install(self.new, self.home, self.OLD_GT)
        self.assertNotIn("gt-wiki", self.enabled())
        # with it, the rollback installs exactly the release 0.13.0 shipped with
        shutil.copytree(self.old_wiki, self.new / "golden-thread-wiki" / self.OLD_WIKI,
                        ignore=IGNORE)
        p = self.run_install(self.new, self.home, self.OLD_GT)
        self.assertIn("Chose gt-wiki %s, not the newest" % self.OLD_WIKI, p.stdout)
        self.assertNotIn("Module wiki is off", p.stdout)
        self.assertIn("gt-wiki", self.enabled())
        self.assertEqual(sorted(d.name for d in (cache / "gt-wiki").iterdir()), [self.OLD_WIKI])
        self.assertTrue((cache / "gt-wiki" / self.OLD_WIKI / "skills").is_dir())

    def test_rollback_removes_what_a_newer_release_wired_and_keeps_the_users_files(self):
        # Validator 2026-09-14: ./install.sh 0.12.8 from the 0.15.0 tree left the 0.15.0
        # guard_protected_paths hook wired and its files in the hooks dir; 0.12.8 then
        # reported drift every session with no setting to silence it.
        old = REPO / "golden-thread" / "0.12.8"
        if (old / "hooks" / "guard_protected_paths.sh").exists() or not old.is_dir():
            self.skipTest("fixture needs a gt 0.12.8 without guard_protected_paths")
        shutil.copytree(old, self.new / "golden-thread" / "0.12.8", ignore=IGNORE)
        c = self.claude(self.home)
        hooks = c / "golden-thread" / "hooks"
        self.run_install(self.new, self.home)
        self.assertTrue((hooks / "guard_protected_paths.sh").is_file(), "fixture: newest wires it")
        mine = hooks / "my_own_hook.sh"
        mine.write_text("#!/bin/sh\nexit 0\n")
        p = self.run_install(self.new, self.home, "0.12.8", "--no-vault")
        self.assertFalse((hooks / "guard_protected_paths.sh").exists(), p.stdout)
        self.assertFalse((hooks / "guard_protected_paths.py").exists(), p.stdout)
        cmds = [h.get("command", "")
                for blocks in json.loads((c / "settings.json").read_text()).get("hooks", {}).values()
                for b in blocks for h in b.get("hooks", [])]
        self.assertFalse([x for x in cmds if "guard_protected_paths" in x], cmds)
        self.assertTrue(mine.is_file(), "a file of the user's own was removed")

    def test_rollback_honours_install_demo_no(self):
        c = self.claude(self.home)
        self.run_install(self.new, self.home)
        cfg = c / "vault-config.json"
        d = json.loads(cfg.read_text()) if cfg.exists() else {}
        d["install_demo"] = "no"
        cfg.write_text(json.dumps(d))
        # a plain rollback with no recorded choice still reads install_demo=no
        p = self.run_install(self.new, self.home, self.OLD_GT, "--no-vault")
        gt_cache = c / "plugins" / "cache" / MARKET / "gt" / self.OLD_GT
        market = c / "plugins" / "marketplaces" / MARKET / "plugins" / "gt"
        for where in (gt_cache, market):
            self.assertNotIn("gt-demo", self.gt_skills(where), where)
            self.assertFalse((where / "scripts" / "gt_demo.sh").exists(), where)
            self.assertFalse((where / "templates" / "demo-pizzabot").exists(), where)
        self.assertIn("Demo not installed", p.stdout)
        # --with demo on the same rollback brings it back, as 0.13.0 with install_demo=yes
        p = self.run_install(self.new, self.home, self.OLD_GT, "--with", "demo", "--no-vault")
        self.assertIn("gt-demo", self.gt_skills(gt_cache))
        self.assertIn("gt-demo", self.gt_skills(market))
        self.assertNotIn("Demo not installed", p.stdout)

    def test_upgrade_names_the_commands_that_moved_to_a_module(self):
        # fresh install: nothing moved, nothing said
        other = self.tmp / "fresh-home"
        other.mkdir()
        p = self.run_install(self.new, other)
        self.assertNotIn("Moved:", p.stdout)
        # a 0.13.0 machine (gt-watch and gt-farm inside gt) upgraded to the newest gt
        self.run_install(self.new, self.home, self.OLD_GT)
        old = self.gt_skills(self.claude(self.home) / "plugins" / "cache" / MARKET / "gt"
                             / self.OLD_GT)
        self.assertIn("gt-watch", old)
        p = self.run_install(self.new, self.home)
        self.assertIn("Moved: /gt:gt-watch → /gt-watch:gt-watch", p.stdout)
        self.assertIn("Moved: /gt:gt-farm → /gt-farm:gt-farm", p.stdout)
        for line in re.findall(r"^Moved: .*$", p.stdout, re.M):
            skill = line.split()[1].split(":", 1)[1]
            self.assertNotIn(skill, self.gt_skills(GT), "a skill gt still ships is not moved")
        self.assertEqual(len(re.findall(r"^Moved: /gt:gt-watch ", p.stdout, re.M)), 1,
                         "said once, even across the migration re-run")
        # farm is turned ON for this upgrader by a machine migration mid-install; the notice
        # used to be computed before it and said "module farm is off" (validator 2026-09-14)
        self.assertRegex(p.stdout, r"Modules: .*\bfarm on\b", "fixture: farm should end on")
        self.assertNotIn("module farm is off", p.stdout)
        # the next install has nothing new to say
        p = self.run_install(self.new, self.home)
        self.assertNotIn("Moved:", p.stdout)


if __name__ == "__main__":
    unittest.main()
