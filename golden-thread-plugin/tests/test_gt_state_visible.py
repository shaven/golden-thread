"""gt_state's write-out is visible to the user, and a session sees only its own context figure
(0.18.1, 2026-10-01-gt-state-write-out-visible-to-the-user).

A hook's plain stdout reaches the model only. These run gt_state the way Claude Code does --
with the hook's JSON payload on stdin -- and assert on the `systemMessage` field, which is
what reaches the terminal:

  * a write emits exactly one systemMessage line with the path and the percentage
  * PreCompact always emits one: wrote, or did not and why
  * a below-threshold turn emits none
  * a failed write emits one saying it failed
  * a fresh session never reports another session's context figure (the shared ledger used to
    hand the newest row, whoever wrote it, to whoever asked)
"""
import json

from _harness import SCRIPTS
from test_gt_state import StateBase

STATE = SCRIPTS / "gt_state.py"
OLD = "aaaaaaaa-1111-2222-3333-444444444444"
NEW = "bbbbbbbb-5555-6666-7777-888888888888"


class HookOutputBase(StateBase):
    def payload(self, event, session=OLD):
        return json.dumps({"hook_event_name": event, "session_id": session,
                           "transcript_path": "/dev/null", "cwd": str(self.tmp)})

    def run_hook(self, args, event, session=OLD):
        p = self.py(STATE, *args, input=self.payload(event, session), cwd=str(self.tmp))
        self.assertOk(p)
        return p

    def messages(self, proc):
        """Every systemMessage on stdout (hook JSON is one object per line)."""
        out = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                d = json.loads(line)
                if "systemMessage" in d:
                    out.append(d["systemMessage"])
        return out

    def break_state_dir(self):
        """A FILE where the state directory should be: mkdir fails even for root."""
        d = self.home / ".claude" / "golden-thread"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state").write_text("not a directory\n")


class UserPromptSubmitIsVisibleOnlyWhenItActs(HookOutputBase):
    def test_a_write_emits_exactly_one_line_with_path_and_percentage(self):
        self.ledger([self.reading(91, session=OLD[:8])])
        p = self.run_hook(["check", "--write"], "UserPromptSubmit")
        msgs = self.messages(p)
        self.assertEqual(1, len(msgs), p.stdout)
        self.assertEqual(1, len(msgs[0].splitlines()), msgs[0])
        files = self.state_files()
        self.assertEqual(1, len(files))
        self.assertIn(str(files[0]), msgs[0])
        self.assertIn("91%", msgs[0])

    def test_below_threshold_emits_no_system_message(self):
        self.ledger([self.reading(40, session=OLD[:8])])
        p = self.run_hook(["check", "--write"], "UserPromptSubmit")
        self.assertEqual([], self.messages(p), p.stdout)
        self.assertIn("not yet", p.stdout, "the model line should stay")

    def test_at_the_threshold_it_writes_and_says_so(self):
        self.ledger([self.reading(80, session=OLD[:8])])        # normal: 85 - 5
        p = self.run_hook(["check", "--write"], "UserPromptSubmit")
        self.assertEqual(1, len(self.messages(p)), p.stdout)

    def test_after_the_write_later_turns_are_silent_to_the_user(self):
        self.ledger([self.reading(91, session=OLD[:8])])
        self.run_hook(["check", "--write"], "UserPromptSubmit")
        p = self.run_hook(["check", "--write"], "UserPromptSubmit")
        self.assertEqual([], self.messages(p), p.stdout)

    def test_a_failed_write_says_it_failed(self):
        self.ledger([self.reading(91, session=OLD[:8])])
        self.break_state_dir()
        p = self.run_hook(["check", "--write"], "UserPromptSubmit")
        msgs = self.messages(p)
        self.assertEqual(1, len(msgs), p.stdout + p.stderr)
        self.assertIn("could NOT be written", msgs[0])

    def test_run_by_hand_it_prints_plain_text(self):
        self.ledger([self.reading(91, session=OLD[:8])])
        p = self.py(STATE, "check", "--write", input="")
        self.assertOk(p)
        self.assertNotIn("systemMessage", p.stdout)
        self.assertIn("written to", p.stdout)


