"""guard_vault_writes.sh / .py -- the PreToolUse hook for core_explicit_vault_target.

Contract:
  * Input: the PreToolUse payload on stdin. Output: ALWAYS one JSON object with
    hookSpecificOutput.hookEventName == PreToolUse and permissionDecision allow|deny;
    exit 0.
  * Deny a Bash command that invokes a vault tool with a WRITING subcommand and names
    no target: no --vault, no --dry-run, no GT_VAULT (inline or in the environment).
  * Read-only subcommands (status, list, check) are never denied.
  * FAIL OPEN: malformed input, other tools, empty command, unparseable shell, an
    unknown tool or an unknown subcommand -> allow. This hook sits in front of every
    Bash call, so a wrong deny costs more than a missed one.
"""
import json
import shutil
import unittest

from _harness import Sandbox, HOOKS


class GuardBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        for f in HOOKS.iterdir():
            if f.is_file():
                shutil.copy2(f, self.hooks / f.name)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env.pop("GT_VAULT", None)

    def decide_raw(self, stdin, env=None):
        proc = self.sh(self.hooks / "guard_vault_writes.sh", input=stdin, env=env or self.env)
        self.assertOk(proc, "the guard must always exit 0")
        try:
            out = json.loads(proc.stdout)
        except ValueError as exc:
            self.fail(f"guard stdout is not one JSON object ({exc}):\n{proc.stdout!r}")
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        return hso

    def decide(self, command, tool="Bash", env=None):
        payload = {"session_id": "caller", "hook_event_name": "PreToolUse",
                   "tool_name": tool, "tool_input": {"command": command}}
        return self.decide_raw(json.dumps(payload), env=env)

    def assertDenied(self, command, msg, env=None):
        hso = self.decide(command, env=env)
        self.assertEqual(hso["permissionDecision"], "deny", msg)
        return hso

    def assertAllowed(self, command, msg, tool="Bash", env=None):
        hso = self.decide(command, tool=tool, env=env)
        self.assertEqual(hso["permissionDecision"], "allow", msg)
        return hso


