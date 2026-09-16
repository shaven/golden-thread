"""gt-farm as a gt 0.15.0 module: decoupled from any one vault, and off by default.

Contracts pinned here (design finding F8, owner decision 2026-09-14):
  * module.json is valid by the shipped validator and dev/plugins.py, agrees with
    plugin.json and its directory, lists exactly what it ships, owns no hooks, and
    replaces the core's skills/gt-farm, which the core no longer ships;
  * default off: a fresh install leaves no gt-farm plugin, skill or enabled entry;
    --with farm installs and enables it and records the choice;
  * the skill names no vault-local project (external-ai-tools), no vendor, and no
    file under scripts/ or templates/ that the release does not ship; pbcopy appears
    only with a portable fallback beside it; the packet dir is configurable;
  * skill_lint finds no trigger collision across the release's skills;
  * the module passes the release scrub when the scrub terms can be found.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

from _harness import Sandbox, REPO, GT, WIKI, SCRIPTS, latest_version_dir, gt_requires_range

FARM = latest_version_dir(REPO / "golden-thread-farm")
SKILL = FARM / "skills" / "gt-farm" / "SKILL.md"
INSTALL = REPO / "install.sh"
MARKET = "golden-thread-plugin"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
# Resolved from the developer's real environment BEFORE any sandbox strips HOME and GT_*.
_REAL_ENV = dict(os.environ)


def _components():
    from _harness import load_module
    return load_module(SCRIPTS / "gt_components.py", "gt_components_for_farm")


class FarmModuleJson(unittest.TestCase):
    def setUp(self):
        self.mod = json.loads((FARM / "module.json").read_text(encoding="utf-8"))
        self.plugin = json.loads((FARM / ".claude-plugin" / "plugin.json").read_text())

    def test_validates_with_the_shipped_validator(self):
        data, reasons = _components().validate_module(FARM)
        self.assertIsNotNone(data, "no module.json")
        self.assertEqual(reasons, [])

    def test_dev_module_check(self):
        p = subprocess.run([sys.executable, str(REPO / "dev" / "plugins.py"), "module-check",
                            str(FARM)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_manifest_matches_the_tree(self):
        p = subprocess.run([sys.executable, str(REPO / "dev" / "plugins.py"), "manifest-check",
                            str(FARM)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_contract(self):
        m = self.mod
        self.assertEqual((m["schema"], m["name"], m["plugin"], m["version"]),
                         (1, "farm", "gt-farm", FARM.name))
        # the module tracks the gt release; a module left behind at an older
        # version is skipped at install, silently, on every machine
        self.assertEqual(FARM.name, GT.name)
        self.assertEqual(m["version"], FARM.name)
        self.assertEqual(self.plugin["name"], "gt-farm")
        self.assertEqual(self.plugin["version"], m["version"])
        self.assertEqual(m["requires_gt"], gt_requires_range(GT.name))
        self.assertEqual(m["default"], "off")
        self.assertEqual(m["skills"], ["gt-farm"])
        self.assertEqual(m.get("hooks", []), [])
        self.assertEqual(m.get("hookdir_scripts", []), [])
        self.assertEqual(m.get("settings", []), [])
        self.assertEqual(m["replaces_core"], ["skills/gt-farm"])
        core_pj = json.loads((GT / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(self.plugin["author"], core_pj["author"])

    def test_lists_exactly_what_it_ships(self):
        for group in ("skills", "scripts", "templates"):
            d = FARM / group
            shipped = sorted(p.name for p in d.iterdir()
                             if p.name != "__pycache__" and not p.name.startswith(".")) \
                if d.is_dir() else []
            self.assertEqual(sorted(self.mod.get(group, [])), shipped, group)

    def test_core_no_longer_ships_the_skill(self):
        self.assertFalse((GT / "skills" / "gt-farm").exists(),
                         "gt %s still ships skills/gt-farm, which the farm module replaces"
                         % GT.name)


class FarmSkillIsPortable(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")

    def test_frontmatter_names_the_skill(self):
        self.assertRegex(self.text, r"\A---\nname: gt-farm\ndescription: ")

    def test_no_vault_local_project(self):
        self.assertNotIn("external-ai-tools", self.text)
        literal = [p for p in re.findall(r"Projects/([^/`\s<>]+)/", self.text)]
        self.assertEqual(literal, [], "names a specific vault project: %s" % literal)

    def test_no_vendor_names(self):
        vendors = r"\b(openai|chatgpt|gpt-\d|gemini|google|perplexity|anthropic|mistral|" \
                  r"cohere|exa|firecrawl|tavily|grok|xai|deepseek|llama|copilot)\b"
        hits = sorted(set(m.group(0) for m in re.finditer(vendors, self.text, re.I)))
        self.assertEqual(hits, [], "vendor names in the skill")

    def test_references_only_shipped_files(self):
        # Any scripts/ or templates/ path the skill names must ship with gt or this module.
        for ref in re.findall(r"\b((?:scripts|templates|skills)/[\w./-]+)", self.text):
            ref = ref.rstrip(".")
            self.assertTrue((GT / ref).exists() or (FARM / ref).exists(),
                            "the skill references %s, which is not shipped" % ref)

    def test_pbcopy_has_a_portable_fallback(self):
        self.assertIn("pbcopy", self.text)
        for tool in ("wl-copy", "xclip", "clip.exe"):
            self.assertIn(tool, self.text)
        self.assertIn("command -v", self.text)

    def test_packet_dir_is_configurable_with_the_documented_default(self):
        self.assertIn("farm_packet_dir", self.text)
        self.assertIn("Projects/<current project>/packets/", self.text)

    def test_gates_are_inlined(self):
        for gate in ("Stateless", "Self-contained", "Checkable", "Releasable"):
            self.assertIn("**%s**" % gate, self.text)

    def test_skill_lint_finds_no_collision_across_the_release(self):
        roots = [GT, FARM, WIKI]
        try:
            roots.append(latest_version_dir(REPO / "golden-thread-demo"))
        except (RuntimeError, OSError):
            pass
        p = subprocess.run([sys.executable, str(SCRIPTS / "skill_lint.py"), *map(str, roots)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        advertised = p.stdout.split("No advertised triggers", 1)
        self.assertFalse(len(advertised) > 1 and re.search(r"^\s+gt-farm$", advertised[1],
                                                           re.M),
                         "gt-farm advertises no triggers, so rule 2 cannot check it")

    def test_scrub_clean(self):
        tool = REPO / "dev" / "scrub_check.py"
        env = dict(_REAL_ENV)
        p = subprocess.run([sys.executable, str(tool), "--no-pdf", str(FARM)],
                           capture_output=True, text=True, env=env)
        if p.returncode == 2:
            self.skipTest("scrub terms not available here: " + (p.stdout + p.stderr).strip())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class FarmInstall(Sandbox):
    """Off by default in a fresh home; --with farm brings it in."""

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "src" / MARKET
        self.repo.mkdir(parents=True)
        shutil.copy2(INSTALL, self.repo / "install.sh")
        shutil.copytree(GT, self.repo / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, self.repo / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        shutil.copytree(FARM, self.repo / "golden-thread-farm" / FARM.name, ignore=IGNORE)
        gt_src = self.repo / "golden-thread" / GT.name
        # As test_install_modules does: the copy describes itself, so concurrent edits to
        # the core in a working tree do not turn this into a manifest test.
        self.assertOk(self.py(gt_src / "scripts" / "gt_components.py", "manifest", gt_src))
        self.claude = self.home / ".claude"

    def install(self, *args):
        p = self.sh(self.repo / "install.sh", *args, "--no-vault", timeout=300)
        self.assertOk(p, "install.sh failed")
        return p

    def jload(self, rel, default=None):
        p = self.claude / rel
        return json.loads(p.read_text()) if p.exists() else default

    def farm_skills_installed(self):
        return list((self.claude / "plugins").rglob("skills/gt-farm/SKILL.md")) \
            if (self.claude / "plugins").exists() else []

    def test_fresh_install_leaves_farm_off(self):
        p = self.install()
        key = "gt-farm@%s" % MARKET
        self.assertEqual(self.farm_skills_installed(), [], "gt-farm skill installed while off")
        self.assertFalse((self.claude / "plugins" / "cache" / MARKET / "gt-farm").exists())
        self.assertNotIn(key, (self.jload("plugins/installed_plugins.json", {}) or {})
                         .get("plugins", {}))
        self.assertNotIn(key, (self.jload("settings.json", {}) or {}).get("enabledPlugins", {}))
        self.assertNotIn("/gt-farm:gt-farm", p.stdout)
        self.assertNotIn("/gt:gt-farm", p.stdout)

    def test_with_farm_installs_and_enables_it(self):
        p = self.install("--with", "farm")
        key = "gt-farm@%s" % MARKET
        cache = self.claude / "plugins" / "cache" / MARKET / "gt-farm"
        self.assertTrue(list(cache.glob("*/skills/gt-farm/SKILL.md")), "not in the cache")
        self.assertIn(key, self.jload("plugins/installed_plugins.json")["plugins"])
        self.assertIs(self.jload("settings.json")["enabledPlugins"].get(key), True)
        self.assertIn("/gt-farm:gt-farm", p.stdout)
        choices = (self.jload("golden-thread/install-choices.json", {}) or {}).get("choices", {})
        self.assertEqual(choices.get("farm"), "on")
        gt_cache = self.claude / "plugins" / "cache" / MARKET / "gt"
        self.assertEqual(list(gt_cache.glob("*/skills/gt-farm")), [],
                         "the core still carries the skill the module replaces")


if __name__ == "__main__":
    unittest.main()
