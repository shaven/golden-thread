"""install.sh installs EVERY plugin the tree ships, and runs the release's machine migrations.

Added in 0.14.0. Until then install.sh named gt and gt-wiki by hand in ~22 places, so a
third plugin placed beside them would have been silently skipped -- the failure
dev/plugins.py already closed for the build tools.

Contracts pinned here:
  * install.sh's own discovery (--list-plugins) equals `dev/plugins.py list` on the real
    repo -- install.sh must stand alone, so it carries the rule rather than calling dev/;
  * a fixture third plugin is copied to the cache and marketplace, listed in
    marketplace.json, registered in installed_plugins.json, enabled and summarised --
    and no gt-specific step (hooks dir, hook registrations, demo) runs for it;
  * a pinned version pins gt only -- except that each other plugin installs the release
    that pinned gt shipped with: install.sh's SHIPPED_WITH table equals what git history
    says each gt release commit carried (0.15.0);
  * scripts/gt_machine_migrate.py, when the release ships it, runs after hooks are wired
    and before the vault step: exit 0 continues and prints its output, exit 1 or 2 stops
    with INSTALL INCOMPLETE after printing; when absent, nothing is printed.
"""
import ast
import json
import re
import shutil
import subprocess
import unittest

from _harness import Sandbox, REPO, GT, WIKI

INSTALL = REPO / "install.sh"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
EXTRA_DIR, EXTRA_VER, EXTRA_NAME = "gt-extra", "1.0.0", "gt-extra"


def _vkey(v):
    return tuple(int(x) for x in v.split("."))


class ShippedWithMatchesHistory(unittest.TestCase):
    """SHIPPED_WITH (in install.sh's helper) says which release of each other plugin a gt
    release installs on a rollback. It is derived from git history, so it is checked
    against it: at the commit that first added golden-thread/<gt>/, the newest release of
    every other plugin present is what that gt's own installer installed."""

    def test_table_equals_git_history(self):
        m = re.search(r"^SHIPPED_WITH = (\{.*?^\})$", INSTALL.read_text(), re.M | re.S)
        self.assertIsNotNone(m, "SHIPPED_WITH not found in install.sh")
        table = ast.literal_eval(m.group(1))
        root = REPO.parent
        if not shutil.which("git") or subprocess.run(
                ["git", "-C", str(root), "rev-parse"], capture_output=True).returncode != 0:
            self.skipTest("not a git checkout")
        checked = 0
        for gt, plugins in table.items():
            added = subprocess.run(
                ["git", "-C", str(root), "log", "--diff-filter=A", "--format=%h", "--reverse",
                 "--", "%s/golden-thread/%s/.claude-plugin/plugin.json" % (REPO.name, gt)],
                capture_output=True, text=True).stdout.split()
            if not added:
                continue
            for d in ("golden-thread-wiki", "golden-thread-demo"):
                names = subprocess.run(
                    ["git", "-C", str(root), "ls-tree", "--name-only", added[0],
                     "%s/%s/" % (REPO.name, d)], capture_output=True, text=True).stdout.split()
                vers = [n.rsplit("/", 1)[-1] for n in names]
                vers = sorted((v for v in vers if re.fullmatch(r"\d+\.\d+\.\d+", v)), key=_vkey)
                plugin = "gt-" + d.split("-", 2)[2]
                self.assertEqual(plugins.get(plugin), vers[-1] if vers else None,
                                 "gt %s: %s" % (gt, plugin))
                checked += 1
        self.assertGreater(checked, 0, "no table row could be checked against history")