class GuardVaultWrites(GuardBase):
    # ---- the incident this hook exists for -------------------------------------

    def test_the_2026_09_11_command_is_denied(self):
        """The exact shape that migrated the live vault during a rehearsal."""
        hso = self.assertDenied(
            "python3 Projects/golden-thread/tools/gt_adr.py migrate myproject",
            "gt_adr.py migrate with no target is the command that hit the live vault")
        self.assertIn("core_explicit_vault_target", hso["permissionDecisionReason"])
        self.assertIn("--vault", hso["permissionDecisionReason"],
                      "the denial must name the flag that fixes it, not just refuse")

    def test_rehearsal_loop_shape_is_denied(self):
        """cd to a copy first: the cd does NOT make gt_adr.py write the copy."""
        self.assertDenied(
            "cd /tmp/rehearsal && python3 Projects/golden-thread/tools/gt_adr.py migrate proj",
            "a cd into the copy is exactly the false safety that caused the incident")

    # ---- naming the target satisfies the rule -----------------------------------

    def test_vault_flag_allows(self):
        self.assertAllowed("python3 tools/gt_adr.py migrate proj --vault /tmp/copy",
                           "--vault names the target, which is the whole requirement")

    def test_vault_equals_form_allows(self):
        self.assertAllowed("python3 tools/gt_adr.py migrate proj --vault=/tmp/copy",
                           "--vault=X is the same statement as --vault X")

    def test_dry_run_allows(self):
        self.assertAllowed("python3 tools/gt_log.py migrate --dry-run",
                           "a dry run writes nothing, so it needs no target")

    def test_inline_gt_vault_allows(self):
        self.assertAllowed("GT_VAULT=/tmp/copy python3 tools/gt_adr.py merge proj",
                           "GT_VAULT in front of the command names the target")

    def test_exported_gt_vault_allows(self):
        env = dict(self.env, GT_VAULT="/tmp/copy")
        self.assertAllowed("python3 tools/gt_adr.py merge proj",
                           "a session opened against one vault has already said which",
                           env=env)

    def test_empty_gt_vault_does_not_count(self):
        env = dict(self.env, GT_VAULT="   ")
        self.assertDenied("python3 tools/gt_adr.py merge proj",
                          "a blank GT_VAULT names nothing and must not satisfy the rule",
                          env=env)

    # ---- read-only work is never in the way ------------------------------------

    def test_status_allowed(self):
        self.assertAllowed("python3 tools/gt_log.py status",
                           "status writes nothing; blocking it would make the hook hated")

    def test_list_allowed(self):
        self.assertAllowed("python3 tools/gt_adr.py status myproject",
                           "status is read-only for gt_adr too")

    def test_unknown_subcommand_allows(self):
        self.assertAllowed("python3 tools/gt_adr.py explain proj",
                           "an unrecognised subcommand is an uncertainty, and those allow")

    # ---- fail open --------------------------------------------------------------

    def test_non_bash_tool_allows(self):
        self.assertAllowed("anything at all", "only Bash carries a command line",
                           tool="Write")

    def test_unrelated_command_allows(self):
        self.assertAllowed("git status && ls -la", "no vault tool named -> not our business")

    def test_empty_command_allows(self):
        self.assertAllowed("", "nothing to inspect -> allow")

    def test_malformed_json_allows(self):
        hso = self.decide_raw("{not json")
        self.assertEqual(hso["permissionDecision"], "allow",
                         "unparseable input must fail open, never deny")

    def test_unbalanced_quotes_allow(self):
        self.assertAllowed('python3 tools/gt_adr.py migrate "proj',
                           "shell we cannot parse is an uncertainty -> allow")

    def test_similarly_named_file_is_not_a_vault_tool(self):
        self.assertAllowed("python3 my_gt_adr.py.bak migrate proj",
                           "only the exact tool basenames count")

    # ---- multi-command lines ----------------------------------------------------

    def test_mutator_in_second_segment_is_denied(self):
        self.assertDenied("echo hi && python3 tools/gt_tasks.py",
                          "every segment of the command line is inspected, not just the first")

    def test_targeted_segment_does_not_excuse_an_untargeted_one(self):
        self.assertDenied(
            "python3 tools/gt_log.py merge --vault /tmp/copy; python3 tools/gt_adr.py merge p",
            "a --vault on one command says nothing about the next one")

    def test_gt_tasks_always_writes(self):
        self.assertDenied("python3 Projects/golden-thread/tools/gt_tasks.py",
                          "gt_tasks.py rewrites TASKS.md however it is invoked")

    def test_vault_init_destructive_mode_denied(self):
        self.assertDenied("python3 scripts/vault_init.py merge-project a b",
                          "merge-project moves files; it must say which vault")


class HeredocsAreDataNotCommands(GuardBase):
    """2026-09-12: the guard denied a command that was WRITING a script containing the
    text of a vault-tool call. A guard that fires on a quoted mention is one people
    switch off, and then it guards nothing."""

    def test_a_tool_named_inside_a_heredoc_is_not_a_command(self):
        cmd = ("cat > patch.sh <<'EOF'\n"
               "python3 scripts/vault_init.py install-core-rules \\\n"
               "  --vault \"$VAULT_PATH\"\n"
               "EOF")
        self.assertAllowed(cmd, "text being written is not a command being run")

    def test_a_real_command_after_a_heredoc_is_still_inspected(self):
        cmd = ("cat > notes.txt <<'EOF'\n"
               "just some notes\n"
               "EOF\n"
               "python3 tools/gt_adr.py migrate proj")
        self.assertDenied(cmd, "the guard stopped looking after the heredoc ended")

    def test_a_real_command_before_a_heredoc_is_still_inspected(self):
        cmd = ("python3 tools/gt_tasks.py\n"
               "cat > notes.txt <<'EOF'\n"
               "text\n"
               "EOF")
        self.assertDenied(cmd, "a command before the heredoc was skipped")

    def test_unterminated_heredoc_fails_open(self):
        cmd = ("cat > f <<'EOF'\n"
               "python3 tools/gt_adr.py migrate proj\n")
        self.assertAllowed(cmd, "an unterminated heredoc must not deny; it allows")

    def test_unquoted_heredoc_delimiter_is_handled(self):
        cmd = ("cat > f <<EOF\n"
               "python3 tools/gt_log.py migrate\n"
               "EOF")
        self.assertAllowed(cmd, "<<EOF without quotes is still a heredoc body")


if __name__ == "__main__":
    unittest.main()
