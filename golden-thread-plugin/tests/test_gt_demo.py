"""gt_demo.sh + the demo tour — the demo runs in its own vault and never touches the real one.

Rewritten for 0.9.14 (request 2026-09-11-demo-full-tour-own-vault). The previous design
ran in the user's real vault and rewound its git history on `clean`; its tests pinned the
guards around that reset. The reset no longer exists, so neither do those tests: what is
pinned now is that nothing outside the demo vault changes, whatever the demo does.

0.14.0: the demo is a MODULE — its own plugin `gt-demo` in `golden-thread-demo/<version>/`.
The demo's files come from the newest module dir; gt's core scripts (vault_init.py,
gt_watch.py, gt_lint.py) come from the gt release under test (GT_TEST_VERSION). `remove`
no longer deletes plugin files: uninstalling a module is install.sh's job.
"""
import hashlib
import json
import re
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, GT, WIKI, REPO, PYTHON, latest_version_dir

ACTS = 10  # core acts only; module acts (e.g. wiki's demo/act.md) are added by `tour-acts`
DEMO_MODULE = latest_version_dir(REPO / "golden-thread-demo")
MARKET = "golden-thread-plugin"
MOVED = ("skills/gt-demo", "scripts/gt_demo.sh", "templates/demo-pizzabot")


def tree_digest(root: Path):
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


# Other tests import scripts from the source tree in parallel and write __pycache__ into it;
# copying a half-written .pyc makes copytree fail. Caches are never part of a release.
_NO_CACHE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyc.*")


def install_demo_plugin(dest: Path):
    dest.mkdir(parents=True)
    for d in (".claude-plugin", "scripts", "templates", "skills"):
        shutil.copytree(DEMO_MODULE / d, dest / d, ignore=_NO_CACHE)
    for f in ("module.json", "MANIFEST.json"):
        shutil.copy2(DEMO_MODULE / f, dest / f)
    return dest


