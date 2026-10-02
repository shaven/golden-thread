"""gt_pipeline.py -- a project's release pipeline -- and where gt uses it.

Pinned (request 2026-10-01-project-build-pipeline-build-sh):

  * init writes release-pipeline.tsv + release.sh with the seven default gates in order;
    re-running changes nothing; a release.sh gt did not write is never overwritten;
  * release.sh runs the steps in declared order, stops at the first FAIL, reports how many ran;
    the owner gate stops without --go; a gate that cannot run FAILS;
  * add inserts a user gate at a stated position (after sync); a missing command and a cycle
    are refused; removing a default gate needs --reason, which is recorded in the steps file;
  * check fails on a hand-edited release.sh;
  * /gt:gt-allin gains a `pipeline` member only for a repo that has one, and runs it up to the
    owner gate without recursing into itself; /gt:gt-allin-commit refuses on a failed
    pre-commit step even with --allow-findings;
  * create-project records release_pipeline, scaffolds the pipeline for `yes` with a code root
    and lists it in source.md; gt-lint flags `no` on a project with code and `yes` with no
    release.sh; `flag --all-missing` records `planned` through the write queue; gt_upgrade has a
    migration for it.
"""
import json
import os
import re
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, TEMPLATES

TOOL = SCRIPTS / "gt_pipeline.py"
DEFAULT_ORDER = ["tests", "allin", "branch", "install", "owner-gate", "push", "sync"]


class PipelineBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo = self.git_init(self.tmp / "repo")
        self.run_cmd(["git", "-C", self.repo, "checkout", "-q", "-b", "feature"])
        self.env["GT_EXECUTION_METRICS"] = "off"
        self.env["GT_PIPELINE_PY"] = str(TOOL)

    def pl(self, *args, **kw):
        return self.py(TOOL, *[str(a) for a in args], **kw)

    def order(self):
        p = self.pl("list", "--repo", self.repo)
        self.assertOk(p)
        return [l.split()[1] for l in p.stdout.splitlines() if re.match(r"^\s*\d+\.", l)]

    def release(self, *args, env=None):
        e = dict(self.env)
        e.update(env or {})
        return self.run_cmd(["bash", self.repo / "release.sh", *args], env=e, cwd=self.repo)


class InitAndOrder(PipelineBase):
    def test_init_writes_the_default_gates_and_is_idempotent(self):
        self.assertOk(self.pl("init", "--repo", self.repo, "--project", "demo"))
        self.assertTrue((self.repo / "release-pipeline.tsv").is_file())
        self.assertTrue(os.access(self.repo / "release.sh", os.X_OK))
        self.assertEqual(self.order(), DEFAULT_ORDER)
        tsv = (self.repo / "release-pipeline.tsv").read_text()
        self.assertEqual(tsv.count("\tdefault\n"), 7, "default steps are marked default")
        before = {p: p.read_bytes() for p in (self.repo / "release-pipeline.tsv",
                                              self.repo / "release.sh")}
        p = self.pl("init", "--repo", self.repo)
        self.assertOk(p)
        self.assertIn("nothing changed", p.stdout)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertOk(self.pl("check", "--repo", self.repo))

    def test_dry_run_writes_nothing(self):
        self.assertOk(self.pl("init", "--repo", self.repo, "--dry-run"))
        self.assertFalse((self.repo / "release-pipeline.tsv").exists())

    def test_a_foreign_release_sh_is_never_overwritten(self):
        mine = "#!/bin/sh\necho my own release\n"
        (self.repo / "release.sh").write_text(mine)
        p = self.pl("init", "--repo", self.repo)
        self.assertOk(p)
        self.assertEqual((self.repo / "release.sh").read_text(), mine)
        self.assertTrue((self.repo / "release-pipeline.sh").is_file())
        self.assertIn("script: release-pipeline.sh",
                      (self.repo / "release-pipeline.tsv").read_text())
        self.assertOk(self.pl("check", "--repo", self.repo))

    def test_add_a_user_gate_after_sync_and_refusals(self):
        self.assertOk(self.pl("init", "--repo", self.repo))
        (self.repo / "copy.sh").write_text("#!/bin/sh\necho copied\n")
        os.chmod(self.repo / "copy.sh", 0o755)
        self.assertOk(self.pl("add", "copy", "--repo", self.repo, "--kind", "gate",
                              "--cmd", "./copy.sh --dest x", "--after", "sync"))
        self.assertEqual(self.order(), DEFAULT_ORDER + ["copy"])
        self.assertOk(self.pl("add", "lint2", "--repo", self.repo, "--cmd", "true",
                              "--after", "tests"))
        self.assertEqual(self.order()[:2], ["tests", "lint2"])
        p = self.pl("add", "ghost", "--repo", self.repo, "--cmd", "./nope.sh")
        self.assertEqual(p.returncode, 2)
        self.assertIn("does not exist", p.stdout)
        p = self.pl("add", "x", "--repo", self.repo, "--cmd", "true", "--after", "nowhere")
        self.assertEqual(p.returncode, 2)
        # A cycle can only come from a hand edit; check refuses it.
        tsv = self.repo / "release-pipeline.tsv"
        tsv.write_text(tsv.read_text().replace("tests\tstep\tthe test suite passed and a "
                                               "receipt was recorded\t@tests\t-",
                                               "tests\tstep\tthe test suite passed and a "
                                               "receipt was recorded\t@tests\tsync"))
        p = self.pl("check", "--repo", self.repo)
        self.assertEqual(p.returncode, 1)
        self.assertIn("cycle", p.stdout)

    def test_removing_a_default_gate_needs_a_reason_that_is_recorded(self):
        self.assertOk(self.pl("init", "--repo", self.repo))
        p = self.pl("remove", "owner-gate", "--repo", self.repo)
        self.assertEqual(p.returncode, 2)
        self.assertIn("--reason", p.stdout)
        self.assertOk(self.pl("remove", "install", "--repo", self.repo, "--reason",
                              "nothing to install: a library"))
        tsv = (self.repo / "release-pipeline.tsv").read_text()
        self.assertRegex(tsv, r"# removed: install \(default\) on \d{4}-\d\d-\d\d: nothing to "
                              r"install: a library")
        self.assertNotIn("install", self.order())
        self.assertEqual(self.order()[self.order().index("branch") + 1], "owner-gate",
                         "the chain must close over the removed step")

    def test_check_fails_on_a_hand_edited_release_sh(self):
        self.assertOk(self.pl("init", "--repo", self.repo))
        with open(self.repo / "release.sh", "a") as fh:
            fh.write("echo extra\n")
        p = self.pl("check", "--repo", self.repo)
        self.assertEqual(p.returncode, 1)
        self.assertIn("hand-edited", p.stdout)


