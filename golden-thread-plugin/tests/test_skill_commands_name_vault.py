"""Every vault-tool command a shipped skill tells the model to run names its vault.

Core rule core_explicit_vault_target, enforced by hooks/guard_vault_writes.py, denies a
Bash command that runs a writing vault-tool subcommand (the guard's MUTATORS) with no
--vault, --dry-run or GT_VAULT. A skill that tells the model to run such a command bare
steers it straight into that denial -- or, without the hook, into the real vault.

So every command in every shipped SKILL.md -- gt's own skills and the newest release of
every module and the wiki plugin -- is fed to the guard itself, with GT_VAULT unset, and
none may be denied. What counts as a command:
  * each line of a fenced code block, with backslash continuations joined;
  * each inline `code span` that has at least one word after the program -- so
    `vault_init.py create-project` is a command, while `gt_tasks.py` on its own is a
    mention of the file.
"""
import contextlib
import io
import json
import os
import re
import sys
import unittest

from _harness import GT, WIKI, HOOKS, DEMO, WATCH, REPORT_CARD, FARM, FLOW, load_module

ROOTS = [d for d in (GT, WIKI, DEMO, WATCH, REPORT_CARD, FARM, FLOW) if d is not None]
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")


def skill_files():
    out = []
    for root in ROOTS:
        skills = root / "skills"
        if skills.is_dir():
            out.extend(sorted(skills.glob("*/SKILL.md")))
    return out


def commands(text):
    """(line number, command) for every candidate command in one SKILL.md."""
    found, in_fence, pending, start = [], False, "", 0
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            if in_fence and pending:
                found.append((start, pending))
            in_fence, pending = not in_fence, ""
            continue
        if in_fence:
            stripped = line.strip()
            if not pending:
                start = n
            if stripped.endswith("\\"):
                pending += stripped[:-1] + " "
                continue
            pending += stripped
            if pending.strip():
                found.append((start, pending))
            pending = ""
        else:
            for span in INLINE.findall(line):
                if len(span.split()) >= 2:
                    found.append((n, span))
    return found


class SkillCommandsNameTheVault(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = load_module(HOOKS / "guard_vault_writes.py", "guard_for_skill_commands")

    def verdict(self, command):
        """Run the guard's own main() on a PreToolUse payload; the deny reason or None."""
        payload = json.dumps({"session_id": "t", "hook_event_name": "PreToolUse",
                              "tool_name": "Bash", "tool_input": {"command": command}})
        saved_stdin, saved_env = sys.stdin, os.environ.pop("GT_VAULT", None)
        out = io.StringIO()
        try:
            sys.stdin = io.StringIO(payload)
            with contextlib.redirect_stdout(out):
                try:
                    self.guard.main()
                except SystemExit:
                    pass
        finally:
            sys.stdin = saved_stdin
            if saved_env is not None:
                os.environ["GT_VAULT"] = saved_env
        if not out.getvalue().strip():
            return None
        hso = json.loads(out.getvalue())["hookSpecificOutput"]
        return hso["permissionDecisionReason"] if hso.get("permissionDecision") == "deny" \
            else None

    def mutating(self, command):
        """Does the guard see this command run a writing vault-tool subcommand?"""
        try:
            parts = self.guard.segments(command)
        except self.guard.Uncertain:
            return False
        for seg in parts:
            hit = self.guard.inspect(seg)
            if hit and self.guard.writes(hit[0], hit[1]):
                return True
        return False

    def test_the_harness_sees_a_denial(self):
        # If this stops failing the guard, the real assertion below proves nothing.
        self.assertIsNotNone(self.verdict(
            'python3 <vault>/Projects/golden-thread/tools/gt_log.py add "<the line>"'))
        self.assertIsNone(self.verdict(
            'python3 <vault>/Projects/golden-thread/tools/gt_log.py --vault "<vault>" add "x"'))

    def test_no_shipped_skill_command_is_denied(self):
        files = skill_files()
        self.assertTrue(any("/golden-thread/" in str(f) for f in files), "no gt skills found")
        checked, denied = 0, []
        for f in files:
            for n, cmd in commands(f.read_text(encoding="utf-8")):
                if not self.mutating(cmd):
                    continue
                checked += 1
                if self.verdict(cmd) is not None:
                    denied.append("%s:%d: %s" % (f, n, cmd))
        self.assertEqual(denied, [], "skill commands the guard would deny -- add "
                                     "--vault <vault>:\n  " + "\n  ".join(denied))
        # the extraction must actually be finding the commands it exists to check
        self.assertGreaterEqual(checked, 15, "only %d vault-writing commands extracted" % checked)


if __name__ == "__main__":
    unittest.main()
