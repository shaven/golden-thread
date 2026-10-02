"""gt-optimize as an aggregator, and its `session` member (gt_optimize_session.py).

Contract (request 2026-09-23-optimize-aggregator-session-member):
  * gt_optimize.py declares exactly two members, vault and session, and every run prints
    `N of 2 member(s) ran` -- including when a member crashes or cannot run.
  * A member that cannot run is reported as such (exit 3), never as a clean result.
  * `--only vault` reproduces the vault report (`--member vault`).
  * The session member classifies cache writes as cold / growth / expiry / invalid; one API
    request written as several transcript rows counts once; the TTL comes from what was last
    WRITTEN; streams never affect each other; dollars use 1.25x / 2.0x / 0.1x of base input.
  * No output carries a session id longer than 8 characters or an absolute path.
  * Every threshold is a registered setting.
Every fixture is synthetic and lives in the sandbox HOME -- never the developer's history.
"""
import datetime
import json
import os
import re
import shutil
import sys
import unittest

from _harness import Sandbox, SCRIPTS

OPT = SCRIPTS / "gt_optimize.py"
SESS = SCRIPTS / "gt_optimize_session.py"

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"
T0 = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)


def row(sid, minutes, read, write, req, msg=None, w1h=0, model="claude-sonnet-4-5",
        sidechain=False):
    """One assistant transcript row. w1h > 0 marks the write as 1-hour TTL; else 5-minute."""
    ts = (T0 + datetime.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    cc = {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": write - w1h}
    return {"type": "assistant", "sessionId": sid, "isSidechain": sidechain, "timestamp": ts,
            "requestId": req, "uuid": "%s-%s" % (req, msg or req),
            "message": {"id": msg or ("msg_" + req), "model": model, "role": "assistant",
                        "usage": {"input_tokens": 3, "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": write,
                                  "cache_creation": cc, "output_tokens": 10}}}


class SessionMemberTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.projects = self.home / ".claude" / "projects"
        self.projects.mkdir(parents=True)
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "alpha").mkdir(parents=True)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def transcript(self, rows, name=SID_A, project="-home-someone-proj", tail=""):
        d = self.projects / project
        d.mkdir(parents=True, exist_ok=True)
        p = d / ("%s.jsonl" % name)
        p.write_text("".join(json.dumps(r) + "\n" for r in rows) + tail, encoding="utf-8")
        return p

    def session(self, *args):
        return self.py(SESS, *args)

    def data(self, *args):
        r = self.session("--json", *args)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        return json.loads(r.stdout)

    # -- dedup ------------------------------------------------------------------------
    def test_one_request_in_five_rows_counts_once(self):
        rows = [row(SID_A, 0, 0, 1000, "r1")]
        rows += [row(SID_A, 1, 1000, 500, "r2")] * 5          # one request, five blocks
        self.transcript(rows)
        d = self.data()
        self.assertEqual(d["turns"], 2)
        self.assertEqual(d["tokens_written"], 1500, "cumulative usage must not be summed")

    def test_distinct_requests_are_distinct_turns(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1"), row(SID_A, 1, 1000, 500, "r2"),
                         row(SID_A, 2, 1500, 500, "r3")])
        self.assertEqual(self.data()["turns"], 3)

    # -- causes -----------------------------------------------------------------------
    def split(self):
        return self.data()["write_split"]

    def test_first_turn_is_cold_and_growth_is_growth(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1"), row(SID_A, 1, 1000, 300, "r2")])
        s = self.split()
        self.assertEqual(s["cold"], 1000)
        self.assertEqual(s["growth"], 300)
        self.assertEqual(s["expiry"] + s["invalid"], 0)

    def test_a_17_minute_gap_expires_a_5_minute_write(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1"), row(SID_A, 17, 0, 1200, "r2")])
        s = self.split()
        self.assertEqual(s["expiry"], 1000, "the rebuilt prefix is expiry")
        self.assertEqual(s["growth"], 200, "what is beyond the old prefix is growth")

    def test_the_same_gap_does_not_expire_a_1_hour_write(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1", w1h=1000),
                         row(SID_A, 17, 1000, 200, "r2", w1h=200)])
        s = self.split()
        self.assertEqual(s["expiry"], 0)
        self.assertEqual(s["growth"], 200)

    def test_a_1_hour_write_does_expire_after_an_hour(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1", w1h=1000),
                         row(SID_A, 75, 0, 1000, "r2", w1h=1000)])
        self.assertEqual(self.split()["expiry"], 1000)

    def test_a_changed_prefix_inside_the_ttl_is_invalid(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1"), row(SID_A, 1, 200, 900, "r2")])
        s = self.split()
        self.assertEqual(s["invalid"], 800)
        self.assertEqual(s["growth"], 100)

    def test_sessions_never_affect_each_other(self):
        """Interleaved: each session's first turn is cold, even though another session's turn
        sits between them in time."""
        self.transcript([row(SID_A, 0, 0, 1000, "a1"), row(SID_A, 2, 1000, 100, "a2")],
                        name=SID_A)
        self.transcript([row(SID_B, 1, 0, 700, "b1"), row(SID_B, 3, 700, 50, "b2")],
                        name=SID_B)
        s = self.split()
        self.assertEqual(s["cold"], 1700)
        self.assertEqual(s["invalid"], 0)
        self.assertEqual(s["growth"], 150)

    def test_the_turn_after_a_compaction_is_cold_not_invalid(self):
        rows = [row(SID_A, 0, 0, 1000, "r1"),
                {"type": "system", "subtype": "compact_boundary", "sessionId": SID_A},
                row(SID_A, 1, 0, 300, "r2")]
        self.transcript(rows)
        s = self.split()
        self.assertEqual(s["invalid"], 0)
        self.assertEqual(s["cold"], 1300)

    # -- cost arithmetic --------------------------------------------------------------
    def test_multipliers_are_exact(self):
        # sonnet: $3 / MTok. 1M 5-minute write = 3 * 1.25; 1M 1-hour write = 3 * 2.0;
        # 1M read = 3 * 0.1.
        self.transcript([row(SID_A, 0, 0, 1_000_000, "r1"),
                         row(SID_A, 1, 1_000_000, 1_000_000, "r2", w1h=1_000_000)])
        d = self.data()
        self.assertAlmostEqual(d["equivalent_usd"]["writes"]["cold"], 3.75, places=2)
        self.assertAlmostEqual(d["equivalent_usd"]["writes"]["growth"], 6.0, places=2)
        self.assertAlmostEqual(d["equivalent_usd"]["reads"], 0.3, places=2)
        self.assertEqual(d["price_basis"]["write_5m"], 1.25)
        self.assertEqual(d["price_basis"]["write_1h"], 2.0)
        self.assertEqual(d["price_basis"]["read"], 0.1)
        self.assertIn("not a bill", d["price_basis"]["label"])

    def test_an_unknown_model_falls_back_rather_than_raising(self):
        self.transcript([row(SID_A, 0, 0, 1_000_000, "r1", model="mystery-model-9")])
        d = self.data()
        self.assertAlmostEqual(d["equivalent_usd"]["writes"]["cold"], 3.75, places=2)
        self.assertEqual(d["unpriced_models"], ["mystery-model-9"])

    # -- output surfaces --------------------------------------------------------------
    def test_report_names_every_cause_and_the_avoidable_share(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1"), row(SID_A, 30, 0, 1000, "r2")])
        r = self.session()
        self.assertEqual(r.returncode, 1, r.stderr)
        for word in ("tokens read", "tokens written", "cold", "growth", "expiry", "invalid",
                     "avoidable share", "list-price equivalent"):
            self.assertIn(word, r.stdout)

    def test_brief_is_one_line_with_share_causes_and_dollars(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1"), row(SID_A, 30, 0, 1000, "r2")])
        r = self.session("--brief")
        lines = r.stdout.strip().split("\n")
        self.assertEqual(len(lines), 1, r.stdout)
        for word in ("avoidable", "expiry", "invalid", "$", "equivalent"):
            self.assertIn(word, lines[0])

    def test_check_exits_3_above_the_threshold_and_0_below(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1"), row(SID_A, 30, 0, 1000, "r2")])
        self.assertEqual(self.session("--check", "10").returncode, 3)    # 50% avoidable
        self.assertEqual(self.session("--check", "90").returncode, 0)

    def test_check_without_pct_uses_the_registered_setting(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1"), row(SID_A, 30, 0, 1000, "r2")])
        self.config(optimize_avoidable_pct="25")
        self.assertEqual(self.session("--check").returncode, 3)
        self.config(optimize_avoidable_pct="75")
        self.assertEqual(self.session("--check").returncode, 0)

    def test_no_transcripts_is_could_not_run_not_zero_percent(self):
        r = self.session()
        self.assertEqual(r.returncode, 4)
        self.assertIn("could not run", r.stderr)
        self.assertNotIn("0.0%", r.stdout)

    def test_a_truncated_last_line_does_not_stop_the_read(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1"), row(SID_A, 1, 1000, 300, "r2")],
                        tail='{"type": "assistant", "sessionId": "aaa", "mess')
        self.assertEqual(self.data()["turns"], 2)

    def test_no_long_session_id_or_absolute_path_in_any_output(self):
        self.transcript([row(SID_A, 0, 0, 1000, "r1"), row(SID_A, 30, 0, 1000, "r2")],
                        project="-Users-someone-secret-project")
        outs = [self.session(*a) for a in ([], ["--json"], ["--brief"])]
        outs.append(self.py(OPT, "--vault", str(self.vault), "--only", "session"))
        for r in outs:
            text = r.stdout + r.stderr
            self.assertNotIn(SID_A, text)
            self.assertNotRegex(text, r"[0-9a-f]{8}-[0-9a-f]{4}", "a session id over 8 chars")
            self.assertNotIn(str(self.home), text)
            self.assertNotIn("secret-project", text)

    def test_thresholds_are_registered_settings_listed_by_show(self):
        r = self.py(SCRIPTS / "gt_settings.py", "show")
        self.assertOk(r)
        for key in ("optimize_session_days", "optimize_avoidable_pct"):
            self.assertIn(key, r.stdout)


class AggregatorTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "alpha" / "memory").mkdir(parents=True)
        (self.vault / "Projects" / "alpha" / "memory" / "a.md").write_text(
            "One clear fact that stands on its own.\n", encoding="utf-8")
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def opt(self, *args, script=OPT):
        return self.py(script, "--vault", str(self.vault), *args)

    def with_transcript(self):
        d = self.home / ".claude" / "projects" / "-p"
        d.mkdir(parents=True)
        (d / ("%s.jsonl" % SID_A)).write_text(
            json.dumps(row(SID_A, 0, 0, 1000, "r1")) + "\n", encoding="utf-8")

    def test_declares_exactly_two_members(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            import importlib
            mod = importlib.import_module("gt_optimize")
            self.assertEqual(sorted(mod.MEMBERS), ["session", "vault"])
        finally:
            sys.path.remove(str(SCRIPTS))

    def test_both_ran_says_2_of_2(self):
        self.with_transcript()
        r = self.opt()
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        self.assertIn("2 of 2 member(s) ran", r.stdout)

    def test_no_transcripts_is_1_of_2_and_exit_3(self):
        r = self.opt()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("1 of 2 member(s) ran", r.stdout)
        self.assertIn("COULD NOT RUN", r.stdout)
        self.assertIn("session", r.stdout)

    def test_a_crashing_member_is_could_not_run_not_a_finding(self):
        """Stub the session member with one that dies with a traceback and no output."""
        fake = self.tmp / "scripts"
        shutil.copytree(str(SCRIPTS), str(fake))
        (fake / "gt_optimize_session.py").write_text("raise RuntimeError('boom')\n",
                                                     encoding="utf-8")
        r = self.opt(script=fake / "gt_optimize.py")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("1 of 2 member(s) ran", r.stdout)
        self.assertIn("crashed", r.stdout)
        j = json.loads(self.opt("--json", script=fake / "gt_optimize.py").stdout)
        self.assertEqual([m["member"] for m in j["could_not_run"]], ["session"])

    def test_only_vault_reproduces_the_vault_report(self):
        (self.vault / "Projects" / "alpha" / "memory" / "b.md").write_text(
            "We rotated the signing key last week and the fleet picked it up.\n",
            encoding="utf-8")
        agg = json.loads(self.opt("--only", "vault", "--json").stdout)
        direct = json.loads(self.opt("--member", "vault", "--json").stdout)
        self.assertEqual(agg["findings"], direct["findings"])
        self.assertEqual(agg["ran"], ["vault"])
        text = self.opt("--only", "vault")
        self.assertIn("1 of 1 member(s) ran", text.stdout)
        self.assertIn("JUDGEMENT -- reported, never applied", text.stdout)
        self.assertEqual(text.returncode, 1)

    def test_only_rejects_an_unknown_member(self):
        self.assertEqual(self.opt("--only", "nosuch").returncode, 2)

    def test_allin_runs_only_the_vault_member(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("allin_t", str(SCRIPTS / "gt_allin.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cmd = mod.build_cmd("optimize", mod.MEMBERS["optimize"], str(self.vault), str(self.tmp))
        self.assertEqual(cmd[-2:], ["--only", "vault"])


if __name__ == "__main__":
    unittest.main()
