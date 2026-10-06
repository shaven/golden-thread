"""gt-lotr 0.4.0 strict tool tiers: each call tool carries only its own tier.

Until 0.4.0 call_write also carried reads and call_consent carried everything, so "a writer agent
holds no call_read" stopped nothing: it read through call_write. Strict (the default) refuses a
tier mismatch with `wrong_tool` naming the right tool, before anything is sent; a connection's
`strict_tools: false` restores the old rule.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _harness import IS_WINDOWS, REPO, latest_version_dir, needs_dev

GW = latest_version_dir(REPO / "golden-thread-lotr")
sys.path.insert(0, str(GW / "scripts"))

from lotrlib import registry  # noqa: E402
from lotrlib.engine import Engine  # noqa: E402
from lotrlib.errors import GatewayError  # noqa: E402

FAKE = REPO / "dev" / "fake_lotr_mcp.py"
OWNER = {"owner": "acme", "repo": "widgets"}


def _makeServer():
    class H(BaseHTTPRequestHandler):
        seen = []

        def log_message(self, *a):
            pass

        def _reply(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            H.seen.append((self.command, self.path.split("?")[0]))
            raw = json.dumps({"number": 1, "title": "t", "state": "open", "issues": [],
                              "merged": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _reply
    return H


Srv = _makeServer()


class StrictToolTiers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Srv)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def conn(self, cid, profile="github", **over):
        c = {"id": cid, "identity": "t", "zone": "personal", "kind": "http", "profile": profile,
             "description": "t", "base_url": f"http://127.0.0.1:{self.port}",
             "network": {"hosts": ["127.0.0.1"]},
             "auth": {"scheme": "bearer", "token_ref": "env:LOTR_STRICT_TOKEN"},
             "headers": {}, "trust": "T0", "enabled": True,
             "policy": {"deny": [], "consent": [], "write": [], "read": []}}
        c.update(over)
        return c

    def engine(self):
        os.environ["LOTR_ALLOW_ENV_SECRETS"] = "1"
        os.environ["LOTR_STRICT_TOKEN"] = "strict-test-placeholder"
        home = Path(tempfile.mkdtemp(dir=None if IS_WINDOWS else "/tmp", prefix="gws"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "gateway.json").write_text(json.dumps(
            {"schema": 1, "zone": "personal", "mode": "local",
             "local": {"allow": ["*"], "max_tier": "consent", "confirm": "dialog"}}))
        (home / "registry.json").write_text(json.dumps(
            {"schema": 1, "zone": "personal", "clients": [], "connections": [
                self.conn("github@personal"),                               # strict by default
                self.conn("loose@personal", strict_tools=False),
                self.conn("explicit@personal", strict_tools=True),
                self.conn("jira@personal", profile="jira-v3")]}))
        os.chmod(home / "registry.json", 0o600)
        os.chmod(home / "gateway.json", 0o600)
        Srv.seen.clear()
        self.asked = []
        return Engine(home, dialog=lambda text: self.asked.append(text) or True)

    READ = ("get_pull", dict(OWNER, pull_number=7))
    WRITE = ("create_issue", dict(OWNER, title="x"))
    CONSENT = ("merge_pull", dict(OWNER, pull_number=7))
    RIGHT = {"read": "call_read", "write": "call_write", "consent": "call_consent"}

    def table(self, e, cid, allowed):
        for tier, (op, args) in (("read", self.READ), ("write", self.WRITE),
                                 ("consent", self.CONSENT)):
            for tool in ("call_read", "call_write", "call_consent"):
                Srv.seen.clear()
                self.asked.clear()
                r = e.call(tool, cid, op, dict(args))
                with self.subTest(connection=cid, tool=tool, tier=tier):
                    if (tool, tier) in allowed:
                        self.assertTrue(r["ok"], r)
                        self.assertEqual(r["tier"], tier)
                        self.assertEqual(len(Srv.seen), 1)
                    else:
                        self.assertFalse(r["ok"], r)
                        self.assertEqual(r["error"]["code"], "wrong_tool")
                        self.assertEqual(r["error"]["hints"], ["use " + self.RIGHT[tier]])
                        self.assertIn(tool, r["error"]["message"])
                        self.assertEqual(Srv.seen, [], "a refused call sends nothing")
                        self.assertEqual(self.asked, [], "a refused call asks for no consent")

    def test_strict_is_the_default_each_tool_carries_only_its_own_tier(self):
        own = {("call_read", "read"), ("call_write", "write"), ("call_consent", "consent")}
        e = self.engine()
        self.table(e, "github@personal", own)
        self.table(e, "explicit@personal", own)

    def test_strict_tools_false_restores_the_old_rule_for_that_connection_only(self):
        e = self.engine()
        self.table(e, "loose@personal", {
            ("call_read", "read"), ("call_write", "read"), ("call_consent", "read"),
            ("call_write", "write"), ("call_consent", "write"), ("call_consent", "consent")})

    def test_a_raw_get_through_call_write_is_refused_and_names_call_read(self):
        e = self.engine()
        r = e.call("call_write", "github@personal", "GET /repos/acme/widgets/pulls/7", {})
        self.assertEqual(r["error"]["code"], "wrong_tool")
        self.assertEqual(r["error"]["hints"], ["use call_read"])
        self.assertEqual(Srv.seen, [])
        r = e.call("call_read", "github@personal", "GET /repos/acme/widgets/pulls/7", {})
        self.assertTrue(r["ok"], r)

    def test_jira_post_search_is_a_read_readers_can_use_and_writers_cannot(self):
        e = self.engine()
        for path in ("/rest/api/3/search/jql", "/rest/api/3/search"):
            Srv.seen.clear()
            r = e.call("call_read", "jira@personal", "POST " + path, {"jql": "project = X"})
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["tier"], "read")
            self.assertEqual(Srv.seen, [("POST", path)])
            Srv.seen.clear()
            r = e.call("call_write", "jira@personal", "POST " + path, {"jql": "project = X"})
            self.assertEqual(r["error"]["code"], "wrong_tool")
            self.assertEqual(Srv.seen, [])
        r = e.call("call_read", "jira@personal", "POST /rest/api/3/issue", {"fields": {}})
        self.assertEqual(r["error"]["code"], "wrong_tool")            # unknown POST stays a write
        self.assertEqual(r["error"]["hints"], ["use call_write"])

    def test_forged_cursor_is_still_refused_under_strict_and_loose(self):
        e = self.engine()
        for cid in ("github@personal", "loose@personal"):
            for tool, code in (("call_read", "bad_cursor"), ("call_write", "cursor_needs_read"),
                               ("call_consent", "cursor_needs_read")):
                Srv.seen.clear()
                r = e.call(tool, cid, "list_pulls", dict(OWNER), cursor="gtc_forged")
                self.assertEqual(r["error"]["code"], code, (cid, tool, r))
                self.assertEqual(Srv.seen, [])

    def test_registry_validates_strict_tools(self):
        registry.validate_connection(self.conn("a@personal"), "personal")
        registry.validate_connection(self.conn("a@personal", strict_tools=False), "personal")
        for bad in ("false", 0, None, []):
            with self.assertRaises(GatewayError) as cm:
                registry.validate_connection(self.conn("a@personal", strict_tools=bad), "personal")
            self.assertEqual(cm.exception.code, "registry_invalid")
            self.assertIn("strict_tools", cm.exception.message)
        mcp = {"id": "m@personal", "zone": "personal", "kind": "mcp", "strict_tools": "x"}
        with self.assertRaises(GatewayError):
            registry.validate_connection(mcp, "personal")


@needs_dev
class ReadersAndWritersAgainstTheFakeServer(unittest.TestCase):
    """The agents' typical calls through dev/fake_lotr_mcp.py, which applies the release's own
    policy.classify and strict tool tiers."""

    def session(self, calls):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18"}}]
        for i, (tool, args) in enumerate(calls, 2):
            msgs.append({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                         "params": {"name": tool, "arguments": args}})
        p = subprocess.run([sys.executable, str(FAKE), "--release", str(GW), "--routing", "on"],
                           input="".join(json.dumps(m) + "\n" for m in msgs),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = {m["id"]: m["result"] for m in map(json.loads, p.stdout.splitlines())}
        return [out[i]["structuredContent"] for i in range(2, 2 + len(calls))]

    def test_reader_calls_work_and_a_writer_tool_cannot_read(self):
        reads = [
            {"connection": "jira@work", "op": "search",
             "args": {"jql": "assignee = currentUser() AND priority in (High, Highest)"}},
            {"connection": "github@work", "op": "github.my_open_prs"},
            {"connection": "m365@work", "op": "list_events", "args": {}},
        ]
        got = self.session([("find", {"query": "open pull requests"})]
                           + [("call_read", r) for r in reads])
        self.assertTrue(got[0]["ok"])
        for r, env in zip(reads, got[1:]):
            self.assertTrue(env["ok"], (r, env))
            self.assertTrue(env["untrusted"])
        for tool in ("call_write", "call_consent"):
            for r, env in zip(reads, self.session([(tool, r) for r in reads])):
                self.assertEqual(env["error"]["code"], "wrong_tool", (tool, r, env))
                self.assertEqual(env["error"]["hints"], ["use call_read"])

    def test_writer_calls_work_only_through_their_own_tool(self):
        comment = {"connection": "jira@work", "op": "add_comment",
                   "args": {"issueIdOrKey": "OPS-12", "body": "hello"}}
        send = {"connection": "m365@work", "op": "send_mail", "args": {"message": {"subject": "s"}}}
        reply = {"connection": "m365@work", "op": "POST /me/messages/AAA/reply",
                 "args": {"comment": "ok"}}
        got = self.session([("call_write", comment), ("call_consent", send),
                            ("call_consent", reply),
                            ("call_consent", comment), ("call_write", send), ("call_write", reply),
                            ("call_read", comment), ("call_read", send)])
        self.assertTrue(got[0]["ok"] and got[1]["ok"] and got[2]["ok"], got[:3])
        self.assertEqual([g["error"]["code"] for g in got[3:]], ["wrong_tool"] * 5)
        self.assertEqual(got[3]["error"]["hints"], ["use call_write"])
        self.assertEqual(got[4]["error"]["hints"], ["use call_consent"])
        self.assertEqual(got[5]["error"]["hints"], ["use call_consent"])


if __name__ == "__main__":
    unittest.main()


class StrictMcpToolNames(unittest.TestCase):
    """Review m1 (2026-10-04): on a non-OAuth `mcp` connection a tool's own name and readOnlyHint
    were trusted, so `find_and_delete` or a `drop_table` claiming read-only rated as a read.
    Since 0.4.0 every `mcp` connection gets the strict rating unless it says strict_tools: false."""

    TOOLS = [{"name": "find_and_delete"}, {"name": "query_exec"},
             {"name": "drop_table", "annotations": {"readOnlyHint": True}},
             {"name": "get_issue"}, {"name": "list_items", "annotations": {"readOnlyHint": True}}]

    def tiers(self, **conn):
        sys.path.insert(0, str(GW / "scripts"))
        from lotrlib import profiles
        prof = profiles.mcp_profile(dict({"tools": self.TOOLS}, **conn))
        return {op["name"]: op["tier"] for op in prof["ops"]}

    def test_a_plain_mcp_connection_is_rated_strictly(self):
        t = self.tiers()
        self.assertEqual(t["find_and_delete"], "consent")
        self.assertEqual(t["query_exec"], "consent")
        self.assertEqual(t["drop_table"], "consent", "a readOnlyHint does not clear a risky name")
        self.assertEqual(t["get_issue"], "read")
        self.assertEqual(t["list_items"], "read")

    def test_strict_tools_false_keeps_the_old_rating_for_that_connection(self):
        t = self.tiers(strict_tools=False)
        self.assertEqual(t["find_and_delete"], "read")
        self.assertEqual(t["drop_table"], "read")
