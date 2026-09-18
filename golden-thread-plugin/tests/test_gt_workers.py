"""gt_workers.py: find abandoned Claude worker shells and classify them by CPU.

NO REAL PROCESS IS INSPECTED OR SIGNALLED. Every test feeds a fixture `ps` table:
CLI runs put a fake `ps` first on PATH; in-process runs replace the module's
`subprocess` and patch `os.kill`/`time.sleep` with recorders. Fixture pids are in
the 900000s, and a real kill path is only ever exercised with os.kill patched.
"""
import json
import os
import socket
import sys
import time
import types
import unittest
from unittest import mock

from _harness import Sandbox, SCRIPTS, load_module

TOOL = SCRIPTS / "gt_workers.py"
SNAP = "/Users/x/.claude/shell-snapshots/snapshot-zsh-1.sh"

# pid ppid etime time args
PS = """\
    1      0 20-00:00:00   9:00.00 /sbin/launchd
900001     1 10-23:00:00   0:00.01 /bin/zsh -c source %(s)s && eval 'find ~ -name foo' < /dev/null && pwd -P >| /tmp/cwd
900002     1  2-00:00:00   0:00.02 /bin/zsh -c source %(s)s && eval 'python3 crunch.py' < /dev/null
900003 900002 2-00:00:00   0:30.00 python3 crunch.py
900004     1       00:30   0:00.00 /bin/zsh -c source %(s)s && eval 'sleep 100' < /dev/null
900005     1    01:00:00  12:00.00 /usr/local/bin/claude --resume
not a ps line at all
""" % {"s": SNAP}


# 2026-09-14 15:50, as a second session's SessionStart saw it: every worker below is a
# shell of ANOTHER, live session (claude 910000). Two are wait loops whose only child is
# `sleep`; one runs tests. The check called the first two ORPHAN and offered `reap`.
TASK = "/private/tmp/claude-501/-Users-x-vault/cb41fa74-0965-4f5c-be6e-cd6c9071803b/tasks/b1.output"
LIVE = """\
    1      0 20-00:00:00   9:00.00 /sbin/launchd
910000     1  1-01:28:32  30:00.00 claude
910001 910000    16:15   0:01.06 /bin/zsh -c source %(s)s && eval 'F=%(t)s; until grep -q "test_install_vault_upgrade:" $F 2>/dev/null; do sleep 5; done; cat $F' < /dev/null && pwd -P >| /tmp/cwd
910011 910001    00:01   0:00.00 sleep 5
910002 910000    06:05   0:00.22 /bin/zsh -c source %(s)s && eval 'F=%(t)s; until grep -q "test_install_vault_upgrade:" $F; do sleep 10; done; tail -3 $F' < /dev/null && pwd -P >| /tmp/cwd
910012 910002    00:04   0:00.00 sleep 10
910003 910000    16:25   0:00.09 /bin/zsh -c source %(s)s && eval 'cd "/repo/golden-thread-plugin"; bash dev/render-pdfs.sh' < /dev/null && pwd -P >| /tmp/cwd
910013 910003    16:20   0:02.71 python3.12 -m unittest test_install
""" % {"s": SNAP, "t": TASK}


class WorkersOfALiveSession(Sandbox):
    """Shells whose `claude` is alive are never orphans and never reaped."""

    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.tmp / "ps.txt").write_text(LIVE)
        ps = self.bin / "ps"
        ps.write_text("#!/bin/sh\ncat '%s'\n" % (self.tmp / "ps.txt"))
        ps.chmod(0o755)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")

    def test_live_session_shells_are_not_orphans(self):
        p = self.py(TOOL, "check")
        self.assertOk(p)
        out = p.stdout
        self.assertNotIn("ORPHAN", out)
        self.assertNotIn("needing a decision", out)
        self.assertNotIn("reap", out)
        self.assertIn("clean", out.splitlines()[0])        # gt-doctor reads line one
        self.assertIn("3 belong to other live sessions", out)
        self.assertIn("session cb41fa74 (claude pid 910000, up 1-01:28:32)", out)
        self.assertIn('WAITING on: grep -q "test_install_vault_upgrade:" %s' % TASK, out)
        self.assertIn("pid 910003", out)

    def test_reap_refuses_workers_of_a_live_session(self):
        p = self.py(TOOL, "reap", "--dry-run")
        self.assertOk(p)
        self.assertIn("would reap 0 stalled worker(s): none", p.stdout)
        self.assertIn("3 belong to live sessions, never reaped", p.stdout)


