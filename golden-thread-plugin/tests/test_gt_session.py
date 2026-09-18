"""gt_session.py: advisory per-session claims on vault files, trusted only while live.

Liveness has two sources. A session file records `pid` and `host`; on this machine a
running pid means LIVE and a dead pid means STALE regardless of the clock. Only when
the pid cannot be judged does the `last_execution` heartbeat decide, against
--stale-after. Tests pin each path:

  * CLAUDE_PID=<this test's pid>  -> demonstrably running
  * CLAUDE_PID=<reaped child pid> -> demonstrably dead
  * CLAUDE_PID=none               -> not a pid, so the heartbeat decides
"""
import argparse
import contextlib
import datetime
import io
import os
import re
import stat
import subprocess
import unittest

from _harness import PYTHON, Sandbox, load_module

TS_FMT = "%Y-%m-%d %H:%M:%S %Z"
LIVE_PID = str(os.getpid())


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return str(p.pid)


class SessionTools(Sandbox):
    """Fixture only: a vault, the tool in it, and the helpers for driving it."""

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

    def gs_raw(self, *args, pid=LIVE_PID):
        """The tool with NO flags moved in front of the verb -- for the parsing tests."""
        return self.py(self.tool, *args, env={"CLAUDE_PID": pid}, cwd=self.vault)

    def files_for(self, sid):
        return sorted(self.sessions.glob(sid + "_*.md"))

    def two_registrations(self, sid="two"):
        """One session id with two registrations: the plain name and the stepped-aside
        `-<pid>` one. This is what `--new` makes on purpose and what a same-minute
        race makes by accident -- and it is the shape H8 resolved by sort order."""
        self.assertOk(self.gs(sid, "register", "--task", "A", "--files", "a.md"))
        self.assertOk(self.gs(sid, "register", "--new", "--task", "B", "--files", "b.md"))
        files = self.files_for(sid)
        self.assertEqual(len(files), 2, [f.name for f in files])
        stepped = [f for f in files if re.search(r"_\d{4}-\d+\.md$", f.name)]
        plain = [f for f in files if re.search(r"_\d{4}\.md$", f.name)]
        self.assertEqual(len(stepped), 1, [f.name for f in files])
        self.assertEqual(len(plain), 1, [f.name for f in files])
        # The defect in one line: the plain name sorts LAST because '.' > '-', so
        # `sorted(..., reverse=True)[0]` handed every command the other file.
        self.assertEqual(sorted(files, reverse=True)[0], plain[0])
        return plain[0], stepped[0]

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.sessions.glob("*")}

    def set_heartbeat(self, sid, minutes_ago):
        (f,) = self.files_for(sid)
        when = datetime.datetime.now().astimezone() - datetime.timedelta(minutes=minutes_ago)
        text = re.sub(r"^last_execution:.*$", "last_execution: " + when.strftime(TS_FMT).strip(),
                      f.read_text(), flags=re.M)
        f.write_text(text)


class GtSessionTest(SessionTools):
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


