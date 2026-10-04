"""Receipts name the gates that ran (0.20.0), and options never turn a full run into a subset.

Found 2026-10-03: `dev/remote-test.sh -j 8` handed `-j 8` to tests/run.sh, which treated ANY
argument as a test selector (`FULL_RUN=no` unless `$# -eq 0`). The run went down the subset path,
which skips the secrets gate and the code gate -- and remote-test.sh still recorded a full receipt
from the exit code. Every remote receipt that day had skipped the secrets scan, and nothing that
reads the ledger could tell.

Pinned:

  * `tests/run.sh -j 2` (and `GT_TEST_JOBS`, and `--hosts local`) with no selector is FULL: both
    gates run, the verdict line names them, and the receipt carries all three verdicts;
  * a selector run is a subset: gates `not-run`, no receipt;
  * a full run whose gate cannot pass records no receipt;
  * in a repo whose runner declares gates (`# gt-receipt-gates: ...`), a passing receipt without
    a `pass` for each is REFUSED at record time and IGNORED by every reader -- `check`, the commit
    guard (which says why), scoped coverage -- while a repo that declares none is unaffected;
  * dev/remote-test.sh -j 2 through a local stand-in for ssh runs both gates on the "runner",
    prints their verdicts, and records a receipt with them; with a gate that cannot pass it
    records nothing.
"""
import json
import os
import time
import unittest

from _harness import Sandbox, REPO, PYTHON, SCRIPTS, needs_dev
from _fakes import install_fake
from test_guard_test_before_commit import CommitGuardBase
from test_prun_execution import FakeRepo, FAKE_SSH

GATED_RUNNER = "#!/usr/bin/env bash\n# gt-receipt-gates: tests secrets code\nexit 0\n"
ALL_PASS = ["--gate", "tests=pass", "--gate", "secrets=pass", "--gate", "code=pass"]


def ledger_rows(home):
    p = home / ".claude" / "golden-thread" / "test-runs.jsonl"
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def drop_failing_fixture(case):
    """Remove the fixture's deliberately failing module from the index AND the tree: the suite
    must pass, and remote-test.sh ships what `git ls-files -co` lists, which still names a
    tracked file that was only deleted from disk."""
    case.run_cmd(["git", "-C", case.root, "rm", "-q", "golden-thread-plugin/tests/test_beta.py"])


class GatedRepoBase(CommitGuardBase):
    """A repo whose test runner declares gates, as this repository's tests/run.sh does."""

    def setUp(self):
        super().setUp()
        self.write("tests/run.sh", GATED_RUNNER)
        self.stage("tests/run.sh")
        self.run_cmd(["git", "-C", str(self.repo), "commit", "-q", "-m", "gated runner"])
        self.tool = self.hooks / "gt_test_receipt.py"

    def record(self, *extra):
        return self.py(self.tool, "record", "--repo", str(self.repo), "--what", "tests/run.sh",
                       "--ok", "--tests", "5", *extra)

    def plant_ungated(self, **extra):
        """A receipt as every recorder before 0.20.0 wrote it: ok, and no gates at all."""
        row = {"repo": str(self.repo.resolve()), "what": "remote:tests/run.sh@box", "ok": True,
               "tests": 3638, "elapsed": 0.0, "at": time.time(), "at_human": "now", "head": ""}
        row.update(extra)
        p = self.home / ".claude" / "golden-thread" / "test-runs.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fh.write(json.dumps(row) + "\n")

    def check(self, *files, scoped=False):
        args = ["check", "--repo", str(self.repo), "--files", *files]
        if scoped:
            args.append("--allow-scoped")
        return self.py(self.tool, *args)


