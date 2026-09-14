"""dev/plugins.py — the one rule for which plugins ship, and the manifest gate's decision.

Contracts pinned here:
  * `list` prints "<dir> <version> <name>" for every top-level dir holding an
    N.N.N/.claude-plugin/plugin.json, newest per dir numerically, sorted by dir;
    non-semver version dirs and version dirs without plugin.json are ignored;
  * the shipped repo lists gt and gt-wiki, and every one carries a MANIFEST.json
    that matches its tree;
  * `manifest-check` exits 2 for a plugin with no MANIFEST.json (the gate fails),
    1 for a stale one, 0 once `manifest` has written it;
  * `modules` prints "<name> <plugin> <version> <default>" for every plugin whose NEWEST
    release carries module.json (gt core has none), sorted by name, exit 0 even if none;
  * `module-check` exits 0 valid, 1 invalid naming the reasons, 2 not a module; with
    `--gt V` it also requires requires_gt to admit V. Format cases live in
    test_module_format.py, which holds this reader to the release's copy.
"""
import json
import shutil
import unittest

from _harness import Sandbox, REPO, GT

TOOL = REPO / "dev" / "plugins.py"


class PluginsTest(Sandbox):
    def plugin(self, root, d, v, name=None, meta=True):
        vd = root / d / v
        (vd / "scripts").mkdir(parents=True)
        (vd / "scripts" / "x.py").write_text("print(%r)\n" % v)
        if meta:
            (vd / ".claude-plugin").mkdir()
            (vd / ".claude-plugin" / "plugin.json").write_text(
                json.dumps({"name": name or d, "version": v}))
        return vd

    def fake_root(self):
        root = self.tmp / "plugin"
        core = self.plugin(root, "golden-thread", "0.9.9", "gt")
        self.plugin(root, "golden-thread", "0.10.0", "gt")
        shutil.copy2(GT / "scripts" / "gt_components.py", core / "scripts" / "gt_components.py")
        shutil.copy2(GT / "scripts" / "gt_components.py",
                     root / "golden-thread" / "0.10.0" / "scripts" / "gt_components.py")
        self.plugin(root, "zeta-module", "1.2.0", "zeta")
        self.plugin(root, "zeta-module", "1.10.0", "zeta")
        self.plugin(root, "zeta-module", "2.0.0", meta=False)          # no plugin.json
        self.plugin(root, "zeta-module", "3.0", "zeta")                 # not semver
        self.plugin(root, "notes", "latest", "notes")                   # no semver dir at all
        (root / "dev").mkdir()                                           # not a plugin
        self.plugin(root, "archive", "0.1.0", meta=False)                # no plugin.json anywhere
        return root

    def test_list_discovers_newest_per_plugin(self):
        p = self.py(TOOL, "list", self.fake_root())
        self.assertOk(p)
        self.assertEqual(p.stdout.splitlines(),
                         ["golden-thread 0.10.0 gt", "zeta-module 1.10.0 zeta"])

    def test_newest_and_empty_root(self):
        root = self.fake_root()
        self.assertEqual(self.py(TOOL, "newest", root / "zeta-module").stdout.strip(), "1.10.0")
        self.assertEqual(self.py(TOOL, "newest", root / "archive").returncode, 1)
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(self.py(TOOL, "list", empty).returncode, 1)

    def test_the_shipped_repo_lists_gt_and_gt_wiki(self):
        p = self.py(TOOL, "list")
        self.assertOk(p)
        names = [line.split()[2] for line in p.stdout.splitlines()]
        self.assertIn("gt", names)
        self.assertIn("gt-wiki", names)

    def test_every_shipped_non_core_plugin_has_a_matching_manifest(self):
        for line in self.py(TOOL, "list").stdout.splitlines():
            d, v, _ = line.split()
            if d == "golden-thread":
                continue        # gt's manifest is gt_components.py's, tested there
            p = self.py(TOOL, "manifest-check", REPO / d / v)
            self.assertEqual(p.returncode, 0, p.stdout)

    def test_a_plugin_without_manifest_fails_the_manifest_step(self):
        vd = self.fake_root() / "zeta-module" / "1.10.0"
        p = self.py(TOOL, "manifest-check", vd)
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("has no MANIFEST.json", p.stdout)

    def test_written_manifest_passes_then_goes_stale(self):
        vd = self.fake_root() / "zeta-module" / "1.10.0"
        self.assertOk(self.py(TOOL, "manifest", vd))
        man = json.loads((vd / "MANIFEST.json").read_text())
        self.assertEqual(set(man["files"]), {"scripts/x.py"})
        self.assertNotIn("hooks", man, "the core's hook registrations are not this plugin's")
        self.assertEqual(self.py(TOOL, "manifest-check", vd).returncode, 0)
        (vd / "scripts" / "x.py").write_text("changed\n")
        p = self.py(TOOL, "manifest-check", vd)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("stale", p.stdout)

    def test_no_core_release_cannot_check(self):
        root = self.tmp / "lonely"
        vd = self.plugin(root, "zeta-module", "1.0.0", "zeta")
        (vd / "MANIFEST.json").write_text('{"files": {}}')
        self.assertEqual(self.py(TOOL, "manifest-check", vd).returncode, 3)

    def module(self, root, d, v, name, default="on", requires=">=0.10.0"):
        vd = root / d / v
        if not vd.exists():
            self.plugin(root, d, v, "gt-" + name)
        plugin = json.loads((vd / ".claude-plugin" / "plugin.json").read_text())["name"]
        (vd / "module.json").write_text(json.dumps({
            "schema": 1, "name": name, "plugin": plugin, "version": v,
            "requires_gt": requires, "summary": "fixture", "default": default}))
        return vd

    def test_modules_lists_only_module_releases_newest_first_sorted_by_name(self):
        root = self.fake_root()
        self.module(root, "zeta-module", "1.10.0", "zeta", default="off")
        self.module(root, "zeta-module", "1.2.0", "zeta")         # older: ignored
        self.module(root, "alpha", "0.1.0", "alpha")
        p = self.py(TOOL, "modules", root)
        self.assertOk(p)
        self.assertEqual(p.stdout.splitlines(),
                         ["alpha gt-alpha 0.1.0 on", "zeta zeta 1.10.0 off"])
        empty = self.tmp / "nomods"
        self.plugin(empty, "golden-thread", "1.0.0", "gt")
        p = self.py(TOOL, "modules", empty)
        self.assertOk(p)
        self.assertEqual(p.stdout, "")

    def test_a_module_whose_newest_release_lacks_module_json_is_not_a_module(self):
        root = self.fake_root()
        self.module(root, "zeta-module", "1.2.0", "zeta")
        self.assertEqual(self.py(TOOL, "modules", root).stdout, "")

    def test_module_check_exit_codes(self):
        root = self.fake_root()
        vd = self.module(root, "zeta-module", "1.10.0", "zeta", requires=">=0.10.0,<0.11.0")
        self.assertEqual(self.py(TOOL, "module-check", vd).returncode, 0)
        self.assertEqual(self.py(TOOL, "module-check", vd, "--gt", "0.10.0").returncode, 0)
        p = self.py(TOOL, "module-check", vd, "--gt", "0.11.0")
        self.assertEqual(p.returncode, 1)
        self.assertIn("does not admit", p.stdout)
        p = self.py(TOOL, "module-check", root / "golden-thread" / "0.10.0")
        self.assertEqual(p.returncode, 2)
        self.assertIn("not a module", p.stdout)
        data = json.loads((vd / "module.json").read_text())
        data["version"] = "9.9.9"
        (vd / "module.json").write_text(json.dumps(data))
        p = self.py(TOOL, "module-check", vd)
        self.assertEqual(p.returncode, 1)
        self.assertIn("differs from the directory name", p.stdout)
        self.assertEqual(self.py(TOOL, "module-check").returncode, 2)
        self.assertEqual(self.py(TOOL, "module-check", vd, "--gt").returncode, 2)

    def test_usage_error_is_exit_2(self):
        self.assertEqual(self.py(TOOL).returncode, 2)
        self.assertEqual(self.py(TOOL, "bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