class WorkersCli(Sandbox):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.ps_out = self.tmp / "ps.txt"
        self.ps_out.write_text(PS)
        ps = self.bin / "ps"
        ps.write_text("#!/bin/sh\ncat '%s'\n" % self.ps_out)
        ps.chmod(0o755)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")
        self.registry = self.home / ".claude" / "golden-thread" / "workers.jsonl"

    def check(self, *extra, **kw):
        p = self.py(TOOL, "check", *extra, **kw)
        self.assertOk(p, "worker check must never fail a session start")
        return p.stdout

    def test_classifies_fixture_table(self):
        out = self.check()
        self.assertIn("CLAUDE WORKERS needing a decision (2)", out)
        self.assertIn("ORPHAN — pid 900001, alive 10-23:00:00, only 0.01s CPU", out)
        self.assertIn("find ~ -name foo", out)
        # CPU is judged over the whole subtree: the shell burned 0.02s, its child 30s
        self.assertIn("UNDECLARED but ACTIVE — pid 900002", out)
        self.assertIn("30.0s CPU", out)
        self.assertNotIn("900004", out)          # too young to judge
        self.assertNotIn("900005", out)          # the claude session itself
        self.assertIn("reap the stalled ones", out)

    def test_declared_worker_banner_and_declared_stalled_alert(self):
        p = self.py(TOOL, "declare", "900002", "crunching", "the", "numbers")
        self.assertOk(p)
        p = self.py(TOOL, "declare", "900001", "indexing home")
        self.assertOk(p)
        rows = [json.loads(l) for l in self.registry.read_text().splitlines()]
        self.assertEqual({r["pid"] for r in rows}, {900001, 900002})
        self.assertEqual(rows[0]["host"], socket.gethostname())
        self.assertEqual(rows[0]["why"], "crunching the numbers")
        out = self.check()
        self.assertIn("ACTIVE CLAUDE WORKER: python3 crunch.py", out)
        self.assertIn("crunching the numbers", out)
        self.assertIn("STALLED, though declared — pid 900001", out)
        self.assertIn("declared for: indexing home", out)
        self.assertIn("NOT happening", out)
        self.assertNotIn("UNDECLARED", out)

    def test_clean_says_so_and_counts(self):
        self.ps_out.write_text(PS.splitlines()[0] + "\n")
        self.assertEqual(self.check().strip(),
                         "GT workers: clean — no background workers alive.")
        young_only = "\n".join(l for l in PS.splitlines() if l.startswith("900004"))
        self.ps_out.write_text(young_only + "\n")
        self.assertIn("1 alive, none orphaned (1 too new to judge)", self.check())

    def test_registry_prunes_dead_pids_and_keeps_live(self):
        self.py(TOOL, "declare", "900002", "live")
        self.py(TOOL, "declare", "900099", "long gone")
        self.check()
        pids = [json.loads(l)["pid"] for l in self.registry.read_text().splitlines()]
        self.assertEqual(pids, [900002])

    def test_a_declaration_for_a_live_non_shell_pid_survives_the_prune(self):
        """`declare <pid>` accepts ANY pid; the prune must judge the same population.

        report() pruned against the Claude-spawned SHELLS only, so declaring 900003 -- the
        live python process doing the actual work, right there in the same ps table -- deleted
        the declaration on the very next check. The alert it was suppressing came back with
        nothing left on disk to explain why it had ever been quiet, which is the failure mode
        the registry exists to prevent."""
        self.py(TOOL, "declare", "900003", "the python child, doing the work")
        self.py(TOOL, "declare", "900005", "the claude session itself")
        self.py(TOOL, "declare", "900099", "long gone")
        self.check()
        pids = sorted(json.loads(l)["pid"] for l in self.registry.read_text().splitlines())
        self.assertEqual(pids, [900003, 900005],
                         "a declaration was dropped for a process that is plainly alive")

    def test_other_hosts_declarations_do_not_count(self):
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        self.registry.write_text(json.dumps({"pid": 900001, "host": "some-other-host",
                                             "why": "x"}) + "\nnot json\n")
        out = self.check()
        self.assertIn("ORPHAN — pid 900001", out)

    def test_policy_off(self):
        self.config(vault_path=str(self.tmp), orphan_check="off")
        self.assertEqual(self.check(), "")

    def test_hook_json(self):
        d = json.loads(self.check("--hook"))
        self.assertIn("ORPHAN — pid 900001", d["systemMessage"])
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(d["hookSpecificOutput"]["additionalContext"], d["systemMessage"])

    def test_reap_dry_run_names_only_stalled(self):
        p = self.py(TOOL, "reap", "--dry-run")
        self.assertOk(p)
        self.assertEqual(p.stdout.strip(), "would reap 1 stalled worker(s): 900001")

    def test_list(self):
        p = self.py(TOOL, "list")
        self.assertOk(p)
        pids = [l.split()[0] for l in p.stdout.splitlines()]
        self.assertEqual(sorted(pids), ["900001", "900002", "900004"])

    def test_ps_failure_is_clean_not_crash(self):
        (self.bin / "ps").write_text("#!/bin/sh\nexit 1\n")
        self.assertIn("no background workers alive", self.check())

    def test_usage(self):
        p = self.py(TOOL, "bogus")
        self.assertEqual(p.returncode, 2)