class PluginDiscoveryAgrees(Sandbox):
    def test_install_sh_discovery_equals_dev_plugins_list(self):
        plugins_py = REPO / "dev" / "plugins.py"
        if not plugins_py.is_file():
            self.skipTest("dev/ not present (gt-src or a package)")
        want = self.py(plugins_py, "list", REPO)
        self.assertOk(want)
        got = self.sh(INSTALL, "--list-plugins")
        self.assertOk(got)
        self.assertEqual(got.stdout.splitlines(), want.stdout.splitlines())
        self.assertTrue(got.stdout.strip(), "discovery found nothing")

    def test_list_plugins_installs_nothing(self):
        self.assertOk(self.sh(INSTALL, "--list-plugins"))
        self.assertFalse((self.home / ".claude" / "plugins").exists())
        self.assertFalse((self.home / ".claude" / "settings.json").exists())


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / "golden-thread-plugin"
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        shutil.copytree(GT, self.src, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        self.manifest()

    @property
    def src(self):
        return self.repo / "golden-thread" / GT.name

    @property
    def plugins(self):
        return self.home / ".claude" / "plugins"

    def manifest(self):
        self.assertOk(self.py(self.src / "scripts" / "gt_components.py", "manifest", self.src))

    def install(self, *args):
        if not any(a in ("--vault", "--no-vault") for a in args):
            args = (*args, "--no-vault")
        return self.sh(self.repo / "install.sh", *args, timeout=300)


class InstallsAThirdPlugin(Base):
    def setUp(self):
        super().setUp()
        v = self.repo / EXTRA_DIR / EXTRA_VER
        (v / ".claude-plugin").mkdir(parents=True)
        (v / ".claude-plugin" / "plugin.json").write_text(json.dumps(
            {"name": EXTRA_NAME, "version": EXTRA_VER, "description": "Fixture module."}))
        (v / "skills" / "extra-hello").mkdir(parents=True)
        (v / "skills" / "extra-hello" / "SKILL.md").write_text(
            "---\nname: extra-hello\ndescription: Say hello from the fixture plugin.\n---\n")
        (v / "scripts").mkdir()
        (v / "scripts" / "extra_tool.py").write_text("print('extra')\n")
        # an older version beside it: only the newest ships
        old = self.repo / EXTRA_DIR / "0.9.0" / ".claude-plugin"
        old.mkdir(parents=True)
        (old / "plugin.json").write_text(json.dumps({"name": EXTRA_NAME, "version": "0.9.0"}))

    def test_the_third_plugin_is_installed_registered_and_enabled(self):
        p = self.install()
        self.assertOk(p)
        key = "%s@golden-thread-plugin" % EXTRA_NAME
        cache = self.plugins / "cache" / "golden-thread-plugin" / EXTRA_NAME / EXTRA_VER
        self.assertTrue((cache / "skills" / "extra-hello" / "SKILL.md").is_file())
        self.assertTrue((cache / "scripts" / "extra_tool.py").is_file())
        self.assertEqual(sorted(d.name for d in cache.parent.iterdir()), [EXTRA_VER])
        market = self.plugins / "marketplaces" / "golden-thread-plugin"
        self.assertTrue((market / "plugins" / EXTRA_NAME / "skills" / "extra-hello"
                         / "SKILL.md").is_file())
        self.assertTrue((market / "plugins" / EXTRA_NAME / ".claude-plugin"
                         / "plugin.json").is_file())
        mp = json.loads((market / ".claude-plugin" / "marketplace.json").read_text())
        entries = {e["name"]: e for e in mp["plugins"]}
        self.assertEqual(set(entries), {"gt", "gt-wiki", EXTRA_NAME})
        self.assertEqual(entries[EXTRA_NAME]["source"], "./plugins/%s" % EXTRA_NAME)
        self.assertEqual(entries[EXTRA_NAME]["description"], "Fixture module.")
        self.assertEqual(mp["plugins"][0]["name"], "gt", "the core plugin is listed first")
        inst = json.loads((self.plugins / "installed_plugins.json").read_text())["plugins"]
        self.assertEqual(inst[key][0]["installPath"], str(cache))
        self.assertEqual(inst[key][0]["version"], EXTRA_VER)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertEqual(settings["enabledPlugins"],
                         {"gt@golden-thread-plugin": True, "gt-wiki@golden-thread-plugin": True,
                          key: True})
        self.assertIn("Installed %s plugin files" % EXTRA_NAME, p.stdout)
        self.assertIn("%s skills:" % EXTRA_NAME, p.stdout)
        self.assertIn("/%s:extra-hello" % EXTRA_NAME, p.stdout)
        self.assertIn(", %s %s" % (EXTRA_NAME, EXTRA_VER), p.stdout.splitlines()[0])

    def test_no_gt_specific_step_runs_for_it(self):
        p = self.install()
        self.assertOk(p)
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.assertFalse((hooks / "extra_tool.py").exists(),
                         "a non-core plugin's script landed in the gt hooks dir")
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        flat = [h.get("command", "") for bl in settings.get("hooks", {}).values()
                for b in bl for h in b.get("hooks", [])]
        self.assertFalse([c for c in flat if EXTRA_NAME in c or "extra_tool" in c], flat)
        self.assertIn("Registered 5 hooks", p.stdout, "hook registration count changed")

    def test_a_superseded_cache_of_the_third_plugin_is_pruned(self):
        old = self.plugins / "cache" / "golden-thread-plugin" / EXTRA_NAME / "0.9.0"
        old.mkdir(parents=True)
        p = self.install()
        self.assertOk(p)
        self.assertFalse(old.exists())
        self.assertIn("Removed superseded %s cache" % EXTRA_NAME, p.stdout)

    def test_a_version_pin_applies_to_gt_only(self):
        p = self.install(GT.name)
        self.assertOk(p)
        self.assertIn("pinned", p.stdout)
        inst = json.loads((self.plugins / "installed_plugins.json").read_text())["plugins"]
        self.assertEqual(inst["gt@golden-thread-plugin"][0]["version"], GT.name)
        self.assertEqual(inst["%s@golden-thread-plugin" % EXTRA_NAME][0]["version"], EXTRA_VER)

    def test_a_manifest_version_mismatch_in_any_plugin_refuses(self):
        pj = self.repo / EXTRA_DIR / EXTRA_VER / ".claude-plugin" / "plugin.json"
        pj.write_text(json.dumps({"name": EXTRA_NAME, "version": "2.0.0"}))
        p = self.install()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("version mismatch", p.stdout)
        self.assertFalse(self.plugins.exists())

    def test_a_duplicate_plugin_name_refuses(self):
        pj = self.repo / EXTRA_DIR / EXTRA_VER / ".claude-plugin" / "plugin.json"
        pj.write_text(json.dumps({"name": "gt-wiki", "version": EXTRA_VER}))
        p = self.install()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("gt-wiki", p.stdout)
        self.assertFalse(self.plugins.exists())


STUB = """import sys
args = sys.argv[1:]
assert "run" in args and "--release" in args, args
print(%r)
sys.exit(%d)
"""


class RunsMachineMigrations(Base):
    def stub(self, output, code):
        """A stand-in for the release's gt_machine_migrate.py, in the SANDBOX copy only."""
        (self.src / "scripts" / "gt_machine_migrate.py").write_text(STUB % (output, code))
        self.manifest()

    def test_success_prints_its_output_and_continues(self):
        self.stub("applied demo-to-install-choices", 0)
        p = self.install()
        self.assertOk(p)
        self.assertIn("Machine migrations:", p.stdout)
        self.assertIn("applied demo-to-install-choices", p.stdout)
        self.assertNotIn("INSTALL INCOMPLETE", p.stdout)
        self.assertIn("Restart Claude Code", p.stdout)

    def test_it_runs_before_the_vault_step(self):
        self.stub("applied x", 0)
        p = self.install("--vault", str(self.tmp / "v"))
        self.assertOk(p)
        self.assertLess(p.stdout.index("Machine migrations:"), p.stdout.index("Vault ready"))
        self.assertGreater(p.stdout.index("Machine migrations:"),
                           p.stdout.index("Verified hook wiring"))

    def test_it_receives_the_release_being_installed(self):
        (self.src / "scripts" / "gt_machine_migrate.py").write_text(
            "import sys\nprint('ARGS ' + ' | '.join(sys.argv[1:]))\n")
        self.manifest()
        p = self.install()
        self.assertOk(p)
        self.assertIn("run | --release | %s" % self.src, p.stdout)

    def test_it_receives_the_gt_installed_before_this_install(self):
        """0.15.0: read from installed_plugins.json BEFORE install.sh overwrites it, and
        passed through the environment so an older release's migrator is not handed an
        option it would refuse."""
        (self.src / "scripts" / "gt_machine_migrate.py").write_text(
            "import os, sys\nprint('PREV=' + os.environ.get('GT_PREVIOUS_RELEASE', '<unset>'))\n"
            "assert '--previous-release' not in sys.argv\n")
        self.manifest()
        inst = self.plugins / "installed_plugins.json"
        inst.parent.mkdir(parents=True)
        inst.write_text(json.dumps({"version": 2, "plugins": {
            "gt@golden-thread-plugin": [{"version": "0.12.8"}]}}))
        p = self.install()
        self.assertOk(p)
        self.assertIn("PREV=0.12.8", p.stdout)
        p = self.install()                              # now gt GT.name is what was there
        self.assertIn("PREV=%s" % GT.name, p.stdout)

    def test_a_first_install_has_no_previous_release(self):
        (self.src / "scripts" / "gt_machine_migrate.py").write_text(
            "import os\nprint('PREV=[' + os.environ.get('GT_PREVIOUS_RELEASE', '<unset>') + ']')\n")
        self.manifest()
        p = self.install()
        self.assertOk(p)
        self.assertIn("PREV=[]", p.stdout)

    def test_a_failed_migration_stops_the_install(self):
        self.stub("FAILED demo-to-install-choices: cannot write", 1)
        p = self.install("--vault", str(self.tmp / "v"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("FAILED demo-to-install-choices: cannot write", p.stdout)
        self.assertIn("INSTALL INCOMPLETE — machine migration failed; nothing was rolled back; "
                      "fix and re-run install.sh", p.stdout)
        self.assertLess(p.stdout.index("FAILED"), p.stdout.index("INSTALL INCOMPLETE"))
        self.assertNotIn("Vault ready", p.stdout, "the vault step ran after a failed migration")
        self.assertNotIn("Restart Claude Code", p.stdout)

    def test_could_not_evaluate_also_stops_the_install(self):
        self.stub("cannot read ~/.claude/golden-thread/machine-migrations.json", 2)
        p = self.install()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("INSTALL INCOMPLETE — machine migration failed", p.stdout)

    def test_an_absent_migrator_is_silent(self):
        mig = self.src / "scripts" / "gt_machine_migrate.py"
        if mig.exists():
            mig.unlink()
            self.manifest()
        p = self.install()
        self.assertOk(p)
        self.assertNotIn("Machine migrations", p.stdout)
        self.assertNotIn("INSTALL INCOMPLETE", p.stdout)


if __name__ == "__main__":
    unittest.main()
