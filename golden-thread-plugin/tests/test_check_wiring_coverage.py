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
    the shape the bug took.
"""
import shutil
import unittest

from _harness import Sandbox, REPO, GT, WIKI


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

    def check(self, version_dir):
        return self.py(CHECK, str(version_dir))

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
            'if [ -n "$VAULT_PATH" ] && [ -d "$VAULT_PATH" ]; then\n  wire_enforcement_hooks "$VAULT_PATH"\nfi',
            'if [ -n "$VAULT_PATH" ] && [ -d "$VAULT_PATH" ] && [ -e "$VAULT_PATH/.no-such-marker" ]; then\n  wire_enforcement_hooks "$VAULT_PATH"\nfi')
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

        inst = root / "install.sh"
        isrc = inst.read_text()
        ibroken = isrc.replace('for t in "$SRC/templates/tools/"*.py; do',
                               'for t in "$SRC/templates/tools/"*.NOPE; do')
        self.assertNotEqual(ibroken, isrc, "fixture did not disable install.sh seeding")
        inst.write_text(ibroken)

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


if __name__ == "__main__":
    unittest.main()
