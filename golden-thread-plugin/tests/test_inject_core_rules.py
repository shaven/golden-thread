"""inject_core_rules.sh -- the UserPromptSubmit hook (Core rules, Reminder tier).

Contract:
  * stdout is one JSON object: hookSpecificOutput.hookEventName == UserPromptSubmit and
    additionalContext starting "Current date and time: <YYYY-MM-DD HH:MM TZ>".
  * Healthy: lists every core_*.md rule with level: core and an imperative (minus the
    priority-model file and any `inject: false`), Validated rules first. The text is
    read from the vault at run time, so editing a rule changes what is injected.
  * Vault unreachable: STILL emits the timestamp, plus a loud ENFORCEMENT DEGRADED
    banner and "Begin your reply with this exact timestamp: <stamp>".
  * Exit 0 always; stdin (the prompt payload) is not needed.

The hooks are installed into the sandbox HOME at ~/.claude/golden-thread/hooks/, the
path settings.json references, and the vault is found through the sandbox
vault-config.json.
"""
import json
import re
import shutil
import unittest

from _harness import Sandbox, HOOKS, TEMPLATES

STAMP_RE = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+"
CORE = "Projects/golden-thread/core-rules"
PAYLOAD = json.dumps({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
                      "prompt": "hello", "cwd": "/"})


class InjectTestBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        for f in HOOKS.iterdir():
            if f.is_file():
                shutil.copy2(f, self.hooks / f.name)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def inject(self, stdin=PAYLOAD, env=None):
        proc = self.sh(self.hooks / "inject_core_rules.sh", input=stdin, env=env)
        self.assertOk(proc, "the injector must always exit 0")
        try:
            out = json.loads(proc.stdout)
        except ValueError as exc:
            self.fail(f"injector stdout is not valid JSON ({exc}):\n{proc.stdout}")
        hso = out.get("hookSpecificOutput", {})
        self.assertEqual(hso.get("hookEventName"), "UserPromptSubmit")
        ctx = hso.get("additionalContext", "")
        self.assertRegex(ctx, r"^Current date and time: " + STAMP_RE,
                         "the timestamp line must open the injected context")
        return ctx

    def stamp(self, ctx):
        return re.match(r"^Current date and time: (" + STAMP_RE + ")", ctx).group(1)

    def assertDegraded(self, ctx, reason=None):
        self.assertIn("ENFORCEMENT DEGRADED", ctx)
        self.assertNotIn("CORE RULES", ctx)
        self.assertIn(f"Begin your reply with this exact timestamp: {self.stamp(ctx)}", ctx,
                      "a degraded turn must still carry the timestamp imperative")
        if reason:
            self.assertIn(f"Reason: {reason}", ctx)


