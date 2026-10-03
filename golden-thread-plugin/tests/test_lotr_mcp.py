"""gt-lotr 0.2.0: front an SSO/OAuth-protected downstream MCP endpoint (request
2026-10-02-lotr-front-oauth-mcp-endpoints; owner 2026-10-02: "lotr has to handle sso").

LOTR reuses the bearer token the local MCP client already holds -- BY REFERENCE
(`file:<path>#<json-field>`), never by value -- and on a 401 runs the client's own refresh
helper and retries once. Downstream tools become LOTR ops, tiered read/write/consent by their
annotations or, failing that, their names, and searchable with find.

No external network: a fake MCP server on 127.0.0.1 checks the bearer token and answers 401 to
a stale one; a fake refresh helper rewrites the token file in place. Every error path asserts
the token text never appears.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _harness import REPO, latest_version_dir
from _harness import LOTR_POSIX_ONLY, skip_on_windows

GW = latest_version_dir(REPO / "golden-thread-lotr")
sys.path.insert(0, str(GW / "scripts"))

from lotrlib import profiles, registry, secrets          # noqa: E402
from lotrlib.errors import GatewayError                  # noqa: E402

STALE = "mcp-stale-not-real-2b9d"
FRESH = "mcp-fresh-not-real-8c41"
TOOLS = [
    {"name": "search_issues", "description": "Search issues with a query",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "create_issue", "description": "Create an issue",
     "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}}}},
    {"name": "delete_issue", "description": "Delete an issue",
     "annotations": {"destructiveHint": True}},
    {"name": "get_comments", "description": "Comments on an issue"},
]


def _makeFakeMcp():
    """Built in a function: tests/prun.py treats every top-level class as a test unit."""
    class FakeMcp(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            srv = self.server
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            srv.seen.append((body.get("method"), self.headers.get("Authorization"),
                             self.headers.get("Mcp-Session-Id")))
            if self.headers.get("Authorization") != "Bearer " + srv.valid:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Bearer realm="sso"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if "id" not in body:                                  # a notification
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            m = body["method"]
            if m == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fake", "version": "1"}}
            elif m == "tools/list":
                result = {"tools": TOOLS}
            elif m == "tools/call":
                name = body["params"]["name"]
                args = body["params"].get("arguments") or {}
                result = {"content": [{"type": "text", "text": json.dumps(
                    {"tool": name, "args": args, "items": [{"key": "ABC-1"}]})}]}
            else:
                result = None
            msg = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            if srv.sse:
                raw = ("event: message\ndata: %s\n\n" % json.dumps(msg)).encode()
                ctype = "text/event-stream"
            else:
                raw, ctype = json.dumps(msg).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Mcp-Session-Id", "sess-1")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return FakeMcp


@skip_on_windows(LOTR_POSIX_ONLY)
class McpBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _makeFakeMcp())
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir="/tmp", prefix="lotrmcp"))
        self.tokens = self.tmp / "client-tokens.json"
        self.write_tokens(FRESH)
        self.httpd.valid, self.httpd.sse, self.httpd.seen = FRESH, False, []
        # the refresh helper: rewrites the token file in place, prints the token (which LOTR
        # must never capture or echo)
        self.refresh = self.tmp / "refresh.py"
        self.refresh.write_text(
            "import json, os, sys\np = sys.argv[1]\n"
            "json.dump({'access_token': %r, 'refresh_token': 'r', 'expires_at': 1}, open(p, 'w'))\n"
            "os.chmod(p, 0o600)\nprint(%r)\n" % (FRESH, FRESH))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_tokens(self, access):
        self.tokens.write_text(json.dumps({"access_token": access, "refresh_token": "r",
                                           "expires_at": 1}))
        os.chmod(self.tokens, 0o600)

    def conn(self, refresh=True, **over):
        c = {"id": "jira@personal", "identity": "me @ fake", "zone": "personal", "kind": "mcp",
             "profile": "mcp", "endpoint": "http://127.0.0.1:%d/mcp" % self.port,
             "transport": "http",
             "auth": {"scheme": "bearer", "token_ref": "file:%s#access_token" % self.tokens},
             "refresh_cmd": [sys.executable, str(self.refresh), str(self.tokens)] if refresh
             else None,
             "network": {"hosts": ["127.0.0.1"]}, "trust": "T1", "tools": TOOLS,
             "policy": {"deny": [], "consent": [], "write": [], "read": []}, "enabled": True}
        c.update(over)
        return c

    def assertNoToken(self, text):
        for t in (STALE, FRESH):
            self.assertNotIn(t, text)


class FileJsonFieldRef(McpBase):
    def test_a_field_of_an_owner_only_json_file_resolves(self):
        self.assertEqual(secrets.resolve("file:%s#access_token" % self.tokens), FRESH)

    def test_a_missing_field_names_the_ref_never_a_value(self):
        with self.assertRaises(GatewayError) as cm:
            secrets.resolve("file:%s#nope" % self.tokens)
        self.assertEqual(cm.exception.code, "secret_missing")
        self.assertNoToken(json.dumps(cm.exception.to_dict()))

    def test_a_group_readable_token_file_is_refused(self):
        os.chmod(self.tokens, 0o640)
        with self.assertRaises(GatewayError) as cm:
            secrets.resolve("file:%s#access_token" % self.tokens)
        self.assertEqual(cm.exception.code, "secret_perms")

    def test_describe_stats_without_reading(self):
        d = secrets.describe("file:%s#access_token" % self.tokens)
        self.assertEqual((d["scheme"], d["present"]), ("file", True))


class TierDerivation(unittest.TestCase):
    def test_annotations_first_then_the_name_rule_then_write(self):
        got = {t["name"]: profiles.mcp_tier(t) for t in TOOLS}
        self.assertEqual(got, {"search_issues": "read", "create_issue": "write",
                               "delete_issue": "consent", "get_comments": "read"})
        for name, tier in (("list_projects", "read"), ("transition_issue", "write"),
                           ("send_mail", "consent"), ("merge_pr", "consent"),
                           ("frobnicate", "write")):
            self.assertEqual(profiles.mcp_tier({"name": name}), tier, name)


class RegistryRefusesWhatItShould(McpBase):
    def test_a_sound_mcp_connection_validates(self):
        self.assertIsNone(registry.validate_connection(self.conn(), "personal"))

    def test_a_literal_token_is_refused_and_never_echoed(self):
        c = self.conn()
        c["auth"]["token_ref"] = STALE
        with self.assertRaises(GatewayError) as cm:
            registry.validate_connection(c, "personal")
        self.assertNoToken(json.dumps(cm.exception.to_dict()))

    def test_plain_http_off_this_machine_is_refused(self):
        with self.assertRaises(GatewayError):
            registry.validate_connection(self.conn(endpoint="http://example.org/mcp",
                                                   network={"hosts": ["example.org"]}),
                                         "personal")

    def test_only_the_http_transport_in_this_release(self):
        with self.assertRaises(GatewayError) as cm:
            registry.validate_connection(self.conn(transport="stdio"), "personal")
        self.assertIn("transport", cm.exception.to_dict()["message"])


class TheDownstreamClient(McpBase):
    def client(self, **over):
        from lotrlib.conn_mcp import McpConnection
        c = self.conn(**over)
        return McpConnection(c, profiles.for_connection(c), timeout=5)

    def op(self, name):
        c = self.conn()
        return profiles.resolve_op(profiles.for_connection(c), name)

    def test_initialize_then_call_with_the_referenced_bearer(self):
        r = self.client().call(self.op("search_issues"), {"query": "assignee = me"})
        self.assertEqual(r["data"]["tool"], "search_issues")
        self.assertEqual(r["data"]["args"], {"query": "assignee = me"})
        methods = [m for m, _a, _s in self.httpd.seen]
        self.assertEqual(methods[:3], ["initialize", "notifications/initialized", "tools/call"])
        self.assertTrue(all(a == "Bearer " + FRESH for _m, a, _s in self.httpd.seen))
        self.assertEqual(self.httpd.seen[-1][2], "sess-1", "the session id is carried")

    def test_an_sse_framed_reply_is_read(self):
        self.httpd.sse = True
        r = self.client().call(self.op("search_issues"), {})
        self.assertEqual(r["data"]["items"], [{"key": "ABC-1"}])

    def test_discover_lists_the_downstream_tools(self):
        self.assertEqual([t["name"] for t in self.client().discover()],
                         [t["name"] for t in TOOLS])

    def test_a_401_runs_the_refresh_helper_and_retries_once(self):
        self.write_tokens(STALE)
        r = self.client().call(self.op("search_issues"), {})
        self.assertEqual(r["data"]["tool"], "search_issues")
        auths = [a for _m, a, _s in self.httpd.seen]
        self.assertEqual(auths[0], "Bearer " + STALE)
        self.assertEqual(auths[-1], "Bearer " + FRESH)
        self.assertNoToken(json.dumps(r.get("notes") or []))

    def test_without_a_refresh_helper_a_401_is_an_error_naming_the_ref(self):
        self.write_tokens(STALE)
        with self.assertRaises(GatewayError) as cm:
            self.client(refresh=False).call(self.op("search_issues"), {})
        d = json.dumps(cm.exception.to_dict())
        self.assertEqual(cm.exception.code, "auth_failed")
        self.assertIn("#access_token", d)
        self.assertNoToken(d)

    def test_a_refresh_that_does_not_help_fails_once_not_forever(self):
        self.write_tokens(STALE)
        self.refresh.write_text("import sys\nsys.exit(0)\n")       # refreshes nothing
        with self.assertRaises(GatewayError) as cm:
            self.client().call(self.op("search_issues"), {})
        self.assertEqual(cm.exception.code, "auth_failed")
        self.assertEqual(len([m for m, _a, _s in self.httpd.seen if m == "initialize"]), 2)


class ThroughTheGateway(McpBase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "gw"
        self.home.mkdir(mode=0o700)          # LOTR refuses a gateway home others can read
        (self.home / "gateway.json").write_text(json.dumps(
            {"schema": 1, "zone": "personal", "mode": "local",
             "local": {"allow": ["*"], "max_tier": "consent", "confirm": "dialog"}}))
        (self.home / "registry.json").write_text(json.dumps(
            {"schema": 1, "zone": "personal", "connections": [self.conn()], "clients": []}))
        for f in ("gateway.json", "registry.json"):
            os.chmod(self.home / f, 0o600)
        from lotrlib.engine import Engine
        self.engine = Engine(self.home, dialog=lambda text: True)

    def test_find_lists_downstream_tools_with_their_tiers(self):
        r = self.engine.find("search issues", connection="jira@personal")
        hits = {h["op"]: h for h in r["results"] if h.get("op")}
        self.assertEqual(hits["search_issues"]["tier"], "read")

    def test_a_read_tool_answers_through_call_read(self):
        r = self.engine.call("call_read", "jira@personal", "search_issues", {"query": "x"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["tool"], "search_issues")
        self.assertEqual(r["tier"], "read")

    def test_a_write_tool_through_call_read_names_the_right_tool(self):
        r = self.engine.call("call_read", "jira@personal", "create_issue", {"summary": "s"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "wrong_tool")
        self.assertIn("call_write", json.dumps(r["error"]))


class AddMcpCommand(McpBase):
    def lotr(self, *args):
        home = self.tmp / "gw2"
        if not (home / "registry.json").exists():
            home.mkdir(mode=0o700, exist_ok=True)
            (home / "gateway.json").write_text(json.dumps(
                {"schema": 1, "zone": "personal", "mode": "local"}))
            (home / "registry.json").write_text(json.dumps(
                {"schema": 1, "zone": "personal", "connections": [], "clients": []}))
            for f in ("gateway.json", "registry.json"):
                os.chmod(home / f, 0o600)
        env = dict(os.environ, LOTR_HOME=str(home))
        p = subprocess.run([sys.executable, str(GW / "scripts" / "lotr.py"), "--home", str(home)]
                           + list(args), capture_output=True, text=True, env=env, timeout=60)
        return p, home

    def test_add_mcp_discovers_and_caches_the_tools(self):
        p, home = self.lotr("add-mcp", "jira@personal",
                            "--endpoint", "http://127.0.0.1:%d/mcp" % self.port,
                            "--identity", "me @ fake",
                            "--auth-ref", "file:%s#access_token" % self.tokens,
                            "--refresh-cmd", "%s %s %s" % (sys.executable, self.refresh,
                                                           self.tokens))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        reg = json.loads((home / "registry.json").read_text())
        (c,) = reg["connections"]
        self.assertEqual((c["kind"], c["transport"]), ("mcp", "http"))
        self.assertEqual([t["name"] for t in c["tools"]], [t["name"] for t in TOOLS])
        self.assertIsInstance(c["refresh_cmd"], list)
        self.assertNoToken(p.stdout + p.stderr + (home / "registry.json").read_text())

    def test_a_literal_auth_ref_is_refused(self):
        p, home = self.lotr("add-mcp", "jira@personal",
                            "--endpoint", "http://127.0.0.1:%d/mcp" % self.port,
                            "--identity", "me", "--auth-ref", FRESH)
        self.assertNotEqual(p.returncode, 0)
        self.assertNoToken(p.stdout + p.stderr + (home / "registry.json").read_text())


if __name__ == "__main__":
    unittest.main()
