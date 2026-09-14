"""gt-wiki as a gt 0.14.0 module: its module.json is valid and agrees with the tree.

  * required keys present, no unknown keys, schema 1, name/plugin/version agree with
    .claude-plugin/plugin.json and the version directory name;
  * every listed skill, script and template exists, and nothing shipped is unlisted;
  * no hooks (the wiki module owns none), replaces_core empty, requires_gt is a clause list;
  * dev/plugins.py module-check passes on it when that command exists.
"""
import json
import re
import subprocess
import sys
import unittest

from _harness import Sandbox, WIKI, REPO

KEYS = {"schema", "name", "plugin", "version", "requires_gt", "summary", "default", "demo",
        "skills", "scripts", "templates", "hooks", "hookdir_scripts", "settings",
        "requires_modules", "replaces_core"}
REQUIRED = {"schema", "name", "plugin", "version", "requires_gt", "summary", "default",
            "skills", "scripts", "templates", "hooks", "replaces_core"}


class WikiModuleJson(Sandbox):
    def setUp(self):
        super().setUp()
        self.path = WIKI / "module.json"
        if not self.path.is_file():
            self.skipTest("%s predates modules (no module.json)" % WIKI.name)
        self.mod = json.loads(self.path.read_text(encoding="utf-8"))
        self.plugin = json.loads((WIKI / ".claude-plugin" / "plugin.json").read_text())

    def test_keys(self):
        keys = set(self.mod)
        self.assertEqual(REQUIRED - keys, set(), "module.json missing required keys")
        self.assertEqual(keys - KEYS, set(), "module.json has unknown keys")
        self.assertEqual(self.mod["schema"], 1)
        self.assertRegex(self.mod["name"], r"^[a-z][a-z0-9-]*$")
        self.assertIn(self.mod["default"], ("on", "off"))
        self.assertTrue(self.mod["summary"].strip() and "\n" not in self.mod["summary"])

    def test_identity_agrees_with_plugin_json_and_dir(self):
        self.assertEqual(self.mod["name"], "wiki")
        self.assertEqual(self.mod["plugin"], self.plugin["name"])
        self.assertEqual(self.mod["version"], self.plugin["version"])
        self.assertEqual(self.mod["version"], WIKI.name)

    def test_requires_gt_is_a_clause_list(self):
        for clause in self.mod["requires_gt"].split(","):
            self.assertRegex(clause.strip(), r"^(>=|<=|==|<|>)\d+\.\d+\.\d+$")

    def test_listed_entries_exist_and_cover_the_tree(self):
        for group in ("skills", "scripts", "templates"):
            listed = self.mod[group]
            for entry in listed:
                self.assertTrue((WIKI / group / entry).exists(),
                                "module.json lists %s/%s, which does not exist" % (group, entry))
            shipped = sorted(p.name for p in (WIKI / group).iterdir()
                             if p.name != "__pycache__" and not p.name.startswith("."))
            self.assertEqual(sorted(listed), shipped,
                             "module.json %s disagrees with %s/" % (group, group))

    def test_no_hooks_and_replaces_nothing(self):
        self.assertEqual(self.mod["hooks"], [])
        self.assertEqual(self.mod.get("hookdir_scripts", []), [])
        self.assertEqual(self.mod["replaces_core"], [])

    def test_dev_module_check_when_available(self):
        tool = REPO / "dev" / "plugins.py"
        usage = subprocess.run([sys.executable, str(tool)], capture_output=True, text=True)
        if "module-check" not in (usage.stdout + usage.stderr):
            self.skipTest("dev/plugins.py has no module-check yet")
        p = subprocess.run([sys.executable, str(tool), "module-check", str(WIKI)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
