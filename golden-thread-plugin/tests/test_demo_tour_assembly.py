"""`gt_demo.sh tour-acts` -- the demo tour is assembled from gt's core acts plus installed modules.

Owner request 2026-09-14: "a demo flow that updates when an extension is installed". Each
first-party module may carry its own act (module.json `demo`); the tour is gt's core acts
from templates/demo-pizzabot/tour.md, then one act per INSTALLED module that ships one,
ordered by module name and numbered in sequence. Installed = enabled in settings.json
enabledPlugins AND the newest cache dir holds module.json with a valid `demo` path.
A bad module act is skipped with a note, never fatal.

0.15.0: the gt-watch act moved out of the core tour into the watch module (golden-thread-watch),
so it is in the tour only while that module is installed.
"""
import json
import re
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, WIKI, REPO, latest_version_dir

DEMO_MODULE = latest_version_dir(REPO / "golden-thread-demo")
WATCH = latest_version_dir(REPO / "golden-thread-watch")
MARKET = "golden-thread-plugin"
CORE_ACTS = len(re.findall(r"^## Act \d+ — ", (DEMO_MODULE / "templates" / "demo-pizzabot" / "tour.md")
                           .read_text(), re.M))
ACT = "## {title}\n\nnarration: n.\ndo: Invoke the gt-query skill.\npoint: p.\n"
_NO_CACHE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyc.*")


