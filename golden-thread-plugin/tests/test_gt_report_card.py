"""gt_report_card.py: the PreCompact / SessionEnd report card.

Fixture: a fresh vault from vault_init.py, fully committed so the baseline is clean.
Modes off / minimal / full; hygiene findings (uncommitted files, stale TASKS.md,
unrecoverable vs recoverable safe_write backlog, unattributed edits); feature
findings only under `full`; closeout candidates gated by closeout_check.

Since 0.12.9 the card is also parked in ~/.claude/golden-thread/notices/report-card.md
(PreCompact/SessionEnd stdout reaches no one, per the hooks reference, 2026-09-13) and
delivered by `surface --hook` at SessionStart as JSON.
"""
import json
import os
import time
import unittest

from _harness import Sandbox, SCRIPTS

TOOL = SCRIPTS / "gt_report_card.py"


class ReportCard(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        if (self.vault / ".git").exists():
            self.run_cmd(["git", "-C", self.vault, "add", "-A"])
            self.run_cmd(["git", "-C", self.vault, "commit", "-q", "-m", "baseline"])
        self.setting(closeout_check="off")

    def setting(self, **kv):
        p = self.home / ".claude" / "vault-config.json"
        d = json.loads(p.read_text())
        d.update(kv)
        p.write_text(json.dumps(d))

    def card(self, *args, input=""):
        p = self.py(TOOL, *args, input=input)
        self.assertOk(p, "report card must never fail a session close")
        return p.stdout

    def age(self, path, days):
        t = time.time() - days * 86400
        os.utime(path, (t, t))

    # -- modes ---------------------------------------------------------------------
    def test_clean_baseline(self):
        self.assertEqual(self.card().strip(), "GOLDEN THREAD report card (minimal): clean.")

    def test_off_is_silent(self):
        self.setting(report_card="off")
        (self.vault / "x.md").write_text("x")
        self.assertEqual(self.card(), "")

    def test_invalid_mode_is_minimal(self):
        self.setting(report_card="verbose")
        self.assertIn("report card (minimal)", self.card())

    def test_no_vault_configured(self):
        p = self.home / ".claude" / "vault-config.json"
        p.write_text(json.dumps({"vault_path": str(self.tmp / "missing")}))
        self.assertEqual(self.card(), "")
        p.unlink()
        self.assertEqual(self.card(), "")

    def test_tolerates_hook_flag(self):
        self.assertIn("clean", self.card("--hook"))

    # -- hygiene -------------------------------------------------------------------
    def test_uncommitted_threshold(self):
        for i in range(7):
            (self.vault / ("n%d.md" % i)).write_text("x")
        self.assertIn("clean", self.card())
        (self.vault / "n7.md").write_text("x")
        out = self.card()
        self.assertIn("This session:", out)
        self.assertIn("8 uncommitted file(s)", out)

    def test_tasks_md_older_than_a_project_readme(self):
        self.age(self.vault / "TASKS.md", 2)
        out = self.card()
        self.assertIn("TASKS.md is older than 1 project README(s) (golden-thread)", out)

    def test_safe_write_backlog_counts_only_recoverable(self):
        pending = self.tmp / "pending.md"
        pending.write_text("diverted content")
        ledger = self.home / ".claude" / "safe_write_ledger.jsonl"
        rows = [
            {"target": "/v/a.md", "wrote_to": str(pending), "done": False},
            {"target": "/v/b.md", "wrote_to": str(self.tmp / "deleted.md"), "done": False},
            {"target": "/v/c.md", "wrote_to": str(pending) + ".x", "done": True},
        ]
        ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\nbroken line\n")
        out = self.card()
        self.assertIn("1 safe_write entr(ies) never landed", out)
        # the same pair landing later supersedes the pending entry
        with open(ledger, "a") as fh:
            fh.write(json.dumps({"target": "/v/a.md", "wrote_to": str(pending),
                                 "done": True}) + "\n")
        self.assertIn("clean", self.card())

    def test_unattributed_vault_writes(self):
        ledger = self.vault / ".git" / "gt-edits.jsonl"
        ledger.write_text(json.dumps({"path": "Knowledge/x.md", "session": "abcd1234"})
                          + "\n")
        out = self.card()
        self.assertIn("no registered task (abcd1234)", out)
        self.assertIn("NO session registered at all", out)
        # registered session with a task: neither finding
        sdir = self.vault / "Projects" / "golden-thread" / "sessions"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "abcd1234.md").write_text("session\n")
        ledger.write_text(json.dumps({"path": "Knowledge/x.md", "session": "abcd1234",
                                      "task": "t1"}) + "\n")
        self.assertIn("clean", self.card())

    # -- feature (full only) -------------------------------------------------------
    def _feature_fixtures(self):
        kn = self.vault / "Knowledge"
        page = kn / "old-page.md"
        page.write_text("---\ntitle: Old\nstatus: stale\n---\nbody\n")
        self.age(page, 45)
        with open(self.vault / "review-queue.md", "a") as fh:
            fh.write("- [ ] look at this\n- [x] done already\n- [ ] and this\n")
        mem = self.vault / "Projects" / "golden-thread" / "memory"
        mem.mkdir()
        (mem / "MEMORY.md").write_text("- [listed](listed.md)\n")
        (mem / "listed.md").write_text("x")
        (mem / "orphan-note.md").write_text("x")
        self.run_cmd(["git", "-C", self.vault, "add", "-A"])
        self.run_cmd(["git", "-C", self.vault, "commit", "-q", "-m", "fixtures"])

    def test_minimal_never_shows_feature_findings(self):
        self._feature_fixtures()
        out = self.card()
        self.assertNotIn("Available and unused", out)
        self.assertIn("clean", out)

    def test_full_shows_feature_findings(self):
        self._feature_fixtures()
        self.setting(report_card="full")
        out = self.card()
        self.assertIn("report card (full)", out)
        self.assertIn("Available and unused:", out)
        self.assertIn("No Knowledge page has changed in 45 days", out)
        self.assertIn("2 item(s) waiting in review-queue.md", out)
        self.assertIn("1 Knowledge page(s) marked `status: stale` (old-page.md)", out)
        self.assertIn("golden-thread (1)", out)                   # one unlisted memory file
        self.assertIn("independently verified", out)

    def test_full_validation_finding_clears_when_used(self):
        self.setting(report_card="full")
        (self.vault / "Projects" / "golden-thread" / "research.md").write_text(
            "## 2026-09-01\nThe figure was independently verified.\n")
        self.run_cmd(["git", "-C", self.vault, "add", "-A"])
        self.run_cmd(["git", "-C", self.vault, "commit", "-q", "-m", "r"])
        self.assertNotIn("labelled `independently verified`", self.card())

    # -- closeout ------------------------------------------------------------------
    def _finished_project(self):
        d = self.vault / "Projects" / "done-proj"
        d.mkdir()
        (d / "README.md").write_text(
            "---\ntype: project\nslug: done-proj\nstage: active\n---\n\n# Done\n\n"
            "## Tasks\n\n- [x] one\n- [x] two\n\n## Notes\n")
        self.run_cmd(["git", "-C", self.vault, "add", "-A"])
        self.run_cmd(["git", "-C", self.vault, "commit", "-q", "-m", "p"])

    def test_closeout_off_asks_nothing(self):
        self._finished_project()
        self.assertNotIn("look finished", self.card())
        self.assertFalse((self.vault / "Projects" / "golden-thread" /
                          "closeout-signals.jsonl").exists())

    def test_closeout_ask_names_candidate_and_records_it(self):
        self._finished_project()
        self.setting(closeout_check="ask")
        out = self.card()
        self.assertIn("ASK THE USER whether to close each one", out)
        self.assertIn("done-proj (active): empty: every task is checked off", out)
        rec = self.vault / "Projects" / "golden-thread" / "closeout-signals.jsonl"
        rows = [json.loads(l) for l in rec.read_text().splitlines()]
        self.assertEqual([(r["event"], r["slug"], r["source"]) for r in rows],
                         [("asked", "done-proj", "report-card")])

    # -- notice file + SessionStart surface (0.12.9) --------------------------------
    @property
    def notice(self):
        return self.home / ".claude" / "golden-thread" / "notices" / "report-card.md"

    def precompact(self, sid="sess-1"):
        return self.card(input=json.dumps({"session_id": sid,
                                           "hook_event_name": "PreCompact"}))

    def surface(self):
        p = self.py(TOOL, "surface", "--hook", input="")
        self.assertOk(p, "surface must fail open")
        return p

    def test_precompact_writes_notice(self):
        for i in range(8):
            (self.vault / ("n%d.md" % i)).write_text("x")
        out = self.precompact()
        self.assertIn("8 uncommitted file(s)", out)          # stdout still carries it
        body = self.notice.read_text()
        self.assertIn("session sess-1", body)
        self.assertIn("PreCompact", body)
        self.assertIn("8 uncommitted file(s)", body)
        self.assertRegex(body, r"## Report card -- \d{4}-\d{2}-\d{2}T")
        self.assertEqual([f.name for f in self.notice.parent.iterdir()], ["report-card.md"],
                         "atomic write must not leave temp files behind")

    def test_off_writes_no_notice(self):
        self.setting(report_card="off")
        self.assertEqual(self.precompact(), "")
        self.assertFalse(self.notice.exists())

    def test_surface_emits_json_and_removes_notice(self):
        for i in range(8):
            (self.vault / ("n%d.md" % i)).write_text("x")
        self.precompact()
        p = self.surface()
        d = json.loads(p.stdout)
        self.assertIn("systemMessage", d)
        self.assertIn("1 finding", d["systemMessage"])
        self.assertEqual(len(d["systemMessage"].splitlines()), 1)
        hso = d["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        self.assertIn("8 uncommitted file(s)", hso["additionalContext"])
        self.assertIn("first reply", hso["additionalContext"])
        self.assertFalse(self.notice.exists())
        self.assertEqual(self.surface().stdout, "")          # delivered once only

    def test_surface_without_notice_is_silent(self):
        p = self.surface()
        self.assertEqual(p.stdout, "")
        self.assertEqual(p.stderr, "")

    def test_surface_unreadable_notice_is_silent(self):
        self.notice.mkdir(parents=True)                      # a directory, not a file
        self.assertEqual(self.surface().stdout, "")
        self.notice.rmdir()
        self.notice.write_bytes(b"")                         # empty
        self.assertEqual(self.surface().stdout, "")
        self.notice.write_text("x")
        self.notice.chmod(0)                                 # unreadable
        try:
            p = self.surface()
            if os.geteuid() != 0:
                self.assertEqual(p.stdout, "")
        finally:
            self.notice.chmod(0o600)

    def test_surface_truncates_large_notice(self):
        self.notice.parent.mkdir(parents=True)
        self.notice.write_text("GOLDEN THREAD report card (minimal)\n" + "   - y\n" * 5000)
        p = self.surface()
        self.assertLess(len(p.stdout), 10000)
        self.assertIn("truncated", json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_successive_runs_stack_up_to_cap(self):
        self.precompact("s1")
        self.precompact("s2")
        body = self.notice.read_text()
        self.assertIn("session s1", body)
        self.assertIn("session s2", body)
        self.precompact("s3")
        self.precompact("s4")
        body = self.notice.read_text()
        self.assertNotIn("session s1", body)
        for s in ("s2", "s3", "s4"):
            self.assertIn("session %s" % s, body)
        ctx = json.loads(self.surface().stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertLess(ctx.index("session s2"), ctx.index("session s4"))

    def test_closeout_calls_pass_vault(self):
        tools = self.vault / "Projects" / "golden-thread" / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        log = self.tmp / "argv.jsonl"
        (tools / "gt_closeout.py").write_text(
            "import json, sys\n"
            "open(%r, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if 'candidates' in sys.argv:\n"
            "    print(json.dumps([{'slug': 'p1', 'stage': 'active', 'reasons': ['r']}]))\n"
            % str(log))
        self.run_cmd(["git", "-C", self.vault, "add", "-A"])
        self.run_cmd(["git", "-C", self.vault, "commit", "-q", "-m", "stub"])
        self.setting(closeout_check="ask")
        out = self.precompact()
        self.assertIn("ASK THE USER", out)
        calls = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertEqual(c[0], "--vault")
            self.assertEqual(os.path.realpath(c[1]), os.path.realpath(self.vault))
        self.assertIn("ask", calls[1])
        self.assertIn("ASK THE USER", self.notice.read_text())


if __name__ == "__main__":
    unittest.main()