class RegisterKeepsClaimsTest(SessionTools):
    """H1: re-registering a session must not throw away what it holds.

    `register` never went through the compare-and-swap. It rebuilt the body from
    `--files` alone, and `--resume` unlinked the old file first -- so the claims a
    live session held vanished, the command printed "registered", and exited 0.
    Every one of these fails against that code.
    """

    def test_re_registering_keeps_the_claims_already_held(self):
        self.assertOk(self.gs("keep", "register", "--task", "one", "--files", "x.md"))
        self.assertOk(self.gs("keep", "claim", "y.md"))
        proc = self.gs("keep", "register", "--task", "two")
        self.assertOk(proc)
        (f,) = self.files_for("keep")
        text = f.read_text()
        for held in ("x.md", "y.md"):
            self.assertIn("- `%s`" % held, text,
                          "re-registering dropped the live claim on %s:\n%s" % (held, text))
        self.assertIn("task: two", text, "the re-registration did not take over the entry")
        self.assertIn("carried forward 2 claim(s)", proc.stdout,
                      "nothing said what was carried forward")

    def test_resume_carries_claims_forward_and_unions_files(self):
        self.assertOk(self.gs("res", "register", "--files", "a.md", "b.md"))
        proc = self.gs("res", "register", "--resume", "--task", "later", "--files", "c.md")
        self.assertOk(proc)
        (f,) = self.files_for("res")
        text = f.read_text()
        for held in ("a.md", "b.md", "c.md"):
            self.assertIn("- `%s`" % held, text,
                          "--resume replaced the claims instead of unioning them:\n" + text)
            self.assertEqual(text.count("- `%s`" % held), 1, "a claim was duplicated")

    def test_re_registering_our_own_entry_does_not_make_a_second_one(self):
        """`mine` with a different minute-stamp left the old file in place."""
        self.assertOk(self.gs("dupe", "register", "--files", "x.md"))
        (f,) = self.files_for("dupe")
        f.rename(f.with_name("dupe_2026-01-01_0101.md"))     # registered in an earlier minute
        self.assertOk(self.gs("dupe", "register", "--task", "again"))
        files = self.files_for("dupe")
        self.assertEqual(len(files), 1,
                         "re-registering made a SECOND registration for one session id: %s"
                         % [p.name for p in files])
        self.assertIn("- `x.md`", files[0].read_text())

    def test_replacing_a_dead_predecessor_says_how_many_claims_it_dropped(self):
        self.assertOk(self.gs("gone", "register", "--files", "n.md", "m.md", pid=dead_pid()))
        proc = self.gs("gone", "register", "--task", "fresh")
        self.assertOk(proc)
        self.assertIn("releasing 2 claim(s)", proc.stdout,
                      "a dead session's claims were released in silence:\n" + proc.stdout)
        self.assertIn("n.md", proc.stdout)
        (f,) = self.files_for("gone")
        self.assertNotIn("- `n.md`", f.read_text(), "the dead session's claims were kept")


class OneIdTwoRegistrationsTest(SessionTools):
    """H8: a process must never operate on another process's registration.

    `_path` resolved an id with `sorted(glob, reverse=True)[0]`, and the plain
    `..._1702.md` sorts after `..._1702-73851.md` because '.' > '-'. So the
    process that stepped aside into the `-pid` name addressed the OTHER file with
    every command: its claims landed there, and that process's `release` cleared
    them. Deterministic, and every command exited 0.
    """

    def test_a_registration_is_addressed_by_name_not_by_sort_order(self):
        plain, stepped = self.two_registrations()
        proc = self.gs("two", "--entry", stepped.name, "claim", "mine.md")
        self.assertOk(proc)
        self.assertIn("- `mine.md`", stepped.read_text(),
                      "the claim did not land in the registration it was aimed at")
        self.assertNotIn("mine.md", plain.read_text(),
                         "the claim landed in ANOTHER process's registration")

    def test_an_ambiguous_id_is_refused_rather_than_guessed(self):
        plain, stepped = self.two_registrations()
        for verb in (["claim", "z.md"], ["beat"]):
            with self.subTest(cmd=verb[0]):
                proc = self.gs("two", *verb)
                self.assertEqual(proc.returncode, 2,
                                 "an ambiguous id was resolved silently:\n%s%s"
                                 % (proc.stdout, proc.stderr))
                self.assertIn("2 registrations", proc.stderr)
        self.assertNotIn("z.md", plain.read_text(), "a guessed claim went into another's file")
        self.assertNotIn("z.md", stepped.read_text())

    def test_release_cannot_delete_a_registration_that_is_not_ours(self):
        plain, stepped = self.two_registrations()
        rel = self.gs("two", "release")
        self.assertEqual(rel.returncode, 2,
                         "release deleted one of two registrations without being told which:\n"
                         + rel.stdout + rel.stderr)
        self.assertEqual(len(self.files_for("two")), 2, "release removed a registration")
        rel = self.gs("two", "--entry", stepped.name, "release")
        self.assertOk(rel)
        self.assertEqual([p.name for p in self.files_for("two")], [plain.name])
        self.assertIn("- `a.md`", plain.read_text(), "the other registration's claim was cleared")

    def test_release_refuses_a_registration_held_by_another_running_process(self):
        holder = subprocess.Popen(["sleep", "120"])
        self._procs.append(holder)
        self.assertOk(self.gs("solo", "register", "--files", "n.md", pid=str(holder.pid)))
        rel = self.gs("solo", "release")
        self.assertEqual(rel.returncode, 1,
                         "released a live OTHER process's claims:\n" + rel.stdout + rel.stderr)
        self.assertTrue(self.files_for("solo"), "the registration was deleted anyway")
        self.assertIn("still running", rel.stderr)
        self.assertOk(self.gs("solo", "release", "--force"))
        self.assertEqual(self.files_for("solo"), [])