class InjectHealthyTest(InjectTestBase):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.core = self.vault / CORE

    def rule_lines(self, ctx):
        return re.findall(r"^\d+\. (.*)$", ctx, flags=re.M)

    def test_injects_every_shipped_core_rule(self):
        ctx = self.inject()
        self.assertNotIn("DEGRADED", ctx)
        self.assertIn(f"CORE RULES (always on, every turn, every project — see {CORE}):", ctx)
        lines = self.rule_lines(ctx)
        shipped = [p for p in (TEMPLATES / "core-rules").glob("core_*.md")
                   if p.name != "core_rule_priority_model.md"]
        self.assertEqual(len(lines), len(shipped),
                         f"one numbered line per shipped core rule; got:\n{ctx}")
        self.assertIn("Register your session and claim a vault file before writing it; "
                      "never write a file another live session has claimed.", lines)
        self.assertTrue(any(l.startswith("Begin every response with the current wall-clock "
                                         "timestamp") for l in lines))
        self.assertFalse(any("Rule Priority Model" in l or "which rules survive" in l
                             for l in lines), "the priority-model file is not a per-turn rule")

    def test_validated_rules_come_first(self):
        ctx = self.inject()
        lines = self.rule_lines(ctx)
        validated = {"Register your session", "Never put a secret", "Begin every response"}
        kinds = [any(l.startswith(v) for v in validated) for l in lines]
        self.assertEqual(sum(kinds), 3, f"expected the three Validated rules:\n{ctx}")
        self.assertEqual(kinds, sorted(kinds, reverse=True),
                         f"all Validated rules must precede Reminder rules:\n{ctx}")

    def test_rule_text_is_read_from_the_vault_at_run_time(self):
        p = self.core / "core_global_memory_scope.md"
        text = p.read_text(encoding="utf-8")
        text = text.replace("**`global-memory/` contains only facts needed in EVERY project.**",
                            "**Edited imperative from the vault.**")
        p.write_text(text, encoding="utf-8")
        ctx = self.inject()
        self.assertIn("Edited imperative from the vault.", self.rule_lines(ctx))

    def test_opt_out_and_non_core_rules_are_not_injected(self):
        (self.core / "core_zz_optout.md").write_text(
            "---\nlevel: core\nenforcement: reminder\ninject: false\n---\n\n**OPTED OUT RULE.**\n",
            encoding="utf-8")
        (self.core / "core_zz_context.md").write_text(
            "---\nlevel: context\nenforcement: reminder\n---\n\n**CONTEXT TIER RULE.**\n",
            encoding="utf-8")
        (self.core / "core_zz_noimperative.md").write_text(
            "---\nlevel: core\n---\n\nNo bold statement anywhere.\n", encoding="utf-8")
        (self.core / "core_zz_added.md").write_text(
            "---\nlevel: core\nenforcement: reminder\n---\n\n**NEWLY ADDED RULE.**\n",
            encoding="utf-8")
        ctx = self.inject()
        self.assertNotIn("OPTED OUT RULE", ctx)
        self.assertNotIn("CONTEXT TIER RULE", ctx)
        self.assertIn("NEWLY ADDED RULE.", self.rule_lines(ctx))

    def test_stdin_is_irrelevant(self):
        for stdin in ("", "not json at all {", "[1, 2, 3]"):
            with self.subTest(stdin=stdin):
                ctx = self.inject(stdin=stdin)
                self.assertIn("CORE RULES", ctx)

    def test_moved_core_rules_self_heal_and_are_recorded(self):
        new = self.vault / "Projects" / "memory-system" / "core-rules"
        new.parent.mkdir(parents=True)
        self.core.rename(new)
        ctx = self.inject()
        self.assertNotIn("DEGRADED", ctx)
        self.assertIn("see Projects/memory-system/core-rules", ctx)
        cfg = json.loads((self.home / ".claude" / "vault-config.json").read_text())
        self.assertEqual(cfg["core_rules_path"], "Projects/memory-system/core-rules")

    def test_only_the_model_file_left_is_degraded_not_silent(self):
        for p in self.core.glob("core_*.md"):
            if p.name != "core_rule_priority_model.md":
                p.unlink()
        self.assertDegraded(self.inject(), reason="no core rules resolved")


class InjectDegradedTest(InjectTestBase):
    def test_no_vault_config(self):
        self.assertDegraded(self.inject(), reason="core-rules folder not found")

    def test_vault_path_unreachable(self):
        self.config(vault_path=str(self.tmp / "dropbox-unmounted" / "vault"),
                    core_rules_path=CORE)
        self.assertDegraded(self.inject(), reason="core-rules folder not found")

    def test_malformed_vault_config(self):
        (self.home / ".claude" / "vault-config.json").write_text("{oops", encoding="utf-8")
        self.assertDegraded(self.inject())

    def test_vault_without_core_rules(self):
        v = self.tmp / "bare"
        (v / "Projects").mkdir(parents=True)
        self.config(vault_path=str(v))
        self.assertDegraded(self.inject(), reason="core-rules folder not found")

    def test_gt_paths_missing_is_degraded_with_reason(self):
        (self.hooks / "gt_paths.py").unlink()
        self.make_vault()
        ctx = self.inject()
        self.assertDegraded(ctx)
        self.assertIn("Reason: ModuleNotFoundError", ctx)

    def test_python_unavailable_still_emits_valid_json_with_timestamp(self):
        # The bash fallback (printf) is the last line of defence when python3 cannot
        # run at all. Its output must still be the JSON the hook contract promises.
        stub = self.tmp / "stub-bin"
        stub.mkdir()
        (stub / "python3").write_text("#!/bin/sh\nexit 127\n")
        (stub / "python3").chmod(0o755)
        proc = self.sh(self.hooks / "inject_core_rules.sh", input=PAYLOAD,
                       env={"PATH": f"{stub}:/usr/bin:/bin"})
        self.assertOk(proc)
        self.assertRegex(proc.stdout, r"Current date and time: " + STAMP_RE)
        self.assertIn("ENFORCEMENT DEGRADED", proc.stdout)
        try:
            out = json.loads(proc.stdout)
        except ValueError as exc:
            self.fail("DEFECT: with python3 unavailable the printf fallback emits invalid "
                      f"JSON ({exc}) -- its format string turns \\n into raw newlines inside "
                      "the additionalContext string, so the hook output is not the "
                      f"structured UserPromptSubmit object it claims to be:\n{proc.stdout}")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Begin your reply with this exact timestamp:", ctx)


if __name__ == "__main__":
    unittest.main()