class TourAssembly(Sandbox):
    def setUp(self):
        super().setUp()
        self.cache = self.home / ".claude" / "plugins" / "cache" / MARKET
        demo = self.cache / "gt-demo" / DEMO_MODULE.name
        demo.mkdir(parents=True)
        for d in ("scripts", "templates", "skills"):
            shutil.copytree(DEMO_MODULE / d, demo / d, ignore=_NO_CACHE)
        shutil.copy2(DEMO_MODULE / "module.json", demo / "module.json")
        self.script = demo / "scripts" / "gt_demo.sh"
        self.enabled = {"gt-demo@" + MARKET: True}
        self.write_settings()

    def write_settings(self):
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": self.enabled}))

    def install_wiki(self, enabled=True):
        dest = self.cache / "gt-wiki" / WIKI.name
        shutil.copytree(WIKI, dest, ignore=_NO_CACHE)
        self.enabled["gt-wiki@" + MARKET] = enabled
        self.write_settings()
        return dest

    def install_watch(self, enabled=True):
        dest = self.cache / "gt-watch" / WATCH.name
        shutil.copytree(WATCH, dest, ignore=_NO_CACHE)
        self.enabled["gt-watch@" + MARKET] = enabled
        self.write_settings()
        return dest

    def fake_module(self, name, demo_rel=None, act_title=None, version="1.0.0", enabled=True):
        vd = self.cache / ("gt-" + name) / version
        vd.mkdir(parents=True)
        m = {"schema": 1, "name": name, "plugin": "gt-" + name, "version": version}
        if demo_rel is not None:
            m["demo"] = demo_rel
        (vd / "module.json").write_text(json.dumps(m))
        if act_title:
            (vd / "demo").mkdir()
            (vd / "demo" / "act.md").write_text(ACT.format(title=act_title))
        self.enabled["gt-%s@%s" % (name, MARKET)] = enabled
        self.write_settings()
        return vd

    def tour(self):
        p = self.sh(self.script, "tour-acts")
        self.assertOk(p)
        heads = re.findall(r"^## Act (\d+) — (.+)$", p.stdout, re.M)
        nums = [int(n) for n, _ in heads]
        self.assertEqual(nums, list(range(1, len(nums) + 1)), "acts are not numbered contiguously")
        return p, [t for _, t in heads]

    def test_core_only_when_no_module_ships_an_act(self):
        p, titles = self.tour()
        self.assertEqual(len(titles), CORE_ACTS)
        self.assertIn("/gt-demo:gt-demo", p.stdout, "the preamble is kept")
        self.assertEqual(p.stderr.strip(), "")

    def test_installed_wiki_act_runs_before_the_closing_act(self):
        # Module acts go before the last core act, which closes the tour and prints the
        # receipt -- a module's work has to happen before it to appear in the receipt.
        self.install_wiki()
        p, titles = self.tour()
        self.assertEqual(len(titles), CORE_ACTS + 1)
        self.assertEqual(titles[-2], "The wiki (module: wiki)")
        self.assertNotIn("module:", titles[-1], "the closing core act must stay last")
        act = p.stdout.split("## Act %d — " % CORE_ACTS)[1].split("## Act %d — " % (CORE_ACTS + 1))[0]
        self.assertIn("gt-wiki-ingest skill", act)
        for key in ("narration:", "do:", "point:"):
            self.assertRegex(act, r"(?m)^%s " % key)

    def test_wiki_disabled_or_uncached_leaves_no_act(self):
        self.install_wiki(enabled=False)
        _, titles = self.tour()
        self.assertEqual(len(titles), CORE_ACTS)
        self.enabled.pop("gt-wiki@" + MARKET)
        self.write_settings()
        self.assertEqual(len(self.tour()[1]), CORE_ACTS)
        # enabled but no cache dir
        shutil.rmtree(self.cache / "gt-wiki")
        self.enabled["gt-wiki@" + MARKET] = True
        self.write_settings()
        p, titles = self.tour()
        self.assertEqual(len(titles), CORE_ACTS)
        self.assertNotIn("wiki", " ".join(titles))

    def test_installed_watch_act_runs_before_the_closing_act(self):
        self.install_watch()
        p, titles = self.tour()
        self.assertEqual(len(titles), CORE_ACTS + 1)
        self.assertEqual(titles[-2], "Watch upstream (module: watch)")
        self.assertNotIn("module:", titles[-1], "the closing core act must stay last")
        act = p.stdout.split("## Act %d — " % CORE_ACTS)[1].split("## Act %d — " % (CORE_ACTS + 1))[0]
        self.assertIn("gt-watch skill", act)
        self.assertIn("<module:watch>/gt_watch.py", act)
        self.assertNotIn("<core>/gt_watch.py", act)
        for key in ("narration:", "do:", "point:"):
            self.assertRegex(act, r"(?m)^%s " % key)

    def test_watch_absent_or_disabled_leaves_no_act(self):
        _, titles = self.tour()
        self.assertNotIn("Watch upstream", " ".join(titles))
        self.install_watch(enabled=False)
        _, titles = self.tour()
        self.assertEqual(len(titles), CORE_ACTS)
        self.assertNotIn("Watch upstream", " ".join(titles))

    def test_watch_and_wiki_acts_are_ordered_by_module_name(self):
        self.install_wiki()
        self.install_watch()
        _, titles = self.tour()
        self.assertEqual(titles[CORE_ACTS - 1:-1], ["Watch upstream (module: watch)",
                                                    "The wiki (module: wiki)"])

    def test_module_scripts_resolves_the_installed_watch_or_skips_with_a_reason(self):
        p = self.sh(self.script, "module-scripts", "watch")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("not installed", p.stderr)
        self.assertEqual(p.stdout, "")
        dest = self.install_watch()
        p = self.sh(self.script, "module-scripts", "watch")
        self.assertOk(p)
        self.assertEqual(Path(p.stdout.strip()), dest / "scripts")
        self.assertTrue((Path(p.stdout.strip()) / "gt_watch.py").is_file())

    def test_newest_cache_version_is_the_one_read(self):
        self.fake_module("zeta", "demo/act.md", "Old act", version="1.2.0")
        self.fake_module("zeta", "demo/act.md", "New act", version="1.10.0")
        _, titles = self.tour()
        self.assertEqual(titles[-2], "New act (module: zeta)")

    def test_bad_demo_paths_are_skipped_with_a_note(self):
        esc = self.fake_module("escape", "../act.md")
        (esc.parent / "act.md").write_text(ACT.format(title="Escaped"))
        self.fake_module("missing", "demo/act.md")
        self.fake_module("absolute", "/etc/hosts")
        bad = self.fake_module("shapeless", "demo/act.md")
        (bad / "demo").mkdir()
        (bad / "demo" / "act.md").write_text("just prose, no heading\n")
        self.fake_module("good", "demo/act.md", "Good act")
        p, titles = self.tour()
        self.assertEqual(titles[CORE_ACTS - 1:-1], ["Good act (module: good)"])
        notes = [l for l in p.stderr.splitlines() if l.startswith("note: ")]
        self.assertEqual(len(notes), 4, p.stderr)
        for name in ("escape", "missing", "absolute", "shapeless"):
            self.assertTrue(any("module %s:" % name in l and "skipped" in l for l in notes), name)
        self.assertNotIn("Escaped", p.stdout)

    def test_modules_are_ordered_by_module_name(self):
        self.fake_module("zulu", "demo/act.md", "Zulu act")
        self.fake_module("alpha", "demo/act.md", "Alpha act")
        self.install_wiki()
        _, titles = self.tour()
        self.assertEqual(titles[CORE_ACTS - 1:-1], ["Alpha act (module: alpha)", "The wiki (module: wiki)",
                                                    "Zulu act (module: zulu)"])

    def test_unreadable_settings_is_core_only_not_fatal(self):
        self.install_wiki()
        (self.home / ".claude" / "settings.json").write_text("{nope")
        self.assertEqual(len(self.tour()[1]), CORE_ACTS)

    def test_core_tour_carries_no_module_special_case(self):
        tour = (DEMO_MODULE / "templates" / "demo-pizzabot" / "tour.md").read_text()
        self.assertNotIn("module is not installed", tour)
        self.assertNotIn("gt-wiki", tour)
        self.assertNotIn("gt-watch", tour)
        self.assertNotIn("gt_watch.py", tour)


if __name__ == "__main__":
    unittest.main()
