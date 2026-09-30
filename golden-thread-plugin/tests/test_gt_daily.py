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

    def test_a_crash_is_could_not_run_never_nothing_to_report(self):
        """Python's uncaught-exception exit is 1 -- this job's "nothing recorded", which
        gt_schedule treats as a normal night. That is how the weekly lint's crashes went
        unseen (2026-09-28), so a crash here exits 3."""
        self.commit("first", {"Projects/alpha/README.md": "# alpha\n"})
        (self.vault / "Daily Notes" / ("%s.md" % DATE)).mkdir()   # unwritable as a file
        proc = self.run_daily(expect=3)
        self.assertIn("COULD NOT RUN", proc.stderr)

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


class ItDoesNotMangleTheTextItQuotes(Sandbox):
    """Two defects found on the FIRST run against real data, both silent.

    A summary that alters what it quotes is worse than one that omits it: `gtdaily.py` is a
    filename someone would grep for and never find, and a subject reduced to one character
    looks like an empty commit rather than a broken summariser.
    """

    def short(self, text):
        from _harness import SCRIPTS, load_module
        return load_module(SCRIPTS / "gt_daily.py", "gt_daily_short").short(text)

    def test_an_underscore_in_a_filename_survives(self):
        """`_` was stripped as markdown emphasis, turning gt_daily.py into gtdaily.py."""
        self.assertIn("gt_daily.py", self.short("Daily capture: gt_daily.py writes the facts"))

    def test_a_commit_subject_containing_bold_is_not_reduced_to_the_bold_bit(self):
        """`BOLD.search` matched anywhere, so a subject with bold in the middle became just
        that fragment -- measured: one subject came out as the single character 's'."""
        out = self.short("Daily capture: writes the day**s** facts and nothing else")
        self.assertIn("Daily capture", out)
        self.assertGreater(len(out), 20, "the subject was replaced by a fragment: %r" % out)

    def test_a_task_title_still_uses_its_leading_bold(self):
        """The behaviour the bold rule exists for: a gt task is `**Title** — explanation`,
        and taking the title is what makes the list scannable."""
        out = self.short("**Reserve the IP** — a long explanation that nobody needs [p:: 2]")
        self.assertEqual(out, "Reserve the IP")

    def test_dataview_fields_are_stripped_from_a_task_title(self):
        self.assertNotIn("p::", self.short("**Do the thing** [p:: 1] [waiting:: user]"))


README = "---\ntype: project\nslug: {slug}\n{domain}stage: active\n---\n\n# {slug}\n\n## Tasks\n\n{tasks}"


def readme(slug, tasks="", domain=None):
    return README.format(slug=slug, tasks=tasks,
                         domain=("domain: %s\n" % domain) if domain else "")