class ReceiptsNeedTheirGates(GatedRepoBase):
    def test_a_passing_receipt_without_gates_is_refused_at_record_time(self):
        p = self.record()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("secrets", p.stderr)
        self.assertEqual(ledger_rows(self.home), [], "a refused receipt was written anyway")

    def test_a_gate_that_did_not_pass_is_refused(self):
        p = self.record("--gate", "tests=pass", "--gate", "secrets=findings", "--gate", "code=pass")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        p = self.record("--gate", "tests=pass", "--gate", "code=pass")
        self.assertEqual(p.returncode, 2, "a receipt with no secrets verdict was accepted")
        self.assertEqual(ledger_rows(self.home), [])

    def test_a_failed_run_needs_no_gates(self):
        p = self.py(self.tool, "record", "--repo", str(self.repo), "--what", "tests/run.sh",
                    "--failed")
        self.assertOk(p, "recording a FAILED run must not demand gate verdicts")

    def test_an_ungated_receipt_is_not_accepted_as_full(self):
        self.write("app.py")
        time.sleep(1.1)
        self.plant_ungated()
        p = self.check("app.py")
        self.assertEqual(p.returncode, 1, "a receipt with no gate verdicts covered a file")
        self.assertIn("secrets", p.stdout, "the refusal must say which gates are missing")
        self.assertIn("remote:tests/run.sh@box", p.stdout)

    def test_a_receipt_with_all_gates_covers_and_an_ungated_newer_one_cannot_shadow_it(self):
        self.write("app.py")
        time.sleep(1.1)
        self.assertOk(self.record(*ALL_PASS))
        self.assertEqual(ledger_rows(self.home)[-1]["gates"],
                         {"tests": "pass", "secrets": "pass", "code": "pass"})
        self.plant_ungated()
        p = self.check("app.py")
        self.assertOk(p, "the gated receipt stopped counting once a gate-less row followed it")
        self.assertIn("tests/run.sh at", p.stdout)

    def test_an_ungated_scoped_receipt_is_not_accepted(self):
        self.run_cmd(["git", "-C", str(self.repo), "checkout", "-q", "-b", "feature"])
        self.write("app.py")
        time.sleep(1.1)
        self.plant_ungated(scope="scoped", files=["app.py"])
        p = self.check("app.py", scoped=True)
        self.assertEqual(p.returncode, 1, "a scoped receipt with no gates covered a file")

    def test_the_commit_guard_refuses_it_and_says_why(self):
        self.write("app.py")
        self.stage("app.py")
        time.sleep(1.1)
        self.plant_ungated()
        hso = self.assertDenied("a commit went through on a receipt that skipped the gates")
        reason = hso["permissionDecisionReason"]
        self.assertIn("gate(s)", reason)
        self.assertIn("secrets", reason)
        time.sleep(1.1)
        self.assertOk(self.record(*ALL_PASS))
        self.assertAllowed("a receipt carrying all three gates must license the commit")


class ReposWithoutGatesAreUnaffected(CommitGuardBase):
    def test_a_plain_receipt_still_covers_in_a_repo_that_declares_no_gates(self):
        self.with_test_entry_point()
        tool = self.hooks / "gt_test_receipt.py"
        self.write("app.py")
        time.sleep(1.1)
        self.receipt()
        p = self.py(tool, "check", "--repo", str(self.repo), "--files", "app.py")
        self.assertOk(p, "a repo with no declared gates must not start demanding them")