class DryRunAndParsingTest(SessionTools):
    def test_dry_run_beat_does_not_advance_the_heartbeat(self):
        """cmd_beat had no dry() guard, and `last_execution` is exactly what every
        OTHER session reads to decide whether this one is stale."""
        self.assertOk(self.gs("dr", "register", pid="none"))
        self.set_heartbeat("dr", 90)
        (f,) = self.files_for("dr")
        before = f.read_bytes()
        proc = self.gs("dr", "--dry-run", "beat", pid="none")
        self.assertOk(proc)
        self.assertIn("dry run", proc.stdout)
        self.assertEqual(f.read_bytes(), before,
                         "--dry-run beat wrote to disk:\n" + f.read_text())

    def test_no_subcommand_writes_under_dry_run(self):
        self.assertOk(self.gs("dr2", "register", "--files", "x.md"))
        self.set_heartbeat("dr2", 90)
        before = self.snapshot()
        for verb in (["register", "--task", "changed"], ["beat"], ["claim", "y.md"],
                     ["release"], ["register", "--resume"], ["register", "--new"]):
            with self.subTest(cmd=" ".join(verb)):
                proc = self.gs("dr2", "--dry-run", *verb)
                self.assertOk(proc)
                self.assertEqual(self.snapshot(), before,
                                 "--dry-run %s changed the registry" % " ".join(verb))

    def test_dry_run_register_still_reports_a_refusal(self):
        """A dry run must predict what a real one would do, not just exit 0."""
        holder = subprocess.Popen(["sleep", "120"])
        self._procs.append(holder)
        self.assertOk(self.gs("busy", "register", "--task", "first", pid=str(holder.pid)))
        proc = self.gs("busy", "--dry-run", "register", "--task", "second")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("ALREADY OPEN", proc.stderr)

    def test_id_parses_after_the_subcommand_too(self):
        """`--id X claim f` used to die with "unrecognized arguments": --id was
        declared on the top-level parser only. gt_log.py and gt_adr.py fixed the
        same defect with parents=[common] on every subparser."""
        self.assertOk(self.gs_raw("register", "--id", "argp", "--task", "t"))
        proc = self.gs_raw("claim", "f.md", "--id", "argp")
        self.assertOk(proc)
        (f,) = self.files_for("argp")
        self.assertIn("- `f.md`", f.read_text())
        for tail in (["beat", "--id", "argp"], ["check", "f.md", "--id", "argp"],
                     ["list", "--id", "argp"]):
            with self.subTest(cmd=tail[0]):
                self.assertOk(self.gs_raw(*tail))

    def test_missing_registration_and_missing_id_are_usage_failures(self):
        """The documented contract: 2 means the command could not be aimed."""
        self.assertEqual(self.gs("ghost", "beat").returncode, 2)
        self.assertEqual(self.gs("ghost", "claim", "x.md").returncode, 2)
        bare = self.py(self.tool, "list", env={"CLAUDE_PID": LIVE_PID,
                                               "CLAUDE_CODE_SESSION_ID": ""}, cwd=self.tmp)
        self.assertEqual(bare.returncode, 0, bare.stderr)   # list needs no id


