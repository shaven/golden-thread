"""guard_test_before_commit.sh / .py -- the PreToolUse hook for core_test_before_commit.

Contract:
  * Input: the PreToolUse payload on stdin. Output: ALWAYS one JSON object with
    hookSpecificOutput.hookEventName == PreToolUse and permissionDecision allow|deny;
    exit 0.
  * Deny `git commit` when the repo has CODE staged, has a discoverable test command,
    and no passing receipt newer than every staged file.
  * Allow: docs-only commits, a repo with `.gt-no-test-gate`, GT_TEST_GATE=off,
    test_gate=off, a repo with no test entry point under `auto`, a non-repo cwd, any
    command that is not a commit, any tool that is not Bash.
  * FAIL OPEN on everything it cannot parse -- it sits in front of every Bash call.
"""
import json
import shutil
import subprocess
import time
import unittest

from _harness import Sandbox, HOOKS, SCRIPTS, load_module


class CommitGuardBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        for f in HOOKS.iterdir():
            if f.is_file():
                shutil.copy2(f, self.hooks / f.name)
        # install.sh also copies these from scripts/; the guard imports both at run time.
        comp = load_module(SCRIPTS / "gt_components.py", "gt_components_for_commit_guard")
        for name in ("gt_paths.py",) + tuple(comp.HOOK_DIR_SCRIPTS):
            src = SCRIPTS / name
            if src.is_file():
                shutil.copy2(src, self.hooks / name)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env.pop("GT_TEST_GATE", None)
        self.repo = self.tmp / "repo"
        self.git_init(self.repo)
        self.config(vault_path=str(self.tmp))

    # -- fixture helpers ------------------------------------------------------------
    def write(self, rel, text="x = 1\n"):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def stage(self, *rels):
        self.run_cmd(["git", "-C", str(self.repo), "add", *rels])

    def with_test_entry_point(self):
        """A discoverable way to run tests, which is what `auto` keys off."""
        self.write("pytest.ini", "[pytest]\n")
        self.stage("pytest.ini")
        # COMMIT it: a staged pytest.ini is itself code by this gate's reckoning (it
        # changes what the tests do), so leaving it staged would make every case below
        # a code commit and the docs-only case untestable.
        self.run_cmd(["git", "-C", str(self.repo), "commit", "-q", "-m", "test entry point"])

    def receipt(self, ok=True, repo=None):
        tool = self.hooks / "gt_test_receipt.py"
        p = self.py(tool, "record", "--repo", str(repo or self.repo),
                    "--what", "tests/run.sh", "--ok" if ok else "--failed")
        self.assertOk(p, "recording a receipt must succeed")

    # -- driving the hook -----------------------------------------------------------
    def decide(self, command="git commit -m 'x'", tool="Bash", cwd=None, env=None):
        payload = {"session_id": "caller", "hook_event_name": "PreToolUse",
                   "tool_name": tool, "tool_input": {"command": command},
                   "cwd": str(cwd or self.repo)}
        proc = self.sh(self.hooks / "guard_test_before_commit.sh",
                       input=json.dumps(payload), env=env or self.env)
        self.assertOk(proc, "the guard must always exit 0")
        try:
            out = json.loads(proc.stdout)
        except ValueError as exc:
            self.fail(f"guard stdout is not one JSON object ({exc}):\n{proc.stdout!r}")
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        return hso

    def assertDenied(self, msg, **kw):
        hso = self.decide(**kw)
        self.assertEqual(hso["permissionDecision"], "deny", msg)
        return hso

    def assertAllowed(self, msg, **kw):
        hso = self.decide(**kw)
        self.assertEqual(hso["permissionDecision"], "allow", msg)
        return hso


