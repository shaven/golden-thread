"""module.json (schema 1) -- one format, two readers that must agree.

dev/plugins.py validates modules for the release gate; the release's own
scripts/gt_components.py validates them on a user's machine (it ships, so it cannot
import dev/). Two copies of a rule are two rules unless something forces them to agree,
and this file is that something.

Contracts pinned here:
  * both CLIs give the same exit code on every fixture (0 valid, 1 invalid, 2 not a
    module), and both validate_module() functions give the SAME reasons;
  * the optional `demo` key must name an existing file inside the module;
  * refused: an unknown key, a listed skill/script/template that does not exist, a hook
    naming a Core-rule enforcement hook, a version disagreeing with the directory or
    plugin.json, a malformed requires_gt;
  * requires_gt: >=,<,<=,>,== clauses, comma = AND, compared numerically per field;
  * the two validator blocks are textually identical;
  * every module the repo ships is valid and admits the gt release under test.
"""
import json
import re
import unittest

from _harness import Sandbox, REPO, SCRIPTS, GT, load_module

PLUGINS = REPO / "dev" / "plugins.py"
COMPONENTS = SCRIPTS / "gt_components.py"


def write_module(root, d="golden-thread-zed", version="1.0.0", plugin="gt-zed",
                 plugin_version=None, **fields):
    vd = root / d / version
    (vd / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (vd / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": plugin, "version": plugin_version or version}))
    for sub in ("skills/gt-zed", "scripts", "templates/zed-tree", "hooks"):
        (vd / sub).mkdir(parents=True, exist_ok=True)
    (vd / "skills" / "gt-zed" / "SKILL.md").write_text("---\nname: gt-zed\n---\n")
    (vd / "scripts" / "gt_zed.py").write_text("print('zed')\n")
    (vd / "scripts" / "zed_report.py").write_text("print('report')\n")
    (vd / "hooks" / "zed_guard.sh").write_text("#!/bin/sh\nexit 0\n")
    data = {"schema": 1, "name": "zed", "plugin": plugin, "version": version,
            "requires_gt": ">=0.14.0,<0.15.0", "summary": "a fixture module",
            "default": "on", "skills": ["gt-zed"], "scripts": ["gt_zed.py"],
            "templates": ["zed-tree"],
            "hooks": [{"event": "SessionStart", "script": "zed_report.py",
                       "args": ["--hook"], "kind": "reporter"}],
            "hookdir_scripts": ["zed_report.py"],
            "settings": [{"key": "zed_mode", "default": "calm", "values": ["calm", "loud"],
                          "summary": "how loud"}],
            "requires_modules": [{"name": "watch", "soft": True}],
            "replaces_core": ["skills/gt-zed", "scripts/gt_zed.py"]}
    for k, v in fields.items():
        if v is DROP:
            data.pop(k, None)
        else:
            data[k] = v
    (vd / "module.json").write_text(json.dumps(data, indent=1))
    return vd


DROP = object()


class ModuleFormat(Sandbox):
    def setUp(self):
        super().setUp()
        self.pl = load_module(PLUGINS, "plugins_for_format")
        self.gc = load_module(COMPONENTS, "gt_components_for_format")

    def fixtures(self):
        """name -> (version dir, expected exit)."""
        r = self.tmp / "fx"
        fx = {}
        fx["valid"] = (write_module(r / "valid"), 0)
        fx["minimal"] = (write_module(r / "minimal", skills=DROP, scripts=DROP,
                                      templates=DROP, hooks=DROP, hookdir_scripts=DROP,
                                      settings=DROP, requires_modules=DROP,
                                      replaces_core=DROP), 0)
        fx["unknown key"] = (write_module(r / "unknown", colour="blue"), 1)
        fx["missing skill"] = (write_module(r / "noskill", skills=["gt-zed", "gt-ghost"]), 1)
        fx["missing script"] = (write_module(r / "noscript", scripts=["nope.py"]), 1)
        fx["missing template"] = (write_module(r / "notpl", templates=["ghost"]), 1)
        fx["core enforcement hook"] = (write_module(r / "enforce", hooks=[
            {"event": "PreToolUse", "script": "guard_vault_writes.sh", "kind": "guard"}]), 1)
        fx["hook script absent"] = (write_module(r / "nohook", hooks=[
            {"event": "Stop", "script": "ghost.sh", "kind": "guard"}]), 1)
        fx["bad hook kind"] = (write_module(r / "kind", hooks=[
            {"event": "Stop", "script": "zed_guard.sh", "kind": "enforcer"}]), 1)
        # version field differs from its directory name and from plugin.json
        vd = write_module(r / "vmis")
        data = json.loads((vd / "module.json").read_text())
        data["version"] = "1.0.1"
        (vd / "module.json").write_text(json.dumps(data))
        fx["version mismatch"] = (vd, 1)
        fx["plugin.json version mismatch"] = (write_module(r / "pjv", plugin_version="0.9.0"), 1)
        fx["plugin name mismatch"] = (self._plugin_mismatch(r / "pname"), 1)
        for i, bad in enumerate((">=0.x", "0.14.0", ">=0.14.0;<0.15.0", "", "~>1.0.0",
                                 ">= x.y.z", 14)):
            fx["bad requires_gt %r" % (bad,)] = (write_module(r / ("req%d" % i),
                                                              requires_gt=bad), 1)
        fx["bad default"] = (write_module(r / "default", default="yes"), 1)
        fx["bad name"] = (write_module(r / "name", name="Zed_1"), 1)
        fx["gt name"] = (write_module(r / "gtname", name="gt"), 1)
        fx["missing required"] = (write_module(r / "noreq", summary=DROP), 1)
        fx["setting default not in values"] = (write_module(r / "setdef", settings=[
            {"key": "zed_mode", "default": "mute", "values": ["calm"], "summary": "x"}]), 1)
        # optional `detail` (0.15.0): the long explanation a setting keeps when it moves
        fx["setting with detail"] = (write_module(r / "setdetail", settings=[
            {"key": "zed_mode", "default": "calm", "values": ["calm"], "summary": "x",
             "detail": "calm  quiet\n\nWhy it exists."}]), 0)
        fx["setting detail not a string"] = (write_module(r / "setdetailbad", settings=[
            {"key": "zed_mode", "default": "calm", "values": ["calm"], "summary": "x",
             "detail": ["not", "text"]}]), 1)
        fx["path escape"] = (write_module(r / "escape", scripts=["../x.py"],
                                          replaces_core=["../../etc"]), 1)
        fx["demo act"] = (self._with_demo(r / "demo", "demo/act.md"), 0)
        fx["demo missing"] = (write_module(r / "demomiss", demo="demo/act.md"), 1)
        fx["demo escapes"] = (self._with_demo(r / "demoesc", "../act.md"), 1)
        fx["demo absolute"] = (self._with_demo(r / "demoabs", "/etc/hosts"), 1)
        fx["schema 2"] = (write_module(r / "schema", schema=2), 1)
        notjson = write_module(r / "notjson")
        (notjson / "module.json").write_text("{nope")
        fx["not json"] = (notjson, 1)
        core = r / "core" / "golden-thread" / "0.14.0"
        (core / ".claude-plugin").mkdir(parents=True)
        (core / ".claude-plugin" / "plugin.json").write_text('{"name":"gt","version":"0.14.0"}')
        fx["not a module"] = (core, 2)
        return fx

    def _with_demo(self, root, rel):
        vd = write_module(root, demo=rel)
        (vd / "demo").mkdir()
        (vd / "demo" / "act.md").write_text("# act\n")
        (vd.parent / "act.md").write_text("# outside the module\n")
        return vd

    def _plugin_mismatch(self, root):
        vd = write_module(root)
        (vd / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "gt-other", "version": "1.0.0"}))
        return vd

    def test_both_readers_give_identical_verdicts(self):
        for label, (vd, want) in self.fixtures().items():
            with self.subTest(label):
                a = self.py(PLUGINS, "module-check", vd)
                b = self.py(COMPONENTS, "module-check", vd)
                self.assertEqual(a.returncode, want, "plugins.py on %s:\n%s" % (label, a.stdout))
                self.assertEqual(b.returncode, want,
                                 "gt_components.py on %s:\n%s" % (label, b.stdout))
                self.assertEqual(a.stdout, b.stdout, "the two CLIs disagree on %s" % label)
                ra, rb = self.pl.validate_module(vd), self.gc.validate_module(vd)
                self.assertEqual(ra, rb, "the two validators disagree on %s" % label)
                if want == 1:
                    self.assertTrue(ra[1], label)

    def test_the_named_refusals_say_why(self):
        fx = self.fixtures()
        why = {"unknown key": "unknown key 'colour'",
               "missing skill": "'gt-ghost' does not exist",
               "core enforcement hook": "Core-rule enforcement hook",
               "version mismatch": "differs from the directory name",
               "bad requires_gt '0.14.0'": "requires_gt clause",
               "demo missing": "does not exist in the module",
               "demo escapes": "not a relative path inside the module",
               "setting detail not a string": "settings[0] detail must be a string"}
        for label, text in why.items():
            p = self.py(PLUGINS, "module-check", fx[label][0])
            self.assertIn(text, p.stdout, label)

    def test_every_core_enforcement_hook_is_refused(self):
        hooks = load_module(COMPONENTS, "gc_regs").HOOK_REGISTRATIONS
        enforcement = {r["script"] for r in hooks if r["owner"].startswith("vault_init.py")}
        enforcement.add("guard_protected_paths.sh")
        self.assertTrue(enforcement <= set(self.gc.CORE_ENFORCEMENT_HOOKS),
                        "a Core-rule hook is missing from CORE_ENFORCEMENT_HOOKS")
        for name in self.gc.CORE_ENFORCEMENT_HOOKS:
            vd = write_module(self.tmp / name, hooks=[
                {"event": "PreToolUse", "script": name, "kind": "guard"}])
            (vd / "hooks" / name).write_text("#!/bin/sh\n")
            self.assertEqual(self.py(PLUGINS, "module-check", vd).returncode, 1, name)
            self.assertEqual(self.py(COMPONENTS, "module-check", vd).returncode, 1, name)

    def test_requires_gt_semantics(self):
        cases = [(">=0.14.0,<0.15.0", "0.14.0", True), (">=0.14.0,<0.15.0", "0.14.9", True),
                 (">=0.14.0,<0.15.0", "0.15.0", False), (">=0.14.0,<0.15.0", "0.13.99", False),
                 (">=0.9.0", "0.10.0", True),          # numeric, not string, comparison
                 ("<0.10.0", "0.9.13", True), ("==0.14.0", "0.14.0", True),
                 ("==0.14", "0.14.0", True), (">0.14.0", "0.14.0", False),
                 ("<=0.14.0", "0.14.0", True), (" >= 1.0.0 , < 2.0.0 ", "1.5.0", True)]
        for spec, ver, want in cases:
            with self.subTest(spec=spec, ver=ver):
                self.assertEqual(self.pl.requires_gt_admits(spec, ver), want)
                self.assertEqual(self.gc.requires_gt_admits(spec, ver), want)
        for bad in ("", ">=", "=>0.1.0", "0.14.0", ">=0.14.0,", "!=0.1.0"):
            for mod in (self.pl, self.gc):
                with self.assertRaises(ValueError, msg=bad):
                    mod.requires_gt_admits(bad, "0.14.0")

    def test_gt_flag_requires_the_release_to_be_admitted(self):
        vd = write_module(self.tmp / "adm")
        for tool in (PLUGINS, COMPONENTS):
            self.assertEqual(self.py(tool, "module-check", vd, "--gt", "0.14.2").returncode, 0)
            p = self.py(tool, "module-check", vd, "--gt", "0.15.0")
            self.assertEqual(p.returncode, 1)
            self.assertIn("does not admit gt 0.15.0", p.stdout)

    def test_the_validator_blocks_are_identical_text(self):
        def block(path):
            m = re.search(r"# BEGIN module validator.*?# END module validator",
                          path.read_text(encoding="utf-8"), re.S)
            self.assertIsNotNone(m, "%s carries no marked validator block" % path)
            return m.group(0)
        self.assertEqual(block(PLUGINS), block(COMPONENTS),
                         "the validator in dev/plugins.py and scripts/gt_components.py "
                         "have drifted apart; copy one over the other")

    def test_modules_listing_agrees(self):
        root = self.tmp / "root"
        write_module(root)
        write_module(root, d="golden-thread-yak", plugin="gt-yak", name="yak", default="off",
                     version="2.0.0")
        write_module(root, d="golden-thread-yak", plugin="gt-yak", name="yak", version="1.9.0")
        core = root / "golden-thread" / "0.14.0" / ".claude-plugin"
        core.mkdir(parents=True)
        (core / "plugin.json").write_text('{"name":"gt","version":"0.14.0"}')
        a = self.py(PLUGINS, "modules", root)
        b = self.py(COMPONENTS, "modules", root)
        self.assertOk(a)
        self.assertOk(b)
        self.assertEqual(a.stdout.splitlines(), ["yak gt-yak 2.0.0 off", "zed gt-zed 1.0.0 on"])
        self.assertEqual(a.stdout, b.stdout)

    def test_every_shipped_module_is_valid_and_admits_this_gt(self):
        for line in self.py(PLUGINS, "list").stdout.splitlines():
            d, v, _ = line.split()
            vd = REPO / d / v
            if not (vd / "module.json").is_file():
                continue
            p = self.py(PLUGINS, "module-check", vd, "--gt", GT.name)
            self.assertEqual(p.returncode, 0, p.stdout)


if __name__ == "__main__":
    unittest.main()