class ThisRepoDeclaresItsGates(unittest.TestCase):
    def test_tests_run_sh_declares_tests_secrets_and_code(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("gt_test_receipt_decl",
                                                      SCRIPTS / "gt_test_receipt.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        found = mod.GATES_MARKER.findall((REPO / "tests" / "run.sh").read_text())
        self.assertEqual(sorted(" ".join(found).split()), ["code", "secrets", "tests"])


class OptionsAreNotSelectors(FakeRepo):
    """tests/run.sh in a throwaway repo shaped like this one, with a suite that passes."""

    def setUp(self):
        super().setUp()
        drop_failing_fixture(self)
        self.rec = self.rel / "scripts" / "gt_test_receipt.py"
        self.file = "golden-thread-plugin/golden-thread/%s/scripts/alpha_tool.py" % self.rel.name

    def run_sh(self, *args, env=None):
        e = {"GT_TEST_LOAD_AWARE": "0"}
        e.update(env or {})
        return self.run_cmd(["bash", "tests/run.sh", *args], env=e, cwd=self.plugin, timeout=600)

    def last_line(self, p):
        return [l for l in p.stdout.splitlines() if l.strip()][-1]

    def assertFullWithGates(self, p, what):
        self.assertOk(p, what)
        self.assertEqual(self.last_line(p),
                         "gt-gates: scope=full tests=pass count=4 secrets=pass code=pass",
                         "%s did not run as a FULL run with both gates:\n%s" % (what, p.stdout))
        rows = ledger_rows(self.home)
        self.assertTrue(rows, "%s passed and recorded no receipt" % what)
        self.assertEqual(rows[-1]["gates"], {"tests": "pass", "secrets": "pass", "code": "pass"})
        self.assertNotEqual(rows[-1].get("scope"), "scoped")
        self.assertOk(self.py(self.rec, "check", "--repo", self.root, "--files", self.file))

    def test_dash_j_is_still_a_full_run_and_runs_both_gates(self):
        self.assertFullWithGates(self.run_sh("-j", "2"), "tests/run.sh -j 2")

    def test_glued_dash_j_and_env_jobs_are_full_runs_too(self):
        self.assertFullWithGates(self.run_sh("-j2"), "tests/run.sh -j2")
        self.assertFullWithGates(self.run_sh(env={"GT_TEST_JOBS": "2"}), "GT_TEST_JOBS=2")

    def test_hosts_local_is_a_full_run_with_the_gates_run_here(self):
        self.assertFullWithGates(self.run_sh("--hosts", "local", "-j", "2"),
                                 "tests/run.sh --hosts local")

    def test_a_selector_is_a_subset_no_gates_no_receipt(self):
        p = self.run_sh("-j", "2", "test_alpha")
        self.assertOk(p)
        self.assertEqual(self.last_line(p),
                         "gt-gates: scope=subset tests=pass count=4 secrets=not-run code=not-run")
        self.assertEqual(ledger_rows(self.home), [], "a subset recorded a receipt")

    def test_gates_option_runs_both_gates_on_a_subset_but_records_no_full_receipt(self):
        p = self.run_sh("--gates", "test_alpha")
        self.assertOk(p)
        self.assertEqual(self.last_line(p),
                         "gt-gates: scope=subset tests=pass count=4 secrets=pass code=pass")
        self.assertEqual(ledger_rows(self.home), [])

    def test_an_unknown_option_is_refused_not_taken_for_a_selector(self):
        p = self.run_sh("--frobnicate")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("unknown option", p.stderr)

    def test_a_gate_that_cannot_run_means_no_receipt(self):
        (self.rel / "scripts" / "gt_secrets.py").unlink()
        p = self.run_sh("-j", "2")
        self.assertNotEqual(p.returncode, 0, "a full run with no secrets scanner passed")
        self.assertEqual(self.last_line(p),
                         "gt-gates: scope=full tests=pass count=4 secrets=cannot-run code=pass",
                         "the code gate must still run, and say so, when secrets cannot")
        self.assertEqual(ledger_rows(self.home), [])


class ARedSuiteStillRunsTheGates(FakeRepo):
    """The fixture as shipped: test_beta fails on purpose."""

    def test_gates_run_the_count_is_the_suites_and_nothing_is_recorded(self):
        p = self.run_cmd(["bash", "tests/run.sh", "-j", "2"], env={"GT_TEST_LOAD_AWARE": "0"},
                         cwd=self.plugin, timeout=600)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        last = [l for l in p.stdout.splitlines() if l.strip()][-1]
        # count=5 (four in test_alpha, one in test_beta), not the "Ran 1 test" quoted from the
        # failing unit's own output after the summary.
        self.assertEqual(last, "gt-gates: scope=full tests=fail count=5 secrets=pass code=pass")
        self.assertEqual(ledger_rows(self.home), [], "a red suite recorded a receipt")


@needs_dev
class RemoteTestRunsTheGates(FakeRepo):
    """dev/remote-test.sh -j 2 with `ssh` replaced by a local stand-in (the "runner" is here)."""

    def setUp(self):
        super().setUp()
        drop_failing_fixture(self)
        (self.plugin / "dev").mkdir()
        (self.plugin / "dev" / "remote-test.sh").write_bytes(
            (REPO / "dev" / "remote-test.sh").read_bytes())
        bindir = self.tmp / "bin"
        bindir.mkdir()
        install_fake(self, bindir / "ssh", FAKE_SSH)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env.get("PATH", "")
        self.env["GT_TEST_LOAD_AWARE"] = "0"
        self.rtmp = self.tmp / "rtmp"
        self.env["GT_REMOTE_TMP"] = self.rtmp.as_posix()

    def remote(self, *args):
        return self.run_cmd(["bash", "dev/remote-test.sh", "--host", "box", *args],
                            cwd=self.plugin, timeout=900)

    def test_dash_j_runs_both_gates_on_the_runner_and_the_receipt_names_them(self):
        p = self.remote("-j", "2")
        self.assertOk(p, "remote-test.sh -j 2")
        self.assertIn("== gates on box: gt-gates: scope=full tests=pass count=4 secrets=pass "
                      "code=pass", p.stdout)
        self.assertIn("receipt recorded", p.stdout)
        row = ledger_rows(self.home)[-1]
        self.assertEqual(row["what"], "remote:tests/run.sh@box")
        self.assertEqual(row["gates"], {"tests": "pass", "secrets": "pass", "code": "pass"})
        # The run's temp files lived under <GT_REMOTE_TMP>/gt-test-<uid>/<run id> on the
        # runner, and are gone.
        per_uid = list(self.rtmp.glob("gt-test-*")) if self.rtmp.is_dir() else []
        self.assertEqual(len(per_uid), 1, "the run did not use a per-run TMPDIR under GT_REMOTE_TMP")
        self.assertEqual(list(per_uid[0].iterdir()), [], "the run left its temp dir behind")

    def test_a_gate_that_cannot_pass_on_the_runner_records_nothing(self):
        (self.rel / "scripts" / "gt_secrets.py").unlink()
        p = self.remote("-j", "2")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("secrets=cannot-run", p.stdout)
        self.assertEqual(ledger_rows(self.home), [], "a run whose secrets gate failed was receipted")


if __name__ == "__main__":
    unittest.main()