class CommitGuard(CommitGuardBase):
    # ---- the case the rule exists for ---------------------------------------------

    def test_staged_code_with_no_receipt_is_denied(self):
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        hso = self.assertDenied("staged code with no test run must not commit")
        reason = hso["permissionDecisionReason"]
        self.assertIn("core_test_before_commit", reason)
        for expected in ("gt_test_receipt.py", ".gt-no-test-gate", "GT_TEST_GATE=off"):
            self.assertIn(expected, reason,
                          "a denial must name every way out, not just refuse")

    def test_a_passing_receipt_allows_the_commit(self):
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        self.receipt()
        self.assertAllowed("a passing receipt newer than the staged files is the evidence")

    def test_editing_after_the_run_invalidates_the_receipt(self):
        """The 2026-09-12 stale-manifest shape: tests pass, then the tree changes."""
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        self.receipt()
        time.sleep(1.1)                      # mtime granularity
        self.write("app.py", "x = 2\n")
        self.stage("app.py")
        hso = self.assertDenied("a file edited after the run is not covered by it")
        self.assertIn("app.py", hso["permissionDecisionReason"],
                      "name the file that went stale -- 'tests are stale' is unactionable")

    def test_a_failing_receipt_is_not_evidence(self):
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        self.receipt(ok=False)
        self.assertDenied("a FAILED run must never satisfy the gate")

    # ---- the escapes ---------------------------------------------------------------

    def test_repo_opt_out_file_allows_everything(self):
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        (self.repo / ".gt-no-test-gate").write_text("", encoding="utf-8")
        self.assertAllowed("a repo carrying .gt-no-test-gate is exempt")

    def test_env_escape_allows_one_commit(self):
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        env = dict(self.env, GT_TEST_GATE="off")
        self.assertAllowed("GT_TEST_GATE=off is the one-command escape", env=env)

    def test_setting_off_allows(self):
        self.config(vault_path=str(self.tmp), test_gate="off")
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        self.assertAllowed("test_gate=off disables the gate for this machine")

    def test_warn_mode_allows_but_says_so(self):
        self.config(vault_path=str(self.tmp), test_gate="warn")
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        hso = self.assertAllowed("warn mode must not block", )
        self.assertIn("core_test_before_commit", hso.get("additionalContext", ""),
                      "warn mode has to actually say something, or it is just off")

    # ---- where it must stay quiet --------------------------------------------------

    def test_docs_only_commit_is_allowed(self):
        self.with_test_entry_point()
        self.write("README.md", "# hi\n")
        self.stage("README.md")
        self.assertAllowed("a docs-only commit cannot break a test")

    def test_repo_without_tests_is_allowed_under_auto(self):
        self.write("app.py")
        self.stage("app.py")
        self.assertAllowed("auto does not demand tests from a repo that has none")

    def test_repo_without_tests_is_denied_under_block(self):
        self.config(vault_path=str(self.tmp), test_gate="block")
        self.write("app.py")
        self.stage("app.py")
        self.assertDenied("block mode holds every repo to it, tests or not")

    def test_non_commit_git_commands_are_allowed(self):
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        for cmd in ("git status", "git log --oneline -5", "git diff --cached",
                    "echo 'git commit' >> notes.txt",
                    "git commit --help"):
            self.assertAllowed("not a commit of code: %r" % cmd, command=cmd)

    def test_a_quoted_mention_is_not_a_commit(self):
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        self.assertAllowed(
            "a heredoc that merely mentions the command is data, not a commit",
            command="cat > doc.md <<'EOF'\nrun git commit -m x\nEOF")

    def test_other_tools_and_bad_input_fail_open(self):
        self.with_test_entry_point()
        self.write("app.py")
        self.stage("app.py")
        self.assertAllowed("only Bash commands are inspected",
                           tool="Write", command="git commit -m x")
        for stdin in ("", "{not json", "[]"):
            payload = stdin
            proc = self.sh(self.hooks / "guard_test_before_commit.sh",
                           input=payload, env=self.env)
            self.assertOk(proc, "malformed input must still exit 0")
            self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]
                             ["permissionDecision"], "allow",
                             "unparseable payload must fail OPEN")

    def test_outside_a_repo_is_allowed(self):
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        self.assertAllowed("nothing to gate outside a git repo", cwd=outside)

    def test_nothing_staged_is_allowed(self):
        self.with_test_entry_point()
        self.assertAllowed("an empty commit carries no code")


class ReceiptTool(CommitGuardBase):
    def test_record_latest_and_check_round_trip(self):
        tool = self.hooks / "gt_test_receipt.py"
        self.write("app.py")
        p = self.py(tool, "check", "--repo", str(self.repo), "--files",
                    str(self.repo / "app.py"))
        self.assertNotEqual(p.returncode, 0, "no receipt yet")
        self.receipt()
        p = self.py(tool, "latest", "--repo", str(self.repo))
        self.assertOk(p)
        self.assertEqual(json.loads(p.stdout)["what"], "tests/run.sh")
        p = self.py(tool, "check", "--repo", str(self.repo), "--files",
                    str(self.repo / "app.py"))
        self.assertOk(p, "a passing receipt newer than the file covers it")

    def test_a_receipt_is_scoped_to_its_repo(self):
        other = self.tmp / "other"
        self.git_init(other)
        self.receipt()
        p = self.py(self.hooks / "gt_test_receipt.py", "latest", "--repo", str(other))
        self.assertNotEqual(p.returncode, 0,
                            "one repo's passing tests say nothing about another's")


if __name__ == "__main__":
    unittest.main()
