"""gt_ipc, the local front door gt's unlock authority and gt-lotr share (0.20.1).

Pinned here (design-unlock.md §3, acceptance f):
  * the peer's identity comes from the kernel and matches the real process (pid + start time);
  * POSIX: the socket is 600 in a 700 directory; a directory others can enter is refused; a live
    server is never displaced; a too-long path falls back to a private per-user directory;
  * Windows: the pipe's DACL is exactly the user's SID; a second FIRST instance (a squatter) is
    refused; a peer whose SID is not ours is refused before its request is read; a client
    refuses a server running as someone else;
  * a server may ask its client mid-request, and only the client's own answer comes back.
"""
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, rmtree

sys.path.insert(0, str(SCRIPTS))
import gt_ipc  # noqa: E402


def echo(conn):
    while True:
        r = conn.recv_obj()
        if r is None:
            return
        if r.get("method") == "ask":
            conn.send_obj({"id": r["id"], "result": conn.ask(r["id"], {"prompt": "?"})})
        else:
            conn.send_obj({"id": r["id"], "result": {"peer": conn.peer}})


class Served(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gtipc-")
        self.home = os.path.join(self.tmp, "h")
        os.makedirs(self.home, mode=0o700)
        self.addr = gt_ipc.default_address(self.home, "t")
        self.stop = threading.Event()
        ready = threading.Event()
        self.t = threading.Thread(target=gt_ipc.serve, args=(self.addr, echo),
                                  kwargs={"stop_event": self.stop,
                                          "on_ready": lambda a: ready.set()}, daemon=True)
        self.t.start()
        self.assertTrue(ready.wait(5))

    def tearDown(self):
        self.stop.set()
        self.t.join(5)
        rmtree(self.tmp)

    def test_peer_is_this_process_from_the_kernel(self):
        c = gt_ipc.connect(self.addr)
        try:
            peer = c.call("who")["peer"]
        finally:
            c.close()
        self.assertEqual(peer["pid"], os.getpid())
        self.assertEqual(peer["start"], gt_ipc.process_info(os.getpid())["start"])
        if IS_WINDOWS:
            self.assertEqual(peer["sid"], gt_ipc.own_sid())
        else:
            self.assertEqual(peer["uid"], os.getuid())

    def test_child_process_peer_is_the_child(self):
        code = ("import sys; sys.path.insert(0, %r); import gt_ipc, os; "
                "c = gt_ipc.connect(%r); p = c.call('who')['peer']; "
                "print(p['pid'] == os.getpid())" % (str(SCRIPTS), self.addr))
        out = subprocess.run([PYTHON, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)

    def test_server_question_is_answered_by_the_client(self):
        c = gt_ipc.connect(self.addr, answer=lambda need: "the-answer")
        try:
            self.assertEqual(c.call("ask"), "the-answer")
        finally:
            c.close()
        c = gt_ipc.connect(self.addr)               # no answerer: the server gets None
        try:
            self.assertIsNone(c.call("ask"))
        finally:
            c.close()

    def test_a_live_server_is_never_displaced(self):
        with self.assertRaises(gt_ipc.IpcError) as cm:
            gt_ipc.serve(self.addr, echo, stop_event=threading.Event())
        self.assertIn(cm.exception.code, ("already_running", "pipe_squatted"))

    @unittest.skipIf(IS_WINDOWS, "POSIX-only: socket file modes")
    def test_socket_is_600_in_a_private_dir(self):
        st = os.stat(self.addr)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.addr)).st_mode) & 0o077, 0)

    @unittest.skipUnless(IS_WINDOWS, "Windows named pipe")
    def test_pipe_dacl_is_the_user_sid_only(self):
        self.assertEqual(gt_ipc.pipe_sddl(), "D:P(A;;GA;;;%s)" % gt_ipc.own_sid())
        ps = ("$a = [System.IO.Directory]::GetAccessControl('%s'); "
              "$a.GetSecurityDescriptorSddlForm('Access')" % self.addr)
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60)
        sddl = out.stdout.strip()
        if not sddl:
            self.skipTest("could not read the pipe ACL from PowerShell: %s" % out.stderr[-200:])
        self.assertIn(gt_ipc.own_sid(), sddl)
        for broad in ("WD", "AN", "BU", "AU", "S-1-1-0"):
            self.assertNotIn(";;;%s)" % broad, sddl)

    @unittest.skipUnless(IS_WINDOWS, "Windows named pipe")
    def test_a_squatter_first_instance_is_refused(self):
        with self.assertRaises(gt_ipc.IpcError) as cm:
            gt_ipc.create_pipe_instance(self.addr, first=True)
        self.assertEqual(cm.exception.code, "pipe_squatted")