class TheDaysTasksAndProjects(DailyBase):
    """Feature request 2026-09-30-gt-daily-enhancements-tasks-domain-new-project."""

    def seed(self):
        # Yesterday's state: two existing projects, one with a domain and one without.
        self.run_cmd(["git", "-C", str(self.vault), "commit", "-q", "--allow-empty", "-m", "root"],
                     env={"GIT_AUTHOR_DATE": "2026-09-26T10:00:00-05:00",
                          "GIT_COMMITTER_DATE": "2026-09-26T10:00:00-05:00"})
        for rel, text in {
                "Projects/alpha/README.md": readme("alpha", "- [ ] **Old task** [p:: 2]\n",
                                                   domain="orchard"),
                "Projects/beta/README.md": readme("beta", "- [ ] **Edited task** first words\n")}.items():
            p = self.vault / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git("add", "-A")
        self.run_cmd(["git", "-C", str(self.vault), "commit", "-q", "-m", "seed"],
                     env={"GIT_AUTHOR_DATE": "2026-09-26T11:00:00-05:00",
                          "GIT_COMMITTER_DATE": "2026-09-26T11:00:00-05:00"})

    def test_new_tasks_are_listed_and_edits_are_not(self):
        self.seed()
        self.commit("add and edit tasks", {
            "Projects/alpha/README.md": readme(
                "alpha", "- [ ] **Old task** [p:: 2]\n- [ ] **Brand new task** [p:: 1]\n",
                domain="orchard"),
            "Projects/beta/README.md": readme("beta", "- [ ] **Edited task** other words\n")})
        out = self.run_daily("--dry-run", expect=0).stdout
        self.assertIn("**Tasks added**", out)
        added = out.split("**Tasks added**", 1)[1].split("**Commits**", 1)[0]
        self.assertIn("Brand new task", added)
        self.assertNotIn("Edited task", added, "an edited task was counted as new")
        self.assertNotIn("[p:: 1]", added)
        self.assertNotIn("Brand new task", out.split("**Tasks added**", 1)[0],
                         "a new task also appeared as closed")
        self.assertIn("1 task(s) added", out)

    def test_due_today_lists_only_open_tasks_due_today(self):
        self.seed()
        self.commit("dues", {"Projects/alpha/README.md": readme(
            "alpha", "- [ ] **Old task** [p:: 2]\n"
                     "- [ ] **Pay the invoice** [due:: %s]\n"
                     "- [x] **Already sent** [due:: %s]\n"
                     "- [ ] **Next week** [due:: 2026-10-04]\n" % (DATE, DATE),
            domain="orchard")})
        out = self.run_daily("--dry-run", expect=0).stdout
        due = out.split("**Due today (open)**", 1)[1].split("**Commits**", 1)[0]
        self.assertIn("Pay the invoice", due)
        self.assertNotIn("Already sent", due)
        self.assertNotIn("Next week", due)

    def test_empty_sections_are_left_out(self):
        self.seed()
        self.commit("just a note", {"notes.md": "hello\n"})
        out = self.run_daily("--dry-run", expect=0).stdout
        for heading in ("**Tasks added**", "**Due today (open)**", "**Tasks closed**",
                        "**New projects**"):
            self.assertNotIn(heading, out)

    def test_sections_are_grouped_by_domain_with_uncategorized_last(self):
        self.seed()
        self.commit("work in both", {
            "Projects/alpha/README.md": readme(
                "alpha", "- [ ] **Old task** [p:: 2]\n- [ ] **Alpha new**\n", domain="orchard"),
            "Projects/beta/README.md": readme(
                "beta", "- [ ] **Edited task** first words\n- [ ] **Beta new**\n")})
        out = self.run_daily("--dry-run", expect=0).stdout
        added = out.split("**Tasks added**", 1)[1].split("**Commits**", 1)[0]
        self.assertLess(added.index("### orchard"), added.index("Alpha new"))
        self.assertLess(added.index("### uncategorized"), added.index("Beta new"))
        self.assertLess(added.index("### orchard"), added.index("### uncategorized"))
        commits = out.split("**Commits**", 1)[1]
        self.assertIn("### orchard", commits)
        self.assertIn("- **alpha** (1)", commits, "a vault commit is filed under its project")

    def test_a_project_created_today_is_flagged(self):
        self.seed()
        self.commit("new project", {"Projects/gamma/README.md": readme("gamma")})
        out = self.run_daily("--dry-run", expect=0).stdout
        self.assertIn("**New projects** — gamma", out)
        self.assertLess(out.index("**New projects**"), out.index("**Commits**"))

    def test_no_new_projects_line_when_none_was_created(self):
        self.seed()
        self.commit("edit", {"Projects/alpha/README.md": readme("alpha", "- [ ] **X**\n",
                                                                domain="orchard")})
        self.assertNotIn("New projects", self.run_daily("--dry-run", expect=0).stdout)


