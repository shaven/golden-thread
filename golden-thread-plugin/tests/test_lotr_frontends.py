"""gt-lotr front ends: unix socket server, HTTP hub server, Client, CLI, daemon, MCP shim.

The engine is a FAKE here (find/call/status/catalog_text per SPEC), so these tests pin the
transports and the MCP surface, not the engine. Socket dirs live under /tmp because macOS
caps a unix socket path at 104 bytes.
"""
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from _harness import REPO, latest_version_dir
from _harness import skip_on_windows
from _harness import SCRIPTS as GT_SCRIPTS


def _gateway_dir():
    root = REPO / "golden-thread-lotr"
    try:
        return latest_version_dir(root)
    except RuntimeError:
        cands = [d for d in root.iterdir() if d.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+", d.name)]
        return max(cands, key=lambda d: tuple(int(x) for x in d.name.split(".")))


SCRIPTS = _gateway_dir() / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _harness import IS_WINDOWS                 # noqa: E402
from lotrlib import server                      # noqa: E402
from lotrlib.client import Client               # noqa: E402
from lotrlib.errors import GatewayError         # noqa: E402

# 0.3.0: on native Windows lotrd's local door is gt core's named pipe (gt_ipc), found through
# GT_HOOKS_DIR -- gt core of the release under test. The runner below serves the pipe for the
# home there, so the CLI and MCP-shim tests run unchanged; the tests of the unix socket's own
# mechanics skip with this reason.
WIN_UNIX_SOCKET = ("Unix-domain socket mechanics (raw AF_UNIX clients, a 600 socket file, "
                   "SIGTERM cleanup); the Windows door is gt_ipc's named pipe, exercised by the "
                   "CLI / shim tests here and test_lotr_unlock.WindowsPipe")
_SAVED_HOOKS = []


def setUpModule():
    if IS_WINDOWS:
        _SAVED_HOOKS.append(os.environ.get("GT_HOOKS_DIR"))
        os.environ["GT_HOOKS_DIR"] = str(GT_SCRIPTS)


def tearDownModule():
    if IS_WINDOWS and _SAVED_HOOKS:
        if _SAVED_HOOKS[0] is None:
            os.environ.pop("GT_HOOKS_DIR", None)
        else:
            os.environ["GT_HOOKS_DIR"] = _SAVED_HOOKS[0]

SECRET = "s3cr3t-value-ABCDEFGHIJKLMNOP"


class FakeEngine:
    def __init__(self):
        self.calls = []

    def find(self, query="", connection=None, limit=8, detail="summary", client_id=None):
        self.calls.append(("find", query, connection, limit, detail, client_id))
        if query == "boom":
            raise RuntimeError("leaky detail " + SECRET)
        if query == "gwerr":
            raise GatewayError("unknown_connection", "no such", hints=["github@personal"])
        return {"ok": True, "hits": [{"key": "github@personal.list_pulls", "summary": "List PRs",
                                      "tier": "read"}], "client": client_id, "query": query}

    def call(self, tool, connection, op, args=None, select=None, cursor=None, client_id=None):
        self.calls.append(("call", tool, connection, op, args, select, cursor, client_id))
        if op == "fail":
            return {"ok": False, "error": {"code": "http_404", "message": "nope", "hints": []}}
        return {"ok": True, "connection": connection, "identity": "me", "client": client_id,
                "op": op, "tier": tool.split("_", 1)[1], "data": {"args": args or {}},
                "next_cursor": None, "notes": [], "untrusted": True}

    def status(self):
        return {"ok": True, "connections": 1}

    def catalog_text(self, max_chars=1800):
        return "CATALOG: github@personal (shaven @ github.com)"[:max_chars]


class FakeRegistry:
    """Mirrors Registry.authenticate's codes: unknown_client / auth_failed / client_revoked."""

    def __init__(self):
        self.clients = {"mbp": {"id": "mbp", "secret": SECRET, "revoked": None},
                        "old": {"id": "old", "secret": SECRET, "revoked": "2026-09-30T00:00:00Z"}}

    def authenticate(self, client_id, secret):
        c = self.clients.get(client_id)
        if c is None:
            raise GatewayError("unknown_client", f"no client {client_id!r}")
        if secret != c["secret"]:
            raise GatewayError("auth_failed", f"client {client_id} failed authentication")
        if c["revoked"]:
            raise GatewayError("client_revoked", f"client {client_id} revoked")
        return {"id": client_id}


def _sockdir():
    return tempfile.mkdtemp(prefix="gw", dir=None if IS_WINDOWS else "/tmp")


class _UnixRunner:
    def __init__(self, sock, engine=None):
        self.sock = sock
        self.engine = engine or FakeEngine()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.err = None
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            if IS_WINDOWS:     # the pipe for the home the socket would have lived in
                server.serve_pipe(lambda: self.engine, os.path.dirname(self.sock),
                                  stop_event=self.stop, on_ready=lambda p: self.ready.set())
                return
            server.serve_unix(lambda: self.engine, self.sock, stop_event=self.stop,
                              on_ready=lambda p: self.ready.set())
        except Exception as e:                # noqa: BLE001
            self.err = e
            self.ready.set()

    def __enter__(self):
        self.t.start()
        assert self.ready.wait(5), "unix server never came up"
        if self.err:
            raise self.err
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join(5)


class _HttpRunner:
    def __init__(self, engine=None, registry=None):
        self.engine = engine or FakeEngine()
        self.registry = registry or FakeRegistry()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.port = None
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        def ready(host, port):
            self.port = port
            self.ready.set()
        server.serve_http(lambda: self.engine, "127.0.0.1", 0, registry_getter=lambda: self.registry,
                          stop_event=self.stop, on_ready=ready)

    def __enter__(self):
        self.t.start()
        assert self.ready.wait(5), "http server never came up"
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join(5)

    def post(self, path, body=b"{}", headers=None, method="POST"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body=body if method == "POST" else None, headers=headers or {})
            r = c.getresponse()
            return r.status, json.loads(r.read().decode() or "null")
        finally:
            c.close()


def _auth(cid="mbp", secret=SECRET):
    return {"Authorization": "Bearer " + secret, "X-GT-Client": cid,
            "Content-Type": "application/json"}


@skip_on_windows(WIN_UNIX_SOCKET)
class UnixServerTests(unittest.TestCase):
    def setUp(self):
        self.dir = _sockdir()
        self.sock = os.path.join(self.dir, "s.sock")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_roundtrip_all_methods(self):
        with _UnixRunner(self.sock) as r:
            c = Client(sock_path=self.sock)
            self.assertTrue(c.request("ping", {})["pong"])
            res = c.request("find", {"query": "prs", "limit": 3})
            self.assertEqual(res["client"], "local")
            self.assertEqual(r.engine.calls[-1], ("find", "prs", None, 3, "summary", "local"))
            env = c.request("call", {"tool": "call_read", "connection": "github@personal",
                                     "op": "list_pulls", "args": {"state": "open"}})
            self.assertTrue(env["ok"])
            self.assertEqual(env["client"], "local")
            self.assertEqual(env["data"]["args"], {"state": "open"})
            self.assertEqual(c.request("status", {})["connections"], 1)
            self.assertIn("CATALOG", c.request("catalog", {})["text"])

    def test_errors(self):
        with _UnixRunner(self.sock):
            c = Client(sock_path=self.sock)
            with self.assertRaises(GatewayError) as cm:
                c.request("find", {"query": "gwerr"})
            self.assertEqual(cm.exception.code, "unknown_connection")
            self.assertEqual(cm.exception.hints, ["github@personal"])
            with self.assertRaises(GatewayError) as cm:
                c.request("find", {"query": "boom"})
            self.assertEqual(cm.exception.code, "internal")
            self.assertEqual(cm.exception.message, "RuntimeError")
            self.assertNotIn(SECRET, str(cm.exception.to_dict()))
            for m in ("enroll", "revoke", "nope"):
                with self.assertRaises(GatewayError) as cm:
                    c.request(m, {})
                self.assertEqual(cm.exception.code, "unknown_method")
            with self.assertRaises(GatewayError) as cm:
                c.request("call", {"tool": "call_read"})
            self.assertEqual(cm.exception.code, "bad_request")

    def test_bad_json_line_and_multiple_requests_per_connection(self):
        with _UnixRunner(self.sock):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(self.sock)
            f = s.makefile("rwb")
            f.write(b"not json\n{\"id\": 7, \"method\": \"ping\"}\n")
            f.flush()
            a = json.loads(f.readline())
            b = json.loads(f.readline())
            s.close()
            self.assertEqual(a["error"]["code"], "bad_request")
            self.assertEqual(b["id"], 7)
            self.assertTrue(b["result"]["pong"])

    def test_socket_mode_600_and_removed_on_stop(self):
        with _UnixRunner(self.sock):
            mode = stat.S_IMODE(os.stat(self.sock).st_mode)
            self.assertEqual(mode, 0o600, oct(mode))
        self.assertFalse(os.path.exists(self.sock))

    def test_stale_socket_replaced_live_one_refused(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.sock)                 # a socket file nobody listens on
        stale.close()
        self.assertTrue(os.path.exists(self.sock))
        with _UnixRunner(self.sock):
            self.assertTrue(server.socket_alive(self.sock))
            second = _UnixRunner(self.sock)
            second.t.start()
            second.ready.wait(5)
            self.assertIsInstance(second.err, GatewayError)
            self.assertEqual(second.err.code, "daemon_running")
            self.assertTrue(server.socket_alive(self.sock), "the live daemon's socket was stolen")

    def test_engine_factory_failure_not_cached(self):
        n = {"i": 0}

        def factory():
            n["i"] += 1
            if n["i"] == 1:
                raise GatewayError("registry_invalid", "bad field x")
            return FakeEngine()
        stop, ready = threading.Event(), threading.Event()
        t = threading.Thread(target=server.serve_unix, args=(factory, self.sock),
                             kwargs={"stop_event": stop, "on_ready": lambda p: ready.set()},
                             daemon=True)
        t.start()
        ready.wait(5)
        try:
            c = Client(sock_path=self.sock)
            self.assertTrue(c.request("ping")["pong"])     # ping needs no engine
            with self.assertRaises(GatewayError) as cm:
                c.request("status")
            self.assertEqual(cm.exception.code, "registry_invalid")
            self.assertEqual(c.request("status")["connections"], 1)
        finally:
            stop.set()
            t.join(5)


class HttpServerTests(unittest.TestCase):
    def test_auth_codes(self):
        with _HttpRunner() as h:
            self.assertEqual(h.post("/v1/find")[0], 401)
            self.assertEqual(h.post("/v1/find", headers={"Authorization": "Bearer " + SECRET})[0], 401)
            st, body = h.post("/v1/find", headers=_auth(secret="wrong"))
            self.assertEqual(st, 401)
            self.assertEqual(body["error"]["code"], "unauthorized")
            self.assertEqual(h.post("/v1/find", headers=_auth(cid="ghost"))[0], 401)
            self.assertEqual(h.post("/v1/find", headers=_auth(cid="old"))[0], 403)
            st, body = h.post("/v1/find", body=json.dumps({"query": "prs"}).encode(), headers=_auth())
            self.assertEqual(st, 200)
            self.assertEqual(body["result"]["client"], "mbp")
            self.assertEqual(h.engine.calls[-1][-1], "mbp")
            self.assertNotIn(SECRET, json.dumps(body))

    def _refused_with_a_body(self, h, path, headers, late=False):
        """POST `path` with a 200 KB body the server refuses before reading it, and read the
        reply only after the server has replied and closed: -> the status line.

        Two orderings, both of which the old server turned into a reset. `late=False`: headers
        and body in one go, so the body sits unread when the server closes; closing a socket
        with unread data sends RST, and macOS and Linux then discard the reply the client had
        not read (ECONNRESET on every run). `late=True`: the body follows only after the server
        has replied and closed -- what http.client does, headers and body in two send() calls,
        when it is descheduled between them -- so it reaches a closed socket, which answers
        RST; on Windows that is the WinError 10053/10054 test_auth_codes hit under load."""
        body = b"x" * 200000                    # far beyond any read buffer, under MAX_BODY
        hdrs = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        head = (f"POST {path} HTTP/1.1\r\nHost: x\r\n{hdrs}"
                f"Content-Length: {len(body)}\r\n\r\n").encode()
        s = socket.create_connection(("127.0.0.1", h.port), timeout=10)
        try:
            try:
                if late:
                    s.sendall(head)
                    time.sleep(0.3)             # the server has replied and closed
                    s.sendall(body)
                else:
                    s.sendall(head + body)
            except OSError:
                pass                            # a reset while sending: the read below says
            time.sleep(0.3)                     # the reply and the close (or RST) are in
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            s.close()
        return data.split(b"\r\n", 1)[0].decode()

    def test_a_refused_request_with_a_body_gets_its_reply_not_a_reset(self):
        with _HttpRunner() as h:
            for late in (False, True):
                for code, path, headers in ((401, "/v1/find", _auth(secret="no")),
                                            (403, "/v1/find", _auth(cid="old")),
                                            (404, "/v1/enroll", _auth()),
                                            (401, "/v1/find", {})):
                    with self.subTest(code=code, path=path, late=late):
                        self.assertIn(" %d " % code,
                                      self._refused_with_a_body(h, path, headers, late=late))

    def test_admin_and_other_routes_404(self):
        with _HttpRunner() as h:
            for path in ("/v1/enroll", "/v1/add", "/v1/revoke", "/v1/reload", "/v2/find", "/",
                         "/v1/find/x"):
                self.assertEqual(h.post(path, headers=_auth())[0], 404, path)
            self.assertEqual(h.post("/v1/enroll")[0], 404)
            self.assertEqual(h.post("/v1/find", headers=_auth(), method="GET")[0], 404)

    def test_body_cap_and_bad_json(self):
        with _HttpRunner() as h:
            # announce an oversized body and send none: the server must refuse on the header
            # alone, without reading (or waiting for) the body
            s = socket.create_connection(("127.0.0.1", h.port), timeout=5)
            hdrs = "".join(f"{k}: {v}\r\n" for k, v in _auth().items())
            s.sendall((f"POST /v1/find HTTP/1.1\r\nHost: x\r\n{hdrs}"
                       f"Content-Length: {1024 * 1024 + 1}\r\n\r\n").encode())
            status_line = s.makefile("rb").readline().decode()
            s.close()
            self.assertIn(" 413 ", status_line)
            self.assertEqual(h.post("/v1/find", body=b"{nope", headers=_auth())[0], 400)
            st, body = h.post("/v1/call", body=b'{"tool":"call_read","connection":"c@z","op":"fail"}',
                              headers=_auth())
            self.assertEqual(st, 200)
            self.assertFalse(body["result"]["ok"])

    def test_client_over_http_from_home(self):
        with _HttpRunner() as h:
            home = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, home, True)
            cfg = {"schema": 1, "zone": "personal", "mode": "client",
                   "hub": {"url": f"http://127.0.0.1:{h.port}", "client_id": "mbp",
                           "credential_ref": "file:/nowhere", "timeout_s": 2}}
            (home / "gateway.json").write_text(json.dumps(cfg))
            seen = []

            def resolver(ref):
                seen.append(ref)
                return SECRET
            c = Client.from_home(home, resolver=resolver)
            self.assertEqual(c.request("find", {"query": "x"})["client"], "mbp")
            self.assertEqual(seen, ["file:/nowhere"])
            self.assertFalse(any(SECRET in str(v) for v in vars(c).values()))
            bad = Client.from_home(home, resolver=lambda ref: "wrong")
            with self.assertRaises(GatewayError) as cm:
                bad.request("find", {})
            self.assertEqual(cm.exception.code, "unauthorized")
            with self.assertRaises(GatewayError) as cm:
                c.request("enroll", {})
            self.assertEqual(cm.exception.code, "not_found")

    def test_hub_down_is_daemon_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        c = Client(url=f"http://127.0.0.1:{port}", client_id="mbp", credential_ref="x",
                   resolver=lambda r: SECRET, timeout=3)
        with self.assertRaises(GatewayError) as cm:
            c.request("ping")
        self.assertEqual(cm.exception.code, "daemon_unreachable")
        self.assertNotIn(SECRET, str(cm.exception.to_dict()))


