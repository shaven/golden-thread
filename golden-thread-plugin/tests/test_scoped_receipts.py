"""Scoped test receipts (0.18.1): accepted on a feature branch, never on the default branch or
at a release gate.

Pinned (request 2026-10-01-fast-build-loop-and-execution-optimizer, test plan "a scoped receipt
is accepted on a branch, refused on the default branch and at release; a stale scoped receipt is
refused"):

  * the commit guard lets a commit through on a feature branch when a passing SCOPED receipt
    names every staged code file and is newer than each;
  * the same commit on the default branch is denied;
  * a file the scoped receipt does not name, or one edited after it, is denied;
  * `scoped_receipts off` makes the feature branch need a full receipt again;
  * `gt_test_receipt.py check` without --allow-scoped (the release gates' question) ignores
    scoped receipts entirely, and `latest` still returns only full ones;
  * recording a scoped receipt without the files it covers is refused.
"""
import json
import time
import unittest

from test_guard_test_before_commit import CommitGuardBase


class ScopedBase(CommitGuardBase):
    def setUp(self):
        super().setUp()
        self.with_test_entry_point()
        self.run_cmd(["git", "-C", str(self.repo), "checkout", "-q", "-b", "feature"])

    def scoped(self, *files):
        lst = self.tmp / "affected.json"
        lst.write_text(json.dumps({"files": list(files)}))
        p = self.py(self.hooks / "gt_test_receipt.py", "record", "--repo", str(self.repo),
                    "--what", "tests/run.sh --affected", "--ok", "--scope", "scoped",
                    "--files-from", lst)
        self.assertOk(p)

    def denied(self, hso):
        return hso.get("permissionDecision") == "deny"


class ScopedReceipts(ScopedBase):
    def test_feature_branch_accepts_a_covering_scoped_receipt(self):
        self.write("a.py")
        self.stage("a.py")
        time.sleep(1.1)
        self.scoped("a.py")
        self.assertFalse(self.denied(self.decide()), "a covering scoped receipt was refused")

    def test_default_branch_refuses_it(self):
        self.run_cmd(["git", "-C", str(self.repo), "checkout", "-q", "main"])
        self.write("a.py")
        self.stage("a.py")
        time.sleep(1.1)
        self.scoped("a.py")
        self.assertTrue(self.denied(self.decide()), "the default branch took a scoped receipt")

    def test_unnamed_or_stale_files_are_refused(self):
        self.write("a.py")
        self.write("b.py")
        self.stage("a.py", "b.py")
        time.sleep(1.1)
        self.scoped("a.py")
        d = self.decide()
        self.assertTrue(self.denied(d))
        self.assertIn("b.py", d["permissionDecisionReason"])
        self.scoped("a.py", "b.py")
        self.assertFalse(self.denied(self.decide()))
        time.sleep(1.1)
        self.write("a.py", "x = 2\n")
        self.stage("a.py")
        self.assertTrue(self.denied(self.decide()), "a stale scoped receipt was accepted")

    def test_setting_off_needs_a_full_receipt(self):
        self.config(vault_path=str(self.tmp), scoped_receipts="off")
        self.write("a.py")
        self.stage("a.py")
        time.sleep(1.1)
        self.scoped("a.py")
        self.assertTrue(self.denied(self.decide()))
        self.receipt()                     # a FULL receipt still works
        self.assertFalse(self.denied(self.decide()))

    def test_release_check_and_latest_ignore_scoped(self):
        self.write("a.py")
        time.sleep(1.1)
        self.scoped("a.py")
        tool = self.hooks / "gt_test_receipt.py"
        p = self.py(tool, "check", "--repo", str(self.repo), "--files", "a.py")
        self.assertEqual(p.returncode, 1, "a release-gate check accepted a scoped receipt")
        p = self.py(tool, "check", "--repo", str(self.repo), "--files", "a.py",
                    "--allow-scoped")
        self.assertOk(p)
        p = self.py(tool, "latest", "--repo", str(self.repo))
        self.assertEqual(p.returncode, 1, "latest returned a scoped receipt")

    def test_scoped_needs_its_files(self):
        p = self.py(self.hooks / "gt_test_receipt.py", "record", "--repo", str(self.repo),
                    "--what", "x", "--ok", "--scope", "scoped")
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
