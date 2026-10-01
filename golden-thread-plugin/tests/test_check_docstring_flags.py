"""dev/check_docstring_flags.py -- a flag a usage block advertises must be one argparse has.

Contract:
  * Exit 0 when every `--flag` on a usage line is accepted.
  * Exit 1, naming the script and the flag, when one is not.
  * A flag shown on a SUBCOMMAND line must be accepted by THAT subcommand.
  * PROSE IS NOT A CLAIM. A docstring saying "there is no --push flag" is correct, and the
    first version of this gate reported three such sentences as defects. That test is the one
    that matters here: the gate exists to catch a wrong document, and a gate that calls a right
    document wrong gets switched off.
"""
import shutil
import unittest

from _harness import Sandbox, REPO, GT, needs_dev

CHECK = REPO / "dev" / "check_docstring_flags.py"
_NO_CACHE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyc.*")


@needs_dev
class DocstringFlags(Sandbox):
    def check(self, version_dir):
        return self.py(CHECK, str(version_dir))

    def copy_release(self):
        dst = self.tmp / "release"
        shutil.copytree(GT, dst, ignore=_NO_CACHE)
        return dst

    def patch(self, rel, script, old, new):
        p = rel / "scripts" / script
        src = p.read_text()
        self.assertIn(old, src, "fixture did not find the text it meant to patch")
        p.write_text(src.replace(old, new, 1))

    def test_the_shipped_release_advertises_only_real_flags(self):
        p = self.check(GT)
        self.assertEqual(p.returncode, 0,
                         "a shipped usage block advertises a flag argparse does not have:\n"
                         + p.stdout + p.stderr)

    def test_an_invented_flag_on_a_usage_line_fails_the_build(self):
        rel = self.copy_release()
        self.patch(rel, "gt_sweep.py",
                   "gt_sweep.py --vault V", "gt_sweep.py --vault V [--invented]")
        p = self.check(rel)
        self.assertEqual(p.returncode, 1, "an invented flag passed the gate\n" + p.stdout)
        self.assertIn("gt_sweep.py", p.stdout)
        self.assertIn("--invented", p.stdout)

    def test_a_flag_shown_on_the_WRONG_subcommand_fails(self):
        """The gt_state.py defect exactly: the flag exists, on a sibling verb. A person who
        types what the block shows still gets `unrecognized arguments`."""
        rel = self.copy_release()
        self.patch(rel, "gt_state.py",
                   "gt_state.py show ", "gt_state.py show   [--margin N] ")
        p = self.check(rel)
        self.assertEqual(p.returncode, 1,
                         "a flag advertised on the wrong subcommand passed\n" + p.stdout)
        self.assertIn("show", p.stdout)
        self.assertIn("--margin", p.stdout)

    def test_PROSE_about_a_flag_is_not_a_claim_about_this_tool(self):
        """The gate's own crying-wolf failure, pinned. `gt_allin_commit.py` says "There is no
        --push flag" and `gt_schedule.py` explains a rule about scripts having "--check";
        reading every `--flag` in the docstring reported both as defects."""
        rel = self.copy_release()
        p = rel / "scripts" / "gt_sweep.py"
        src = p.read_text()
        head, sep, tail = src.partition('"""')
        body, sep2, rest = tail.partition('"""')
        p.write_text(head + sep + body +
                     "\nThere is no --wholly-invented flag, and there never will be.\n"
                     + sep2 + rest)
        r = self.check(rel)
        self.assertEqual(r.returncode, 0,
                         "prose stating a flag does NOT exist was reported as a defect\n"
                         + r.stdout)

    def test_a_verb_registered_in_a_LOOP_is_still_checked(self):
        """gt_schedule.py registers three of its four subcommands in a loop, so `add_parser`
        appears once in its source. Reading only that, the gate could not find their help and
        reported four real flags as missing. The verb list is the union of the source and the
        parser's own choice list."""
        rel = self.copy_release()
        self.patch(rel, "gt_schedule.py",
                   "gt_schedule.py install <job> --vault V",
                   "gt_schedule.py install <job> --vault V [--not-a-flag]")
        r = self.check(rel)
        self.assertEqual(r.returncode, 1, "a loop-registered verb was not checked\n" + r.stdout)
        self.assertIn("--not-a-flag", r.stdout)


if __name__ == "__main__":
    unittest.main()
