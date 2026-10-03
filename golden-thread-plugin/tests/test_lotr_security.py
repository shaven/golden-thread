"""gt-lotr's local front door is not open (ADR-5). Each guarantee is pinned here:

  * the daemon refuses a home, gateway.json or registry.json readable by anyone else;
  * the socket directory must be private, and the socket is created 600;
  * every socket connection's peer uid is read from the kernel and must be the daemon's own
    (peer_uid works on this platform -- if it returned None, nobody would be served);
  * a hub refuses to listen beyond loopback without TLS;
  * local callers are subject to gateway.json `local`: an allow list and a tier ceiling;
  * every consent-tier operation needs the owner's confirmation raised by the engine itself,
    whoever calls -- deny, timeout and "cannot ask" all refuse, and `refuse` refuses outright;
  * the confirmation text never carries a credential-shaped argument value.
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from _harness import REPO, latest_version_dir
from _harness import LOTR_POSIX_ONLY, skip_on_windows

GW = latest_version_dir(REPO / "golden-thread-lotr")
sys.path.insert(0, str(GW / "scripts"))

from lotrlib import confirm as confirm_mod  # noqa: E402
from lotrlib.errors import GatewayError  # noqa: E402
from _harness import IS_WINDOWS  # noqa: E402
if IS_WINDOWS:
    # lotrlib.server defines a Unix-domain socket server at import, which Windows' socketserver
    # does not have; every class below is skipped there with LOTR_POSIX_ONLY.
    is_loopback = peer_uid = serve_http = serve_unix = None
else:
    from lotrlib.server import is_loopback, peer_uid, serve_http, serve_unix  # noqa: E402


class FakeConn:
    calls = []

    def __init__(self, conn, profile, **kw):
        self.conn = conn

    def call(self, op, args, *, cursor=None):
        FakeConn.calls.append((op.get("name"), args))
        return {"status": 200, "data": {"ok": True}, "next_cursor": None}


def _home(local=None):
    home = Path(tempfile.mkdtemp(dir="/tmp", prefix="gws"))
    g = {"schema": 1, "zone": "personal", "mode": "local"}
    if local is not None:
        g["local"] = local
    conn = {"id": "github@personal", "identity": "shaven", "zone": "personal", "kind": "http",
            "profile": "github", "description": "GitHub", "base_url": "https://api.github.com",
            "network": {"hosts": ["api.github.com"]},
            "auth": {"scheme": "bearer", "token_ref": "keychain:gt-lotr/x"},
            "trust": "T0", "policy": {"deny": [], "consent": [], "write": [], "read": []}}
    jira = dict(conn, id="jira@cloud", profile="jira-v3", description="Jira",
                base_url="https://x.atlassian.net", network={"hosts": ["x.atlassian.net"]})
    (home / "gateway.json").write_text(json.dumps(g))
    (home / "registry.json").write_text(json.dumps(
        {"schema": 1, "zone": "personal", "connections": [conn, jira], "clients": []}))
    for f in ("gateway.json", "registry.json"):
        os.chmod(home / f, 0o600)
    return home


def _engine(home, dialog=lambda t: True):
    from lotrlib.engine import Engine
    return Engine(home, connection_factory=FakeConn, dialog=dialog)


MERGE = {"owner": "a", "repo": "b", "pull_number": 1}


@skip_on_windows(LOTR_POSIX_ONLY)
class PrivateFiles(unittest.TestCase):
    def setUp(self):
        self.home = _home()
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_world_readable_registry_is_refused(self):
        os.chmod(self.home / "registry.json", 0o644)
        with self.assertRaises(GatewayError) as c:
            _engine(self.home)
        self.assertEqual(c.exception.code, "insecure_perms")

    def test_group_readable_home_is_refused(self):
        os.chmod(self.home, 0o750)
        with self.assertRaises(GatewayError) as c:
            _engine(self.home)
        self.assertEqual(c.exception.code, "insecure_perms")

    def test_private_home_loads(self):
        self.assertTrue(_engine(self.home).find("")["ok"])


@skip_on_windows(LOTR_POSIX_ONLY)
class Socket(unittest.TestCase):
    def test_peer_uid_identifies_this_process(self):
        d = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(d + "/s")
        srv.listen(1)
        c = socket.socket(socket.AF_UNIX)
        c.connect(d + "/s")
        conn, _ = srv.accept()
        try:
            self.assertEqual(peer_uid(conn), os.getuid())
        finally:
            for s in (conn, c, srv):
                s.close()

    def test_socket_in_shared_dir_is_refused(self):
        d = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        os.chmod(d, 0o755)
        with self.assertRaises(GatewayError) as c:
            serve_unix(lambda: None, d + "/lotrd.sock", stop_event=threading.Event())
        self.assertEqual(c.exception.code, "insecure_dir")

    def test_socket_is_600_in_private_dir(self):
        d = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        stop, ready = threading.Event(), threading.Event()
        t = threading.Thread(target=serve_unix, args=(lambda: None, d + "/lotrd.sock"),
                             kwargs={"stop_event": stop, "on_ready": lambda p: ready.set()})
        t.start()
        try:
            self.assertTrue(ready.wait(5))
            self.assertEqual(os.stat(d + "/lotrd.sock").st_mode & 0o777, 0o600)
        finally:
            stop.set()
            t.join(5)


@skip_on_windows(LOTR_POSIX_ONLY)
class HubListen(unittest.TestCase):
    def test_non_loopback_without_tls_is_refused(self):
        with self.assertRaises(GatewayError) as c:
            serve_http(lambda: None, "0.0.0.0", 0, registry_getter=lambda: None,
                       stop_event=threading.Event())
        self.assertEqual(c.exception.code, "insecure_listen")

    def test_loopback_detection(self):
        for h in ("127.0.0.1", "::1", "localhost", "127.8.8.8"):
            self.assertTrue(is_loopback(h), h)
        for h in ("0.0.0.0", "10.0.0.5", "lotrlib.example.com"):
            self.assertFalse(is_loopback(h), h)


@skip_on_windows(LOTR_POSIX_ONLY)
class LocalPolicy(unittest.TestCase):
    def tearDown(self):
        shutil.rmtree(getattr(self, "home", ""), True) if hasattr(self, "home") else None

    def test_local_allow_list_limits_reach(self):
        self.home = _home({"allow": ["github@*"], "max_tier": "consent", "confirm": "none"})
        e = _engine(self.home)
        self.assertEqual([r["connection"] for r in e.find("")["results"]], ["github@personal"])
        r = e.call("call_read", "jira@cloud", "myself", {})
        self.assertEqual(r["error"]["code"], "client_not_allowed")

    def test_local_tier_ceiling(self):
        self.home = _home({"allow": ["*"], "max_tier": "read", "confirm": "none"})
        e = _engine(self.home)
        r = e.call("call_write", "github@personal", "create_issue", {"owner": "a", "repo": "b", "title": "t"})
        self.assertEqual(r["error"]["code"], "tier_ceiling")


@skip_on_windows(LOTR_POSIX_ONLY)
class ConsentConfirmation(unittest.TestCase):
    def setUp(self):
        FakeConn.calls = []

    def tearDown(self):
        shutil.rmtree(getattr(self, "home", ""), True) if hasattr(self, "home") else None

    def _merge(self, confirm, dialog):
        self.home = _home({"allow": ["*"], "max_tier": "consent", "confirm": confirm})
        return _engine(self.home, dialog=dialog).call("call_consent", "github@personal",
                                                      "merge_pull", MERGE)

    def test_allowed_dialog_runs_the_op(self):
        r = self._merge("dialog", lambda t: True)
        self.assertTrue(r["ok"], r)
        self.assertEqual([c[0] for c in FakeConn.calls], ["merge_pull"])

    def test_denied_dialog_never_reaches_downstream(self):
        r = self._merge("dialog", lambda t: False)
        self.assertEqual(r["error"]["code"], "consent_denied")
        self.assertEqual(FakeConn.calls, [])

    def test_dialog_that_cannot_be_shown_refuses(self):
        r = self._merge("dialog", lambda t: None)
        self.assertEqual(r["error"]["code"], "consent_unconfirmed")
        self.assertEqual(FakeConn.calls, [])

    def test_refuse_mode_refuses(self):
        r = self._merge("refuse", lambda t: True)
        self.assertEqual(r["error"]["code"], "consent_refused")
        self.assertEqual(FakeConn.calls, [])

    def test_reads_and_writes_do_not_ask(self):
        asked = []
        self.home = _home({"allow": ["*"], "max_tier": "consent", "confirm": "dialog"})
        e = _engine(self.home, dialog=lambda t: asked.append(t) or True)
        self.assertTrue(e.call("call_read", "github@personal", "get_pull", MERGE)["ok"])
        self.assertTrue(e.call("call_write", "github@personal", "create_issue",
                               {"owner": "a", "repo": "b", "title": "t"})["ok"])
        self.assertEqual(asked, [])

    def test_auto_refuses_where_no_dialog_exists(self):
        real = confirm_mod.sys.platform
        confirm_mod.sys.platform = "linux"
        try:
            with self.assertRaises(GatewayError) as c:
                confirm_mod.confirm("auto", "x", dialog=lambda t: True)
            self.assertEqual(c.exception.code, "consent_refused")
        finally:
            confirm_mod.sys.platform = real

    def test_confirmation_text_withholds_credential_values(self):
        fake = "ghp_" + "Q" * 36
        text = confirm_mod.describe("github@personal", "shaven", "merge_pull",
                                    {"note": fake, "pull_number": 1}, "local")
        self.assertNotIn(fake, text)
        self.assertIn("<withheld>", text)
        self.assertIn("merge_pull", text)


if __name__ == "__main__":
    unittest.main()
