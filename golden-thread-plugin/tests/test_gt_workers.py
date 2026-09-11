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
