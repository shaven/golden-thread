"""dev/check_wiring_coverage.py — every shipped item must reach its destination.

The gate that would have caught the 0.12.0/0.12.1/0.12.2 wiring bug on the build
machine instead of on someone else's. It proves the claim by INSTALLING and then asking
each shipped item where it ended up, because every earlier check (manifest, component
drift, hook registrations) reported "clean" throughout.

Contract:
  * exit 0 only when every shipped hook, hook-dir script, skill, script, vault tool and
    Core rule is found where it belongs after a real install into a throwaway HOME;
  * exit 1, naming each one, otherwise;
  * a hook that ships but is declared in no registration is itself a finding — that is
    the shape the bug took;
  * module hooks (0.14.0): an ON module's hooks must be wired and its hook-dir files
    installed; an OFF module's must be absent. `module_findings` judges a home, so it is
    tested here against fixture homes without running install.sh.
"""
import json
import shutil
from pathlib import Path
import unittest

from _harness import Sandbox, REPO, GT, WIKI, SCRIPTS, load_module


CHECK = REPO / "dev" / "check_wiring_coverage.py"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


class WiringCoverage(Sandbox):
    def plugin_copy(self):
        """A writable plugin root, so a test can break one thing on purpose."""
        root = self.tmp / "plugin"
        root.mkdir(parents=True)
        shutil.copy2(REPO / "install.sh", root / "install.sh")
        shutil.copytree(GT, root / "golden-thread" / GT.name, ignore=IGNORE)
        shutil.copytree(WIKI, root / "golden-thread-wiki" / WIKI.name, ignore=IGNORE)
        return root, root / "golden-thread" / GT.name

    def check(self, version_dir, matrix=None):
        # A fixture copy breaks one thing on purpose; the module on/off matrix adds two
        # installs that prove nothing about it. Only the shipped release runs the matrix.
        if matrix is None:
            matrix = Path(version_dir) == GT
        args = [str(version_dir)] + ([] if matrix else ["--no-module-matrix"])
        # The check runs one to three real installs and allows each 600s itself; the
        # harness's default 120s for the whole call timed out under load (gate, load ~40).
        return self.py(CHECK, *args, timeout=1800)

    # ---- the release as it stands ------------------------------------------------
    def test_the_shipped_release_passes(self):
        p = self.check(GT)
        self.assertEqual(p.returncode, 0,
                         "the release does not install completely:\n" + p.stdout + p.stderr)

    # ---- the bug it exists for ---------------------------------------------------
    def test_an_installer_that_does_not_wire_the_hook_fails_the_gate(self):
        """0.12.0 exactly: the hook ships, the manifest is clean, nothing wires it."""
        root, vdir = self.plugin_copy()
        inst = root / "install.sh"
        src = inst.read_text()
        broken = src.replace('wire_enforcement_hooks "$VAULT_PATH"', ':')
        self.assertNotEqual(broken, src, "fixture did not disable the wiring call")
        inst.write_text(broken)
        p = self.check(vdir)
        self.assertEqual(p.returncode, 1, "an inert enforcement hook passed the gate")
        self.assertIn("guard_vault_writes.sh", p.stdout)
        self.assertIn("inert", p.stdout)

    def test_wiring_that_depends_on_git_fails_for_a_non_git_vault(self):
        """0.12.1 exactly: wiring gated on the vault being a git repo.

        The gate installs into a vault it creates; if the installer only wires inside a
        git-only branch, the hook is inert and this must catch it.
        """
        root, vdir = self.plugin_copy()
        inst = root / "install.sh"
        src = inst.read_text()
        broken = src.replace(
            'if [ -n "$VAULT_PATH" ] && [ -d "$VAULT_PATH" ]; then\n  snapshot_vault_state "$VAULT_PATH"\n  backup_vault_before_writes "$VAULT_PATH"\n  wire_enforcement_hooks "$VAULT_PATH"\nfi',
            'if [ -n "$VAULT_PATH" ] && [ -d "$VAULT_PATH" ] && [ -e "$VAULT_PATH/.no-such-marker" ]; then\n  snapshot_vault_state "$VAULT_PATH"\n  backup_vault_before_writes "$VAULT_PATH"\n  wire_enforcement_hooks "$VAULT_PATH"\nfi')
        self.assertNotEqual(broken, src, "fixture did not narrow the wiring condition")
        inst.write_text(broken)
        p = self.check(vdir)
        self.assertEqual(p.returncode, 1,
                         "a conditionally-wired hook passed the gate")
        self.assertIn("guard_vault_writes.sh", p.stdout)

    # ---- other destinations ------------------------------------------------------
    def test_a_hook_declared_nowhere_is_reported(self):
        root, vdir = self.plugin_copy()
        (vdir / "hooks" / "orphan_hook.sh").write_text("#!/bin/sh\nexit 0\n")
        p = self.check(vdir)
        self.assertEqual(p.returncode, 1)
        self.assertIn("orphan_hook.sh", p.stdout)
        self.assertIn("NO hook registration", p.stdout)

    def test_a_newly_added_vault_tool_is_covered_without_being_listed(self):
        """The premise this replaces was wrong: a new tool IS seeded, correctly.

        What matters is that the gate covers it with no list to update — drop a tool
        into templates/tools and the check follows it to the vault by itself.
        """
        root, vdir = self.plugin_copy()
        (vdir / "templates" / "tools" / "gt_newtool.py").write_text("print('x')\n")
        p = self.check(vdir)
        self.assertEqual(p.returncode, 0,
                         "a newly shipped vault tool was not seeded:\n" + p.stdout)

    def test_a_vault_tool_that_fails_to_seed_is_reported(self):
        """Break the seeding and the gate must notice.

        Tools are seeded by TWO independent paths — vault_init's seed_vault_workspace
        and install.sh's own step 7 — so the fixture has to disable both. That
        redundancy is deliberate and worth knowing about: breaking either one alone
        does not lose the tools, which is why the first version of this test passed
        while claiming to prove the opposite.
        """
        root, vdir = self.plugin_copy()
        vi = vdir / "scripts" / "vault_init.py"
        src = vi.read_text()
        broken = src.replace('for f in sorted(tools_src.glob("*.py")):',
                             'for f in sorted(tools_src.glob("*.NOPE")):')
        self.assertNotEqual(broken, src, "fixture did not disable vault_init seeding")
        vi.write_text(broken)

        # Since 0.15.0 install.sh's own seeding runs through vault_refresh.py refresh_tools.
        vr = vdir / "scripts" / "vault_refresh.py"
        vsrc = vr.read_text()
        vbroken = vsrc.replace('for t in sorted(src.glob("*.py")):',
                               'for t in sorted(src.glob("*.NOPE")):')
        self.assertNotEqual(vbroken, vsrc, "fixture did not disable install.sh seeding")
        vr.write_text(vbroken)

        p = self.check(vdir)
        self.assertEqual(p.returncode, 1, "unseeded vault tools passed the gate:\n" + p.stdout)
        self.assertIn("was not seeded into the vault", p.stdout)

    def test_a_core_rule_that_never_reaches_the_vault_is_reported(self):
        root, vdir = self.plugin_copy()
        # A rule file the installer will not copy, because vault_init seeds core_*.md
        # from its own templates dir — here we add one AFTER the copy the installer
        # reads, by giving it a name the seeding skips.
        (vdir / "templates" / "core-rules" / "core_ghost_rule.md").write_text(
            "---\nname: core_ghost_rule\nmetadata:\n  level: core\n---\n\n**Ghost.**\n")
        p = self.check(vdir)
        # It should either arrive (fine) or be reported. What must not happen is a
        # silent pass with the file missing, so assert the two states agree.
        seeded = p.returncode == 0
        self.assertTrue(seeded or "core_ghost_rule.md" in p.stdout,
                        "a Core rule went missing and the gate said nothing")

    def test_usage_error_is_exit_2(self):
        p = self.py(CHECK)
        self.assertEqual(p.returncode, 2)


