"""gt_minimize.py and the gt-minimize skill.

Request 2026-09-23-gt-minimize-capture-and-cut, as rescoped by the owner (2026-10-01):
gt-minimize PRUNES -- measure, triage (keepers to Knowledge/INBOX), drop the rest, tell the user
to cut while the cache is warm. It writes NO in-flight note; carry-forward is gt-handoff's.

Contract:
  * The reported context is the billed size from the last usage row (input + cache read +
    cache write), not a character estimate; the peak since the last compaction beside it.
  * The per-source breakdown is labelled an estimate; attachment and system rows are
    attributed (as injected); the remainder is "unattributed", clamped at zero when the
    estimate exceeds the billed size.
  * Only rows after the last compact_boundary count.
  * Cache warmth uses the TTL the last turn WROTE at.
  * It never writes anything; with no way to know which session, it refuses (exit 2); with
    no transcript it could not run (exit 4).
  * No output carries a session id over 8 characters or an absolute path.
"""
import datetime
import hashlib
import json
import os
import unittest

from _harness import Sandbox, SCRIPTS, GT

TOOL = SCRIPTS / "gt_minimize.py"
SID = "cccccccc-1111-2222-3333-444444444444"


def ts(minutes_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return t.isoformat().replace("+00:00", "Z")


def turn(minutes_ago, read, write, req, w1h=0, inp=5):
    return {"type": "assistant", "sessionId": SID, "timestamp": ts(minutes_ago),
            "requestId": req,
            "message": {"id": "m" + req, "role": "assistant",
                        "content": [{"type": "text", "text": "x" * 400}],
                        "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": write,
                                  "cache_creation": {"ephemeral_1h_input_tokens": w1h,
                                                     "ephemeral_5m_input_tokens": write - w1h}}}}


def tree(root):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            h.update(p.encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


class MinimizeTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.dir = self.home / ".claude" / "projects" / "-Users-someone-private"
        self.dir.mkdir(parents=True)

    def transcript(self, rows):
        p = self.dir / ("%s.jsonl" % SID)
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p

    def measure(self, *args, sid=SID):
        r = self.py(TOOL, "--json", *args, env={"CLAUDE_CODE_SESSION_ID": sid} if sid else None)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_context_is_the_billed_size_of_the_last_turn(self):
        self.transcript([turn(10, 0, 50_000, "r1"), turn(2, 50_000, 3_000, "r2", inp=7)])
        m = self.measure()
        self.assertEqual(m["context_tokens"], 50_000 + 3_000 + 7)
        self.assertEqual(m["peak_context_tokens"], 53_007)

    def test_attachment_and_system_rows_are_attributed_and_the_rest_is_unattributed(self):
        rows = [{"type": "attachment", "sessionId": SID, "attachment": {"content": "h" * 4000}},
                {"type": "system", "sessionId": SID, "content": "s" * 4000},
                {"type": "user", "sessionId": SID,
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "content": "t" * 8000}]}},
                turn(1, 0, 100_000, "r1")]
        self.transcript(rows)
        e = self.measure()["estimate"]
        self.assertIn("estimate", e["label"])
        self.assertGreaterEqual(e["injected"], 2000)
        self.assertGreaterEqual(e["tool_output"], 2000)
        attributed = e["conversation"] + e["tool_output"] + e["tool_calls"] + e["injected"]
        self.assertEqual(e["unattributed"], 100_005 - attributed)
        self.assertFalse(e["residual_clamped"])

    def test_an_estimate_over_the_billed_size_clamps_at_zero(self):
        rows = [{"type": "attachment", "sessionId": SID, "attachment": "h" * 400_000},
                turn(1, 0, 1_000, "r1")]
        self.transcript(rows)
        e = self.measure()["estimate"]
        self.assertEqual(e["unattributed"], 0)
        self.assertTrue(e["residual_clamped"])

    def test_only_rows_after_the_last_compaction_count(self):
        self.transcript([turn(30, 0, 900_000, "r1"),
                         {"type": "system", "subtype": "compact_boundary", "sessionId": SID},
                         turn(1, 0, 20_000, "r2")])
        m = self.measure()
        self.assertEqual(m["peak_context_tokens"], 20_005)
        self.assertEqual(m["turns_since_compaction"], 1)

    def test_cache_warmth_uses_the_ttl_last_written(self):
        self.transcript([turn(17, 0, 1000, "r1")])                       # 5-minute write
        self.assertFalse(self.measure()["cache"]["warm"])
        self.transcript([turn(17, 0, 1000, "r1", w1h=1000)])             # 1-hour write
        c = self.measure()["cache"]
        self.assertTrue(c["warm"])
        self.assertEqual(c["ttl_minutes"], 60)
        self.assertGreater(c["minutes_left"], 40)

    def test_one_request_in_several_rows_is_one_turn(self):
        self.transcript([turn(3, 0, 1000, "r1")] * 4 + [turn(1, 1000, 100, "r2")])
        self.assertEqual(self.measure()["turns_since_compaction"], 2)

    def test_no_session_refuses_and_no_transcript_cannot_run(self):
        r = self.py(TOOL)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--session", r.stderr)
        r = self.py(TOOL, env={"CLAUDE_CODE_SESSION_ID": "nosuch"})
        self.assertEqual(r.returncode, 4)
        self.assertIn("could not run", r.stderr)

    def test_it_writes_nothing_and_leaks_nothing(self):
        self.transcript([turn(3, 0, 1000, "r1")])
        before = tree(self.tmp)
        outs = [self.py(TOOL, env={"CLAUDE_CODE_SESSION_ID": SID}),
                self.py(TOOL, "--json", env={"CLAUDE_CODE_SESSION_ID": SID}),
                self.py(TOOL, "--latest")]
        self.assertEqual(tree(self.tmp), before)
        for r in outs:
            self.assertEqual(r.returncode, 0, r.stderr)
            text = r.stdout + r.stderr
            self.assertNotIn(SID, text)
            self.assertNotIn(str(self.home), text)
            self.assertNotIn("someone-private", text)
        self.assertIn("WARM", outs[0].stdout)
        self.assertIn("unattributed", outs[0].stdout)

    def test_the_skill_prunes_and_points_carry_forward_at_handoff(self):
        skill = (GT / "skills" / "gt-minimize" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("gt_minimize.py", skill)
        self.assertIn("/gt:gt-handoff", skill)
        self.assertIn("Never write an in-flight note", skill)
        self.assertIn("INBOX.md", skill)
        self.assertIn("gt_write_queue.py", skill)
        self.assertNotIn("inflight/", skill)


if __name__ == "__main__":
    unittest.main()
