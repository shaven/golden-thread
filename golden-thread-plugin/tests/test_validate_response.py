"""validate_response.sh -- the Stop hook (Core rules, Validated tier).

Contract:
  * Reads the Stop payload on stdin, then the JSONL transcript at transcript_path.
  * The current turn starts at the last genuine user prompt (tool_result entries are
    type 'user' too, but do not start a turn).
  * core_no_secrets_in_transcript: any high-confidence secret in ANY assistant text of
    the turn -> {"decision":"block"}. Placeholders, variable references, redactions
    and the sudoers NOPASSWD tag are allowed.
  * core_timestamp_every_message: the FIRST assistant text of the turn must begin with
    YYYY-MM-DD HH:MM (markdown wrappers allowed) -> otherwise block.
  * Allow == exit 0 with no stdout. Block == exit 0 with the decision JSON.
  * FAIL OPEN: empty/malformed stdin, stop_hook_active, no/missing/garbled transcript,
    tool-only turn -> allow.

Secret-shaped fixtures are assembled at run time so this file itself never holds one.
"""
import json
import re
import shutil
import unittest

from _harness import Sandbox, HOOKS

TS = "2026-09-11 11:05 CDT"


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def tool_result(tid="t1"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def say(*texts, tool=False, string=False):
    if string:
        return {"type": "assistant", "message": {"role": "assistant", "content": texts[0]}}
    blocks = [{"type": "text", "text": t} for t in texts]
    if tool:
        blocks.append({"type": "tool_use", "id": "t1", "name": "Bash",
                       "input": {"command": "ls"}})
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def tool_only():
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}


# -- secret-shaped values, built so no literal secret sits in the source ------------
def _alnum(n):
    return ("aB3dE5fG7h" * 10)[:n]


LEAKS = {
    "GitHub token": "gh" + "p_" + _alnum(36),
    "Google OAuth client secret": "GOC" + "SPX-" + _alnum(28),
    "AWS access key id": "AK" + "IA" + "QWERTYUIOPASDFGH",
    "Slack token": "xo" + "xb-" + "1234567890-abcdefghijkl",
    "private key block": "-----BEGIN RSA " + "PRIVATE KEY-----",
    "keyword literal": "NEST_CLIENT_" + "SECRET=" + "q8Zr2Lm9Kx4w",
    "namespaced aws key": "aws_secret_access_" + "key = " + "wJalrXUtnFEMIK7MDENG",
    "quoted password": "db_pass" + 'word: "' + "Tr0ub4dor3x" + '"',
    "basic auth": "curl -u admin:" + "s3cr3tPw " + "https://host/api",
    "bearer": "Authorization: Bear" + "er " + _alnum(32),
}

HARMLESS = [
    "Set password = $DB_PASSWORD in the env file.",
    "api_key: <your-api-key>",
    "token=REDACTED",
    "client_secret = ${CLIENT_SECRET}",
    "password: xxxxxxxxxx",
    "curl -u admin:$ADMIN_PASS https://host/api",
    "Authorization: Bearer $TOKEN",
    "shaven ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart nginx",
    "token_count = 123456 and cache_key = abcdef123456",
    "The password is stored in the keychain; rotate the token monthly.",
    "Use GOCSPX-short as the prefix check.",
]


class ValidateResponseTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        for f in HOOKS.iterdir():
            if f.is_file():
                shutil.copy2(f, self.hooks / f.name)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.transcript = self.tmp / "transcript.jsonl"

    # -- running ----------------------------------------------------------------
    def run_hook(self, stdin):
        proc = self.sh(self.hooks / "validate_response.sh", input=stdin)
        self.assertOk(proc, "the Stop hook must always exit 0 (block is signalled in JSON)")
        return proc

    def stop(self, entries, **payload):
        self.transcript.write_text("".join(json.dumps(e) + "\n" for e in entries),
                                   encoding="utf-8")
        body = {"session_id": "s1", "hook_event_name": "Stop",
                "transcript_path": str(self.transcript), "stop_hook_active": False}
        body.update(payload)
        return self.run_hook(json.dumps(body))

    def assertAllowed(self, proc, msg=""):
        self.assertEqual(proc.stdout.strip(), "", f"expected allow (no output). {msg}")

    def assertBlocked(self, proc, rule, msg=""):
        self.assertTrue(proc.stdout.strip(), f"expected a block for {rule}, got allow. {msg}")
        out = json.loads(proc.stdout)
        self.assertEqual(out.get("decision"), "block")
        self.assertIn(rule, out.get("reason", ""))
        return out

    # -- timestamp rule -----------------------------------------------------------
    def test_timestamped_reply_is_allowed(self):
        self.assertAllowed(self.stop([user("hi"), say(f"{TS} — hello")]))

    def test_markdown_wrapped_timestamps_are_allowed(self):
        for first in (f"**{TS}**\n\nhello", f"## {TS}", f"`{TS}` hi", f"> {TS} quoted",
                      f"_{TS}_ hi", f"- {TS} bullet", "2026-09-11T11:05 iso form",
                      f"\n\n  {TS} leading whitespace"):
            with self.subTest(first=first):
                self.assertAllowed(self.stop([user("hi"), say(first)]))

    def test_missing_timestamp_is_blocked(self):
        self.assertBlocked(self.stop([user("hi"), say("Hello there.")]),
                           "core_timestamp_every_message")

    def test_date_without_time_is_blocked(self):
        self.assertBlocked(self.stop([user("hi"), say("2026-09-11 — hello")]),
                           "core_timestamp_every_message")

    def test_timestamp_not_at_start_is_blocked(self):
        self.assertBlocked(self.stop([user("hi"), say(f"Done. ({TS})")]),
                           "core_timestamp_every_message")

    def test_untimestamped_preamble_before_tool_call_is_blocked(self):
        entries = [user("hi"), say("Let me check that:", tool=True), tool_result(),
                   say(f"{TS} — here is the answer")]
        self.assertBlocked(self.stop(entries), "core_timestamp_every_message",
                           "the FIRST text of the turn is what must carry the timestamp")

    def test_tool_result_does_not_start_a_new_turn(self):
        entries = [user("hi"), say(f"{TS} — checking", tool=True), tool_result(),
                   say("Here is the answer, no stamp needed on the second block.")]
        self.assertAllowed(self.stop(entries))

    def test_previous_turns_timestamp_does_not_count(self):
        entries = [user("first"), say(f"{TS} — one"), user("second"), say("Two, unstamped.")]
        self.assertBlocked(self.stop(entries), "core_timestamp_every_message")

    def test_tool_only_first_then_text_is_checked(self):
        entries = [user("hi"), tool_only(), tool_result(), say("No stamp here.")]
        self.assertBlocked(self.stop(entries), "core_timestamp_every_message")

    def test_string_content_is_read(self):
        self.assertAllowed(self.stop([user("hi"), say(f"{TS} hi", string=True)]))
        self.assertBlocked(self.stop([user("hi"), say("no stamp", string=True)]),
                           "core_timestamp_every_message")

    def test_list_form_user_prompt_starts_a_turn(self):
        prompt = {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "second prompt"}]}}
        entries = [user("first"), say(f"{TS} one"), prompt, say("unstamped")]
        self.assertBlocked(self.stop(entries), "core_timestamp_every_message")

    def test_injected_stamp_satisfies_the_validator(self):
        # The injector and the validator must agree on the format: a reply that begins
        # with exactly what inject_core_rules.sh stamped must pass.
        inj = self.sh(self.hooks / "inject_core_rules.sh", input="{}")
        self.assertOk(inj)
        ctx = json.loads(inj.stdout)["hookSpecificOutput"]["additionalContext"]
        stamp = re.match(r"^Current date and time: (.+)$", ctx, flags=re.M).group(1)
        self.assertAllowed(self.stop([user("hi"), say(f"{stamp} — reply")]))

    # -- fail open ----------------------------------------------------------------
    def test_loop_guard_never_blocks_twice(self):
        leak = LEAKS["GitHub token"]
        self.assertAllowed(self.stop([user("hi"), say(f"no stamp {leak}")],
                                     stop_hook_active=True))

    def test_fail_open_on_bad_input(self):
        cases = {
            "empty stdin": "",
            "malformed stdin": "{not json",
            "json array": "[1, 2]",
            "no transcript_path": json.dumps({"hook_event_name": "Stop"}),
            "missing transcript": json.dumps({"transcript_path": str(self.tmp / "nope.jsonl")}),
        }
        for name, stdin in cases.items():
            with self.subTest(case=name):
                self.assertAllowed(self.run_hook(stdin))

    def test_fail_open_on_bad_transcript(self):
        self.transcript.write_text(json.dumps(user("hi")) + "\n{garbled line\n"
                                   + json.dumps(say("no stamp")) + "\n", encoding="utf-8")
        self.assertAllowed(self.run_hook(json.dumps({"transcript_path": str(self.transcript)})))
        self.assertAllowed(self.stop([]), "empty transcript")
        self.assertAllowed(self.stop([user("hi"), tool_only()]), "tool-only turn")
        self.assertAllowed(self.stop([user("hi")]), "no assistant entry yet")

    # -- secrets rule ---------------------------------------------------------------
    def test_secrets_are_blocked_even_with_a_timestamp(self):
        for name, leak in LEAKS.items():
            with self.subTest(leak=name):
                out = self.assertBlocked(self.stop([user("hi"), say(f"{TS} — value: {leak}")]),
                                         "core_no_secrets_in_transcript")
                self.assertNotIn(leak, out["reason"], "the block reason must not echo it")

    def test_secret_later_in_the_turn_is_blocked(self):
        entries = [user("hi"), say(f"{TS} — reading the file", tool=True), tool_result(),
                   say("It contains " + LEAKS["AWS access key id"])]
        self.assertBlocked(self.stop(entries), "core_no_secrets_in_transcript")

    def test_secret_in_an_earlier_turn_is_not_reblocked(self):
        entries = [user("one"), say(f"{TS} " + LEAKS["GitHub token"]),
                   user("two"), say(f"{TS} clean reply")]
        self.assertAllowed(self.stop(entries))

    def test_talking_about_credentials_is_allowed(self):
        for text in HARMLESS:
            with self.subTest(text=text):
                self.assertAllowed(self.stop([user("hi"), say(f"{TS} — {text}")]))


if __name__ == "__main__":
    unittest.main()
