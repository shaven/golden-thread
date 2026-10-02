"""gt_recipe.py: a recipe in, the finished shell script out, from one tested template.

Pinned (request 2026-10-01-fast-build-loop-and-execution-optimizer, test plan "golden-file tests
for the template, plus a smoke run of a generated script"):

  * a steps table renders a REPORT-mode runner: dependency order, ok / FAIL /
    SKIP-not-applicable per step, the count and names of what ran, stop at the first required
    FAIL, an optional FAIL reported and passed over, --until / --from / --list / --dry-run;
  * a cycle, an `after` naming no step, a bad kind and an unknown builtin are refused at render
    time -- never rendered;
  * a script recipe renders a PLAIN-mode script whose step bodies keep heredocs working, with
    generated option parsing, usage and --list-steps;
  * `check` passes on a fresh render and fails on a hand edit or a recipe changed without one;
  * the generated file carries no machine path;
  * dev/copygt.sh IS what dev/copygt.recipe renders (copygt.sh is regenerated from a recipe;
    tests/test_copygt.py is its smoke and behaviour test).
"""
import os
import re
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, REPO, needs_dev

TOOL = SCRIPTS / "gt_recipe.py"
ROW = "{id}\t{kind}\t{g}\t{cmd}\t{after}\t{req}\tuser\n"


def tsv(*rows):
    out = "# project: demo\n"
    for r in rows:
        out += ROW.format(**r)
    return out


def row(id, cmd, after="-", kind="step", req="yes", g=None):
    return {"id": id, "cmd": cmd, "after": after, "kind": kind, "req": req, "g": g or id + " ok"}


class RecipeBase(Sandbox):
    def render(self, text, name="p.tsv", out="release.sh"):
        src = self.tmp / name
        src.write_text(text, encoding="utf-8")
        p = self.py(TOOL, "render", src, "--out", self.tmp / out)
        return p, src, self.tmp / out

    def run_script(self, script, *args):
        env = dict(self.env, GT_EXECUTION_METRICS="off")
        return self.run_cmd(["bash", script, *args], env=env, cwd=self.tmp)


class ReportMode(RecipeBase):
    def setUp(self):
        super().setUp()
        # File order deliberately disagrees with run order: b names a as its predecessor.
        self.text = tsv(row("b", "echo B-RAN", after="a"), row("a", "echo A-RAN"),
                        row("c", "exit 99", after="b", kind="gate"),
                        row("d", "false", after="c", req="no"),
                        row("e", "exit 3", after="d", kind="gate"),
                        row("f", "echo F-RAN", after="e"))
        p, self.src, self.script = self.render(self.text)
        self.assertOk(p)

    def test_runs_in_dependency_order_and_stops_at_the_first_required_fail(self):
        p = self.run_script(self.script)
        self.assertEqual(p.returncode, 1, p.stdout)
        out = p.stdout
        self.assertLess(out.index("A-RAN"), out.index("B-RAN"), "after= did not order the steps")
        self.assertIn("SKIP-not-applicable c", out)
        self.assertRegex(out, r"FAIL\s+d .*optional, continuing")
        self.assertRegex(out, r"FAIL\s+e .*\(exit 3\)")
        self.assertNotIn("F-RAN", out, "a step after the failed gate ran")
        self.assertIn("ran 4 of 6 step(s)", out)
        self.assertIn(": a b d e", out)
        self.assertIn("not applicable: c", out)
        self.assertIn("not run: f", out)
        self.assertIn("STOPPED at the first failure: e", out)

    def test_all_passing_exits_zero_and_counts_everything(self):
        p, _, script = self.render(tsv(row("a", "true"), row("b", "echo hi", after="a")),
                                   name="ok.tsv", out="ok.sh")
        self.assertOk(p)
        r = self.run_script(script)
        self.assertOk(r)
        self.assertIn("ran 2 of 2 step(s)", r.stdout)

    def test_until_from_list_and_dry_run(self):
        r = self.run_script(self.script, "--until", "c")
        self.assertOk(r)
        self.assertIn("ran 2 of 6", r.stdout)
        self.assertIn("not run: c d e f", r.stdout)
        r = self.run_script(self.script, "--from", "f")
        self.assertOk(r)
        self.assertIn("F-RAN", r.stdout)
        self.assertNotIn("A-RAN", r.stdout)
        r = self.run_script(self.script, "--list")
        self.assertOk(r)
        self.assertEqual([l.split()[1] for l in r.stdout.splitlines()], list("abcdef"))
        r = self.run_script(self.script, "--dry-run")
        self.assertOk(r)
        self.assertIn("would run  a", r.stdout)
        self.assertNotIn("A-RAN", r.stdout.splitlines(), "--dry-run ran a step")
        r = self.run_script(self.script, "--until", "nope")
        self.assertEqual(r.returncode, 2)

    def test_check_catches_a_hand_edit_and_a_stale_recipe(self):
        self.assertOk(self.py(TOOL, "check", self.src, "--out", self.script))
        self.script.write_text(self.script.read_text() + "echo sneaky\n")
        self.assertEqual(self.py(TOOL, "check", self.src, "--out", self.script).returncode, 1)
        self.render(self.text)
        self.src.write_text(self.text + ROW.format(**row("g", "true", after="f")))
        p = self.py(TOOL, "check", self.src, "--out", self.script)
        self.assertEqual(p.returncode, 1)
        self.assertIn("render", p.stdout)

    def test_no_machine_path_in_the_generated_file(self):
        text = self.script.read_text()
        self.assertNotIn(str(self.tmp), text)
        self.assertNotIn(str(Path.home()), text)
        self.assertNotIn("/Users/", text)