CONCURRENT_CLAIMER = '''\
"""One concurrent claimer, for the lost-update test.

argv: <gt_session.py> <vault> <session-id> <file-to-claim> <start-flag> <index>

Runs the REAL cmd_claim in its own process, but two things are arranged so the
race is reproducible instead of a coin flip: every claimer spins on the same
start flag, so process startup jitter cannot serialise them, and _render sleeps,
which widens the window between the read and the write to something a scheduler
would otherwise open only occasionally. Neither changes what cmd_claim does --
they change only WHEN, which is the whole subject of the test.
"""
import argparse
import importlib.util
import os
import sys
import time

tool, vault, sid, target, flag, index = sys.argv[1:7]
spec = importlib.util.spec_from_file_location("gt_session_concurrent", tool)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.use_vault(vault)

real_render = mod._render
def slow_render(fm, body):          # the read has happened; the write has not
    time.sleep(0.03)
    return real_render(fm, body)
mod._render = slow_render

while not os.path.exists(flag):
    time.sleep(0.001)
time.sleep(0.005 * int(index))      # stagger the wake-ups; the windows still overlap
sys.exit(mod.cmd_claim(argparse.Namespace(id=sid, files=[target], force=False,
                                          stale_after=30)))
'''


class GtSessionConcurrencyTest(SessionTools):
    """The registry's writes must not lose each other.

    Every command here is a read-modify-write of one session file, and two
    processes CAN share a session id (--resume beside the run it resumed, a hook
    firing next to its session, an agent beside its parent). Before 0.16.4 the
    second write simply overwrote the first, so a claim could vanish with nothing
    reporting it -- and this registry is what Core rule 1, gt_lint_weekly.py's
    INBOX.md check and gt_demote.py's refusal all read to decide whether a file is
    someone else's. These tests fail against a blind `write_text`.
    """

    def load_tool(self):
        """Import gt_session.py pointed at this test's vault, for in-process races."""
        mod = load_module(self.tool, "gt_session_under_test")
        mod.use_vault(self.vault)
        return mod

    def claim_args(self, sid, *files):
        return argparse.Namespace(id=sid, files=list(files), force=False, stale_after=30)

    def quiet(self):
        """Swallow the tool's own stdout: these tests read files, not printouts."""
        return contextlib.redirect_stdout(io.StringIO())

    def test_concurrent_claims_on_one_session_id_all_survive(self):
        self.assertOk(self.gs("race", "register", "--task", "concurrent claims"))
        (f,) = self.files_for("race")
        helper = self.tmp / "claimer.py"
        helper.write_text(CONCURRENT_CLAIMER)
        flag = self.tmp / "go"

        wanted = ["a.md", "b.md", "c.md", "d.md"]
        env = dict(self.env, CLAUDE_PID=LIVE_PID)
        procs = [subprocess.Popen(
            [PYTHON, str(helper), str(self.tool), str(self.vault), "race", name,
             str(flag), str(i)],
            cwd=str(self.vault), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True) for i, name in enumerate(wanted)]
        flag.write_text("go\n")
        outs = [(p.wait(timeout=60), p) for p in procs]

        for rc, p in outs:
            self.assertEqual(rc, 0, "a concurrent claim failed:\n" + p.stderr.read())
        text = f.read_text()
        missing = [w for w in wanted if f"- `{w}`" not in text]
        self.assertEqual(missing, [], "concurrent claims were lost -- the session file "
                                      "kept %d of %d:\n%s" % (len(wanted) - len(missing),
                                                              len(wanted), text))
        for w in wanted:
            self.assertEqual(text.count(f"- `{w}`"), 1, "a retry duplicated " + w)
        self.assertEqual(self.files_for("race"), [f], "a concurrent claim made a second file")
        strays = [q.name for q in self.sessions.iterdir() if q != f]
        self.assertEqual(strays, [], "temp files were left behind in sessions/")

    def test_a_file_rewritten_between_the_read_and_the_write_is_merged_not_clobbered(self):
        """The deterministic half of the race: one writer, one interloper.

        _render is patched to write a competing claim to disk the first time it is
        called -- exactly the moment after cmd_claim has read the file and before
        it writes it back. The claim being written must not erase that, and the
        interloper's line must not erase the new claim either: both belong to the
        session.
        """
        self.assertOk(self.gs("merge", "register", "--task", "interleave"))
        (f,) = self.files_for("merge")
        mod = self.load_tool()

        real_render, calls = mod._render, []

        def render_then_interlope(fm, body):
            calls.append(1)
            if len(calls) == 1:                      # only the first pass is raced
                f.write_text(f.read_text().rstrip("\n") + "\n- `theirs.md`\n")
            return real_render(fm, body)

        mod._render = render_then_interlope
        with self.quiet():
            rc = mod.cmd_claim(self.claim_args("merge", "mine.md"))
        self.assertEqual(rc, 0)
        self.assertGreater(len(calls), 1, "the writer never noticed the file had changed")

        text = f.read_text()
        self.assertIn("- `theirs.md`", text, "the claim clobbered the other writer's claim")
        self.assertIn("- `mine.md`", text, "the retry dropped the claim it was asked to record")

    def test_a_heartbeat_does_not_clobber_a_claim_written_underneath_it(self):
        self.assertOk(self.gs("beatrace", "register", "--task", "heartbeat vs claim"))
        (f,) = self.files_for("beatrace")
        mod = self.load_tool()

        real_render, calls = mod._render, []

        def render_then_interlope(fm, body):
            calls.append(1)
            if len(calls) == 1:
                f.write_text(f.read_text().rstrip("\n") + "\n- `theirs.md`\n")
            return real_render(fm, body)

        mod._render = render_then_interlope
        with self.quiet():
            rc = mod.cmd_beat(argparse.Namespace(id="beatrace"))
        self.assertEqual(rc, 0)
        self.assertIn("- `theirs.md`", f.read_text(),
                      "the heartbeat threw away a claim written while it ran")

    def test_a_write_that_never_wins_fails_loudly_instead_of_reporting_success(self):
        """"Could not record" must never print as "recorded".

        A writer that loses every attempt has NOT recorded the claim, and the one
        thing it must not do is exit 0 -- the caller would go on to edit a file it
        does not hold. Here the file is rewritten on every pass, so no attempt can
        ever win.
        """
        self.assertOk(self.gs("doomed", "register", "--task", "always losing"))
        (f,) = self.files_for("doomed")
        mod = self.load_tool()

        real_render, calls = mod._render, []

        def always_interlope(fm, body):
            calls.append(1)
            f.write_text(f.read_text().rstrip("\n") + f"\n- `other{len(calls)}.md`\n")
            return real_render(fm, body)

        mod._render = always_interlope
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = mod.cmd_claim(self.claim_args("doomed", "mine.md"))
        self.assertNotEqual(rc, 0, "a claim that was never written reported success")
        self.assertEqual(len(calls), mod.CAS_ATTEMPTS, "the retry was not bounded")
        self.assertIn(str(f), err.getvalue(), "the failure did not name the file")
        self.assertIn("NOT recorded", err.getvalue())
        self.assertNotIn("- `mine.md`", f.read_text(),
                         "a failed claim was written anyway")

    def test_the_swap_keeps_the_file_s_own_mode(self):
        """A temp file starts at 0600; the session file must not inherit that.

        Registering and claiming both write through mkstemp now, so without an
        explicit chmod every session file would quietly become owner-only -- a
        permissions change nobody asked for, in a directory other processes read.
        """
        self.assertOk(self.gs("modes", "register"))
        (f,) = self.files_for("modes")
        plain = self.tmp / "plain.md"
        plain.write_text("x\n")                      # what a normal write yields here
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), stat.S_IMODE(plain.stat().st_mode),
                         "register left the session file on the temp file's mode")

        os.chmod(f, 0o640)
        self.assertOk(self.gs("modes", "claim", "x.md"))
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o640,
                         "the claim swap did not preserve the file's mode")

    def test_the_temp_file_is_not_a_predictable_name(self):
        """A `<target>.tmp` beside the target is a collision between writers.

        Two claimers with one fixed temp name write over each other's half-finished
        file and then rename the mixture into place, which is the same lost update
        one level down. So the name must come from mkstemp, not from the target.
        """
        self.assertOk(self.gs("tmpname", "register"))
        (f,) = self.files_for("tmpname")
        mod = self.load_tool()
        seen = []
        real_mkstemp = mod.tempfile.mkstemp

        def watched(*a, **kw):
            fd, path = real_mkstemp(*a, **kw)
            seen.append(path)
            return fd, path

        mod.tempfile.mkstemp = watched
        try:
            with self.quiet():
                self.assertEqual(mod.cmd_claim(self.claim_args("tmpname", "x.md")), 0)
        finally:
            mod.tempfile.mkstemp = real_mkstemp
        self.assertTrue(seen, "the write did not go through a temp file")
        for path in seen:
            self.assertEqual(os.path.dirname(path), str(f.parent),
                             "the temp file was not in the target's own directory")
            self.assertNotEqual(os.path.basename(path), f.name + ".tmp")
            self.assertFalse(os.path.exists(path), "the temp file was left behind")
        self.assertIn("- `x.md`", f.read_text())


