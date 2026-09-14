"""guard_protected_paths.sh / .py -- the PreToolUse hook behind the protected_paths setting.

Contract:
  * Input: the PreToolUse payload on stdin. Always exit 0.
  * No objection -> EMPTY stdout (never permissionDecision "allow", which skips the
    user's permission prompt).
  * ask: Write/Edit/MultiEdit/NotebookEdit into <vault>/.../core-rules/,
    <vault>/global-memory/, ~/.claude/golden-thread/, or onto ~/.claude/settings.json.
  * deny: editing or overwriting an EXISTING file under <vault>/Sources/; a new file
    there passes.
  * Paths are realpath-normalised: `..` and symlinks do not get around the check.
  * protected_paths=off, other tools, malformed input, missing python target -> empty.
"""
import json
import os
import shutil
import unittest

from _harness import Sandbox, HOOKS, SCRIPTS

HOOK = "guard_protected_paths.sh"


class ProtectedPathsTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        for name in ("guard_protected_paths.sh", "guard_protected_paths.py"):
            shutil.copy2(HOOKS / name, self.hooks / name)
        # install.sh copies these from scripts/ into the hooks dir; the guard imports them.
        for name in ("gt_paths.py", "gt_settings.py"):
            shutil.copy2(SCRIPTS / name, self.hooks / name)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

        self.vault = self.tmp / "vault"
        self.core = self.vault / "Projects" / "golden-thread" / "core-rules"
        self.core.mkdir(parents=True)
        (self.core / "core_rule_priority_model.md").write_text("model\n", encoding="utf-8")
        (self.core / "core_example.md").write_text("rule\n", encoding="utf-8")
        (self.vault / "global-memory").mkdir()
        (self.vault / "Sources").mkdir()
        (self.vault / "Sources" / "existing.md").write_text("raw\n", encoding="utf-8")
        (self.vault / "Projects" / "alpha").mkdir(parents=True)
        self.config(vault_path=str(self.vault))

    # -- running ----------------------------------------------------------------------
    def run_raw(self, stdin, script=None):
        proc = self.sh(script or (self.hooks / HOOK), input=stdin)
        self.assertOk(proc, "the guard must always exit 0")
        return proc.stdout

    def guard(self, target, tool="Write", key="file_path"):
        payload = {"session_id": "caller", "hook_event_name": "PreToolUse",
                   "tool_name": tool, "tool_input": {key: str(target), "content": "x"}}
        return self.run_raw(json.dumps(payload))

    def decision(self, out):
        try:
            hso = json.loads(out)["hookSpecificOutput"]
        except (ValueError, KeyError) as exc:
            self.fail(f"expected one hookSpecificOutput JSON object ({exc}):\n{out!r}")
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertTrue(hso.get("permissionDecisionReason", "").strip(),
                        "an ask/deny must carry a reason")
        return hso

    def assertAsk(self, out, msg=""):
        hso = self.decision(out)
        self.assertEqual(hso["permissionDecision"], "ask", msg)
        return hso

    def assertDeny(self, out, msg=""):
        hso = self.decision(out)
        self.assertEqual(hso["permissionDecision"], "deny", msg)
        return hso

    def assertSilent(self, out, msg=""):
        self.assertEqual(out, "", f"no objection must print nothing. {msg}")

    # -- ask --------------------------------------------------------------------------
    def test_write_to_global_memory_asks(self):
        hso = self.assertAsk(self.guard(self.vault / "global-memory" / "new.md"))
        self.assertIn("global-memory", hso["permissionDecisionReason"])

    def test_edit_core_rule_asks(self):
        hso = self.assertAsk(self.guard(self.core / "core_example.md", tool="Edit"))
        self.assertIn("core-rules", hso["permissionDecisionReason"])

    def test_multiedit_and_notebookedit_are_inspected(self):
        self.assertAsk(self.guard(self.core / "core_example.md", tool="MultiEdit"))
        self.assertAsk(self.guard(self.vault / "global-memory" / "n.ipynb",
                                  tool="NotebookEdit", key="notebook_path"))

    def test_relocated_core_rules_found_by_marker(self):
        moved = self.vault / "Projects" / "renamed" / "core-rules"
        moved.mkdir(parents=True)
        (moved / "core_rule_priority_model.md").write_text("model\n", encoding="utf-8")
        self.assertAsk(self.guard(moved / "core_new.md"))

    def test_write_settings_json_asks(self):
        self.assertAsk(self.guard(self.home / ".claude" / "settings.json"))

    def test_write_into_gt_hooks_asks(self):
        self.assertAsk(self.guard(self.hooks / "guard_session_claims.sh"))

    def test_claude_paths_checked_without_a_vault(self):
        self.config()                                  # no vault_path
        self.assertAsk(self.guard(self.home / ".claude" / "settings.json"))
        self.assertSilent(self.guard(self.vault / "global-memory" / "x.md"))

    def test_dotdot_traversal_into_global_memory_asks(self):
        sneaky = f"{self.vault}/Projects/alpha/../../global-memory/x.md"
        self.assertAsk(self.guard(sneaky))

    def test_symlink_into_global_memory_asks(self):
        link = self.vault / "Projects" / "alpha" / "link.md"
        real = self.vault / "global-memory" / "real.md"
        real.write_text("fact\n", encoding="utf-8")
        os.symlink(real, link)
        self.assertAsk(self.guard(link, tool="Edit"))

    # -- deny / Sources ---------------------------------------------------------------
    def test_edit_existing_source_denied(self):
        hso = self.assertDeny(self.guard(self.vault / "Sources" / "existing.md", tool="Edit"))
        self.assertIn("supersede", hso["permissionDecisionReason"].lower())

    def test_overwrite_existing_source_denied(self):
        self.assertDeny(self.guard(self.vault / "Sources" / "existing.md", tool="Write"))

    def test_new_source_file_is_silent(self):
        self.assertSilent(self.guard(self.vault / "Sources" / "brand-new.md"))

    # -- silent -----------------------------------------------------------------------
    def test_ordinary_vault_file_is_silent(self):
        self.assertSilent(self.guard(self.vault / "Projects" / "alpha" / "research.md"))

    def test_read_tool_is_silent(self):
        self.assertSilent(self.guard(self.vault / "global-memory" / "x.md", tool="Read"))

    def test_setting_off_is_silent(self):
        self.config(vault_path=str(self.vault), protected_paths="off")
        self.assertSilent(self.guard(self.vault / "global-memory" / "x.md"))
        self.assertSilent(self.guard(self.home / ".claude" / "settings.json"))
        self.assertSilent(self.guard(self.vault / "Sources" / "existing.md", tool="Edit"))

    def test_malformed_json_is_silent(self):
        self.assertSilent(self.run_raw("{not json"))
        self.assertSilent(self.run_raw(""))
        self.assertSilent(self.run_raw('["a list"]'))

    def test_wrapper_with_python_missing_is_silent(self):
        (self.hooks / "guard_protected_paths.py").unlink()
        self.assertSilent(self.guard(self.vault / "global-memory" / "x.md"))


if __name__ == "__main__":
    unittest.main()
