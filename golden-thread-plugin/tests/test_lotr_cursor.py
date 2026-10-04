"""gt-lotr cursors are server-side, bound and re-checked (0.20.1 review: forged cursors).

A cursor used to be unsigned base64 JSON passed through for ANY tool, so call_write plus a forged
cursor read an arbitrary path and bypassed both the read/write split and policy.deny.
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _harness import IS_WINDOWS, REPO, latest_version_dir

GW = latest_version_dir(REPO / "golden-thread-lotr")
sys.path.insert(0, str(GW / "scripts"))

from lotrlib.conn_http import HttpConnection  # noqa: E402
from lotrlib.engine import Engine  # noqa: E402
from lotrlib.errors import GatewayError  # noqa: E402

TOKEN = "cursor-test-not-a-real-token"


def _makeServer():
    class H(BaseHTTPRequestHandler):
        seen = []
        link = {"v": None}      # what page 1 points at (a test may poison it)

        def log_message(self, *a):
            pass

        def do_GET(self):
            H.seen.append(self.path)
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            hdr = {}
            if urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("page") == ["2"]:
                body = [{"number": 3, "title": "c", "state": "open"}]
            else:
                body = [{"number": 1, "title": "a", "state": "open"}]
                nxt = H.link["v"] or "/repos/acme/widgets/pulls?page=2"
                hdr["Link"] = f'<{base}{nxt}>; rel="next"' if nxt.startswith("/") else \
                    f'<{nxt}>; rel="next"'
            if self.path.startswith("/admin/"):
                body = {"secret_like": "private"}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            for k, v in hdr.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    return H


Srv = _makeServer()
CONN = "github@personal"
OWNER = {"owner": "acme", "repo": "widgets"}


def forge(url):
    return base64.urlsafe_b64encode(json.dumps({"u": url}).encode()).decode().rstrip("=")


class CursorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Srv)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def engine(self, policy=None, base_path=""):
        os.environ["LOTR_ALLOW_ENV_SECRETS"] = "1"
        os.environ["LOTR_CUR_TOKEN"] = TOKEN
        home = Path(tempfile.mkdtemp(dir=None if IS_WINDOWS else "/tmp", prefix="gwc"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "gateway.json").write_text(json.dumps(
            {"schema": 1, "zone": "personal", "mode": "local",
             "local": {"allow": ["*"], "max_tier": "consent", "confirm": "dialog"}}))

        def conn(cid):
            return {"id": cid, "identity": "t", "zone": "personal", "kind": "http",
                    "profile": "github", "description": "t",
                    "base_url": f"http://127.0.0.1:{self.port}{base_path}",
                    "network": {"hosts": ["127.0.0.1"]},
                    "auth": {"scheme": "bearer", "token_ref": "env:LOTR_CUR_TOKEN"},
                    "headers": {}, "trust": "T0", "enabled": True,
                    "policy": policy or {"deny": [], "consent": [], "write": [], "read": []}}
        (home / "registry.json").write_text(json.dumps(
            {"schema": 1, "zone": "personal", "connections": [conn(CONN), conn("other@personal")],
             "clients": []}))
        os.chmod(home / "registry.json", 0o600)
        os.chmod(home / "gateway.json", 0o600)
        Srv.seen.clear()
        Srv.link["v"] = None
        return Engine(home, dialog=lambda text: True)

    def page1(self, e, **kw):
        return e.call("call_read", CONN, "list_pulls", dict(OWNER), **kw)

    def test_legitimate_pagination_end_to_end(self):
        e = self.engine()
        r1 = self.page1(e)
        self.assertTrue(r1["ok"], r1)
        cur = r1["next_cursor"]
        self.assertTrue(cur.startswith("gtc_"))
        self.assertNotIn("http", cur)
        r2 = e.call("call_read", CONN, "list_pulls", dict(OWNER), cursor=cur)
        self.assertTrue(r2["ok"], r2)
        self.assertIsNone(r2["next_cursor"])
        self.assertEqual(Srv.seen[-1], "/repos/acme/widgets/pulls?page=2")

    def test_write_tool_refuses_any_cursor(self):
        e = self.engine()
        cur = self.page1(e)["next_cursor"]
        Srv.seen.clear()
        for c in (forge(f"http://127.0.0.1:{self.port}/admin/secrets"), cur):
            r = e.call("call_write", CONN, "create_issue", dict(OWNER, title="x"), cursor=c)
            self.assertEqual(r["error"]["code"], "cursor_needs_read", r)
        r = e.call("call_consent", CONN, "merge_pull", dict(OWNER, pull_number=1), cursor=cur)
        self.assertEqual(r["error"]["code"], "cursor_needs_read", r)
        self.assertEqual(Srv.seen, [])

    def test_forged_cursor_refused_on_read_tool(self):
        e = self.engine()
        for c in (forge(f"http://127.0.0.1:{self.port}/admin/secrets"), "gtc_nope", "x", 5):
            Srv.seen.clear()
            r = e.call("call_read", CONN, "list_pulls", dict(OWNER), cursor=c)
            self.assertEqual(r["error"]["code"], "bad_cursor", r)
            self.assertEqual(Srv.seen, [])

    def test_cursor_is_bound_to_connection_seat_op_and_expiry(self):
        e = self.engine()
        cur = self.page1(e)["next_cursor"]
        r = e.call("call_read", "other@personal", "list_pulls", dict(OWNER), cursor=cur)
        self.assertEqual(r["error"]["code"], "bad_cursor", r)
        r = e.call("call_read", CONN, "list_pulls", dict(OWNER), cursor=cur, client_id="seat2")
        self.assertFalse(r["ok"])                       # unknown seat: refused before any fetch
        with self.assertRaises(GatewayError):
            e._take_cursor(cur, CONN, "another-seat", "list_pulls")
        with self.assertRaises(GatewayError):
            e._take_cursor(cur, CONN, "local", "search_issues")
        e._cursors[cur]["exp"] = 0
        r = e.call("call_read", CONN, "list_pulls", dict(OWNER), cursor=cur)
        self.assertEqual(r["error"]["code"], "bad_cursor", r)

    def test_denied_path_via_cursor_is_refused(self):
        e = self.engine(policy={"deny": ["GET /admin/*"], "consent": [], "write": [], "read": []})
        Srv.link["v"] = "/admin/secrets"
        r1 = self.page1(e)
        self.assertTrue(r1["next_cursor"])
        Srv.seen.clear()
        r2 = e.call("call_read", CONN, "list_pulls", dict(OWNER), cursor=r1["next_cursor"])
        self.assertEqual(r2["error"]["code"], "op_denied", r2)
        self.assertEqual(Srv.seen, [])

    def test_traversal_out_of_the_base_path_is_refused(self):
        e = self.engine(base_path="/rest")
        h = HttpConnection(e.registry.connection(CONN), {"name": "t"},
                           secret_resolver=lambda *a, **k: "x")
        ok = f"http://127.0.0.1:{self.port}/rest/repos/a?page=2"
        self.assertEqual(h._check_cursor_url(ok), "/repos/a")
        for bad in ("/rest/../admin/secrets", "/rest/%2e%2e/admin", "/rest/%252e%252e/admin",
                    "/rest/a/./b", "/rest//x", "/restx/a", "/admin", "/rest/..%5cadmin",
                    "/rest/a\\b"):
            with self.assertRaises(GatewayError, msg=bad) as cm:
                h._check_cursor_url(f"http://127.0.0.1:{self.port}{bad}")
            self.assertEqual(cm.exception.code, "bad_cursor")
        for bad in (f"http://u:p@127.0.0.1:{self.port}/rest/a",
                    f"http://127.0.0.1:{self.port + 1}/rest/a",
                    f"https://127.0.0.1:{self.port}/rest/a", "http://evil.invalid/rest/a"):
            with self.assertRaises(GatewayError, msg=bad):
                h._check_cursor_url(bad)

    def test_traversal_next_link_is_refused_end_to_end(self):
        e = self.engine(base_path="")
        Srv.link["v"] = "/repos/../admin/secrets"
        r1 = self.page1(e)
        Srv.seen.clear()
        r2 = e.call("call_read", CONN, "list_pulls", dict(OWNER), cursor=r1["next_cursor"])
        self.assertEqual(r2["error"]["code"], "bad_cursor", r2)
        self.assertEqual(Srv.seen, [])

    def test_a_write_never_gets_a_cursor(self):
        e = self.engine()
        self.assertIsNone(e._issue_cursor("raw", "write", CONN, "local", "create_issue"))
        self.assertIsNone(e._issue_cursor("raw", "consent", CONN, "local", "merge_pull"))
        self.assertIsNone(e._issue_cursor(None, "read", CONN, "local", "list_pulls"))


if __name__ == "__main__":
    unittest.main()
