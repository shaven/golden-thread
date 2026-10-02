"""log_knowledge_read.sh / .py (PostToolUse on Read) and gt-optimize's knowledge-unused finding.

Request 2026-09-24-query-access-logging, with the evaluator's correction: the log is written by
a hook, not by skill prose.

Contract:
  * A Read of <vault>/Knowledge/**.md appends {page, session, date, trigger} to
    <vault>/usage/knowledge.jsonl. Nothing else is logged: other tools, other folders, other
    vaults.
  * usage/ is git-ignored by its own usage/.gitignore.
  * The hook never fails or prints: malformed input, an unwritable log -> exit 0, silent.
  * knowledge_access_log=off logs nothing.
  * gt_optimize knowledge-unused lists pages with no read in 90 days, "never read" first; read
    pages are not listed; with no log at all, nothing is reported.
  * The hook is registered the way every hook is: HOOK_REGISTRATIONS, PostToolUse, matcher Read.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import unittest

from _harness import Sandbox, HOOKS, SCRIPTS

OPT = SCRIPTS / "gt_optimize.py"


class AccessLogTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        for name in ("log_knowledge_read.sh", "log_knowledge_read.py"):
            shutil.copy2(HOOKS / name, self.hooks / name)
        for name in ("gt_paths.py", "gt_settings.py", "gt_components.py"):
            shutil.copy2(SCRIPTS / name, self.hooks / name)
        self.vault = self.tmp / "vault"
        (self.vault / "Knowledge" / "sub").mkdir(parents=True)
        (self.vault / "Projects" / "alpha").mkdir(parents=True)
        for n in range(1, 6):
            (self.vault / "Knowledge" / ("Page %d.md" % n)).write_text("# P\n", encoding="utf-8")
        self.config(vault_path=str(self.vault))
        self.log = self.vault / "usage" / "knowledge.jsonl"

    def hook(self, payload, raw=None):
        r = self.sh(self.hooks / "log_knowledge_read.sh",
                    input=raw if raw is not None else json.dumps(payload))
        self.assertOk(r, "the hook must always exit 0")
        self.assertEqual(r.stdout, "", "the hook must print nothing")
        return r

    def read(self, path, tool="Read", sid="1234567890abcdef"):
        return self.hook({"session_id": sid, "hook_event_name": "PostToolUse",
                          "tool_name": tool, "tool_input": {"file_path": str(path)},
                          "cwd": str(self.tmp)})

    def rows(self):
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]

    def test_a_knowledge_read_is_logged_with_all_four_fields(self):
        self.read(self.vault / "Knowledge" / "Page 1.md")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["page"], "Knowledge/Page 1.md")
        self.assertEqual(r["session"], "12345678", "session id is cut to 8 characters")
        self.assertEqual(r["date"], datetime.date.today().isoformat())
        self.assertEqual(r["trigger"], "read")

    def test_three_reads_three_lines(self):
        for n in (1, 2, 3):
            self.read(self.vault / "Knowledge" / ("Page %d.md" % n))
        self.assertEqual(len(self.rows()), 3)

    def test_nothing_outside_knowledge_and_no_other_tool_is_logged(self):
        (self.vault / "Projects" / "alpha" / "README.md").write_text("x\n", encoding="utf-8")
        self.read(self.vault / "Projects" / "alpha" / "README.md")
        self.read(self.vault / "Knowledge" / "Page 1.md", tool="Write")
        self.read(self.tmp / "elsewhere" / "Knowledge" / "x.md")
        self.assertEqual(self.rows(), [])

    def test_the_log_folder_is_git_ignored(self):
        self.read(self.vault / "Knowledge" / "Page 1.md")
        ign = self.vault / "usage" / ".gitignore"
        self.assertTrue(ign.is_file())
        self.assertIn("*", ign.read_text(encoding="utf-8").split("\n"))
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q", str(self.vault)], check=True,
                           env=self.env)
            st = subprocess.run(["git", "-C", str(self.vault), "status", "--porcelain",
                                 "--untracked-files=all"], capture_output=True, text=True,
                                env=self.env)
            self.assertNotIn("usage/", st.stdout)

    def test_off_logs_nothing(self):
        self.config(vault_path=str(self.vault), knowledge_access_log="off")
        self.read(self.vault / "Knowledge" / "Page 1.md")
        self.assertEqual(self.rows(), [])

    def test_failures_are_silent_and_never_block(self):
        self.hook(None, raw="not json")
        self.hook(None, raw="")
        (self.vault / "usage").mkdir()
        os.chmod(self.vault / "usage", 0o500)              # the log cannot be written
        try:
            self.read(self.vault / "Knowledge" / "Page 1.md")
        finally:
            os.chmod(self.vault / "usage", 0o700)

    def test_registered_as_a_posttooluse_read_hook(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            import importlib
            comp = importlib.import_module("gt_components")
            regs = [r for r in comp.HOOK_REGISTRATIONS if r["script"] == "log_knowledge_read.sh"]
        finally:
            sys.path.remove(str(SCRIPTS))
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0]["event"], "PostToolUse")
        self.assertEqual(regs[0]["matcher"], "Read")
        self.assertEqual(regs[0]["owner"], "install.sh")

    def test_setting_is_registered(self):
        r = self.py(SCRIPTS / "gt_settings.py", "show")
        self.assertIn("knowledge_access_log", r.stdout)


class KnowledgeUnusedTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.vault = self.tmp / "vault"
        (self.vault / "Knowledge").mkdir(parents=True)
        for n in range(1, 6):
            (self.vault / "Knowledge" / ("Page %d.md" % n)).write_text("# P\n", encoding="utf-8")
        (self.vault / "Knowledge" / "_template.md").write_text("# T\n", encoding="utf-8")

    def logged(self, *pairs):
        d = self.vault / "usage"
        d.mkdir(exist_ok=True)
        with open(d / "knowledge.jsonl", "a", encoding="utf-8") as fh:
            for page, days_ago in pairs:
                day = datetime.date.today() - datetime.timedelta(days=days_ago)
                fh.write(json.dumps({"page": "Knowledge/%s.md" % page, "session": "abcd1234",
                                     "date": day.isoformat(), "trigger": "read"}) + "\n")

    def unused(self, *extra):
        r = self.py(OPT, "--vault", str(self.vault), "--only", "vault", "--json", *extra)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        return [f for f in json.loads(r.stdout)["findings"] if f["kind"] == "knowledge-unused"]

    def test_unread_pages_are_listed_and_read_ones_are_not(self):
        self.logged(("Page 1", 1), ("Page 2", 3), ("Page 3", 10))
        got = self.unused()
        self.assertEqual(sorted(f["path"] for f in got),
                         ["Knowledge/Page 4.md", "Knowledge/Page 5.md"])
        for f in got:
            self.assertIn("never read", f["message"])
            self.assertEqual(f["class"], "judgement")

    def test_a_page_last_read_91_days_ago_is_review_due(self):
        self.logged(("Page 1", 1), ("Page 2", 1), ("Page 3", 1), ("Page 4", 1),
                    ("Page 5", 91))
        got = self.unused()
        self.assertEqual([f["path"] for f in got], ["Knowledge/Page 5.md"])
        self.assertIn("91 days ago", got[0]["message"])

    def test_never_read_ranks_before_oldest_read(self):
        self.logged(("Page 1", 1), ("Page 2", 1), ("Page 3", 100), ("Page 4", 200))
        self.assertEqual([f["path"] for f in self.unused()],
                         ["Knowledge/Page 5.md", "Knowledge/Page 4.md", "Knowledge/Page 3.md"])

    def test_no_log_means_no_finding(self):
        self.assertEqual(self.unused(), [])

    def test_the_report_never_writes(self):
        self.logged(("Page 1", 1))
        before = (self.vault / "usage" / "knowledge.jsonl").read_bytes()
        self.unused()
        self.assertEqual((self.vault / "usage" / "knowledge.jsonl").read_bytes(), before)
        self.assertTrue((self.vault / "Knowledge" / "Page 5.md").exists())


if __name__ == "__main__":
    unittest.main()