class Running(PipelineBase):
    def setUp(self):
        super().setUp()
        self.assertOk(self.pl("init", "--repo", self.repo))

    def test_default_steps_failing_and_passing_cases(self):
        # No test entry point: @tests cannot run, which is a FAIL, not a pass.
        p = self.release()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertRegex(p.stdout, r"FAIL\s+tests")
        self.assertIn("ran 1 of 7", p.stdout)
        self.assertIn("STOPPED at the first failure: tests", p.stdout)
        # Exempt the tests and stand in for allin: the run reaches the owner gate and stops.
        (self.repo / ".gt-no-test-gate").write_text("")
        self.assertOk(self.pl("set", "allin", "--repo", self.repo, "--cmd", "true"))
        p = self.release()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("SKIP-not-applicable tests", p.stdout)
        self.assertRegex(p.stdout, r"ok\s+branch")
        self.assertIn("SKIP-not-applicable install", p.stdout)
        self.assertIn("waiting for the owner's explicit go-ahead", p.stdout)
        self.assertIn("STOPPED at the first failure: owner-gate", p.stdout)
        # Up to the owner gate is what gt-allin runs: green.
        p = self.release("--until", "owner-gate")
        self.assertOk(p)
        self.assertIn("not run: owner-gate push sync", p.stdout)
        # With the go-ahead: no remote -> push not applicable, sync not applicable.
        p = self.release("--from", "owner-gate", "--go")
        self.assertOk(p)
        self.assertIn("SKIP-not-applicable push", p.stdout)
        self.assertIn("SKIP-not-applicable sync", p.stdout)

    def test_branch_gate_fails_on_the_default_branch_and_install_needs_declaring(self):
        (self.repo / ".gt-no-test-gate").write_text("")
        self.assertOk(self.pl("set", "allin", "--repo", self.repo, "--cmd", "true"))
        self.run_cmd(["git", "-C", self.repo, "checkout", "-q", "main"])
        p = self.release("--until", "owner-gate")
        self.assertRegex(p.stdout, r"FAIL\s+branch")
        self.run_cmd(["git", "-C", self.repo, "checkout", "-q", "feature"])
        (self.repo / "install.sh").write_text("#!/bin/sh\n")
        p = self.release("--until", "owner-gate")
        self.assertEqual(p.returncode, 1)
        self.assertRegex(p.stdout, r"FAIL\s+install")
        self.assertIn("gt_pipeline.py set install", p.stdout)


