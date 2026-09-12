"""gt_lint_weekly.py: the scheduled wrapper around gt_lint.

Everything it touches is under HOME (config, log, the gt-wiki plugin cache) or the
vault named in the config, so all of it runs in the sandbox. The launchd agent that
schedules it is not part of this script (it is described in the golden-thread
runbook) and is not exercised here.
"""
import datetime
import os
import time
import unittest

from _harness import Sandbox, SCRIPTS, ENFORCEMENT_HOOKS

WEEKLY = SCRIPTS / "gt_lint_weekly.py"


class WeeklyBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.today = datetime.date.today().isoformat()
        self.log = self.home / ".claude" / "golden-thread" / "lint_weekly.log"

    def wired_vault(self):
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for n in ENFORCEMENT_HOOKS:
            (hooks / n).write_text("#!/bin/sh\n")
            (hooks / n).chmod(0o755)
        return self.make_vault()

    def seed_findings(self, v):
        (v / "Knowledge" / "Lonely.md").write_text("see [[Nowhere]]\n")   # index-gap, orphan, broken-link

    def run_weekly(self):
        proc = self.py(WEEKLY)
        self.assertOk(proc, "gt_lint_weekly.py must always exit 0")
        return proc

    def inbox_lines(self, v):
        return [l for l in (v / "INBOX.md").read_text().splitlines() if "Weekly vault lint" in l]


class WeeklyTest(WeeklyBase):
    def test_no_config_is_a_logged_noop(self):
        self.run_weekly()
        self.assertIn("no vault-config.json", self.log.read_text())

    def test_findings_write_report_and_one_inbox_line(self):
        v = self.wired_vault()
        self.seed_findings(v)
        self.run_weekly()
        out = v / "Projects" / "golden-thread" / "lint"
        latest = (out / "latest.md").read_text()
        self.assertEqual((out / f"{self.today}.log").read_text(), latest)
        self.assertIn(f"# Weekly vault lint — {self.today}", latest)
        self.assertIn("Findings: **3**", latest)
        self.assertIn("## gt_lint.py", latest)
        self.assertIn("[broken-link] Knowledge/Lonely.md", latest)
        lines = self.inbox_lines(v)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"- [ ] Weekly vault lint {self.today}: **3 findings**"))
        self.assertIn("[project:: golden-thread]", lines[0])
        self.assertIn("inbox line appended", self.log.read_text())
        # same day again: report refreshed, no second inbox line
        self.run_weekly()
        self.assertEqual(len(self.inbox_lines(v)), 1)

    def test_clean_vault_writes_report_but_no_inbox_line(self):
        v = self.wired_vault()
        before = (v / "INBOX.md").read_text()
        self.run_weekly()
        self.assertIn("Findings: **0**", (v / "Projects/golden-thread/lint/latest.md").read_text())
        self.assertEqual((v / "INBOX.md").read_text(), before)

    def test_report_does_not_report_itself(self):
        """latest.md quotes finding text (including broken [[links]]); the next run
        must not count the report's own contents as new findings."""
        v = self.wired_vault()
        self.seed_findings(v)
        self.run_weekly()
        first = (v / "Projects/golden-thread/lint/latest.md").read_text()
        self.run_weekly()
        second = (v / "Projects/golden-thread/lint/latest.md").read_text()
        self.assertIn("Findings: **3**", first)
        self.assertIn("Findings: **3**", second, "second run counted the first report")

    def test_live_session_claim_on_inbox_is_respected(self):
        v = self.wired_vault()
        self.seed_findings(v)
        sessions = v / "Projects" / "golden-thread" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "2026-09-11_abc.md").write_text("claims:\n- INBOX.md\n")
        before = (v / "INBOX.md").read_text()
        self.run_weekly()
        self.assertEqual((v / "INBOX.md").read_text(), before)
        self.assertTrue((v / "Projects/golden-thread/lint/latest.md").is_file())
        self.assertIn("claimed by a live session", self.log.read_text())

    def test_stale_session_claim_does_not_block(self):
        v = self.wired_vault()
        self.seed_findings(v)
        sessions = v / "Projects" / "golden-thread" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        s = sessions / "2026-09-01_old.md"
        s.write_text("claims:\n- INBOX.md\n")
        old = time.time() - 3 * 86400
        os.utime(s, (old, old))
        self.run_weekly()
        self.assertEqual(len(self.inbox_lines(v)), 1)

    def test_missing_inbox_is_skipped(self):
        v = self.wired_vault()
        self.seed_findings(v)
        (v / "INBOX.md").unlink()
        self.run_weekly()
        self.assertFalse((v / "INBOX.md").exists(), "weekly lint created INBOX.md")
        self.assertIn("no INBOX.md", self.log.read_text())

    def test_wiki_lint_from_sandbox_plugin_cache_is_counted(self):
        v = self.wired_vault()
        wl = (self.home / ".claude" / "plugins" / "cache" / "golden-thread-plugin" / "gt-wiki"
              / "0.1.0" / "scripts" / "wiki_lint.py")
        wl.parent.mkdir(parents=True)
        wl.write_text("print('fake wiki lint\\nFindings: 2')\n")
        self.run_weekly()
        latest = (v / "Projects/golden-thread/lint/latest.md").read_text()
        self.assertIn("## wiki_lint.py", latest)
        self.assertIn("Findings: **2** (wiki-lint 2)", latest)
        self.assertIn("**2 findings** (wiki-lint 2)", self.inbox_lines(v)[0])


if __name__ == "__main__":
    unittest.main()
