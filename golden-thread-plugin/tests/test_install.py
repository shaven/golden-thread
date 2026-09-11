"""install.sh: installs gt + gt-wiki into HOME and wires the reporting hooks.

install.sh writes MANIFEST.json into the version dir it installs FROM, so every
run here uses a throwaway copy of the repo (install.sh + the newest version dirs)
and a sandbox HOME. Nothing touches the real repo or the real ~/.claude.

Contracts pinned here:
  * gt and gt-wiki land in ~/.claude/plugins/cache/... AND the marketplace, file for
    file; both are registered and enabled;
  * the seven hooks install.sh owns are registered in settings.json and verified, and
    point at files that exist in the sandbox;
  * install_demo=no leaves out exactly the demo skill, script and templates from
    BOTH cache and marketplace -- including a demo left by an earlier install;
  * a second run is idempotent; foreign settings survive; old gt caches are pruned;
  * a bad version request or a manifest/dir mismatch refuses before installing.
"""
import json
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, REPO, GT, WIKI

INSTALL = REPO / "install.sh"
DEMO = ("skills/gt-demo", "scripts/gt_demo.sh", "templates/demo-pizzabot")
GT_DIRS = (".claude-plugin", "skills", "scripts", "templates", "commands", "hooks")
WIKI_DIRS = (".claude-plugin", "skills", "scripts", "templates", "commands")
OWNED = {"SessionStart": {"gt_components.py", "gt_workers.py", "gt_version_check.py",
                          "gt_push_check.py", "gt_watch.py"},
         "PreCompact": {"gt_report_card.py"}, "SessionEnd": {"gt_report_card.py"}}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def files_under(root: Path, dirs):
    out = set()
    for d in dirs:
        base = root / d
        if base.is_dir():
            out |= {str(p.relative_to(root)) for p in base.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts}
    return out


def is_demo(rel):
    return any(rel == d or rel.startswith(d + "/") for d in DEMO)


class InstallTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo_copy(self.tmp / "src" / "golden-thread-plugin")

    def make_repo_copy(self, dest: Path) -> Path:
        dest.mkdir(parents=True)
        shutil.copy2(INSTALL, dest / "install.sh")
        shutil.copytree(GT, dest / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, dest / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        return dest

    def install(self, *args, repo=None):
        return self.sh((repo or self.repo) / "install.sh", *args, timeout=300)

    # -- paths -------------------------------------------------------------------
    @property
    def plugins(self):
        return self.home / ".claude" / "plugins"

    def cache(self, which="gt"):
        ver = GT.name if which == "gt" else WIKI.name
        return self.plugins / "cache" / "golden-thread-plugin" / which / ver

    def market(self, which="gt"):
        return self.plugins / "marketplaces" / "golden-thread-plugin" / "plugins" / which

    def settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text())

    def src(self, which="gt"):
        return self.repo / ("golden-thread" if which == "gt" else "golden-thread-wiki") / (
            GT.name if which == "gt" else WIKI.name)

    # ------------------------------------------------------------------------------
    def test_scripts_parse(self):
        for script in (INSTALL, REPO / "package.sh"):
            with self.subTest(script=script.name):
                self.assertOk(self.run_cmd(["bash", "-n", script]))

    def test_fresh_install_lays_down_both_plugins(self):
        p = self.install()
        self.assertOk(p)
        for which, dirs in (("gt", GT_DIRS), ("gt-wiki", WIKI_DIRS)):
            want = files_under(self.src(which), dirs)
            self.assertTrue(want)
            with self.subTest(which=which, where="cache"):
                self.assertEqual(files_under(self.cache(which), dirs), want)
            with self.subTest(which=which, where="marketplace"):
                self.assertEqual(files_under(self.market(which), dirs), want)
        mp = json.loads((self.plugins / "marketplaces" / "golden-thread-plugin" / ".claude-plugin"
                         / "marketplace.json").read_text())
        self.assertEqual({x["name"] for x in mp["plugins"]}, {"gt", "gt-wiki"})
        inst = json.loads((self.plugins / "installed_plugins.json").read_text())["plugins"]
        self.assertEqual(inst["gt@golden-thread-plugin"][0]["installPath"], str(self.cache("gt")))
        self.assertEqual(inst["gt@golden-thread-plugin"][0]["version"], GT.name)
        self.assertEqual(inst["gt-wiki@golden-thread-plugin"][0]["version"], WIKI.name)
        known = json.loads((self.plugins / "known_marketplaces.json").read_text())
        self.assertIn("golden-thread-plugin", known)
        self.assertEqual(self.settings()["enabledPlugins"],
                         {"gt@golden-thread-plugin": True, "gt-wiki@golden-thread-plugin": True})
        # demo is installed by default
        for d in DEMO:
            self.assertTrue((self.cache() / d).exists(), d)
        hooks_dir = self.home / ".claude" / "golden-thread" / "hooks"
        for f in ("inject_core_rules.sh", "validate_response.sh", "gt_paths.py", "gt_components.py"):
            self.assertTrue((hooks_dir / f).is_file(), f)
        self.assertTrue((self.src() / "MANIFEST.json").is_file())

    def registered(self):
        hooks = self.settings().get("hooks", {})
        return {ev: [h["command"] for block in blocks for h in block.get("hooks", [])]
                for ev, blocks in hooks.items()}

    def test_seven_hooks_registered_and_verified(self):
        p = self.install()
        self.assertOk(p)
        self.assertIn("Registered 7 hooks", p.stdout)
        self.assertIn("Verified hook wiring", p.stdout)
        self.assertNotIn("INCOMPLETE", p.stdout)
        reg = self.registered()
        self.assertEqual(sum(len(v) for v in reg.values()), 7, reg)
        hooks_dir = str(self.home / ".claude" / "golden-thread" / "hooks")
        for event, scripts in OWNED.items():
            cmds = reg.get(event, [])
            self.assertEqual(len(cmds), len(scripts), event)
            for s in scripts:
                hit = [c for c in cmds if s in c]
                self.assertEqual(len(hit), 1, f"{event}/{s}: {cmds}")
                self.assertIn(hooks_dir, hit[0])
                self.assertTrue((Path(hooks_dir) / s).is_file(), s)
        # the wiring check, asked independently, agrees
        w = self.run_cmd(["python3", self.src() / "scripts" / "gt_components.py", "wiring",
                          self.src(), "--owner", "install.sh"])
        self.assertOk(w)

    def test_install_demo_no_omits_exactly_the_demo(self):
        for d in DEMO:
            self.assertTrue((self.src() / d).exists(), f"fixture: {d} missing from the source")
        self.config(install_demo="no")
        p = self.install()
        self.assertOk(p)
        self.assertIn("install_demo=no", p.stdout)
        self.assertFalse([l for l in p.stdout.splitlines() if l.startswith("  /gt:gt-demo")],
                         "summary lists the demo skill as installed")
        want = {f for f in files_under(self.src(), GT_DIRS) if not is_demo(f)}
        for where, root in (("cache", self.cache()), ("marketplace", self.market())):
            with self.subTest(where=where):
                for d in DEMO:
                    self.assertFalse((root / d).exists(), f"{where} still has {d}")
                self.assertEqual(files_under(root, GT_DIRS), want)
        # gt-wiki is untouched by the demo setting
        self.assertEqual(files_under(self.cache("gt-wiki"), WIKI_DIRS),
                         files_under(self.src("gt-wiki"), WIKI_DIRS))

    def test_install_demo_no_clears_a_demo_left_by_an_earlier_install(self):
        self.assertOk(self.install())
        self.assertTrue((self.cache() / DEMO[0]).exists())
        self.config(install_demo="no")
        self.assertOk(self.install())
        for root in (self.cache(), self.market()):
            for d in DEMO:
                self.assertFalse((root / d).exists(), f"{root}: {d} survived")

    def test_second_run_is_idempotent(self):
        self.assertOk(self.install())
        s1 = self.settings()
        files1 = files_under(self.cache(), GT_DIRS)
        p = self.install()
        self.assertOk(p)
        self.assertEqual(self.settings(), s1, "second run changed settings.json")
        self.assertEqual(sum(len(v) for v in self.registered().values()), 7, "hooks duplicated")
        self.assertEqual(files_under(self.cache(), GT_DIRS), files1)
        vers = [d.name for d in (self.plugins / "cache" / "golden-thread-plugin" / "gt").iterdir()]
        self.assertEqual(vers, [GT.name])
        self.assertIn("Verified hook wiring", p.stdout)

    def test_foreign_settings_survive_and_old_cache_is_pruned(self):
        user_hook = {"hooks": [{"type": "command", "command": "echo user-hook"}]}
        (self.home / ".claude" / "settings.json").write_text(json.dumps({
            "theme": "dark", "enabledPlugins": {"other@x": True},
            "hooks": {"SessionStart": [user_hook], "Stop": [user_hook]}}))
        old = self.plugins / "cache" / "golden-thread-plugin" / "gt" / "0.0.1"
        old.mkdir(parents=True)
        (old / "stale.txt").write_text("x")
        self.assertOk(self.install())
        s = self.settings()
        self.assertEqual(s["theme"], "dark")
        self.assertTrue(s["enabledPlugins"]["other@x"])
        self.assertIn("echo user-hook", self.registered()["SessionStart"])
        self.assertEqual(self.registered()["Stop"], ["echo user-hook"])
        self.assertEqual(len(self.registered()["SessionStart"]), 6)   # user hook + 5 gt SessionStart hooks
        self.assertFalse(old.exists(), "superseded gt cache left behind")
        backups = list((self.home / ".claude" / "golden-thread" / "backups").glob("settings.json.*"))
        self.assertTrue(backups, "pre-existing settings.json was not backed up")

    def test_path_with_apostrophe_and_space(self):
        repo = self.make_repo_copy(self.tmp / "Sam's Projects" / "golden thread plugin")
        p = self.install(repo=repo)
        self.assertOk(p)
        self.assertIn("Verified hook wiring", p.stdout)

    def test_unknown_pinned_version_refuses_before_installing(self):
        p = self.install("9.9.9")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("9.9.9", p.stdout)
        self.assertFalse((self.home / ".claude" / "settings.json").exists())
        self.assertFalse(self.plugins.exists())

    def test_manifest_version_mismatch_refuses(self):
        pj = self.src() / ".claude-plugin" / "plugin.json"
        d = json.loads(pj.read_text())
        d["version"] = "0.0.0"
        pj.write_text(json.dumps(d))
        p = self.install()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("version mismatch", p.stdout)
        self.assertFalse(self.plugins.exists())


if __name__ == "__main__":
    unittest.main()
