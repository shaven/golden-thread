"""gt-lotr: lotrlib.shaping and lotrlib.conn_http against a local HTTP server.

No external network: every request goes to a ThreadingHTTPServer on 127.0.0.1, and the
off-host cases (Link header, redirect, cursor) are refused before any connection is made.
The credential used throughout is a GitHub-shaped token so the credential scanner would
catch it too; every error path asserts the token text never appears.
"""
import base64
import json
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _harness import REPO, latest_version_dir

sys.path.insert(0, str(latest_version_dir(REPO / "golden-thread-lotr") / "scripts"))

from lotrlib import shaping                          # noqa: E402
from lotrlib.conn_http import HttpConnection, DRIFT_HINT, USER_AGENT   # noqa: E402
from lotrlib.errors import GatewayError              # noqa: E402

TOKEN = "ghp_" + "Zq7x" * 9                       # 40 chars, github_pat-shaped
LEAK = "ghp_" + "Lk9w" * 9


def _make_Handler():
    """Built in a function: tests/prun.py treats every top-level class as a test unit."""
    class _Handler(BaseHTTPRequestHandler):
        server_version = "fake"

        def log_message(self, *a):
            pass

        def _send(self, status, obj=None, headers=None, raw=None):
            body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
            self.send_response(status)
            if raw is None:
                self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _route(self):
            parts = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length else b""
            self.server.seen.append({"method": self.command, "path": parts.path, "query": q,
                                     "raw_query": parts.query, "headers": dict(self.headers),
                                     "body": raw_body})
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            p = parts.path
            if p.startswith("/api/echo") or p.startswith("/api/repos/"):
                body = json.loads(raw_body) if raw_body else None
                return self._send(200, {"method": self.command, "path": p, "query": q,
                                        "raw_query": parts.query, "body": body})
            if p == "/api/text":
                return self._send(200, raw=b"plain words, not json")
            if p == "/api/gh/items":
                page = int(q.get("page", "1"))
                hdr = {}
                if page < 2:
                    hdr["Link"] = (f'<{base}/api/gh/items?page={page + 1}>; rel="next", '
                                   f'<{base}/api/gh/items?page=2>; rel="last"')
                return self._send(200, [{"n": page}], hdr)
            if p == "/api/gh/evil":
                return self._send(200, [{"n": 1}],
                                  {"Link": '<http://evil.example.invalid/api/gh/items?page=2>; rel="next"'})
            if p == "/api/graph/items":
                if q.get("$skiptoken") == "p2":
                    return self._send(200, {"value": [{"n": 2}]})
                return self._send(200, {"value": [{"n": 1}],
                                        "@odata.nextLink": f"{base}/api/graph/items?$skiptoken=p2"})
            if p == "/api/jira/token":
                if q.get("nextPageToken") == "tok2":
                    return self._send(200, {"issues": [{"n": 2}], "isLast": True})
                return self._send(200, {"issues": [{"n": 1}], "nextPageToken": "tok2"})
            if p == "/api/jira/startat":
                start = int(q.get("startAt", "0"))
                return self._send(200, {"startAt": start, "maxResults": 2, "total": 3,
                                        "issues": [{"n": i} for i in range(start, min(start + 2, 3))]})
            if p == "/api/redirect-out":
                return self._send(302, {}, {"Location": "http://evil.example.invalid/x"})
            if p == "/api/leaky":
                return self._send(401, {"message": f"Bad credentials: {LEAK} was rejected"})
            if p == "/api/long":
                return self._send(500, {"message": "x" * 1000})
            if p == "/api/jira-errors":
                return self._send(400, {"errorMessages": ["JQL is invalid"], "errors": {}})
            return self._send(404, {"message": "Not Found"})

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _route
    return _Handler

_Handler = _make_Handler()


class GatewayHttpBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.seen = []
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.seen.clear()
        self.resolved = []

    def resolver(self, ref):
        self.resolved.append(ref)
        if ref == "env:GOOD":
            return TOKEN
        raise GatewayError("secret_missing", f"{ref} not found")

    def conn(self, scheme="bearer", base_path="/api", hosts=("127.0.0.1",), **auth):
        a = {"scheme": scheme, "token_ref": None if scheme == "none" else "env:GOOD",
             "user": None, "header": None}
        a.update(auth)
        return {"id": "test@personal", "identity": "tester @ local", "zone": "personal",
                "kind": "http", "profile": "generic",
                "base_url": f"http://127.0.0.1:{self.port}{base_path}",
                "auth": a, "headers": {}, "network": {"hosts": list(hosts)}, "trust": "T0"}

    def http(self, pagination=None, **kw):
        return HttpConnection(self.conn(**kw), {"name": "t", "pagination": pagination},
                              secret_resolver=self.resolver, timeout=5)

    @staticmethod
    def op(path, method="GET", name=None, query_defaults=None, params=None):
        return {"name": name, "method": method, "path": path,
                "query_defaults": query_defaults or {}, "params": params or {}}

    def assertNoToken(self, err, conn=None):
        blob = " ".join([str(err), repr(err), json.dumps(err.to_dict()), repr(err.args)])
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(TOKEN[4:], blob)
        if conn is not None:
            self.assertNotIn(TOKEN, repr(conn))
            self.assertNotIn(TOKEN, repr(vars(conn)))

    def raises(self, code, fn, *a, **kw):
        with self.assertRaises(GatewayError) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.code, code, cm.exception.to_dict())
        return cm.exception


