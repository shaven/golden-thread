"""install.sh says what a person needs and nothing false (0.20.1, usability run 2026-10-04).

  * A rollback to the previous release is quiet and correct (M3): no raw argparse usage dump
    from a gt_schedule.py that predates a verb the newest installer calls, and no "MODIFIED
    LOCALLY" for vault files that are simply the NEWER release's copies -- they are gt's own
    text and go back to the rolled-back release's copy.
  * settings.json keeps LF line endings across re-installs (Windows wrote CRLF in text mode).
  * Stale /tmp/gt-<uid>-* socket directories (no server listening) are removed.
  * No developer jargon (release-check, parallel_profile) and no "PENDING becomes FAIL" when
    nothing is pending.
"""
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _harness import GT, IS_WINDOWS, REPO, Sandbox

INSTALL = REPO / "install.sh"
BASH = shutil.which("bash")
PREVIOUS = sorted((d for d in (REPO / "golden-thread").iterdir()
                   if d.is_dir() and (d / ".claude-plugin" / "plugin.json").is_file()
                   and d.name != GT.name),
                  key=lambda d: tuple(int(x) for x in d.name.split(".")))


class InstallBase(Sandbox):
    def install(self, *args, ok=True):
        p = self.run_cmd([BASH, INSTALL] + list(args), timeout=900)
        if ok:
            self.assertOk(p, "install.sh %s" % " ".join(args))
        return p


@unittest.skipUnless(PREVIOUS, "the tree carries no previous release to roll back to")
class RollbackIsQuiet(InstallBase):
    def test_rollback_has_no_usage_dump_and_no_false_modified_locally(self):
        vault = self.tmp / "vault"
        # The older release's own gate leaves a read-only file in TEMP on Windows (its smoke
        # cleanup predates 0.20.0's fix): keep TEMP inside the sandbox, which is removed whole.
        t = self.tmp / "t"
        t.mkdir()
        for k in ("TMPDIR", "TMP", "TEMP"):
            self.env[k] = str(t)
        self.install("--vault", str(vault))
        prev = PREVIOUS[-1]
        p = self.install(prev.name)
        out = p.stdout + p.stderr
        self.assertNotIn("usage: gt_schedule.py", out, "argparse's raw usage reached the user")
        self.assertNotIn("MODIFIED LOCALLY", out,
                         "a newer release's own vault files were called local edits")
        # ...and those files went back to the rolled-back release's copies.
        tools = vault / "Projects" / "golden-thread" / "tools"
        for t in sorted((prev / "templates" / "tools").glob("*.py")):
            if (tools / t.name).is_file():
                self.assertEqual((tools / t.name).read_bytes(), t.read_bytes(),
                                 "%s is not the %s copy after the rollback" % (t.name, prev.name))


class InstallOutput(InstallBase):
    def test_settings_json_stays_lf_and_the_output_has_no_jargon(self):
        vault = self.tmp / "vault"
        first = self.install("--vault", str(vault))
        settings = self.home / ".claude" / "settings.json"
        self.assertNotIn(b"\r\n", settings.read_bytes())
        again = self.install()
        self.assertNotIn(b"\r\n", settings.read_bytes(),
                         "a re-install rewrote settings.json with CRLF")
        for p in (first, again):
            for jargon in ("release-check", "parallel_profile", "cpu_max",
                           "PENDING becomes FAIL"):
                self.assertNotIn(jargon, p.stdout, "installer output says %r" % jargon)
        self.assertIn("This machine:", first.stdout)


@unittest.skipIf(IS_WINDOWS, "AF_UNIX socket directories under /tmp are POSIX")
class StaleSocketDirs(InstallBase):
    def test_a_stale_dir_goes_and_a_live_or_young_one_stays(self):
        tag = "gttest%d%d" % (os.getpid(), int(time.time()))
        stale = Path("/tmp/gt-%d-%s-stale" % (os.getuid(), tag))
        young = Path("/tmp/gt-%d-%s-young" % (os.getuid(), tag))
        live = Path("/tmp/gt-%d-%s-live" % (os.getuid(), tag))
        server = None
        try:
            for d in (stale, young, live):
                d.mkdir(mode=0o700)
            # a dead socket file in the stale dir: bound, then closed
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(str(stale / "x.sock"))
            s.close()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(live / "x.sock"))
            server.listen(16)
            # Accept like a real server: a backlog nobody drains is refused on macOS once full,
            # and every parallel test that installs connects to it too.
            import threading

            def _serve(srv=server):
                while True:
                    try:
                        c, _ = srv.accept()
                        c.close()
                    except ConnectionAbortedError:
                        continue        # macOS: the prober closed first (ECONNABORTED)
                    except OSError:
                        return
            threading.Thread(target=_serve, daemon=True).start()
            old = time.time() - 7200
            for d in (stale, live):
                os.utime(str(d), (old, old))
            p = self.install("--no-vault")
            self.assertFalse(stale.exists(), p.stdout[-800:])
            self.assertTrue(young.exists(), "a directory younger than an hour was removed")
            self.assertTrue(live.exists(), "a directory with a listening socket was removed")
            # (Which install removed it -- this one, or a parallel test's -- is not asserted.)
        finally:
            if server is not None:
                server.close()
            for d in (stale, young, live):
                shutil.rmtree(str(d), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