class AllinUsesThePipeline(PipelineBase):
    def test_member_only_with_a_pipeline_and_no_recursion(self):
        allin = SCRIPTS / "gt_allin.py"
        p = self.py(allin, "--repo", self.repo, "--list")
        self.assertNotIn("pipeline", p.stdout)
        self.assertOk(self.pl("init", "--repo", self.repo))
        (self.repo / ".gt-no-test-gate").write_text("")
        p = self.py(allin, "--repo", self.repo, "--list")
        self.assertIn("pipeline", p.stdout)
        p = self.py(allin, "--repo", self.repo, "--only", "pipeline", timeout=300)
        self.assertIn("covered: this run is inside /gt:gt-allin", p.stdout)
        self.assertIn("[pipeline] ran", p.stdout)
        self.assertIn("not run: owner-gate push sync", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout[-3000:])
        # Inside the pipeline's own @allin step, gt-allin does not add the pipeline again.
        e = dict(self.env, GT_PIPELINE_INSIDE="pipeline")
        p = self.run_cmd(["python3", allin, "--repo", self.repo, "--list"], env=e)
        self.assertNotIn("pipeline", p.stdout)

    def test_allin_commit_refuses_a_failed_pipeline_step_even_with_allow_findings(self):
        self.assertOk(self.pl("init", "--repo", self.repo))
        (self.repo / ".gt-no-test-gate").write_text("")
        self.assertOk(self.pl("add", "gate1", "--repo", self.repo, "--kind", "gate",
                              "--cmd", "false", "--after", "branch"))
        (self.repo / "a.py").write_text("x = 1\n")
        self.run_cmd(["git", "-C", self.repo, "add", "-A"])
        p = self.py(SCRIPTS / "gt_allin_commit.py", "--repo", self.repo, "-m", "x",
                    "--allow-findings", "--dry-run", timeout=300)
        self.assertEqual(p.returncode, 1, p.stdout[-3000:])
        self.assertIn("release pipeline step gate1 FAILED", p.stdout)


class ProjectFlag(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.config(vault_path=str(self.vault))

    def create(self, slug, *args):
        p = self.py(SCRIPTS / "vault_init.py", "create-project", "--vault", self.vault,
                    "--name", slug, "--domain", "test", *args)
        self.assertOk(p)
        return self.vault / "Projects" / slug

    def lint(self):
        p = self.py(SCRIPTS / "gt_lint.py", self.vault, "--json")
        return [f for f in json.loads(p.stdout)["findings"] if f["kind"] == "release-pipeline"]

    def test_create_project_records_scaffolds_and_lists_it(self):
        code = self.git_init(self.tmp / "code")
        proj = self.create("shipper", "--topology", "local", "--release-pipeline", "yes",
                           "--project-dir", code)
        self.assertIn("release_pipeline: yes", (proj / "README.md").read_text())
        self.assertTrue((code / "release.sh").is_file())
        self.assertTrue((code / "release-pipeline.tsv").is_file())
        src = (proj / "source.md").read_text()
        self.assertIn("**Release pipeline:** %s" % (code.resolve() / "release.sh"), src)
        notes = self.create("notes")
        self.assertIn("release_pipeline: no", (notes / "README.md").read_text())
        coded = self.create("later", "--topology", "remote")
        self.assertIn("release_pipeline: planned", (coded / "README.md").read_text())
        self.assertEqual(self.lint(), [])

    def test_lint_flags_no_with_code_and_yes_without_release_sh(self):
        code = self.git_init(self.tmp / "code")
        proj = self.create("shipper", "--topology", "local", "--release-pipeline", "yes",
                           "--project-dir", code)
        self.create("nopipe", "--topology", "local", "--release-pipeline", "no")
        (code / "release.sh").unlink()
        msgs = [f["message"] for f in self.lint()]
        self.assertTrue(any("nopipe has code" in m for m in msgs), msgs)
        self.assertTrue(any("shipper says release_pipeline: yes" in m and "does not exist" in m
                            for m in msgs), msgs)

    def test_flag_all_missing_goes_through_the_queue_and_upgrade_offers_it(self):
        proj = self.create("old", "--topology", "local")
        readme = proj / "README.md"
        readme.write_text(readme.read_text().replace("release_pipeline: planned\n", ""))
        sys_up = SCRIPTS / "gt_upgrade.py"
        p = self.py(sys_up, "status", "--vault", self.vault)
        self.assertIn("release-pipeline-flag", p.stdout)
        p = self.py(TOOL, "flag", "--vault", self.vault, "--all-missing", "--dry-run")
        self.assertOk(p)
        self.assertNotIn("release_pipeline", readme.read_text())
        p = self.py(TOOL, "flag", "--vault", self.vault, "--all-missing")
        self.assertOk(p, "flag")
        self.assertIn("release_pipeline: planned", readme.read_text())
        p = self.py(TOOL, "flag", "--project", "old", "--value", "yes")
        self.assertEqual(p.returncode, 2, "flag must refuse without --vault")


class ShippedTemplate(unittest.TestCase):
    def test_the_shipped_template_is_the_default_pipeline(self):
        import sys
        sys.path.insert(0, str(SCRIPTS))
        import gt_pipeline
        import gt_recipe
        d = TEMPLATES / "release-pipeline"
        want = gt_pipeline.serialise(gt_pipeline.default_recipe(None))
        self.assertEqual((d / "release-pipeline.tsv").read_text(), want)
        self.assertEqual((d / "release.sh").read_text(),
                         gt_recipe.render(gt_recipe.parse_tsv(want), want,
                                          "release-pipeline.tsv"))


if __name__ == "__main__":
    unittest.main()
