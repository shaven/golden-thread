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

    def test_write_to_local_packs_asks(self):
        """Local packs are the highest-precedence registry tier and the only one no validator
        and no release gate ever sees. Security review 2026-09-16 wrote a pack that shadowed a
        core naming rule and the guard raised no objection at all."""
        p = self.vault / "Projects" / "golden-thread" / "packs"
        p.mkdir(parents=True, exist_ok=True)
        hso = self.assertAsk(self.guard(p / "naming.mine.pack.json"))
        self.assertIn("packs", hso["permissionDecisionReason"])

    def _case_insensitive(self, probe_dir):
        """Does THIS filesystem fold case? Asked, never assumed.

        The guard compares file identity (st_dev, st_ino), so it INHERITS the volume's
        semantics rather than imposing its own -- and that is right in both directions. On
        APFS/exFAT, PACKS/ and packs/ are one inode, so a case variant reaches the protected
        tier and must be caught. On ext4 they are two directories; the variant is not the
        local pack tier gt_registry reads, so there is nothing behind that spelling to bypass.

        This test assumed the macOS answer and so failed the first time the suite ran on Linux
        (CI, 2026-09-17). Detecting keeps a real assertion on both platforms rather than
        skipping one.
        """
        probe = probe_dir / "gt-case-probe"
        probe.mkdir(parents=True, exist_ok=True)
        return (probe_dir / "GT-CASE-PROBE").is_dir()

    def test_local_packs_guard_survives_a_case_variant_spelling(self):
        """realpath returns the CALLER's spelling on a case-insensitive volume, which is how
        Global-Memory/ and CORE-RULES/ slipped through in 0.15.0. Same path, same trap."""
        p = self.vault / "Projects" / "golden-thread" / "packs"
        p.mkdir(parents=True, exist_ok=True)

        # Filesystem-independent: `..` normalises to the real packs/ on any volume.
        self.assertAsk(self.guard(
            f"{self.vault}/Projects/alpha/../../Projects/golden-thread/packs/x.pack.json"))

        if self._case_insensitive(self.vault / "Projects" / "golden-thread"):
            self.assertAsk(
                self.guard(f"{self.vault}/Projects/golden-thread/PACKS/naming.mine.pack.json"),
                "a case variant reaches the same inode on this volume and must ask")
        else:
            # Case-sensitive: PACKS/ is a different directory and gt_registry never reads it
            # as the local tier. Assert the correctly-spelled path still asks, so this branch
            # proves something rather than merely tolerating a difference.
            self.assertAsk(
                self.guard(f"{self.vault}/Projects/golden-thread/packs/naming.mine.pack.json"),
                "the correctly-spelled local pack path must still ask")

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

    # -- case variants (0.15.1: identity compare, not realpath string) ----------------
    def _case_insensitive(self, base):
        """True when base/a and base/A are the same file on this volume."""
        probe = base / "gtprobe.case"
        try:
            probe.write_text("x", encoding="utf-8")
            return (base / "GTPROBE.CASE").exists()
        except OSError:
            return False
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    def test_case_variant_of_global_memory_asks(self):
        """`Global-Memory/` resolves to the protected `global-memory/` on a case-insensitive
        volume, so a write there must still prompt. Pre-0.15.1 the realpath string compare
        kept the caller's case and stayed silent -- the bug this fix closes."""
        if not self._case_insensitive(self.vault):
            self.skipTest("case-sensitive volume; variant names are distinct files")
        self.assertAsk(self.guard(self.vault / "Global-Memory" / "new.md"),
                       "case variant of global-memory must ask")

    def test_case_variant_of_core_rules_asks(self):
        if not self._case_insensitive(self.vault):
            self.skipTest("case-sensitive volume")
        variant = self.vault / "Projects" / "golden-thread" / "CORE-RULES" / "x.md"
        self.assertAsk(self.guard(variant, tool="Edit"),
                       "case variant of core-rules must ask")

    def test_case_variant_of_settings_json_asks(self):
        if not self._case_insensitive(self.home / ".claude"):
            self.skipTest("case-sensitive volume")
        self.assertAsk(self.guard(self.home / ".claude" / "Settings.json"),
                       "case variant of settings.json must ask")

    def test_case_variant_with_traversal_asks(self):
        """Identity compare and `..` handling together."""
        if not self._case_insensitive(self.vault):
            self.skipTest("case-sensitive volume")
        sneaky = f"{self.vault}/Projects/alpha/../../GLOBAL-MEMORY/x.md"
        self.assertAsk(self.guard(sneaky), "case variant reached via .. must ask")

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
