"""gt_brief.py -- gt-open's catch-up brief (0.18.0, 2026-09-24-cold-start-session-brief).

Fixture: a project with a research entry, one open p::1 task, one waiting::user task and two
commits to its files in the last 10 days, last opened 10 days ago. Contract:
  * --brief generates one regardless of absence
  * without it, absence over `brief_absence_days` (default 7) generates one automatically
  * <= 150 words; mentions what changed, the newest research entry, the oldest p::1 task and
    the waiting::user task; labelled as a generated summary
  * no commits in the window -> nothing (silently); --no-brief -> nothing
  * the threshold is a gt setting; gt-open runs it BEFORE its reading sequence
"""
import datetime
import json
import os

from _harness import GT, Sandbox, SCRIPTS

BRIEF = SCRIPTS / "gt_brief.py"


class BriefBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.proj = self.vault / "Projects" / "quokka"
        self.proj.mkdir(parents=True)
        (self.proj / "README.md").write_text(
            "# Quokka\n\nGoal.\n\n## Tasks\n\n"
            "- [ ] Wire the kestrel consumer [p:: 1] [since:: 2026-09-20]\n"
            "- [ ] Older urgent wombat migration [p:: 1] [since:: 2026-09-02]\n"
            "- [x] Done thing [p:: 1] [since:: 2026-08-01]\n"
            "- [ ] Decide the broker vendor [p:: 2] [waiting:: user] [since:: 2026-09-25]\n"
            "- [ ] Agent chore [p:: 3] [waiting:: agent]\n")
        (self.proj / "research.md").write_text(
            "# Research\n\n## 2026-08-01: ancient finding\n\nold\n\n"
            "## 2026-09-21: kestrel throughput is 40% higher on batch size 64\n\n"
            "Measured on claudebox over three runs.\n")
        self.config(vault_path=str(self.vault))
        self.git_init(self.vault, commit=False)
        self.commit_at(20, "start the project")

    def commit_at(self, days_ago, msg):
        when = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%S")
        f = self.proj / "log.txt"
        with open(f, "a") as fh:
            fh.write(msg + "\n")
        env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        self.run_cmd(["git", "-C", str(self.vault), "add", "-A"], env=env)
        p = self.run_cmd(["git", "-C", str(self.vault), "commit", "-q", "-m", msg], env=env)
        self.assertOk(p)

    def opened(self, days_ago):
        d = self.home / ".claude" / "golden-thread" / "state"
        d.mkdir(parents=True, exist_ok=True)
        when = datetime.datetime.now().astimezone() - datetime.timedelta(days=days_ago)
        key = "%s::quokka" % os.path.realpath(str(self.vault))
        (d / "last-open.json").write_text(json.dumps({key: when.isoformat(timespec="seconds")}))

    def run_brief(self, *args):
        p = self.py(BRIEF, "--vault", str(self.vault), "--project", "quokka", "--json", *args)
        self.assertOk(p)
        return json.loads(p.stdout)


class TenDayAbsence(BriefBase):
    def setUp(self):
        super().setUp()
        self.opened(10)
        self.commit_at(8, "add kestrel consumer skeleton")
        self.commit_at(3, "record throughput measurements")

    def test_auto_brief_after_absence_has_every_part(self):
        out = self.run_brief()
        b = out["brief"]
        self.assertTrue(b, out["why"])
        self.assertTrue(b.startswith("Generated catch-up"), b)
        self.assertIn("not authoritative", b)
        self.assertIn("2 commit(s)", b)
        self.assertIn("record throughput measurements", b)
        self.assertNotIn("start the project", b, "a commit before the last open leaked in")
        self.assertIn("kestrel throughput is 40% higher", b)
        self.assertIn("Older urgent wombat migration", b, "not the OLDEST open p::1 task")
        self.assertNotIn("Done thing", b)
        self.assertIn("Decide the broker vendor", b)
        self.assertNotIn("Agent chore", b)
        self.assertLessEqual(len(b.split()), 150)

    def test_no_brief_suppresses(self):
        self.assertIsNone(self.run_brief("--no-brief")["brief"])

    def test_plain_output_is_the_paragraph(self):
        p = self.py(BRIEF, "--vault", str(self.vault), "--project", "quokka")
        self.assertOk(p)
        self.assertEqual(1, len(p.stdout.strip().splitlines()))

    def test_mark_resets_the_absence(self):
        self.py(BRIEF, "--vault", str(self.vault), "--project", "quokka", "--mark")
        self.assertIsNone(self.run_brief()["brief"])

    def test_dry_run_never_marks(self):
        self.py(BRIEF, "--vault", str(self.vault), "--project", "quokka", "--mark", "--dry-run")
        self.assertTrue(self.run_brief()["brief"])

    def test_threshold_is_a_setting(self):
        self.config(vault_path=str(self.vault), brief_absence_days="14")
        self.assertIsNone(self.run_brief()["brief"])
        self.config(vault_path=str(self.vault), brief_absence_days="off")
        self.assertIsNone(self.run_brief()["brief"])
        self.assertTrue(self.run_brief("--brief")["brief"], "--brief ignores the setting")


class ShortAbsence(BriefBase):
    def test_recent_open_gives_no_brief_unless_asked(self):
        self.opened(2)
        self.commit_at(1, "small change")
        self.assertIsNone(self.run_brief()["brief"])
        b = self.run_brief("--brief")["brief"]
        self.assertTrue(b)
        self.assertIn("small change", b)


class NothingHappened(BriefBase):
    def test_no_commits_in_the_window_is_silent(self):
        self.opened(10)                       # the only commit is 20 days old
        out = self.run_brief()
        self.assertIsNone(out["brief"])
        self.assertIn("no commits", out["why"])
        p = self.py(BRIEF, "--vault", str(self.vault), "--project", "quokka", "--brief")
        self.assertOk(p)
        self.assertEqual("", p.stdout.strip())

    def test_no_record_uses_the_newest_commit_age(self):
        out = self.run_brief()                 # newest commit 20 days ago, never opened
        self.assertTrue(out["brief"], out["why"])

    def test_word_limit_holds_with_long_fields(self):
        self.opened(10)
        for i in range(6):
            self.commit_at(5, "a very long commit subject " * 6 + str(i))
        b = self.run_brief()["brief"]
        self.assertLessEqual(len(b.split()), 150)


class GtOpenRunsItFirst(BriefBase):
    def test_skill_runs_brief_before_reading(self):
        text = (GT / "skills" / "gt-open" / "SKILL.md").read_text()
        self.assertIn("gt_brief.py", text)
        self.assertLess(text.index("gt_brief.py"), text.index("**Step 4 — Read documents"))
        self.assertIn("--brief", text)
        self.assertIn("generated summary", text)