class TestAuth(GatewayHttpBase):
    def test_bearer(self):
        h = self.http()
        r = h.call(self.op("/echo"), {})
        self.assertEqual(r["status"], 200)
        self.assertEqual(self.server.seen[0]["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(self.server.seen[0]["headers"]["User-Agent"], USER_AGENT)
        self.assertNotIn(TOKEN, repr(vars(h)))

    def test_basic(self):
        self.http(scheme="basic", user="me@example.com").call(self.op("/echo"), {})
        got = self.server.seen[0]["headers"]["Authorization"]
        self.assertTrue(got.startswith("Basic "))
        self.assertEqual(base64.b64decode(got[6:]).decode(), "me@example.com:" + TOKEN)

    def test_header(self):
        self.http(scheme="header", header="X-Api-Key").call(self.op("/echo"), {})
        hdrs = self.server.seen[0]["headers"]
        self.assertEqual(hdrs["X-Api-Key"], TOKEN)
        self.assertNotIn("Authorization", hdrs)

    def test_none(self):
        self.http(scheme="none").call(self.op("/echo"), {})
        self.assertNotIn("Authorization", self.server.seen[0]["headers"])
        self.assertEqual(self.resolved, [])

    def test_resolved_per_request(self):
        h = self.http()
        h.call(self.op("/echo"), {})
        h.call(self.op("/echo"), {})
        self.assertEqual(self.resolved, ["env:GOOD", "env:GOOD"])

    def test_resolver_error_names_ref(self):
        h = HttpConnection(self.conn(token_ref="env:MISSING"), {}, secret_resolver=self.resolver)
        e = self.raises("secret_missing", h.call, self.op("/echo"), {})
        self.assertIn("env:MISSING", e.message)
        self.assertEqual(self.server.seen, [])


class TestRequestBuilding(GatewayHttpBase):
    def test_path_params_quoted(self):
        r = self.http().call(self.op("/repos/{owner}/{repo}"), {"owner": "a b", "repo": "x/y"})
        self.assertEqual(self.server.seen[0]["path"], "/api/repos/a%20b/x%2Fy")
        self.assertEqual(r["data"]["query"], {})

    def test_missing_param(self):
        e = self.raises("missing_param", self.http().call,
                        self.op("/repos/{owner}", name="get_repo", params={"owner": "o"}), {})
        self.assertIn("owner", e.message)

    def test_query_merging_get(self):
        op = self.op("/echo", query_defaults={"maxResults": 20, "fields": "summary,status"})
        r = self.http().call(op, {"jql": "a = b", "maxResults": 5, "flag": True})
        self.assertEqual(r["data"]["query"],
                         {"maxResults": "5", "fields": "summary,status", "jql": "a = b",
                          "flag": "true"})
        self.assertIn("fields=summary,status", r["data"]["raw_query"])
        self.assertIsNone(r["data"]["body"])

    def test_explicit_query_verbatim_dollar_keys(self):
        op = self.op("/echo", query_defaults={"$top": 20})
        r = self.http().call(op, {"query": {"$select": "subject,from", "$top": 5}})
        self.assertEqual(r["data"]["query"], {"$top": "5", "$select": "subject,from"})
        self.assertIn("$select=subject,from", r["data"]["raw_query"])

    def test_delete_uses_query(self):
        r = self.http().call(self.op("/echo", method="DELETE"), {"force": "yes"})
        self.assertEqual(r["data"]["method"], "DELETE")
        self.assertEqual(r["data"]["query"], {"force": "yes"})

    def test_post_body_from_args(self):
        r = self.http().call(self.op("/repos/{owner}/issues", method="POST"),
                             {"owner": "o", "title": "T", "labels": ["a"]})
        self.assertEqual(r["data"]["method"], "POST")
        self.assertEqual(r["data"]["body"], {"title": "T", "labels": ["a"]})
        self.assertEqual(self.server.seen[0]["headers"]["Content-Type"], "application/json")

    def test_post_explicit_body_verbatim(self):
        r = self.http().call(self.op("/echo", method="POST"), {"body": {"$weird": [1, 2]}})
        self.assertEqual(r["data"]["body"], {"$weird": [1, 2]})

    def test_graphql_query_string_goes_to_body(self):
        r = self.http().call(self.op("/echo", method="POST", name="graphql"),
                             {"query": "query { viewer { login } }"})
        self.assertEqual(r["data"]["body"], {"query": "query { viewer { login } }"})
        self.assertEqual(r["data"]["query"], {})

    def test_text_fallback(self):
        r = self.http().call(self.op("/text"), {})
        self.assertEqual(r["data"], "plain words, not json")
        self.assertIsNone(r["next_cursor"])


class TestHostAllowList(GatewayHttpBase):
    def test_base_host_not_allowed(self):
        h = self.http(hosts=("api.github.com",))
        e = self.raises("host_not_allowed", h.call, self.op("/echo"), {})
        self.assertNoToken(e, h)
        self.assertEqual(self.server.seen, [])
        self.assertEqual(self.resolved, [])        # refused before the secret was even read

    def test_link_header_to_other_host_withholds_cursor_keeps_page(self):
        # Page 1 came from an allowed host; only the next link is off-list. The page is kept,
        # no cursor is issued, and a note says why -- never a cursor to a disallowed host.
        h = self.http(pagination="link-header")
        out = h.call(self.op("/gh/evil"), {})
        self.assertIsNone(out["next_cursor"])
        self.assertIsNotNone(out["data"])
        self.assertTrue(any("evil.example.invalid" in n for n in out["notes"]))
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_redirect_to_other_host_refused(self):
        h = self.http()
        e = self.raises("host_not_allowed", h.call, self.op("/redirect-out"), {})
        self.assertNoToken(e, h)


class TestPagination(GatewayHttpBase):
    def _two_pages(self, pagination, path, extract):
        h = self.http(pagination=pagination)
        r1 = h.call(self.op(path), {})
        self.assertTrue(r1["next_cursor"], f"{pagination}: no cursor on page 1")
        r2 = h.call(self.op(path), {}, cursor=r1["next_cursor"])
        self.assertEqual(extract(r1["data"]), [1] if pagination != "jira-startat" else [0, 1])
        self.assertEqual(extract(r2["data"]), [2])
        self.assertIsNone(r2["next_cursor"])
        auths = [s["headers"].get("Authorization") for s in self.server.seen]
        self.assertEqual(auths, ["Bearer " + TOKEN] * 2)
        return r1

    def test_link_header(self):
        r1 = self._two_pages("link-header", "/gh/items", lambda d: [x["n"] for x in d])
        url = json.loads(base64.urlsafe_b64decode(r1["next_cursor"] + "=="))["u"]
        self.assertTrue(url.endswith("/api/gh/items?page=2"))

    def test_odata(self):
        self._two_pages("odata", "/graph/items", lambda d: [x["n"] for x in d["value"]])
        self.assertEqual(self.server.seen[1]["query"], {"$skiptoken": "p2"})

    def test_jira_token(self):
        self._two_pages("jira-token", "/jira/token", lambda d: [x["n"] for x in d["issues"]])
        self.assertEqual(self.server.seen[1]["query"]["nextPageToken"], "tok2")

    def test_jira_startat(self):
        self._two_pages("jira-startat", "/jira/startat", lambda d: [x["n"] for x in d["issues"]])
        self.assertEqual(self.server.seen[1]["query"]["startAt"], "2")

    def test_no_pagination_profile(self):
        r = self.http(pagination=None).call(self.op("/gh/items"), {})
        self.assertIsNone(r["next_cursor"])

    def _cursor(self, url):
        return base64.urlsafe_b64encode(json.dumps({"u": url}).encode()).decode().rstrip("=")

    def test_tampered_cursor_other_host(self):
        h = self.http(pagination="link-header")
        self.raises("bad_cursor", h.call, self.op("/gh/items"), {},
                    cursor=self._cursor("http://evil.example.invalid/api/gh/items?page=2"))
        self.assertEqual(self.server.seen, [])

    def test_tampered_cursor_outside_base_path(self):
        h = self.http(pagination="link-header")
        for path in ("/other/gh/items", "/apix/gh/items"):
            self.raises("bad_cursor", h.call, self.op("/gh/items"), {},
                        cursor=self._cursor(f"http://127.0.0.1:{self.port}{path}"))
        self.assertEqual(self.server.seen, [])

    def test_garbage_cursor(self):
        h = self.http(pagination="link-header")
        for bad in ("!!!", "bm90IGpzb24", self._cursor("x").replace("eyJ", "eyK"),
                    base64.urlsafe_b64encode(b'{"x": 1}').decode()):
            self.raises("bad_cursor", h.call, self.op("/gh/items"), {}, cursor=bad)


class TestErrors(GatewayHttpBase):
    def test_404_curated_gets_drift_hint(self):
        h = self.http()
        e = self.raises("http_404", h.call, self.op("/nope", name="get_repo"), {})
        self.assertIn(DRIFT_HINT, e.hints)
        self.assertEqual(e.message, "Not Found")
        self.assertNoToken(e, h)

    def test_404_raw_op_no_drift_hint(self):
        e = self.raises("http_404", self.http().call, self.op("/nope"), {})
        self.assertNotIn(DRIFT_HINT, e.hints)

    def test_credential_in_error_body_withheld(self):
        h = self.http()
        e = self.raises("http_401", h.call, self.op("/leaky"), {})
        blob = json.dumps(e.to_dict()) + str(e) + repr(e)
        self.assertNotIn(LEAK, blob)
        self.assertNotIn(LEAK[4:20], blob)
        self.assertIn("withheld", e.message)
        self.assertNoToken(e, h)

    def test_long_error_truncated(self):
        e = self.raises("http_500", self.http().call, self.op("/long"), {})
        self.assertLessEqual(len(e.message), 300)

    def test_jira_error_messages(self):
        e = self.raises("http_400", self.http().call, self.op("/jira-errors"), {})
        self.assertEqual(e.message, "JQL is invalid")

    def test_network_error_no_secret(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        c = self.conn()
        c["base_url"] = f"http://127.0.0.1:{dead}/api"
        h = HttpConnection(c, {}, secret_resolver=self.resolver, timeout=3)
        e = self.raises("network_error", h.call, self.op("/echo"), {})
        self.assertNoToken(e, h)
        self.assertIsNone(e.__context__)
        self.assertIsNone(e.__cause__)


class TestShaping(unittest.TestCase):
    def test_project_paths(self):
        data = {"issues": [{"key": "A-1", "fields": {"summary": "s1", "status": {"name": "Open"}}},
                           {"key": "A-2", "fields": {"summary": "s2"}}],
                "total": 2, "noise": 1}
        got = shaping.project(data, "issues[].key, issues[].fields.status.name,total,missing.x")
        self.assertEqual(got, {"issues": [{"key": "A-1", "fields": {"status": {"name": "Open"}}},
                                          {"key": "A-2"}],
                               "total": 2})

    def test_project_top_level_list(self):
        data = [{"number": 1, "title": "t", "user": {"login": "u"}}, {"number": 2}]
        self.assertEqual(shaping.project(data, "number,user.login"),
                         [{"number": 1, "user": {"login": "u"}}, {"number": 2}])

    def test_project_none(self):
        self.assertEqual(shaping.project({"a": 1}, None), {"a": 1})
        self.assertEqual(shaping.project({"a": 1}, ""), {"a": 1})

    def test_noise_dropped(self):
        data = {"id": 1, "url": "keep", "html_url": "keep", "clone_url": "drop",
                "avatar_url": "drop", "node_id": "drop", "_links": {}, "self": "drop",
                "custom": "drop", "user": {"login": "u", "followers_url": "drop"}}
        out, notes = shaping.shape(data, noise_keys=["custom"])
        self.assertEqual(out, {"id": 1, "url": "keep", "html_url": "keep",
                               "user": {"login": "u"}})
        self.assertEqual(notes, [])

    def test_select_applies(self):
        out, _ = shaping.shape({"a": {"b": 1, "c": 2}, "d": 3}, select="a.b")
        self.assertEqual(out, {"a": {"b": 1}})

    def test_list_truncation(self):
        out, notes = shaping.shape({"items": list(range(57))}, default_page=20)
        self.assertEqual(out["items"], list(range(20)))
        self.assertEqual(len(notes), 1)
        self.assertIn("items", notes[0])
        self.assertIn("37 of 57", notes[0])

    def test_max_chars(self):
        data = [{"title": "x" * 100, "n": i} for i in range(15)]
        out, notes = shaping.shape(data, max_chars=500, default_page=50)
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False)), 500)
        self.assertTrue(0 < len(out) < 15)
        self.assertTrue(any("select" in n and "limit" in n for n in notes), notes)

    def test_max_chars_dict_and_scalar(self):
        out, notes = shaping.shape({"total": 9, "values": ["y" * 50] * 30},
                                   max_chars=400, default_page=100)
        self.assertEqual(out["total"], 9)
        self.assertLessEqual(len(json.dumps(out)), 400)
        out, notes = shaping.shape("z" * 1000, max_chars=100)
        self.assertEqual(len(out), 100)
        self.assertTrue(notes)

    def test_scan_credentials_rules(self):
        samples = {
            "github_pat": TOKEN,
            "slack_token": "xoxb-" + "1234567890-abcdefghij",
            "aws_access_key": "AKIA" + "ABCDEFGHIJKLMNOP",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
            "bearer_token": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "atlassian_token": "ATATT" + "3xFfGF0" * 5,
        }
        for rule, text in samples.items():
            data = {"a": {"b": ["ok", "ok", f"see {text} here"]}}
            found = shaping.scan_credentials(data)
            self.assertIn(rule, [f["rule"] for f in found], rule)
            hit = [f for f in found if f["rule"] == rule][0]
            self.assertEqual(hit["path"], "a.b[2]")
            self.assertIsInstance(hit["length"], int)
            self.assertNotIn(text[:12], json.dumps(found), f"{rule}: matched text leaked")
            self.assertEqual(set(hit), {"path", "rule", "length"})
        self.assertEqual(shaping.scan_credentials(TOKEN)[0],
                         {"path": "", "rule": "github_pat", "length": 40})

    def test_scan_clean(self):
        self.assertEqual(shaping.scan_credentials({"a": ["hello", 1, None, {"k": "ghp_short"}],
                                                   "bearer": "Bearer short"}), [])


if __name__ == "__main__":
    unittest.main()