class PreCompactAlwaysSpeaks(HookOutputBase):
    def test_precompact_wrote(self):
        self.ledger([self.reading(20, session=OLD[:8])])
        p = self.run_hook(["hook"], "PreCompact")
        msgs = self.messages(p)
        self.assertEqual(1, len(msgs), p.stdout)
        self.assertIn("written to", msgs[0])
        self.assertIn(str(self.state_files()[0]), msgs[0])
        self.assertIn("20%", msgs[0])

    def test_precompact_with_no_ledger_still_speaks(self):
        p = self.run_hook(["hook"], "PreCompact")
        msgs = self.messages(p)
        self.assertEqual(1, len(msgs), p.stdout)
        self.assertIn("context unknown", msgs[0])

    def test_precompact_failed_write_says_why(self):
        self.ledger([self.reading(20, session=OLD[:8])])
        self.break_state_dir()
        p = self.run_hook(["hook"], "PreCompact")
        msgs = self.messages(p)
        self.assertEqual(1, len(msgs), p.stdout)
        self.assertIn("NOT written", msgs[0])
        self.assertTrue("Error" in msgs[0] or "error" in msgs[0], msgs[0])


class ANewSessionNeverInheritsAFigure(HookOutputBase):
    def test_the_previous_sessions_88_percent_is_not_reported(self):
        """The 2026-09-29 observation: a fresh session's first prompt said 88%, already written."""
        self.ledger([self.reading(88, session=OLD[:8])])
        self.run_hook(["check", "--write"], "UserPromptSubmit", session=OLD)
        p = self.run_hook(["check", "--write"], "UserPromptSubmit", session=NEW)
        self.assertNotIn("88", p.stdout + p.stderr)
        self.assertNotIn("already written", p.stdout + p.stderr)
        self.assertIn("cannot tell", p.stderr)
        self.assertEqual([], self.messages(p))

    def test_the_new_sessions_own_reading_is_used_even_when_older_rows_are_newer(self):
        self.ledger([self.reading(30, session=NEW[:8]), self.reading(95, session=OLD[:8])])
        p = self.py(STATE, "check", "--json", input=self.payload("UserPromptSubmit", NEW))
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual((30, False), (out["ctx_pct"], out["due"]))

    def test_session_flag_filters_too(self):
        self.ledger([self.reading(30, session=NEW[:8]), self.reading(95, session=OLD[:8])])
        p = self.py(STATE, "check", "--json", "--session", NEW, input="")
        self.assertEqual(30, json.loads(p.stdout)["ctx_pct"])

    def test_precompact_does_not_borrow_a_figure_either(self):
        self.ledger([self.reading(88, session=OLD[:8])])
        p = self.run_hook(["hook"], "PreCompact", session=NEW)
        self.assertNotIn("88%", self.messages(p)[0])
        self.assertIn("context unknown", self.messages(p)[0])


class CannotTellIsNotSaidEveryTurn(HookOutputBase):
    """0.20.1 (usability run, Linux): "no usage ledger at ... (is the usage module installed?)"
    was printed on EVERY prompt. As a hook it is now said at most once per session, and not at
    all when there is no ledger -- the usage module being off is a choice, not a fault."""

    def test_no_ledger_is_silent_as_a_hook(self):
        p = self.run_hook(["check", "--write"], "UserPromptSubmit", session=NEW)
        self.assertNotIn("cannot tell", p.stderr)
        self.assertNotIn("usage ledger", p.stdout + p.stderr)
        self.assertEqual([], self.messages(p))

    def test_a_ledger_with_nothing_for_this_session_is_said_once(self):
        self.ledger([self.reading(50, session=OLD[:8])])
        first = self.run_hook(["check", "--write"], "UserPromptSubmit", session=NEW)
        self.assertIn("cannot tell", first.stderr)
        second = self.run_hook(["check", "--write"], "UserPromptSubmit", session=NEW)
        self.assertNotIn("cannot tell", second.stderr)