class DemoTest(Sandbox):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git required")
        # A fake install, laid out like the plugin cache: gt and gt-demo side by side.
        cache = self.home / ".claude" / "plugins" / "cache" / MARKET
        self.plugin = cache / "gt" / GT.name
        self.plugin.mkdir(parents=True)
        for d in ("scripts", "templates", "skills", "hooks"):
            shutil.copytree(GT / d, self.plugin / d, ignore=_NO_CACHE)
        self.demo_plugin = install_demo_plugin(cache / "gt-demo" / DEMO_MODULE.name)
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        shutil.copytree(GT / "hooks", hooks, ignore=_NO_CACHE)
        # The user's REAL vault and config — the demo must leave both byte-identical.
        self.real = self.make_vault("real")
        self.cfg = self.home / ".claude" / "vault-config.json"
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text("{}\n")
        self.demo = self.home / ".claude" / "golden-thread" / "demo-vault"
        self.script = self.demo_plugin / "scripts" / "gt_demo.sh"

    def demo_cmd(self, *args, **kw):
        return self.sh(self.script, *args, **kw)

    def snapshot(self):
        return (tree_digest(self.real), self.cfg.read_bytes(), self.settings.read_bytes(),
                (self.home / ".claude" / "CLAUDE.md").read_bytes() if (self.home / ".claude" / "CLAUDE.md").exists() else b"")

    # -- start ---------------------------------------------------------------------------
    def test_start_builds_a_seeded_demo_vault(self):
        self.assertOk(self.demo_cmd("start"))
        self.assertTrue((self.demo / ".demo" / "DEMO_VAULT").is_file())
        self.assertTrue((self.demo / "Projects" / "demo-pizzabot" / "README.md").is_file())
        self.assertTrue(any((self.demo / "Sources").glob("*PizzaBot*.md")))
        self.assertIn("oven timer", (self.demo / "INBOX.md").read_text())
        log = self.run_cmd(["git", "-C", self.demo, "log", "--oneline"]).stdout
        self.assertIn("PizzaBot 3000 seeded", log)
        readme = (self.demo / "Projects" / "demo-pizzabot" / "README.md").read_text()
        self.assertNotIn("{{TODAY}}", readme, "task dates must be filled in at start")

    def test_real_vault_config_and_settings_untouched_by_every_command(self):
        before = self.snapshot()
        for cmd in ("start", "end", "status", "clean", "end", "remove"):
            self.demo_cmd(cmd)
        self.assertEqual(self.snapshot(), before,
                         "the demo changed the real vault, vault-config.json, settings.json or CLAUDE.md")

    def test_start_prints_the_pinned_launch_command(self):
        out = self.demo_cmd("start").stdout
        self.assertIn(f'cd "{self.demo}" && GT_VAULT="{self.demo}" GT_WATCH=report '
                      f'GT_WATCH_STATE="{self.demo}/.demo/watch" claude', out)
        self.assertIn("/gt-demo:gt-demo tour", out)
        self.assertNotIn("/gt:gt-demo", out)

    def test_watch_act_reports_the_upstream_cve_as_p0(self):
        # The gt-watch act (0.10.0), offline: add -> upstream-release -> fetch -> --hook.
        self.assertOk(self.demo_cmd("start"))
        bare = self.demo / ".demo" / "upstream" / "widget-lib.git"
        tags = self.run_cmd(["git", "-C", bare, "tag"]).stdout.split()
        self.assertEqual(tags, ["v1.0.0"], "start seeds the upstream with v1.0.0 only")
        env = {"GT_VAULT": str(self.demo), "GT_WATCH": "report",
               "GT_WATCH_STATE": str(self.demo / ".demo" / "watch")}
        # <core> in the tour: what `core-scripts` prints, i.e. gt's scripts, not the module's.
        core = Path(self.demo_cmd("core-scripts").stdout.strip())
        self.assertEqual(core, self.plugin / "scripts")
        watch = core / "gt_watch.py"
        self.assertOk(self.py(watch, "add", "file://%s" % bare, "--label", "Widget library", env=env))
        self.assertOk(self.demo_cmd("upstream-release"))
        self.assertNotEqual(self.demo_cmd("upstream-release").returncode, 0, "a second release refuses")
        self.assertOk(self.py(watch, "fetch", env=env))
        p = self.py(watch, "--hook", env=env)
        self.assertOk(p)
        msg = json.loads(p.stdout)["systemMessage"]
        p0 = [l for l in msg.splitlines() if l.strip().startswith("P0")]
        self.assertTrue(p0, msg)
        self.assertIn("widget-lib", p0[0])
        self.assertIn("CVE-2026-12345", p0[0])
        # machine state stayed in the ignored .demo/ folder
        self.assertIn(".demo/", (self.demo / ".gitignore").read_text())

    def test_start_twice_refuses(self):
        self.assertOk(self.demo_cmd("start"))
        self.assertNotEqual(self.demo_cmd("start").returncode, 0)

    # -- what the tour relies on -------------------------------------------------------------
    def test_demo_vault_lints_with_only_the_planted_broken_link(self):
        self.assertOk(self.demo_cmd("start"))
        out = self.py(self.plugin / "scripts" / "gt_lint.py", self.demo).stdout
        found = [l for l in re.findall(r"^\[([a-z-]+)\] (.+)$", out, re.M) if l[0] != "core-unenforced"]
        self.assertEqual(found, [("broken-link", "Projects/demo-pizzabot/README.md")], out)

    def test_due_today_task_ranks_first(self):
        self.assertOk(self.demo_cmd("start"))
        tool = self.demo / "Projects" / "golden-thread" / "tools" / "gt_tasks.py"
        self.assertOk(self.py(tool, "--vault", self.demo))
        rows = re.findall(r"^\| `PP\d+-P\d+` \| (.+?) \|", (self.demo / "TASKS.md").read_text(), re.M)
        self.assertTrue(rows, "no ranked tasks in TASKS.md")
        self.assertIn("/order", rows[0])

    def test_act_one_transcript_is_blocked_by_the_stop_hook(self):
        self.assertOk(self.demo_cmd("start"))
        payload = (self.demo / ".demo" / "secret-transcript.json").read_text()
        proc = self.run_cmd(["bash", self.home / ".claude" / "golden-thread" / "hooks" / "validate_response.sh"], input=payload)
        self.assertEqual(json.loads(proc.stdout)["decision"], "block")
        self.assertIn(".demo/", (self.demo / ".gitignore").read_text(), "the transcript must never be committed")

    def test_no_key_shaped_literal_in_the_shipped_plugin(self):
        pat = re.compile(r"AKIA[0-9A-Z]{16}")
        files = (list((GT / "scripts").iterdir()) + list((DEMO_MODULE / "scripts").iterdir())
                 + list((DEMO_MODULE / "templates").rglob("*")))
        for p in files:
            if p.is_file():
                self.assertIsNone(pat.search(p.read_text(errors="replace")), f"key-shaped literal in {p}")

    def test_core_tour_has_ten_complete_acts_naming_real_skills(self):
        tour = (DEMO_MODULE / "templates" / "demo-pizzabot" / "tour.md").read_text()
        acts = re.split(r"^## Act \d+ — ", tour, flags=re.M)[1:]
        self.assertEqual(len(acts), ACTS)
        skills = {p.name for root in (GT, DEMO_MODULE, WIKI) for p in (root / "skills").iterdir()}
        for body in acts:
            for key in ("narration:", "do:", "point:"):
                self.assertIn(key, body, body[:60])
            for s in re.findall(r"\bthe (gt-[a-z-]+) skill\b", body):
                self.assertIn(s, skills, f"tour names a skill that does not ship: {s}")

    def test_the_wiki_act_lives_in_the_wiki_module_not_the_core_tour(self):
        tour = (DEMO_MODULE / "templates" / "demo-pizzabot" / "tour.md").read_text()
        wiki_skills = {p.name for p in (WIKI / "skills").iterdir()}
        self.assertFalse(set(re.findall(r"\bthe (gt-[a-z-]+) skill\b", tour)) & wiki_skills,
                         "the core tour names a wiki-module skill")
        self.assertNotIn("the wiki module is not installed", tour)
        self.assertNotIn("the wiki module is not installed",
                         (DEMO_MODULE / "skills" / "gt-demo" / "SKILL.md").read_text())
        wiki = json.loads((WIKI / "module.json").read_text())
        self.assertEqual(wiki["demo"], "demo/act.md")
        act = (WIKI / wiki["demo"]).read_text()
        self.assertTrue(set(re.findall(r"\bthe (gt-[a-z-]+) skill\b", act)) & wiki_skills)
        for key in ("narration:", "do:", "point:"):
            self.assertRegex(act, r"(?m)^%s " % key)
        module = json.loads((DEMO_MODULE / "module.json").read_text())
        self.assertIn({"name": "wiki", "soft": True}, module["requires_modules"])

    def test_skill_runs_the_assembled_tour_not_tour_md(self):
        skill = (DEMO_MODULE / "skills" / "gt-demo" / "SKILL.md").read_text()
        self.assertIn("gt_demo.sh tour-acts", skill)
        self.assertNotIn("Read `<base>/../../templates/demo-pizzabot/tour.md`", skill)
        self.assertNotIn("eleven-act", skill)

    def test_start_honours_gt_demo_vault_in_a_temp_dir(self):
        alt = self.tmp / "alt-demo"
        p = self.demo_cmd("start", env={"GT_DEMO_VAULT": str(alt)})
        self.assertOk(p)
        self.assertTrue((alt / ".demo" / "DEMO_VAULT").is_file())
        self.assertTrue((alt / ".demo" / "secret-transcript.json").is_file(),
                        "act 1 reads $GT_VAULT/.demo/secret-transcript.json")
        self.assertFalse(self.demo.exists(), "GT_DEMO_VAULT was ignored")
        self.assertIn(f'GT_VAULT="{alt}" GT_WATCH=report GT_WATCH_STATE="{alt}/.demo/watch" claude', p.stdout)

    def test_tour_references_resolve_without_machine_paths(self):
        tour = (DEMO_MODULE / "templates" / "demo-pizzabot" / "tour.md").read_text()
        self.assertNotIn("plugins/marketplaces", tour, "tour pins one install layout")
        self.assertNotIn("plugins/cache", tour, "tour pins one install layout")
        self.assertNotIn("demo-vault", tour, "tour pins the default demo vault; use $GT_VAULT")
        own = re.findall(r"<scripts>/([A-Za-z0-9_.-]+)", tour)
        core = re.findall(r"<core>/([A-Za-z0-9_.-]+)", tour)
        self.assertTrue(own, "tour runs no module scripts")
        self.assertTrue(core, "tour runs no gt core scripts")
        for name in own:
            self.assertTrue((DEMO_MODULE / "scripts" / name).is_file(), f"tour names a missing module script: {name}")
        for name in core:
            self.assertTrue((GT / "scripts" / name).is_file(), f"tour names a missing core script: {name}")
        skill = (DEMO_MODULE / "skills" / "gt-demo" / "SKILL.md").read_text()
        self.assertIn("<base>/../../scripts", skill)
        self.assertIn("gt_demo.sh core-scripts", skill)
        self.assertTrue((DEMO_MODULE / "skills" / "gt-demo" / ".." / ".." / "scripts" / "gt_demo.sh").resolve().is_file())

    def test_skill_launch_command_matches_what_start_prints(self):
        skill = (DEMO_MODULE / "skills" / "gt-demo" / "SKILL.md").read_text()
        printed = self.demo_cmd("start").stdout
        line = next(l.strip() for l in printed.splitlines() if l.strip().startswith("cd "))
        self.assertIn(line.replace(str(self.demo), "<demo vault>"), skill)

    def test_skill_invocation_is_namespaced_by_the_module_plugin(self):
        for p in [DEMO_MODULE / "skills" / "gt-demo" / "SKILL.md", DEMO_MODULE / "scripts" / "gt_demo.sh",
                  DEMO_MODULE / "templates" / "demo-pizzabot" / "tour.md"]:
            t = p.read_text()
            self.assertNotIn("/gt:gt-demo", t, f"{p.name} still invokes the demo through the gt plugin")
            self.assertIn("/gt-demo:gt-demo", t, p.name)

    # -- end, clean, remove ------------------------------------------------------------------
    def test_end_lists_what_the_tour_produced(self):
        self.assertOk(self.demo_cmd("start"))
        (self.demo / "Knowledge" / "Topping Conflict Matrix.md").write_text("# TCM\n")
        self.run_cmd(["git", "-C", self.demo, "add", "-A"])
        self.run_cmd(["git", "-C", self.demo, "commit", "-qm", "promote"])
        out = self.demo_cmd("end").stdout
        self.assertIn("promote", out)
        self.assertIn("Topping Conflict Matrix.md", out)

    def test_clean_rebuilds_without_any_git_reset(self):
        self.assertOk(self.demo_cmd("start"))
        (self.demo / "leftover.md").write_text("from the last run\n")
        self.assertOk(self.demo_cmd("clean"))
        self.assertFalse((self.demo / "leftover.md").exists())
        self.assertTrue((self.demo / ".demo" / "DEMO_VAULT").is_file())
        self.assertNotIn("git reset", self.script.read_text())

    def test_clean_and_remove_refuse_a_directory_without_the_marker(self):
        other = self.tmp / "not-a-demo"
        other.mkdir()
        (other / "precious.md").write_text("keep me\n")
        for cmd in ("clean", "remove"):
            proc = self.demo_cmd(cmd, env={"GT_DEMO_VAULT": str(other)})
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue((other / "precious.md").exists(), f"{cmd} deleted a non-demo directory")

    def test_remove_deletes_the_demo_vault_and_points_at_install_sh(self):
        self.assertOk(self.demo_cmd("start"))
        cache = self.home / ".claude" / "plugins" / "cache"
        before = tree_digest(cache)
        p = self.demo_cmd("remove")
        self.assertOk(p)
        self.assertFalse(self.demo.exists())
        self.assertIn("bash install.sh --without demo", p.stdout)
        self.assertTrue(self.real.exists())
        # uninstalling a module is install.sh's job: no plugin file, choice or setting changes
        self.assertEqual(tree_digest(cache), before, "remove changed the plugin cache")
        self.assertTrue((self.demo_plugin / "skills" / "gt-demo" / "SKILL.md").is_file())
        self.assertTrue(self.script.is_file())
        self.assertNotIn("install_demo", json.loads(self.cfg.read_text()))
        self.assertFalse((self.home / ".claude" / "golden-thread" / "install-choices.json").exists())
        self.assertNotRegex(self.script.read_text(), r"rm -rf \"\$base|gt_settings\.py",
                            "gt_demo.sh still deletes plugin files or writes settings")

    def test_remove_never_touches_a_source_checkout(self):
        src = self.tmp / "checkout"
        install_demo_plugin(src / "golden-thread-demo" / DEMO_MODULE.name)
        p = self.sh(src / "golden-thread-demo" / DEMO_MODULE.name / "scripts" / "gt_demo.sh", "remove")
        self.assertOk(p)
        self.assertTrue((src / "golden-thread-demo" / DEMO_MODULE.name / "skills" / "gt-demo" / "SKILL.md").exists())
        self.assertTrue((src / "golden-thread-demo" / DEMO_MODULE.name / "scripts" / "gt_demo.sh").exists())

    # -- core-script resolution: the demo is a separate plugin from gt ------------------------
    def test_core_scripts_resolve_when_the_demo_is_in_a_separate_cache_dir(self):
        # No gt beside the demo plugin: only ~/.claude/plugins/cache/.../gt can answer.
        elsewhere = install_demo_plugin(self.tmp / "other-cache" / MARKET / "gt-demo" / DEMO_MODULE.name)
        script = elsewhere / "scripts" / "gt_demo.sh"
        p = self.sh(script, "core-scripts")
        self.assertOk(p)
        self.assertEqual(Path(p.stdout.strip()), self.plugin / "scripts")
        self.assertOk(self.sh(script, "start"))
        self.assertTrue((self.demo / "Projects" / "demo-pizzabot" / "README.md").is_file())

    def test_core_scripts_prefer_the_sibling_gt_and_honour_the_override(self):
        cache = self.tmp / "cache2" / MARKET
        sib = cache / "gt" / GT.name
        shutil.copytree(GT / "scripts", sib / "scripts", ignore=_NO_CACHE)
        older = cache / "gt" / "0.0.1" / "scripts"
        older.mkdir(parents=True)
        (older / "vault_init.py").write_text("")
        script = install_demo_plugin(cache / "gt-demo" / DEMO_MODULE.name) / "scripts" / "gt_demo.sh"
        self.assertEqual(Path(self.sh(script, "core-scripts").stdout.strip()), sib / "scripts",
                         "the newest sibling gt must win over an older one and over the home cache")
        p = self.sh(script, "core-scripts", env={"GT_CORE_SCRIPTS": str(self.plugin / "scripts")})
        self.assertEqual(Path(p.stdout.strip()), self.plugin / "scripts")

    def test_missing_gt_fails_with_a_reason_and_builds_nothing(self):
        shutil.rmtree(self.home / ".claude" / "plugins" / "cache" / MARKET / "gt")
        elsewhere = install_demo_plugin(self.tmp / "lonely" / "gt-demo" / DEMO_MODULE.name)
        p = self.sh(elsewhere / "scripts" / "gt_demo.sh", "start")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("core scripts", p.stderr)
        self.assertFalse(self.demo.exists())

    # -- the module itself -----------------------------------------------------------------
    def test_module_json_lists_exactly_the_files_present(self):
        m = json.loads((DEMO_MODULE / "module.json").read_text())
        plugin = json.loads((DEMO_MODULE / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual((m["name"], m["plugin"], m["version"]), ("demo", plugin["name"], DEMO_MODULE.name))
        self.assertEqual(plugin["version"], DEMO_MODULE.name)

        def listing(d):
            return sorted(p.name for p in (DEMO_MODULE / d).iterdir()
                          if not p.name.startswith(".") and p.name != "__pycache__")
        self.assertEqual(sorted(m["skills"]), listing("skills"))
        self.assertEqual(sorted(m["scripts"]), listing("scripts"))
        self.assertEqual(sorted(m["templates"]), listing("templates"))
        self.assertEqual(m["hooks"], [])
        self.assertEqual(m["hookdir_scripts"], [])
        self.assertEqual(sorted(m["replaces_core"]), sorted(MOVED))
        for rel in m["replaces_core"]:
            self.assertTrue((DEMO_MODULE / rel).exists(), rel)
            self.assertFalse((GT / rel).exists(), f"gt {GT.name} still ships {rel}")
        p = self.run_cmd([PYTHON, REPO / "dev" / "plugins.py", "manifest-check", DEMO_MODULE])
        self.assertOk(p, "the module's MANIFEST.json does not match its tree")

    def test_gt_release_no_longer_references_the_demo_location(self):
        pat = re.compile(r"scripts/gt_demo\.sh|templates/demo-pizzabot|skills/gt-demo|/gt:gt-demo")
        hits = []
        for p in GT.rglob("*"):
            # MANIFEST.json is generated, and its drift from the tree is the release gate's
            # `manifest` step (dev/plugins.py manifest-check), not this test's.
            if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc") \
                    and p.name != "MANIFEST.json":
                if pat.search(p.read_text(errors="replace")):
                    hits.append(str(p.relative_to(GT)))
        self.assertEqual(hits, [], "gt still points at the demo's pre-module location")

    # -- every skill can be pinned to the demo vault ---------------------------------------
    def test_every_skill_that_locates_the_vault_honors_gt_vault(self):
        for root in (GT, DEMO_MODULE):
            for p in (root / "skills").glob("*/SKILL.md"):
                t = p.read_text()
                if "vault-config" in t:
                    self.assertIn("GT_VAULT", t, f"{p.parent.name} reads vault-config.json but ignores $GT_VAULT")


if __name__ == "__main__":
    unittest.main()
