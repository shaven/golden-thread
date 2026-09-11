"""gt_session.py: advisory per-session claims on vault files, trusted only while live.

Liveness has two sources. A session file records `pid` and `host`; on this machine a
running pid means LIVE and a dead pid means STALE regardless of the clock. Only when
the pid cannot be judged does the `last_execution` heartbeat decide, against
--stale-after. Tests pin each path:

  * CLAUDE_PID=<this test's pid>  -> demonstrably running
  * CLAUDE_PID=<reaped child pid> -> demonstrably dead
  * CLAUDE_PID=none               -> not a pid, so the heartbeat decides
"""
import datetime
import os
import re
import subprocess
import unittest

from _harness import Sandbox

TS_FMT = "%Y-%m-%d %H:%M:%S %Z"
LIVE_PID = str(os.getpid())


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return str(p.pid)


class GtSessionTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.tool = self.vault / "Projects" / "golden-thread" / "tools" / "gt_session.py"
        self.sessions = self.vault / "Projects" / "golden-thread" / "sessions"
        self._procs = []

    def tearDown(self):
        for p in self._procs:
            p.kill()
            p.wait()
        super().tearDown()

    def gs(self, sid, *args, pid=LIVE_PID, stale_after=None):
        pre = ["--id", sid]
        if stale_after is not None:
            pre += ["--stale-after", str(stale_after)]
        return self.py(self.tool, *pre, *args, env={"CLAUDE_PID": pid}, cwd=self.vault)

    def files_for(self, sid):
        return sorted(self.sessions.glob(sid + "_*.md"))

    def set_heartbeat(self, sid, minutes_ago):
        (f,) = self.files_for(sid)
        when = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes_ago)
        text = re.sub(r"^last_execution:.*$", "last_execution: " + when.strftime(TS_FMT).strip(),
                      f.read_text(), flags=re.M)
        f.write_text(text)

    # -- register ------------------------------------------------------------
    def test_register_writes_a_stamped_session_file(self):
        proc = self.gs("sessA", "register", "--task", "fix the thing", "--files", "log.md", "index.md")
        self.assertOk(proc)
        files = self.files_for("sessA")
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^sessA_\d{4}-\d{2}-\d{2}_\d{4}\.md$")
        text = files[0].read_text()
        self.assertIn("session_id: sessA", text)
        self.assertIn("task: fix the thing", text)
        self.assertIn("pid: " + LIVE_PID, text)
        self.assertIn("- `log.md`", text)
        self.assertIn("- `index.md`", text)

        lst = self.gs("sessA", "list")
        self.assertOk(lst)
        self.assertIn("LIVE  sessA", lst.stdout)
        self.assertIn("<- this session", lst.stdout)
        self.assertIn("open: log.md", lst.stdout)

    def test_list_with_nothing_registered(self):
        proc = self.gs("sessA", "list")
        self.assertOk(proc)
        self.assertIn("no sessions registered", proc.stdout)

    def test_beat_and_claim_require_registration(self):
        for args in (["beat"], ["claim", "x.md"]):
            with self.subTest(cmd=args[0]):
                proc = self.gs("ghost", *args)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("register", proc.stderr)

    def test_same_id_held_by_another_live_process_is_refused_then_resumable(self):
        holder = subprocess.Popen(["sleep", "120"])
        self._procs.append(holder)
        self.assertOk(self.gs("dup", "register", "--task", "first", pid=str(holder.pid)))

        proc = self.gs("dup", "register", "--task", "second")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("ALREADY OPEN", proc.stderr)
        (f,) = self.files_for("dup")
        self.assertIn("task: first", f.read_text(), "the refused register overwrote the live one")

        proc = self.gs("dup", "register", "--task", "second", "--resume")
        self.assertOk(proc)
        (f,) = self.files_for("dup")
        self.assertIn("task: second", f.read_text())
        self.assertIn("pid: " + LIVE_PID, f.read_text())

    def test_same_id_whose_process_is_dead_is_replaced(self):
        self.assertOk(self.gs("dup", "register", "--task", "crashed", pid=dead_pid()))
        proc = self.gs("dup", "register", "--task", "fresh")
        self.assertOk(proc)
        self.assertIn("dead", proc.stdout)
        (f,) = self.files_for("dup")
        self.assertIn("task: fresh", f.read_text())

    # -- claim / check -------------------------------------------------------
    def test_claim_of_a_file_held_by_another_live_session_is_refused(self):
        self.assertOk(self.gs("sessA", "register", "--files", "notes.md"))
        self.assertOk(self.gs("sessB", "register"))

        proc = self.gs("sessB", "claim", "notes.md")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("CONFLICT  notes.md  held by sessA", proc.stderr)
        (fb,) = self.files_for("sessB")
        self.assertNotIn("`notes.md`", fb.read_text(), "a refused claim was still recorded")

        self.assertOk(self.gs("sessB", "claim", "other.md"))
        self.assertIn("- `other.md`", fb.read_text())

        self.assertOk(self.gs("sessB", "claim", "notes.md", "--force"))
        self.assertIn("- `notes.md`", fb.read_text())

    def test_claims_are_per_session_and_not_duplicated(self):
        self.assertOk(self.gs("sessA", "register"))
        self.assertOk(self.gs("sessB", "register"))
        self.assertOk(self.gs("sessA", "claim", "x.md", "y.md"))
        self.assertOk(self.gs("sessA", "claim", "x.md"))
        (fa,) = self.files_for("sessA")
        (fb,) = self.files_for("sessB")
        self.assertEqual(fa.read_text().count("- `x.md`"), 1, "re-claiming duplicated the line")
        self.assertNotIn("x.md", fb.read_text())

    @unittest.expectedFailure  # defect: 2026-09-11-session-claim-placeholder
    def test_first_claim_replaces_the_nothing_claimed_placeholder(self):
        # cmd_claim means to drop the placeholder on the first claim, but compares the
        # WHOLE body to it; register always writes a heading above it, so the
        # comparison never matches and the file reads "nothing claimed yet" above
        # the claims.
        self.assertOk(self.gs("sessA", "register"))
        self.assertOk(self.gs("sessA", "claim", "x.md"))
        (fa,) = self.files_for("sessA")
        self.assertNotIn("_nothing claimed yet_", fa.read_text(),
                         "the session file still says '_nothing claimed yet_' after a claim")

    def test_check_reports_live_holder_and_exit_code(self):
        self.assertOk(self.gs("sessA", "register", "--files", "notes.md"))
        self.assertOk(self.gs("sessB", "register"))

        other = self.gs("sessB", "check", "notes.md")
        self.assertEqual(other.returncode, 1, other.stdout)
        self.assertIn("LIVE  notes.md  held by sessA", other.stdout)

        own = self.gs("sessA", "check", "notes.md")
        self.assertEqual(own.returncode, 0, own.stdout)
        self.assertIn("(this session)", own.stdout)

        free = self.gs("sessB", "check", "unclaimed.md")
        self.assertEqual(free.returncode, 0)
        self.assertIn("free: unclaimed.md", free.stdout)

    # -- staleness -----------------------------------------------------------
    def test_heartbeat_older_than_stale_after_is_not_live(self):
        self.assertOk(self.gs("sessA", "register", "--files", "notes.md", pid="none"))
        self.assertOk(self.gs("sessB", "register"))
        self.set_heartbeat("sessA", 120)

        chk = self.gs("sessB", "check", "notes.md")
        self.assertEqual(chk.returncode, 0, "a stale holder still blocks:\n" + chk.stdout)
        self.assertIn("STALE  notes.md  held by sessA", chk.stdout)

        chk = self.gs("sessB", "check", "notes.md", stale_after=180)
        self.assertEqual(chk.returncode, 1, "the same heartbeat under a wider threshold must be LIVE")
        self.assertIn("LIVE", chk.stdout)

        lst = self.gs("sessB", "list")
        self.assertIn("STALE sessA", lst.stdout)

        # A heartbeat makes it live again under the default threshold.
        self.assertOk(self.gs("sessA", "beat", pid="none"))
        chk = self.gs("sessB", "check", "notes.md")
        self.assertEqual(chk.returncode, 1, chk.stdout)

    def test_stale_claim_does_not_block_a_new_claim(self):
        self.assertOk(self.gs("sessA", "register", "--files", "notes.md", pid="none"))
        self.assertOk(self.gs("sessB", "register"))
        self.set_heartbeat("sessA", 120)
        self.assertOk(self.gs("sessB", "claim", "notes.md"), "claim blocked by a stale session")

    def test_dead_pid_is_stale_whatever_the_heartbeat_says(self):
        self.assertOk(self.gs("sessA", "register", "--files", "notes.md", pid=dead_pid()))
        self.assertOk(self.gs("sessB", "register"))
        chk = self.gs("sessB", "check", "notes.md", stale_after=10000)
        self.assertEqual(chk.returncode, 0, chk.stdout)
        self.assertIn("STALE", chk.stdout)

    # -- release -------------------------------------------------------------
    def test_release_deletes_the_file_and_frees_claims(self):
        self.assertOk(self.gs("sessA", "register", "--files", "notes.md"))
        self.assertOk(self.gs("sessB", "register"))
        rel = self.gs("sessA", "release")
        self.assertOk(rel)
        self.assertEqual(self.files_for("sessA"), [])
        self.assertTrue(self.files_for("sessB"), "release removed another session's file")
        chk = self.gs("sessB", "check", "notes.md")
        self.assertEqual(chk.returncode, 0)
        self.assertIn("free: notes.md", chk.stdout)
        again = self.gs("sessA", "release")
        self.assertOk(again)
        self.assertIn("nothing to release", again.stdout)


if __name__ == "__main__":
    unittest.main()