class ThePushComesFirst(DailyBase):
    def remote(self, reachable=True):
        bare = self.tmp / "remote.git"
        if reachable:
            self.run_cmd(["git", "init", "-q", "--bare", str(bare)])
        self.git("remote", "add", "origin", str(bare))
        return bare

    def pushed_heads(self, bare):
        p = self.run_cmd(["git", "-C", str(bare), "for-each-ref", "refs/heads"])
        return p.stdout.strip()

    def test_a_successful_push_adds_no_note_and_reaches_the_remote(self):
        bare = self.remote()
        self.commit("did a thing", {"notes.md": "x\n"})
        self.git("push", "-q", "-u", "origin", "main")
        self.commit("did another", {"notes.md": "y\n"})
        self.run_daily(expect=0)
        self.assertNotIn("push failed", self.note())
        self.assertNotIn("no git remote", self.note())
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertIn(head, self.pushed_heads(bare), "the push did not happen before the write")

    def test_a_failed_push_is_noted_and_the_note_is_still_written(self):
        self.remote(reachable=False)
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        body = self.note()
        self.assertIn("gt_daily:begin", body, "the write was abandoned after a failed push")
        self.assertIn("> NOTE vault push failed before write", body)

    def test_no_remote_is_noted_not_fatal(self):
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        self.assertIn("no git remote", self.note())

    def test_dry_run_never_pushes(self):
        bare = self.remote()
        self.commit("did a thing", {"notes.md": "x\n"})
        out = self.run_daily("--dry-run", expect=0).stdout
        self.assertEqual(self.pushed_heads(bare), "", "--dry-run pushed")
        self.assertNotIn("push failed", out)
        self.assertEqual(self.note(), "")

    def test_check_reports_an_unreachable_remote(self):
        self.remote(reachable=False)
        self.commit("did a thing", {"notes.md": "x\n"})
        proc = self.run_daily("--check", expect=3)
        self.assertIn("git push is not possible", proc.stderr)
        self.assertEqual(self.note(), "")

    def test_check_passes_with_a_reachable_remote(self):
        self.remote()
        self.commit("did a thing", {"notes.md": "x\n"})
        self.git("push", "-q", "-u", "origin", "main")
        proc = self.run_daily("--check", expect=0)
        self.assertIn("vault push possible", proc.stdout)


class OnlyReadmeTasksCount(DailyBase):
    def test_a_handoff_checklist_is_not_a_task(self):
        self.commit("handoff", {
            "Projects/alpha/handoff/2026-09-27-handoff.md":
                "## What the next session must not assume\n\n- [ ] Do the tests pass right now?\n",
            "Projects/alpha/README.md": "# alpha\n\n## Tasks\n\n- [ ] **A real task**\n"})
        self.commit("tick the checklist", {
            "Projects/alpha/handoff/2026-09-27-handoff.md":
                "## What the next session must not assume\n\n- [x] Do the tests pass right now?\n"})
        out = self.run_daily("--dry-run", expect=0).stdout
        self.assertIn("A real task", out)
        self.assertNotIn("Do the tests pass", out, "a handoff checklist line was read as a task")