class NoFlockIsAnnouncedTest(SessionTools):
    """H7: `_hold` returns True when fcntl is missing -- correct, but not equal.

    Measured with the shipped code and fcntl stubbed out, four concurrent claimers,
    ten trials each: with flock, 0/10 trials silently dropped a claim; without it,
    9/10 did, and every process printed success. The degradation must not be
    fatal -- a network mount without flock has to keep working -- but it must be
    audible, on the write paths and to anyone surveying the vault.
    """

    def unlocked_tool(self):
        mod = load_module(self.tool, "gt_session_noflock")
        mod.use_vault(self.vault)
        mod.fcntl = None                 # the platform this module's fallback is for
        return mod

    def claim_args(self, sid, *files):
        return argparse.Namespace(id=sid, files=list(files), force=False, stale_after=30)

    def capture(self, fn):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = fn()
        return rc, err.getvalue()

    def test_a_claim_written_without_flock_says_so_and_still_works(self):
        self.assertOk(self.gs("nolock", "register"))
        mod = self.unlocked_tool()
        rc, err = self.capture(lambda: mod.cmd_claim(self.claim_args("nolock", "x.md")))
        self.assertEqual(rc, 0, "the missing lock was made fatal: " + err)
        (f,) = self.files_for("nolock")
        self.assertIn("- `x.md`", f.read_text(), "the degraded path stopped recording claims")
        self.assertIn("NOT lock-guarded", err,
                      "a write that could not be guarded said nothing:\n" + err)

    def test_the_warning_is_printed_once_per_run_not_once_per_attempt(self):
        self.assertOk(self.gs("noloop", "register"))
        (f,) = self.files_for("noloop")
        mod = self.unlocked_tool()
        real_render, calls = mod._render, []

        def render_then_interlope(fm, body):         # force at least one CAS retry
            calls.append(1)
            if len(calls) == 1:
                f.write_text(f.read_text().rstrip("\n") + "\n- `theirs.md`\n")
            return real_render(fm, body)

        mod._render = render_then_interlope
        rc, err = self.capture(lambda: mod.cmd_claim(self.claim_args("noloop", "mine.md")))
        self.assertEqual(rc, 0)
        self.assertGreater(len(calls), 1, "the retry this test needs never happened")
        self.assertEqual(err.count("NOT lock-guarded"), 1,
                         "the degradation warning repeated per attempt:\n" + err)

    def test_list_and_check_surface_that_claims_cannot_be_guaranteed(self):
        self.assertOk(self.gs("survey", "register", "--files", "x.md"))
        for verb, ns in (("list", argparse.Namespace(id="survey", stale_after=30)),
                         ("check", argparse.Namespace(id="survey", file="x.md", stale_after=30))):
            with self.subTest(cmd=verb):
                mod = self.unlocked_tool()
                rc, err = self.capture(lambda: getattr(mod, "cmd_" + verb)(ns))
                self.assertEqual(rc, 0)
                self.assertIn("cannot guarantee claim integrity", err,
                              "%s did not say this vault cannot guarantee claims:\n%s"
                              % (verb, err))
                self.assertEqual([p for p in self.sessions.glob("*") if p.suffix == ".tmp"], [],
                                 "the lock probe left a temp file behind")


if __name__ == "__main__":
    unittest.main()
