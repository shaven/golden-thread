"""LOTR connection status (gt-lotr 0.4.0, owner 2026-10-06): users must be able to see what is
connected to LOTR and whether each connection is working, the way Claude Code's /mcp does.

Contract:
  * status() gives every connection a `state`:
      connected      its last call or check succeeded
      needs sign-in  its last failure was a sign-in failure (expired, refused or missing token)
      error          its last failure was the connection itself (unreachable, server error)
      disabled       the registry turns it off
      not used yet   nothing has been called or checked since the gateway started
    plus `last_ok` / `last_error` {code, at} and a `fix` hint when it needs attention.
  * Caller and policy errors (wrong_tool, op_denied, bad_request, ...) say nothing about the
    connection and do not change its state.
  * status(check=True) probes every enabled mcp connection with one read-only tools/list and
    records the outcome like a call.
"""
import json
import os
import unittest

from test_lotr_mcp import McpBase  # noqa: F401  (the fake MCP server and its fixtures)


class ConnectionStatus(McpBase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "gw"
        self.home.mkdir(mode=0o700)
        self.write_registry([self.conn()])

    def write_registry(self, conns):
        (self.home / "gateway.json").write_text(json.dumps(
            {"schema": 1, "zone": "personal", "mode": "local",
             "local": {"allow": ["*"], "max_tier": "consent", "confirm": "dialog"}}))
        (self.home / "registry.json").write_text(json.dumps(
            {"schema": 1, "zone": "personal", "connections": conns, "clients": []}))
        for f in ("gateway.json", "registry.json"):
            os.chmod(self.home / f, 0o600)
        from lotrlib.engine import Engine
        self.engine = Engine(self.home, dialog=lambda text: True)

    def row(self, cid="jira@personal", **kw):
        st = self.engine.status(**kw)
        self.assertTrue(st["ok"], st)
        return next(c for c in st["connections"] if c["id"] == cid)

    def test_a_connection_never_used_says_so(self):
        r = self.row()
        self.assertEqual(r["state"], "not used yet")
        self.assertIsNone(r["last_ok"])

    def test_a_successful_call_makes_it_connected(self):
        self.assertTrue(self.engine.call("call_read", "jira@personal", "search_issues",
                                         {"query": "x"})["ok"])
        r = self.row()
        self.assertEqual(r["state"], "connected")
        self.assertTrue(r["last_ok"])

    def test_a_caller_error_does_not_change_the_state(self):
        self.engine.call("call_read", "jira@personal", "search_issues", {"query": "x"})
        r = self.engine.call("call_read", "jira@personal", "create_issue", {"summary": "s"})
        self.assertEqual(r["error"]["code"], "wrong_tool")
        self.assertEqual(self.row()["state"], "connected")

    def test_a_sign_in_failure_says_needs_sign_in_with_a_fix(self):
        self.engine._note_health("jira@personal", None)
        self.engine._note_health("jira@personal", "needs_login")
        r = self.row()
        self.assertEqual(r["state"], "needs sign-in")
        self.assertEqual(r["last_error"]["code"], "needs_login")
        self.assertTrue(r["fix"])

    def test_a_down_connection_says_error_with_the_code_and_time(self):
        self.engine._note_health("jira@personal", None)
        self.engine._note_health("jira@personal", "unreachable")
        r = self.row()
        self.assertEqual(r["state"], "error")
        self.assertEqual(r["last_error"]["code"], "unreachable")
        self.assertTrue(r["last_error"]["at"])

    def test_a_success_after_an_error_clears_it(self):
        self.engine._note_health("jira@personal", "unreachable")
        self.engine._note_health("jira@personal", None)
        self.assertEqual(self.row()["state"], "connected")

    def test_a_disabled_connection_says_disabled(self):
        self.write_registry([self.conn(enabled=False)])
        self.assertEqual(self.row()["state"], "disabled")

    def test_check_probes_an_mcp_connection(self):
        r = self.row(check=True)
        self.assertEqual(r["state"], "connected")
        self.assertEqual(r["checked"], True)

    def test_check_reports_an_unreachable_server(self):
        self.write_registry([self.conn(id="dead@personal", endpoint="http://127.0.0.1:9/mcp")])
        r = self.row("dead@personal", check=True)
        self.assertEqual(r["state"], "error")
        self.assertIn(r["last_error"]["code"], ("unreachable", "network_error"))

    # -- the readable view (lotr status --table, and /gt:gt-settings lotr) ---------------------
    def table(self, **kw):
        from lotrlib import status_view
        return status_view.render(self.engine.status(**kw))

    def test_the_table_shows_one_line_per_connection_with_its_state(self):
        self.engine.call("call_read", "jira@personal", "search_issues", {"query": "x"})
        t = self.table()
        line = next(l for l in t.splitlines() if "jira@personal" in l)
        self.assertIn("✓", line)
        self.assertIn("connected", line)

    def test_the_table_gives_the_fix_for_a_connection_that_needs_attention(self):
        self.engine._note_health("jira@personal", "needs_login")
        line = next(l for l in self.table().splitlines() if "jira@personal" in l)
        self.assertIn("needs sign-in", line)
        self.assertIn("needs_login", line)
        self.assertIn("sign in again", self.table())

    def test_the_summary_line_counts_states(self):
        from lotrlib import status_view
        self.engine._note_health("jira@personal", "unreachable")
        self.assertIn("1 error", status_view.summary(self.engine.status()))

    def test_the_cli_takes_check_and_table(self):
        import subprocess, sys
        from test_lotr_mcp import GW
        p = subprocess.run([sys.executable, str(GW / "scripts" / "lotr.py"), "status", "--help"],
                           capture_output=True, text=True)
        self.assertIn("--check", p.stdout)
        self.assertIn("--table", p.stdout)

    def test_find_empty_lists_each_connection_with_its_state(self):
        """The assistant sees a connection is down before calling it (and under sandbox mode,
        where the shell cannot run lotr, this is its only view)."""
        self.engine._note_health("jira@personal", "unreachable")
        r = self.engine.find("")
        rows = [x for x in r["results"] if x.get("connection") == "jira@personal" and not x.get("op")]
        self.assertTrue(rows, r)
        self.assertEqual(rows[0]["state"], "error")
        self.assertTrue(rows[0].get("fix"))


if __name__ == "__main__":
    unittest.main()