class NamedSections(DailyBase):
    """Owner design 2026-09-30 (supersedes the comms request's fixed none/m365/joule setting):
    named sections other tools fill, a handoff that says where the note is, and a note that
    always lives in Daily Notes/."""

    def handoff(self):
        p = self.vault / "Daily Notes" / ".handoff" / ("%s.md" % DATE)
        return p.read_text() if p.is_file() else ""

    def add(self, name, *extra):
        return self.py(DAILY, "--vault", self.vault, "--section-add", name, *extra)

    def fill(self, name, content):
        note = self.vault / "Daily Notes" / ("%s.md" % DATE)
        text = note.read_text()
        b = "<!-- gt_daily:section:%s:begin" % name
        head, rest = text.split(b, 1)
        marker_end = rest.index("-->") + 3
        tail = rest[rest.index("<!-- gt_daily:section:%s:end -->" % name):]
        note.write_text(head + b + rest[:marker_end] + "\n## Comms\n\n" + content + "\n" + tail)

    def test_no_sections_means_output_identical_to_before(self):
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        self.assertNotIn("gt_daily:section:", self.note())
        self.assertFalse((self.vault / "Daily Notes" / ".handoff").exists())

    def test_a_named_section_gets_room_in_the_note_and_a_handoff(self):
        self.assertOk(self.add("comms", "--title", "Comms",
                               "--instructions", "counts only, never subjects"))
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        note = self.note()
        self.assertIn("<!-- gt_daily:section:comms:begin", note)
        self.assertIn("<!-- gt_daily:section:comms:end -->", note)
        self.assertIn("_Nothing handed off yet._", note)
        self.assertLess(note.index("gt_daily:end"), note.index("gt_daily:section:comms:begin"),
                        "the section should follow the facts block")
        h = self.handoff()
        self.assertIn("note: Daily Notes/%s.md" % DATE, h)
        self.assertIn("name: comms", h)
        self.assertIn("status: waiting", h)
        self.assertIn("counts only, never subjects", h)

    def test_a_tools_content_survives_every_rerun_and_is_marked_filled(self):
        self.add("comms")
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        self.fill("comms", "- 12 emails received, 9 handled")
        self.commit("more", {"notes.md": "y\n"})
        self.run_daily(expect=0)
        self.run_daily(expect=0)
        note = self.note()
        self.assertIn("- 12 emails received, 9 handled", note)
        self.assertEqual(note.count("gt_daily:section:comms:begin"), 1)
        self.assertEqual(note.count("gt_daily:begin"), 1)
        self.assertIn("status: filled", self.handoff())

    def test_sections_only_makes_room_on_a_quiet_day(self):
        self.add("comms")
        p = self.run_daily("--sections-only", expect=0)
        self.assertIn("waiting: comms", p.stdout)
        self.assertIn("gt_daily:section:comms:begin", self.note())
        self.assertNotIn("gt_daily:begin", self.note(), "no facts block was asked for")
        self.assertIn("status: waiting", self.handoff())

    def test_sections_only_without_sections_writes_nothing(self):
        self.run_daily("--sections-only", expect=1)
        self.assertEqual(self.note(), "")

    def test_dry_run_shows_sections_and_writes_nothing(self):
        self.add("comms")
        self.commit("did a thing", {"notes.md": "x\n"})
        out = self.run_daily("--dry-run", expect=0).stdout
        self.assertIn("gt_daily:section:comms:begin", out)
        self.assertIn("--- handoff:", out)
        self.assertEqual(self.note(), "")
        self.assertEqual(self.handoff(), "")

    def test_credential_shaped_section_content_is_flagged_not_quoted(self):
        self.add("comms")
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        fake = "Qm7tR2vX" + "9pL4sK8w" + "N3yB6dF1" + "hJ5cA0eZ"
        self.fill("comms", 'api_key = "%s"' % fake)
        self.commit("more", {"notes.md": "y\n"})
        p = self.run_daily(expect=0)
        note = self.note()
        self.assertIn("credential-shaped text", note)
        self.assertEqual(note.count(fake), 1, "the value was repeated outside its section")
        self.assertNotIn(fake, p.stdout + p.stderr)

    def test_names_are_validated_and_sections_can_be_removed(self):
        p = self.add("Bad Name!")
        self.assertEqual(p.returncode, 2)
        self.assertOk(self.add("comms"))
        self.assertOk(self.add("calendar", "--title", "Calendar"))
        out = self.py(DAILY, "--vault", self.vault, "--sections-list").stdout
        self.assertIn("comms", out)
        self.assertIn("calendar", out)
        self.assertOk(self.py(DAILY, "--vault", self.vault, "--section-remove", "comms"))
        out = self.py(DAILY, "--vault", self.vault, "--sections-list").stdout
        self.assertNotIn("comms", out)
        self.assertEqual(self.py(DAILY, "--vault", self.vault, "--section-remove",
                                 "comms").returncode, 2)

    def test_check_reports_each_sections_state(self):
        self.add("comms")
        p = self.run_daily("--check", expect=0)
        self.assertIn("section comms — not in today's note yet", p.stdout)