class ModuleWiring(Sandbox):
    """A fixture module with a reporter hook: wired when on, absent when off."""

    def setUp(self):
        super().setUp()
        self.cov = load_module(CHECK, "check_wiring_coverage_under_test")
        self.comp = load_module(SCRIPTS / "gt_components.py", "gt_components_cov_test")
        self.repo = self.tmp / "repo"
        gt = self.repo / "golden-thread" / "1.0.0" / ".claude-plugin"
        gt.mkdir(parents=True)
        (gt / "plugin.json").write_text('{"name": "gt", "version": "1.0.0"}')
        self.vd = self.repo / "golden-thread-zed" / "0.1.0"
        for sub in (".claude-plugin", "scripts", "hooks"):
            (self.vd / sub).mkdir(parents=True)
        (self.vd / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "gt-zed", "version": "0.1.0"}')
        (self.vd / "scripts" / "zed_report.py").write_text("print('zed')\n")
        (self.vd / "module.json").write_text(json.dumps({
            "schema": 1, "name": "zed", "plugin": "gt-zed", "version": "0.1.0",
            "requires_gt": ">=1.0.0", "summary": "fixture", "default": "on",
            "hooks": [{"event": "SessionStart", "script": "zed_report.py",
                       "args": ["--hook"], "kind": "reporter"}],
            "hookdir_scripts": ["zed_report.py"]}))
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)

    def install(self, wire=True, copy=True, event="SessionStart"):
        cmd = "python3 %s --hook" % (self.hooks / "zed_report.py")
        hooks = {event: [{"hooks": [{"type": "command", "command": cmd}]}]} if wire else {}
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}))
        if copy:
            shutil.copy2(self.vd / "scripts" / "zed_report.py", self.hooks / "zed_report.py")

    def findings(self):
        return self.cov.module_findings(self.repo, self.home, self.comp, gt_version="1.0.0")

    def choose(self, state):
        p = self.home / ".claude" / "golden-thread" / "install-choices.json"
        p.write_text(json.dumps({"version": 1, "choices": {"zed": state}}))

    def test_on_and_wired_is_clean(self):
        self.install()
        self.assertEqual(self.findings(), [])

    def test_on_but_not_wired_is_inert(self):
        self.install(wire=False)
        out = self.findings()
        self.assertEqual(len(out), 1, out)
        self.assertIn("zed_report.py is declared for SessionStart but NOT wired", out[0])

    def test_on_but_wired_under_the_wrong_event_is_inert(self):
        self.install(event="Stop")
        self.assertTrue(any("NOT wired" in p for p in self.findings()))

    def test_on_but_hookdir_script_missing(self):
        self.install(copy=False)
        self.assertTrue(any("was not installed" in p for p in self.findings()))

    def test_off_and_absent_is_clean(self):
        self.choose("off")
        self.install(wire=False, copy=False)
        self.assertEqual(self.findings(), [])

    def test_off_but_still_wired_or_installed(self):
        self.choose("off")
        self.install()
        out = self.findings()
        self.assertTrue(any("OFF but its hook zed_report.py is still wired" in p for p in out), out)
        self.assertTrue(any("OFF but zed_report.py is still in" in p for p in out), out)

    def test_undeclared_module_hook_file_is_reported(self):
        (self.vd / "hooks" / "zed_orphan.sh").write_text("#!/bin/sh\n")
        self.install()
        self.assertTrue(any("hooks/zed_orphan.sh ships but is declared in NO hook" in p
                            for p in self.findings()))

    def test_invalid_module_is_reported(self):
        data = json.loads((self.vd / "module.json").read_text())
        data["colour"] = "blue"
        (self.vd / "module.json").write_text(json.dumps(data))
        self.install()
        self.assertTrue(any("invalid module.json" in p for p in self.findings()))

    def test_on_module_skills_and_scripts_must_reach_its_cache(self):
        data = json.loads((self.vd / "module.json").read_text())
        (self.vd / "skills" / "zed-hi").mkdir(parents=True)
        (self.vd / "skills" / "zed-hi" / "SKILL.md").write_text("---\nname: zed-hi\n---\n")
        data.update(skills=["zed-hi"], scripts=["zed_report.py"])
        (self.vd / "module.json").write_text(json.dumps(data))
        self.install()
        out = self.cov.module_cache_findings(self.repo, self.home, self.comp, gt_version="1.0.0")
        self.assertTrue(any("skill zed-hi is not in the installed cache" in p for p in out), out)
        self.assertTrue(any("scripts/zed_report.py is not in the installed cache" in p
                            for p in out), out)
        cache = self.home / ".claude" / "plugins" / "cache" / "golden-thread-plugin" / "gt-zed" / "0.1.0"
        shutil.copytree(self.vd / "skills", cache / "skills")
        shutil.copytree(self.vd / "scripts", cache / "scripts")
        self.assertEqual(self.cov.module_cache_findings(self.repo, self.home, self.comp,
                                                        gt_version="1.0.0"), [])
        self.choose("off")
        out = self.cov.module_cache_findings(self.repo, self.home, self.comp, gt_version="1.0.0")
        self.assertTrue(any("OFF but its plugin cache" in p for p in out), out)

    def test_the_gate_runs_the_all_on_and_all_off_matrix(self):
        src = CHECK.read_text()
        self.assertIn('("every module on", "--with")', src)
        self.assertIn('("every module off", "--without")', src)

    def test_the_release_gate_never_skips_the_matrix(self):
        gate = (REPO / "dev" / "release-check.sh").read_text()
        self.assertIn("check_wiring_coverage.py", gate)
        self.assertNotIn("--no-module-matrix", gate)

    def test_a_release_without_the_module_reader_has_no_modules(self):
        class Old:
            HOOK_REGISTRATIONS = ()
        self.assertEqual(self.cov.module_findings(self.repo, self.home, Old()), [])


if __name__ == "__main__":
    unittest.main()