class ClientTests(unittest.TestCase):
    def test_daemon_down_is_daemon_unreachable(self):
        home = Path(_sockdir())
        self.addCleanup(shutil.rmtree, home, True)
        (home / "gateway.json").write_text(json.dumps({"schema": 1, "mode": "local"}))
        c = Client.from_home(home)
        with self.assertRaises(GatewayError) as cm:
            c.request("ping")
        self.assertEqual(cm.exception.code, "daemon_unreachable")
        self.assertTrue(any("lotrd --home" in h for h in cm.exception.hints))

    def test_hybrid_refused_and_missing_config(self):
        home = Path(_sockdir())
        self.addCleanup(shutil.rmtree, home, True)
        with self.assertRaises(GatewayError) as cm:
            Client.from_home(home)
        self.assertEqual(cm.exception.code, "not_initialized")
        (home / "gateway.json").write_text(json.dumps({"schema": 1, "mode": "hybrid"}))
        with self.assertRaises(GatewayError) as cm:
            Client.from_home(home)
        self.assertEqual(cm.exception.code, "not_implemented")


def _run(script, *args, inp=None, env=None, timeout=30):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], input=inp,
                          capture_output=True, text=True, timeout=timeout, env=e)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(_sockdir())
        self.sock = str(self.home / "d.sock")
        (self.home / "gateway.json").write_text(
            json.dumps({"schema": 1, "zone": "personal", "mode": "local", "socket": self.sock}))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_find_read_status_catalog(self):
        with _UnixRunner(self.sock) as r:
            p = _run("lotr.py", "--home", str(self.home), "find", "pulls", "--limit", "2", "--schema")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout)["query"], "pulls")
            self.assertEqual(r.engine.calls[-1], ("find", "pulls", None, 2, "schema", "local"))

            p = _run("lotr.py", "--home", str(self.home), "read", "github@personal", "list_pulls",
                     "--args", '{"owner": "o"}', "state=open", "per_page=5", "--select", "x")
            self.assertEqual(p.returncode, 0, p.stderr)
            env = json.loads(p.stdout)
            self.assertEqual(env["tier"], "read")
            self.assertEqual(r.engine.calls[-1],
                             ("call", "call_read", "github@personal", "list_pulls",
                              {"owner": "o", "state": "open", "per_page": 5}, "x", None, "local"))

            p = _run("lotr.py", "--home", str(self.home), "consent", "c@z", "fail")
            self.assertEqual(p.returncode, 1)
            self.assertFalse(json.loads(p.stdout)["ok"])

            p = _run("lotr.py", "--home", str(self.home), "status")
            self.assertEqual((p.returncode, json.loads(p.stdout)["connections"]), (0, 1))
            p = _run("lotr.py", "--home", str(self.home), "catalog")
            self.assertIn("CATALOG", json.loads(p.stdout)["text"])

    def test_daemon_down_and_usage(self):
        p = _run("lotr.py", "--home", str(self.home), "find", "x")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(p.stdout)["error"]["code"], "daemon_unreachable")
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(_run("lotr.py", "--home", str(self.home), "frobnicate").returncode, 2)
        self.assertEqual(_run("lotr.py", "--home", str(self.home), "read", "c@z").returncode, 2)
        self.assertEqual(_run("lotr.py", "--home", str(self.home), "read", "c@z", "op",
                              "--args", "[1]").returncode, 2)

    def test_init_writes_files_mode_600(self):
        home = self.home / "fresh"
        p = _run("lotr.py", "--home", str(home), "init", "--zone", "work", "--mode", "hub")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for name in ("gateway.json", "registry.json"):
            if not IS_WINDOWS:                                       # WIN_MODE_BITS
                self.assertEqual(stat.S_IMODE((home / name).stat().st_mode), 0o600, name)
        cfg = json.loads((home / "gateway.json").read_text())
        self.assertEqual((cfg["zone"], cfg["mode"]), ("work", "hub"))
        reg = json.loads((home / "registry.json").read_text())
        self.assertEqual(reg, {"schema": 1, "zone": "work", "connections": [], "clients": []})
        self.assertEqual(_run("lotr.py", "--home", str(home), "init").returncode, 1)
        p = _run("lotr.py", "--home", str(self.home / "h2"), "init", "--mode", "hybrid")
        self.assertEqual(json.loads(p.stdout)["error"]["code"], "not_implemented")

    def _registry_available(self):
        # 0.3.0: the path said lotr/ (never existed), so this test always skipped; lotrlib/.
        return (SCRIPTS / "lotrlib" / "registry.py").is_file()

    def test_enroll_never_prints_secret(self):
        if not self._registry_available():
            self.skipTest("lotrlib/registry.py not written yet")
        home = self.home / "hub"
        self.assertEqual(_run("lotr.py", "--home", str(home), "init", "--mode", "hub").returncode, 0)
        out = self.home / "mbp.secret"
        p = _run("lotr.py", "--home", str(home), "enroll", "mbp-shaven", "--machine", "MacBook Pro",
                 "--max-tier", "write", "--secret-out", str(out))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        secret = out.read_text().strip()
        self.assertGreaterEqual(len(secret), 20)
        if not IS_WINDOWS:                                           # WIN_MODE_BITS
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertNotIn(secret, p.stdout)
        self.assertNotIn(secret, p.stderr)
        self.assertNotIn(secret, (home / "registry.json").read_text())
        # refuses an existing path, and does not enroll anyone when it does
        p = _run("lotr.py", "--home", str(home), "enroll", "other", "--machine", "M",
                 "--secret-out", str(out))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(p.stdout)["error"]["code"], "secret_out_exists")
        self.assertEqual(out.read_text().strip(), secret)
        p = _run("lotr.py", "--home", str(home), "connections")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        listed = json.loads(p.stdout)
        self.assertEqual([c["id"] for c in listed["clients"]], ["mbp-shaven"])
        self.assertNotIn(secret, p.stdout)
        p = _run("lotr.py", "--home", str(home), "revoke", "mbp-shaven")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        listed = json.loads(_run("lotr.py", "--home", str(home), "connections").stdout)
        self.assertTrue(listed["clients"][0]["revoked"])

    def test_admin_refused_in_client_mode(self):
        home = self.home / "cl"
        self.assertEqual(_run("lotr.py", "--home", str(home), "init", "--mode", "client").returncode, 0)
        p = _run("lotr.py", "--home", str(home), "enroll", "x", "--machine", "M",
                 "--secret-out", str(self.home / "x.secret"))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(p.stdout)["error"]["code"], "admin_refused")
        self.assertFalse((self.home / "x.secret").exists())


