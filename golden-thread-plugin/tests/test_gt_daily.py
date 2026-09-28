"""gt_daily.py — the day's facts in the daily note.

The load-bearing contracts are about RESTRAINT, not about gathering:

  * it writes only inside its generated block and never touches the owner's prose, and
    specifically never `## Noticed`, which is their unfiled capture surface;
  * a re-run REPLACES the block rather than appending a second one;
  * wiki activity is a COUNT, never page names (the owner asked for "not details ... just an
    overview of the number of items");
  * the active span is labelled as a span and never as time worked, because gt tracks no
    timers and a number presented as effort would be invented;
  * GIT is the primary source. Measured 2026-09-27: events.jsonl held 5 events on a day with
    10 commits across three work streams, so a summary built on events alone reports a busy
    day as almost empty.
"""
import json
import subprocess
import unittest

from _harness import Sandbox, SCRIPTS

DAILY = SCRIPTS / "gt_daily.py"
DATE = "2026-09-27"


class DailyBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        (self.vault / "Daily Notes").mkdir(parents=True)
        (self.vault / "Templates").mkdir(parents=True)
        (self.vault / "Knowledge").mkdir(parents=True)
        (self.vault / "Projects" / "golden-thread").mkdir(parents=True)
        (self.vault / "Templates" / "Daily Note.md").write_text(
            "# {{date:YYYY-MM-DD}} ({{date:dddd}})\n\n## Did\n\n-\n\n## Noticed\n\n- [ ]\n")
        self.git("init", "-q", "-b", "main", ".")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")

    def git(self, *args):
        return self.run_cmd(["git", "-C", str(self.vault), *args])

    def commit(self, message, files):
        for rel, text in files.items():
            p = self.vault / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git("add", "-A")
        # A fixed date so the run is not a race against midnight.
        self.run_cmd(["git", "-C", str(self.vault), "commit", "-q", "-m", message],
                     env={"GIT_AUTHOR_DATE": DATE + "T10:00:00-05:00",
                          "GIT_COMMITTER_DATE": DATE + "T10:00:00-05:00"})

    def events(self, rows):
        path = self.vault / "Projects" / "golden-thread" / "events.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def run_daily(self, *extra, expect=None):
        proc = self.py(DAILY, "--vault", self.vault, "--date", DATE, *extra)
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             "exit %d\n%s\n%s" % (proc.returncode, proc.stdout, proc.stderr))
        return proc

    def note(self):
        p = self.vault / "Daily Notes" / ("%s.md" % DATE)
        return p.read_text() if p.is_file() else ""


class ItWritesOnlyItsOwnBlock(DailyBase):
    def test_the_owners_prose_survives_a_run(self):
        self.commit("first", {"Projects/alpha/README.md": "# alpha\n"})
        note = self.vault / "Daily Notes" / ("%s.md" % DATE)
        note.write_text("# 2026-09-27 (Sunday)\n\n## Did\n\n- I thought about oven timers\n\n"
                        "## Noticed\n\n- [ ] something unfiled\n")
        self.run_daily(expect=0)
        body = self.note()
        self.assertIn("I thought about oven timers", body, "the owner's prose was lost")
        self.assertIn("- [ ] something unfiled", body, "an unfiled item was lost")
        self.assertIn("gt_daily:begin", body)

    def test_a_second_run_replaces_the_block_rather_than_adding_one(self):
        self.commit("first", {"Projects/alpha/README.md": "# alpha\n"})
        self.run_daily(expect=0)
        self.run_daily(expect=0)
        self.assertEqual(self.note().count("gt_daily:begin"), 1,
                         "the generated block was duplicated")

    def test_it_never_writes_into_the_owners_headings(self):
        """`## Did` is theirs. A generator writing there makes its counts and their sentences
        indistinguishable."""
        self.commit("first", {"Projects/alpha/README.md": "# alpha\n"})
        self.run_daily(expect=0)
        body = self.note()
        did = body.split("## Did", 1)[1].split("##", 1)[0]
        self.assertNotIn("commit(s)", did, "the generator wrote into `## Did`")

    def test_it_creates_the_note_from_the_template_when_absent(self):
        self.commit("first", {"Projects/alpha/README.md": "# alpha\n"})
        self.run_daily(expect=0)
        body = self.note()
        self.assertIn("## Noticed", body, "the template was not used")
        self.assertNotIn("{{date:", body, "template placeholders were left unfilled")


