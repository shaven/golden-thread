"""dev/check_cli_contract.py -- the release gate behind core_explicit_vault_target.

Contract:
  * Exit 0 when every COVERED tool accepts --vault and --dry-run, both before and
    after its subcommands.
  * Exit 1, naming the tool and the flag, when one does not.
  * It must RUN the tools, not read their source: the 0.12.0 bug was a flag that
    existed in the source and did not parse after the verb.
"""
import shutil
import unittest

from _harness import Sandbox, REPO, GT


CHECK = REPO / "dev" / "check_cli_contract.py"


# Other tests import scripts from the source tree in parallel and write __pycache__ into it;
# copying a half-written .pyc makes copytree fail. Caches are never part of a release.
_NO_CACHE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyc.*")


class CliContract(Sandbox):
    def check(self, version_dir):
        return self.py(CHECK, str(version_dir))

    def copy_release(self):
        """A writable copy of the release, so a test can break one tool on purpose."""
        dst = self.tmp / "release"
        shutil.copytree(GT, dst, ignore=_NO_CACHE)
        return dst

    def test_the_shipped_release_holds_the_contract(self):
        p = self.check(GT)
        self.assertEqual(p.returncode, 0,
                         "the release under test violates core_explicit_vault_target:\n"
                         + p.stdout + p.stderr)

    def test_a_missing_dry_run_fails_the_build(self):
        """Delete the flag from a tool and the gate must catch it."""
        rel = self.copy_release()
        tool = rel / "templates" / "tools" / "gt_adr.py"
        src = tool.read_text()
        tool.write_text(src.replace('common.add_argument("--dry-run", "-n", action="store_true",',
                                    'common.add_argument("--unrelated", action="store_true",'))
        p = self.check(rel)
        self.assertEqual(p.returncode, 1, "a tool with no --dry-run passed the gate")
        self.assertIn("gt_adr.py", p.stdout)
        self.assertIn("--dry-run", p.stdout)

    def test_a_flag_that_only_parses_before_the_verb_fails(self):
        """The 0.12.0 bug itself: present in --help, unusable where people type it."""
        rel = self.copy_release()
        tool = rel / "templates" / "tools" / "gt_log.py"
        src = tool.read_text()
        # Detach the subcommands from the shared parent -> flags parse only up front.
        broken = src.replace('sub.add_parser("migrate", parents=[common])',
                             'sub.add_parser("migrate")')
        self.assertNotEqual(broken, src, "test fixture did not patch the parser")
        tool.write_text(broken)
        p = self.check(rel)
        self.assertEqual(p.returncode, 1,
                         "a flag that does not parse after the subcommand passed the gate")
        self.assertIn("after the subcommand", p.stdout)

    def test_a_covered_tool_that_stops_shipping_is_reported(self):
        rel = self.copy_release()
        (rel / "templates" / "tools" / "gt_tasks.py").unlink()
        p = self.check(rel)
        self.assertEqual(p.returncode, 1)
        self.assertIn("not shipped", p.stdout)

    def test_exemptions_are_named_with_reasons(self):
        """An exemption is a decision on the record, not a silent omission."""
        mod = self.load_check()
        for tool, reason in mod.EXEMPT.items():
            self.assertTrue(reason.strip(), f"{tool} is exempt with no reason given")
            self.assertGreater(len(reason), 20, f"{tool}'s reason is not an explanation")

    def test_no_tool_is_both_covered_and_exempt(self):
        mod = self.load_check()
        both = set(mod.COVERED) & set(mod.EXEMPT)
        self.assertFalse(both, f"listed twice, so the contract is ambiguous: {both}")

    def load_check(self):
        from _harness import load_module
        return load_module(CHECK, "check_cli_contract")


if __name__ == "__main__":
    unittest.main()