class DaemonTests(unittest.TestCase):
    def test_refuses_client_and_hybrid_modes(self):
        home = Path(_sockdir())
        self.addCleanup(shutil.rmtree, home, True)
        for mode, code in (("client", "no_daemon_in_client_mode"), ("hybrid", "not_implemented")):
            (home / "gateway.json").write_text(json.dumps({"schema": 1, "mode": mode}))
            p = _run("lotrd.py", "--home", str(home))
            self.assertEqual(p.returncode, 1)
            self.assertEqual(json.loads(p.stderr)["error"]["code"], code)

    @skip_on_windows(WIN_UNIX_SOCKET)
    def test_sigterm_removes_socket(self):
        home = Path(_sockdir())
        self.addCleanup(shutil.rmtree, home, True)
        sock = home / "lotrd.sock"
        (home / "gateway.json").write_text(json.dumps({"schema": 1, "mode": "local"}))
        proc = subprocess.Popen([sys.executable, str(SCRIPTS / "lotrd.py"), "--home", str(home)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not server.socket_alive(sock):
                time.sleep(0.1)
            self.assertTrue(server.socket_alive(sock), "daemon never answered ping")
            self.assertEqual(stat.S_IMODE(os.stat(sock).st_mode), 0o600)
            proc.terminate()
            self.assertEqual(proc.wait(10), 0)
            self.assertFalse(sock.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
            proc.stdout.close()
            proc.stderr.close()


class McpShimTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(_sockdir())
        self.sock = str(self.home / "d.sock")
        (self.home / "gateway.json").write_text(
            json.dumps({"schema": 1, "zone": "personal", "mode": "local", "socket": self.sock}))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def _session(self, messages):
        inp = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages)
        p = _run("lotr_mcp.py", "--home", str(self.home), inp=inp)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]

    def _init(self, version="2025-06-18", mid=1):
        return {"jsonrpc": "2.0", "id": mid, "method": "initialize",
                "params": {"protocolVersion": version, "capabilities": {},
                           "clientInfo": {"name": "t", "version": "0"}}}

    def test_full_session_forwarded(self):
        with _UnixRunner(self.sock) as r:
            out = self._session([
                self._init("2025-11-25"),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "find", "arguments": {"query": "pulls", "limit": 2}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "call_read", "arguments": {
                     "connection": "github@personal", "op": "list_pulls",
                     "args": {"state": "open"}, "select": "x"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "call_write", "arguments": {"connection": "c@z", "op": "fail"}}},
                {"jsonrpc": "2.0", "id": 6, "method": "ping"},
            ])
        by_id = {m["id"]: m for m in out}
        self.assertEqual(sorted(by_id), [1, 2, 3, 4, 5, 6], "a notification got a reply")
        init = by_id[1]["result"]
        self.assertEqual(init["protocolVersion"], "2025-11-25")
        # The shim names the release it ships in (0.3.0 pinned "0.3.0" here; 0.4.0 bumped it).
        self.assertEqual(init["serverInfo"], {"name": "gt-lotr", "version": SCRIPTS.parent.name})
        self.assertEqual(init["capabilities"], {"tools": {"listChanged": False}})
        self.assertIn("CATALOG", init["instructions"])
        self.assertLessEqual(len(init["instructions"]), 2048)

        tools = by_id[2]["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["find", "call_read", "call_write", "call_consent"])
        t = {x["name"]: x for x in tools}
        for x in tools:
            self.assertIs(x["_meta"]["anthropic/alwaysLoad"], True, x["name"])
            self.assertEqual(x["inputSchema"]["type"], "object")
        self.assertEqual(set(t["find"]["inputSchema"]["properties"]),
                         {"query", "connection", "limit", "detail"})
        for name in ("call_read", "call_write", "call_consent"):
            sch = t[name]["inputSchema"]
            self.assertEqual(set(sch["properties"]), {"connection", "op", "args", "select", "cursor"})
            self.assertEqual(sch["properties"]["args"]["type"], "object")
            self.assertEqual(sorted(sch["required"]), ["connection", "op"])
        self.assertIs(t["find"]["annotations"]["readOnlyHint"], True)
        self.assertIs(t["call_read"]["annotations"]["readOnlyHint"], True)
        self.assertIs(t["call_write"]["annotations"]["destructiveHint"], False)
        self.assertIs(t["call_consent"]["annotations"]["destructiveHint"], True)
        self.assertIs(t["call_consent"]["_meta"]["anthropic/requiresUserInteraction"], True)
        for name in ("find", "call_read", "call_write"):
            self.assertNotIn("anthropic/requiresUserInteraction", t[name]["_meta"])

        found = by_id[3]["result"]
        self.assertFalse(found["isError"])
        self.assertEqual(found["structuredContent"]["query"], "pulls")
        self.assertEqual(json.loads(found["content"][0]["text"]), found["structuredContent"])

        call = by_id[4]["result"]
        self.assertFalse(call["isError"])
        self.assertEqual(call["structuredContent"]["data"]["args"], {"state": "open"})
        self.assertEqual(call["content"][0]["type"], "text")
        self.assertIn(("call", "call_read", "github@personal", "list_pulls", {"state": "open"},
                       "x", None, "local"), r.engine.calls)
        self.assertTrue(by_id[5]["result"]["isError"])
        self.assertEqual(by_id[6]["result"], {})

    def test_protocol_negotiation_and_errors(self):
        out = self._session([
            self._init("1999-01-01"),
            "{this is not json",
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ])
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertTrue(out[0]["result"]["instructions"])        # static fallback, daemon down
        self.assertEqual(out[1]["error"]["code"], -32700)
        self.assertIsNone(out[1]["id"])
        self.assertEqual(out[2]["error"]["code"], -32601)
        self.assertEqual(out[3]["error"]["code"], -32602)
        self.assertEqual(out[4], {"jsonrpc": "2.0", "id": 4, "result": {}})

    def test_daemon_down_is_tool_error_not_crash(self):
        out = self._session([
            self._init(),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "call_read", "arguments": {"connection": "c@z", "op": "x"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "call_consent", "arguments": {"connection": "c@z"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ])
        res = out[1]["result"]
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["error"]["code"], "daemon_unreachable")
        self.assertEqual(out[2]["result"]["structuredContent"]["error"]["code"], "bad_request")
        self.assertEqual(out[3]["result"], {})

    def test_replies_are_flushed_line_by_line(self):
        with _UnixRunner(self.sock):
            proc = subprocess.Popen([sys.executable, str(SCRIPTS / "lotr_mcp.py"), "--home",
                                     str(self.home)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            try:
                proc.stdin.write(json.dumps(self._init()) + "\n")
                proc.stdin.flush()
                first = json.loads(proc.stdout.readline())     # answered before stdin closes
                self.assertEqual(first["id"], 1)
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n")
                proc.stdin.flush()
                self.assertEqual(json.loads(proc.stdout.readline())["id"], 2)
            finally:
                proc.stdin.close()
                proc.wait(10)
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