class GitIsThePrimarySource(DailyBase):
    def test_commits_are_reported_with_no_events_at_all(self):
        """The 2026-09-27 case: a busy day whose event log knew almost nothing about it."""
        self.commit("did a thing", {"Projects/alpha/README.md": "# alpha\n"})
        out = self.run_daily("--dry-run", expect=0).stdout
        self.assertIn("did a thing", out)
        self.assertIn("1 commit(s)", out)

    def test_a_closed_task_is_found_from_the_diff(self):
        """task.done events come from gt_tasks.py, which may not have run for days. A closed
        checkbox in a commit is a fact that does not depend on a tool having been run."""
        self.commit("open it", {
            "Projects/alpha/README.md": "# alpha\n\n## Tasks\n- [ ] **Reserve the IP** [p:: 2]\n"})
        self.commit("close it", {
            "Projects/alpha/README.md": "# alpha\n\n## Tasks\n- [x] **Reserve the IP** [p:: 2]\n"})
        out = self.run_daily("--dry-run", expect=0).stdout
        self.assertIn("Tasks closed", out)
        self.assertIn("Reserve the IP", out)
        self.assertIn("alpha", out)
        self.assertNotIn("[p:: 2]", out, "the task's dataview fields were not stripped")

    def test_a_day_with_nothing_recorded_exits_one_and_writes_nothing(self):
        """"Nothing happened" and "I could not tell" are different answers."""
        proc = self.py(DAILY, "--vault", self.vault, "--date", "2020-01-01")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("nothing recorded", proc.stdout)


class WhatItDeliberatelyDoesNotSay(DailyBase):
    def test_wiki_activity_is_a_count_and_never_a_page_name(self):
        self.commit("wiki work", {
            "Knowledge/Some Secret Internal Page.md": "# page\n",
            "Knowledge/Another One.md": "# page\n"})
        out = self.run_daily("--dry-run", expect=0).stdout
        self.assertIn("2 wiki page(s) touched", out)
        self.assertNotIn("Some Secret Internal Page", out,
                         "a wiki page name reached the summary; the owner asked for counts")
        self.assertNotIn("Another One", out)

    def test_the_span_is_never_called_time_worked(self):
        """gt tracks no timers. Presenting elapsed wall-clock as effort invents a number."""
        self.commit("a", {"Projects/alpha/README.md": "# a\n"})
        self.events([
            {"ts": DATE + "T09:00:00-05:00", "kind": "capture", "project": "alpha"},
            {"ts": DATE + "T17:30:00-05:00", "kind": "capture", "project": "alpha"}])
        out = self.run_daily("--dry-run", expect=0).stdout
        self.assertIn("NOT time worked", out)
        self.assertIn("8h30m", out)


class ItHonoursClaimsAndChecksProperly(DailyBase):
    def test_a_claimed_note_is_not_written(self):
        """Core rule 1."""
        sessions = self.vault / "Projects" / "golden-thread" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "deadbeef-1111-2222-3333-444455556666_2026-09-27_1100.md").write_text(
            "status: active\nfiles_claimed:\n- `Daily Notes/%s.md`\n" % DATE)
        self.commit("a", {"Projects/alpha/README.md": "# a\n"})
        proc = self.run_daily(expect=3)
        self.assertIn("claimed", proc.stderr)
        self.assertEqual(self.note(), "", "wrote over a live session's claim")

    def test_check_lists_directories_rather_than_testing_existence(self):
        """The documented failure on this Mac is "reached it and found nothing": a scheduled
        job can stat ~/.claude/projects and get True, then list nothing. An existence check
        cannot see that, so --check must LIST."""
        src = DAILY.read_text()
        self.assertIn("os.listdir", src)
        block = src[src.index("def check("):src.index("def main(")]
        self.assertIn("listdir", block, "--check does not list anything")

    def test_check_passes_on_a_healthy_vault(self):
        proc = self.py(DAILY, "--vault", self.vault, "--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("check passed", proc.stdout)

    def test_check_fails_loudly_when_daily_notes_is_missing(self):
        import shutil
        shutil.rmtree(self.vault / "Daily Notes")
        proc = self.py(DAILY, "--vault", self.vault, "--check")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("CANNOT RUN", proc.stderr)

    def test_an_unreadable_repo_is_reported_as_MISSING_not_omitted(self):
        """A repo that could not be read must not silently drop out of the counts."""
        self.commit("a", {"Projects/alpha/README.md": "# a\n"})
        out = self.run_daily("--repo", str(self.tmp / "not-a-repo"), "--dry-run", expect=0).stdout
        self.assertIn("MISSING", out,
                      "an unreadable repo vanished from the summary without a word")


if __name__ == "__main__":
    unittest.main()