class CommsContentIsOffByDefault(NamedSections):
    """Owner, 2026-09-30: an employer may not want mail or Teams content in anything Claude can
    read. One user works from several machines on ONE shared vault, so the policy lives in the
    vault (off by default) and a machine can only force it off, never on."""

    def policy(self, value):
        self.assertOk(self.py(DAILY, "--vault", self.vault, "--comms-content", value))

    def machine(self, value):
        (self.home / ".claude" / "vault-config.json").write_text(
            json.dumps({"daily_comms_content": value}))

    def test_off_by_default_the_comms_section_is_held_back(self):
        p = self.add("mail", "--comms", "--title", "Mail")
        self.assertIn("comms content is OFF", p.stdout)
        self.add("builds")
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        note, h = self.note(), self.handoff()
        self.assertNotIn("gt_daily:section:mail:", note, "a comms section was placed while off")
        self.assertIn("gt_daily:section:builds:begin", note)
        self.assertNotIn("name: mail", h, "a comms section was offered to other tools while off")
        self.assertIn("comms_content: off", h)
        self.assertIn("Email and Teams content is OFF", h)

    def test_sections_and_policy_live_in_the_shared_vault(self):
        self.add("mail", "--comms")
        self.policy("on")
        d = json.loads((self.vault / ".gt" / "daily-sections.json").read_text())
        self.assertEqual(d["comms_content"], "on")
        self.assertEqual([s["name"] for s in d["sections"]], ["mail"])

    def test_on_in_the_vault_places_the_comms_section(self):
        self.policy("on")
        self.add("mail", "--comms")
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        self.assertIn("gt_daily:section:mail:begin", self.note())
        h = self.handoff()
        self.assertIn("name: mail", h)
        self.assertIn("comms_content: on", h)
        self.assertNotIn("content is OFF", h)

    def test_a_machine_can_force_it_off_but_never_on(self):
        self.policy("on")
        self.machine("off")
        self.add("mail", "--comms")
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        self.assertNotIn("gt_daily:section:mail:", self.note(), "the machine override was ignored")
        self.assertIn("forced off on this machine", self.run_daily("--check", expect=0).stdout)
        self.policy("off")
        self.machine("on")          # not a recognised value: a machine cannot loosen the vault
        self.run_daily(expect=0)
        self.assertNotIn("gt_daily:section:mail:", self.note())

    def test_no_open_sections_means_no_handoff_and_a_stale_one_is_removed(self):
        self.policy("on")
        self.add("mail", "--comms")
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        self.assertIn("name: mail", self.handoff())
        self.policy("off")
        self.commit("more", {"notes.md": "y\n"})
        self.run_daily(expect=0)
        self.assertEqual(self.handoff(), "", "a handoff still invites mail after comms went off")

    def test_content_left_in_a_comms_section_after_turning_off_is_flagged_not_deleted(self):
        self.policy("on")
        self.add("mail", "--comms")
        self.commit("did a thing", {"notes.md": "x\n"})
        self.run_daily(expect=0)
        self.fill("mail", "- 4 threads answered")
        self.policy("off")
        self.commit("more", {"notes.md": "y\n"})
        self.run_daily(expect=0)
        note = self.note()
        self.assertIn("- 4 threads answered", note, "gt_daily deleted another tool's writing")
        self.assertIn("holds content although comms content is off", note)

    def test_check_states_the_policy(self):
        self.assertIn("is off — off in the vault", self.run_daily("--check", expect=0).stdout)
        self.policy("on")
        self.assertIn("is ON — on in the vault", self.run_daily("--check", expect=0).stdout)

    def test_the_machine_setting_is_registered_follow_by_default(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("gts", str(SCRIPTS / "gt_settings.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        s = m.SETTINGS["daily_comms_content"]
        self.assertEqual((s["default"], s["values"]), ("follow", ["follow", "off"]))