class WorkersInProcess(Sandbox):
    def setUp(self):
        super().setUp()
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.m = load_module(TOOL, "gt_workers_under_test")
        fake_run = lambda *a, **k: types.SimpleNamespace(stdout=PS, returncode=0)
        self.m.subprocess = types.SimpleNamespace(run=fake_run)
        self.kills = []
        p1 = mock.patch.object(os, "kill", side_effect=lambda pid, sig: self.kills.append((pid, sig)))
        p2 = mock.patch.object(time, "sleep", lambda s: None)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def tearDown(self):
        if self._home is not None:
            os.environ["HOME"] = self._home
        super().tearDown()

    def test_registry_is_in_sandbox(self):
        self.assertTrue(self.m.REGISTRY.startswith(str(self.home)))

    def test_time_parsers(self):
        e = self.m._elapsed_seconds
        self.assertEqual(e("00:30"), 30)
        self.assertEqual(e("01:00:00"), 3600)
        self.assertEqual(e("10-23:00:00"), 10 * 86400 + 23 * 3600)
        self.assertEqual(e(" 5-00:00:01 "), 5 * 86400 + 1)
        self.assertEqual(e("garbage"), 0.0)
        c = self.m._cpu_seconds
        self.assertAlmostEqual(c("0:00.01"), 0.01)
        self.assertEqual(c("1466:32.00"), 1466 * 60 + 32)       # macOS: minutes roll up
        self.assertEqual(c("01:02:03"), 3723)
        self.assertEqual(c("junk"), 0.0)

    def test_cpu_time_with_days_is_parsed(self):
        # procps (Linux) prints TIME as [DD-]HH:MM:SS. A worker past 24h of CPU
        # parses to 0.0, classifies as STALLED, and `reap` kills live work.
        self.assertEqual(self.m._cpu_seconds("1-02:03:04"), 93784.0,
                         "_cpu_seconds cannot parse a DD- prefix; heavy workers read as 0 CPU")

    def test_workers_parse_and_subtree_cpu(self):
        ws = {w["pid"]: w for w in self.m.workers()}
        self.assertEqual(sorted(ws), [900001, 900002, 900004])
        self.assertEqual(ws[900001]["cmd"], "find ~ -name foo")
        self.assertEqual(ws[900001]["elapsed_raw"], "10-23:00:00")
        self.assertAlmostEqual(ws[900002]["cpu"], 30.02)
        self.assertEqual(ws[900002]["ppid"], 1)

    def test_classify_boundaries(self):
        c = self.m.classify
        w = lambda pid, el, cpu: {"pid": pid, "elapsed": el, "cpu": cpu}
        self.assertEqual(c(w(1, 299, 0.0), {}), "young")
        self.assertEqual(c(w(1, 300, 1.99), {}), "undeclared-stalled")
        self.assertEqual(c(w(1, 300, 2.0), {}), "undeclared-working")
        self.assertEqual(c(w(1, 9e5, 0.0), {1: {}}), "declared-stalled")
        self.assertEqual(c(w(1, 9e5, 50.0), {1: {}}), "active")

    def test_reap_kills_only_stalled(self):
        self.m.declare(900002, "real work")
        buf = []
        with mock.patch("builtins.print", lambda *a, **k: buf.append(" ".join(map(str, a)))):
            self.m.reap()
        signalled = {pid for pid, _ in self.kills}
        self.assertEqual(signalled, {900001}, "reap touched a non-stalled worker")
        self.assertIn((900001, 15), self.kills)
        self.assertIn("reaped 1 stalled worker(s): 900001", buf[-1])

    def _live(self):
        fake_run = lambda *a, **k: types.SimpleNamespace(stdout=LIVE, returncode=0)
        self.m.subprocess = types.SimpleNamespace(run=fake_run)

    def test_owner_session_and_waiting_are_derived(self):
        self._live()
        ws = {w["pid"]: w for w in self.m.workers()}
        self.assertEqual(ws[910001]["owner"], 910000)
        self.assertEqual(ws[910001]["owner_elapsed_raw"], "1-01:28:32")
        self.assertEqual(ws[910001]["session"], "cb41fa74-0965-4f5c-be6e-cd6c9071803b")
        self.assertTrue(ws[910001]["waiting"])
        self.assertIn(TASK, ws[910001]["waits_on"])          # $F expanded
        self.assertFalse(ws[910003]["waiting"])
        self.m.subprocess = types.SimpleNamespace(
            run=lambda *a, **k: types.SimpleNamespace(stdout=PS, returncode=0))
        ws = {w["pid"]: w for w in self.m.workers()}
        self.assertIsNone(ws[900001]["owner"])                # reparented to launchd

    def test_reap_never_signals_a_live_sessions_worker(self):
        self._live()
        # even if classify were wrong, the guard in reap must hold
        with mock.patch.object(self.m, "classify", lambda *a, **k: "undeclared-stalled"), \
                mock.patch("builtins.print", lambda *a, **k: None):
            self.m.reap()
        self.assertEqual(self.kills, [], "reap signalled a worker whose claude is alive")

    def test_own_session_workers(self):
        self._live()
        ws = {w["pid"]: w for w in self.m.workers()}
        c = self.m.classify
        self.assertEqual(c(ws[910001], {}, mine=None), "live-session")
        self.assertEqual(c(ws[910003], {}, mine=None), "live-session")
        self.assertEqual(c(ws[910001], {}, mine=910000), "waiting")
        self.assertEqual(c(ws[910003], {}, mine=910000), "undeclared-working")
        self.assertEqual(c(ws[910003], {910003: {}}, mine=910000), "active")
        idle = dict(ws[910003], cpu=0.1)
        self.assertEqual(c(idle, {}, mine=910000), "own-idle")

    def test_policy_reap_runs_report_then_reap_in_one_capture(self):
        real = load_module(SCRIPTS / "gt_settings.py", "gt_settings_for_workers")
        said = []
        fake = types.SimpleNamespace(get=lambda n: "reap", capture=real.capture,
                                     emit=lambda text, **k: said.append(text))
        with mock.patch.dict(sys.modules, {"gt_settings": fake}), \
                mock.patch.object(sys, "argv", ["gt_workers.py", "check", "--hook"]):
            self.assertEqual(self.m.main(), 0)
        self.assertEqual(len(said), 1)
        self.assertIn("ORPHAN — pid 900001", said[0])
        self.assertIn("reaped 1 stalled worker(s): 900001", said[0])
        self.assertNotIn(900002, {pid for pid, _ in self.kills})


if __name__ == "__main__":
    unittest.main()