class ForeignPeers(unittest.TestCase):
    """A peer that is not us, or that cannot be identified, is refused (fail closed)."""

    def test_foreign_or_unknown_peer_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="gtipc-")
        home = os.path.join(tmp, "h")
        os.makedirs(home, mode=0o700)
        addr = gt_ipc.default_address(home, "f")
        stop, ready = threading.Event(), threading.Event()
        reached = []

        def handler(conn):
            reached.append(conn.peer)
            echo(conn)
        if IS_WINDOWS:
            real = gt_ipc._pipe_peer
            gt_ipc._pipe_peer = lambda h: dict(real(h), sid="S-1-5-21-1-2-3-1001")
        else:
            real = gt_ipc.unix_peer
            gt_ipc.unix_peer = lambda s: dict(real(s), uid=os.getuid() + 1)
        try:
            t = threading.Thread(target=gt_ipc.serve, args=(addr, handler),
                                 kwargs={"stop_event": stop, "on_ready": lambda a: ready.set()},
                                 daemon=True)
            t.start()
            self.assertTrue(ready.wait(5))
            c = gt_ipc.connect(addr)
            try:
                with self.assertRaises(gt_ipc.IpcError) as cm:
                    c.call("who")
                self.assertEqual(cm.exception.code, "peer_refused")
            finally:
                c.close()
            self.assertEqual(reached, [], "a foreign peer reached the handler")
        finally:
            if IS_WINDOWS:
                gt_ipc._pipe_peer = real
            else:
                gt_ipc.unix_peer = real
            stop.set()
            t.join(5)
            rmtree(tmp)

    @unittest.skipUnless(IS_WINDOWS, "Windows named pipe")
    def test_client_refuses_a_server_of_another_user(self):
        tmp = tempfile.mkdtemp(prefix="gtipc-")
        addr = gt_ipc.default_address(tmp, "s")
        stop, ready = threading.Event(), threading.Event()
        t = threading.Thread(target=gt_ipc.serve, args=(addr, echo),
                             kwargs={"stop_event": stop, "on_ready": lambda a: ready.set()},
                             daemon=True)
        t.start()
        self.assertTrue(ready.wait(5))
        real = gt_ipc._win_process_sid
        gt_ipc._win_process_sid = lambda pid: "S-1-5-21-9-9-9-1001"
        try:
            with self.assertRaises(gt_ipc.IpcError) as cm:
                gt_ipc.connect(addr)
            self.assertEqual(cm.exception.code, "server_refused")
        finally:
            gt_ipc._win_process_sid = real
            stop.set()
            t.join(5)
            rmtree(tmp)


@unittest.skipIf(IS_WINDOWS, "POSIX-only: directory modes")
class PosixDirs(unittest.TestCase):

    def test_a_directory_others_can_enter_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="gtipc-")
        try:
            os.chmod(tmp, 0o755)
            with self.assertRaises(gt_ipc.IpcError) as cm:
                gt_ipc.serve(os.path.join(tmp, "x.sock"), echo, stop_event=threading.Event())
            self.assertEqual(cm.exception.code, "insecure_dir")
        finally:
            rmtree(tmp)

    def test_long_home_falls_back_to_a_private_short_path(self):
        deep = os.path.join(tempfile.gettempdir(), "x" * 120)
        addr = gt_ipc.default_address(deep, "unlockd")
        self.assertLessEqual(len(addr.encode()), 100)
        self.assertIn("gt-%d-" % os.getuid(), addr)
        self.assertNotEqual(addr, gt_ipc.default_address(deep + "y", "unlockd"))


class ProcessInfo(unittest.TestCase):

    def test_alive_needs_the_same_start(self):
        info = gt_ipc.process_info(os.getpid())
        self.assertTrue(gt_ipc.alive(os.getpid(), info["start"]))
        self.assertFalse(gt_ipc.alive(os.getpid(), "0"))

    def test_exited_child_is_not_alive(self):
        p = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(30)"])
        info = None
        deadline = time.monotonic() + 5
        while info is None and time.monotonic() < deadline:
            info = gt_ipc.process_info(p.pid)
        self.assertIsNotNone(info)
        self.assertEqual(gt_ipc.ancestry(p.pid)[1]["pid"], os.getpid())
        p.kill()
        p.wait(5)
        self.assertFalse(gt_ipc.alive(p.pid, info["start"]))


if __name__ == "__main__":
    unittest.main()