class Refusals(RecipeBase):
    def refused(self, text, why):
        p, _, script = self.render(text, name="bad.tsv", out="bad.sh")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn(why, p.stderr)
        self.assertFalse(script.exists(), "an invalid recipe was rendered")

    def test_cycle(self):
        self.refused(tsv(row("a", "true", after="b"), row("b", "true", after="a")), "cycle")

    def test_after_names_no_step(self):
        self.refused(tsv(row("a", "true", after="ghost")), "not a step")

    def test_bad_kind_and_builtin(self):
        self.refused(tsv(row("a", "true", kind="maybe")), "kind must be")
        self.refused(tsv(row("a", "@teleport")), "unknown builtin")

    def test_duplicate_id(self):
        self.refused(tsv(row("a", "true"), row("a", "true")), "appears twice")


PLAIN = """name: demo.sh
mode: plain
strict: yes
doc:
  demo.sh -- a plain recipe.
usage:
  ./demo.sh --name N [--loud]
option: --name | value | NAME | | who to greet
option: --loud | flag | LOUD | yes | shout
option: --old | ignore | | | accepted, ignored
prologue:
  [ -n "$NAME" ] || { echo "REFUSED: --name is required"; exit 2; }
step: greet | step | it greets | - | yes
  python3 - "$NAME" <<'PYEOF'
  import sys
  print("hello " + sys.argv[1])
  PYEOF
step: shout | step | it shouts when asked | greet | yes
  [ "$LOUD" = yes ] && echo LOUD || echo quiet
  [ "$NAME" = fail ] && exit 7
  true
"""


class PlainMode(RecipeBase):
    def setUp(self):
        super().setUp()
        p, self.src, self.script = self.render(PLAIN, name="demo.recipe", out="demo.sh")
        self.assertOk(p)

    def test_heredoc_bodies_options_and_exit_codes(self):
        r = self.run_script(self.script, "--name", "ann", "--old")
        self.assertOk(r)
        self.assertIn("hello ann", r.stdout)
        self.assertIn("quiet", r.stdout)
        r = self.run_script(self.script, "--name=bo", "--loud")
        self.assertIn("LOUD", r.stdout)
        r = self.run_script(self.script, "--name", "fail")
        self.assertEqual(r.returncode, 7, "a body's own exit code must be the script's")

    def test_usage_refusal_and_list(self):
        r = self.run_script(self.script)
        self.assertEqual(r.returncode, 2)
        self.assertIn("REFUSED: --name is required", r.stdout)
        r = self.run_script(self.script, "--help")
        self.assertOk(r)
        self.assertIn("./demo.sh --name N", r.stdout)
        r = self.run_script(self.script, "--bogus")
        self.assertEqual(r.returncode, 2)
        r = self.run_script(self.script, "--list-steps")
        self.assertIn("greet", r.stdout)
        self.assertIn("it shouts when asked", r.stdout)


@needs_dev
class CopygtIsGenerated(Sandbox):
    def test_copygt_sh_is_what_its_recipe_renders(self):
        p = self.py(TOOL, "check", REPO / "dev" / "copygt.recipe", "--out",
                    REPO / "dev" / "copygt.sh")
        self.assertOk(p, "dev/copygt.sh was edited by hand or its recipe changed without a "
                         "render: python3 <release>/scripts/gt_recipe.py render dev/copygt.recipe "
                         "--out dev/copygt.sh")
        self.assertIn("GENERATED by gt_recipe.py", (REPO / "dev" / "copygt.sh").read_text())


if __name__ == "__main__":
    unittest.main()
