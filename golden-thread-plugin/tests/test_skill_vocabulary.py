"""Verb-first skill vocabulary (0.18.1): gt-create / gt-open / gt-handle / gt-list / gt-close, the
coding loop gt-plan / gt-implement, and gt-learn folded into gt-work.

Requests 2026-10-01-gt-verb-first-vocabulary, -gt-close-project and -gt-plan-implement-learn, with
the owner's decisions of 2026-10-01. A skill is instructions to a model, so "produces identical
output" is asserted the only way a file can carry it: the old command's procedure -- every command
it told the model to run -- is present, verbatim, in the section of the new skill the deprecated
alias now follows, and the alias points at a section that exists. The previous release's skills
are the baseline, so a later edit that drops a command from the new section fails here.
"""
import re
import unittest

from _harness import GT, REPO, SCRIPTS, load_module, _version_key

SKILLS = GT / "skills"


def previous_release():
    """The newest gt release directory older than the one under test (the alias baseline)."""
    root = REPO / "golden-thread"
    older = [d for d in root.iterdir() if d.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+", d.name)
             and _version_key(d.name) < _version_key(GT.name)
             and (d / "skills").is_dir()]
    return max(older, key=lambda d: _version_key(d.name)) if older else None


PREV = previous_release()
LINT = load_module(SCRIPTS / "skill_lint.py", "skill_lint_for_vocabulary")

# old skill -> (new invocation, skill now holding the procedure, its section)
ALIASES = {
    "gt-task": ("/gt:gt-create task", "gt-create", "Task"),
    "gt-handoff": ("/gt:gt-create handoff", "gt-create", "Handoff"),
    "gt-handoff-handle": ("/gt:gt-handle handoff", "gt-handle", "Handoffs"),
    "gt-task-handle": ("/gt:gt-handle task", "gt-handle", "Tasks"),
    "gt-handoff-list": ("/gt:gt-list handoffs", "gt-list", "Handoffs"),
    "gt-task-list": ("/gt:gt-list tasks", "gt-list", "Tasks"),
}


def text(skill, root=SKILLS):
    return (root / skill / "SKILL.md").read_text(encoding="utf-8")


def description(skill, root=SKILLS):
    return LINT.read_description(text(skill, root)[:4000])


def section(body, heading):
    """The `## heading` section, up to the next `## ` outside a code fence."""
    out, inside, fence = [], False, False
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and re.match(r"^## ", line):
            if inside:
                break
            inside = line.strip() == "## " + heading
            continue
        if inside:
            out.append(line)
    return "\n".join(out) if out else None


def fenced_commands(body):
    """Each line of every fenced block, continuations joined, whitespace-normalised."""
    cmds, fence, pending = [], False, ""
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence, pending = not fence, ""
            continue
        if fence:
            s = line.strip()
            if s.endswith("\\"):
                pending += s[:-1] + " "
                continue
            s = " ".join((pending + s).split())
            pending = ""
            if s.startswith("python3"):
                cmds.append(s)
    return cmds


def norm(body):
    return " ".join(body.replace("\\\n", " ").split())


class NewSkillsShip(unittest.TestCase):
    def test_the_new_verbs_exist(self):
        for s in ("gt-create", "gt-open", "gt-handle", "gt-list", "gt-close", "gt-plan",
                  "gt-implement"):
            self.assertTrue((SKILLS / s / "SKILL.md").is_file(), s)

    def test_gt_learn_is_folded_into_gt_work_not_shipped(self):
        self.assertFalse((SKILLS / "gt-learn").exists(), "owner: gt-learn folds into gt-work")
        learn = section(text("gt-work"), "Learn: patterns worth keeping (0.18.1)")
        self.assertIsNotNone(learn, "gt-work has no learn step")
        for must in ("one at a time", "gt_write_queue.py", "already captured", "/gt:gt-promote"):
            self.assertIn(must, learn)

    def test_no_external_plugin_is_needed(self):
        for s in ("gt-plan", "gt-implement", "gt-work"):
            body = text(s)
            for dep in ("review-process", "go-review"):
                self.assertNotIn(dep, body, "%s depends on %s" % (s, dep))


class DeprecatedAliases(unittest.TestCase):
    def test_each_alias_says_it_is_deprecated_and_names_the_new_verb(self):
        for old, (new, _skill, _sec) in ALIASES.items():
            desc = description(old)
            self.assertTrue(desc.startswith("Deprecated alias"), old)
            self.assertIn(new, desc, "%s: description must redirect to %s" % (old, new))
            self.assertIn("Note: %s is deprecated. Use %s instead." % (old, new), text(old),
                          "%s must print a one-line deprecation notice" % old)

    def test_aliases_have_no_triggers_left(self):
        for old in ALIASES:
            self.assertEqual(LINT.triggers(description(old)), set(),
                             "%s still advertises trigger phrases; they moved" % old)

    def test_each_alias_follows_a_section_that_exists(self):
        for old, (_new, skill, sec) in ALIASES.items():
            body = text(old)
            self.assertIn("<base_dir>/../%s/SKILL.md" % skill, body)
            self.assertIn("## %s" % sec, body)
            self.assertIsNotNone(section(text(skill), sec), "%s has no ## %s" % (skill, sec))

    @unittest.skipIf(PREV is None, "no earlier release in this tree to compare with")
    def test_every_old_command_survives_verbatim_in_its_new_section(self):
        """Parity: what the old skill ran, the new section runs. The one deliberate change is
        `gt_task.py done`, which now goes through gt_close.py task -- the shared close logic --
        and that is asserted separately below."""
        for old, (_new, skill, sec) in ALIASES.items():
            if not (PREV / "skills" / old / "SKILL.md").is_file():
                continue
            new_sec = norm(section(text(skill), sec))
            for cmd in fenced_commands(text(old, PREV / "skills")):
                if " done <ID>" in cmd or "--status handled" in cmd:
                    continue        # now gt_close.py task / handoff: asserted below
                self.assertIn(cmd, new_sec, "%s: %r is missing from %s ## %s"
                              % (old, cmd, skill, sec))

    def test_close_in_handle_is_the_shared_close_logic(self):
        handle = text("gt-handle")
        self.assertIn("python3 <close> task <ID>", handle)
        self.assertIn("python3 <close> handoff <handoff>", handle)
        self.assertIn("<base_dir>/../../scripts/gt_close.py", handle)

    @unittest.skipIf(PREV is None, "no earlier release in this tree to compare with")
    def test_old_trigger_phrases_moved_to_the_new_verbs(self):
        for old, (_new, skill, _sec) in ALIASES.items():
            if not (PREV / "skills" / old / "SKILL.md").is_file():
                continue
            before = {t for t in LINT.triggers(description(old, PREV / "skills"))
                      if not t.startswith("/")}
            missing = before - LINT.triggers(description(skill))
            self.assertEqual(missing, set(), "%s's triggers not carried by %s" % (old, skill))

    def test_shipped_skills_still_have_no_trigger_collisions(self):
        trig = {d.name: LINT.triggers(description(d.name)) for d in SKILLS.iterdir()
                if (d / "SKILL.md").is_file()}
        names = sorted(trig)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                self.assertEqual(trig[a] & trig[b], set(), "%s <-> %s" % (a, b))


class Dispatch(unittest.TestCase):
    def test_bare_handle_asks_when_both_are_waiting(self):
        which = section(text("gt-handle"), "Which one?")
        self.assertIsNotNone(which)
        self.assertIn("ask", which)
        self.assertIn("Never pick one silently", which)

    def test_bare_list_shows_both_summarised(self):
        both = section(text("gt-list"), "Both")
        self.assertIsNotNone(both)
        self.assertIn("gt_handoff_status.py list", both)
        self.assertIn("gt_task.py count", both)

    def test_create_dispatches_project_task_handoff(self):
        body = text("gt-create")
        for sec in ("Project", "Task", "Handoff"):
            self.assertIsNotNone(section(body, sec), "gt-create has no ## %s" % sec)
        self.assertIn("vault_init.py create-project", section(body, "Project"))
        self.assertIn("gt_task.py add", section(body, "Task"))
        self.assertIn("gt_handoff.py", section(body, "Handoff"))

    @unittest.skipIf(PREV is None, "no earlier release in this tree to compare with")
    def test_open_project_is_unchanged(self):
        """Every line of the previous gt-open's project steps is still there, in order."""
        old = text("gt-open", PREV / "skills")
        new = text("gt-open")
        steps = old.split("## Steps", 1)[1]
        lines = [l for l in steps.splitlines() if l.strip()]
        pos = 0
        for l in lines:
            at = new.find(l, pos)
            self.assertNotEqual(at, -1, "gt-open project step changed: %r" % l)
            pos = at + len(l)

    def test_open_handles_handoff_and_task(self):
        sec = section(text("gt-open"), "Opening a handoff or a task (0.18.1)")
        self.assertIsNotNone(sec)
        for must in ("/gt:gt-open handoff <id>", "/gt:gt-open task <id>", "gt_task.py list",
                     "gt_handoff_status.py list"):
            self.assertIn(must, sec)
        self.assertIn("Opening a handoff or a task", text("gt-open").split("## Steps")[0],
                      "gt-open must dispatch before its project steps")


class Close(unittest.TestCase):
    def test_close_covers_all_three_and_asks_when_ambiguous(self):
        body = text("gt-close")
        for sec in ("Project", "Task", "Handoff"):
            self.assertIsNotNone(section(body, sec))
        self.assertIn("ask what to close", norm(body.replace("**", "")))
        self.assertIn("Never guess", body)

    def test_project_close_halts_reviews_graduates_and_archives_in_place(self):
        proj = section(text("gt-close"), "Project")
        for must in ("python3 <close> project <slug>", "halts", "shelve", "move", "drop",
                     "/gt:gt-promote", "one at a time", "--archive", "In place, by default",
                     "--move"):
            self.assertIn(must, proj)

    def test_no_close_path_writes_a_vault_file_directly(self):
        body = text("gt-close")
        self.assertNotRegex(body, r"(?m)^\s*(cat|echo|printf)\b.*>\s*\S*Projects/")
        self.assertIn("No Write/Edit on a vault path", body)


class PlanImplement(unittest.TestCase):
    def test_plan_waits_for_explicit_approval_and_writes_no_code(self):
        body = text("gt-plan")
        for must in (".claude/gt-plan-current.md", "status: draft", "status: approved",
                     "Approve this plan?", "no code", "Only the user approves"):
            self.assertIn(must, body)

    def test_implement_refuses_without_an_approved_plan(self):
        step0 = section(text("gt-implement"), "Step 0 — Refuse without an approved plan")
        self.assertIsNotNone(step0)
        self.assertIn("`.claude/gt-plan-current.md` does not exist", step0)
        self.assertIn("is not approved", step0)
        self.assertIn("Write no code", step0)

    def test_implement_is_test_first_and_stops_on_red(self):
        body = text("gt-implement")
        self.assertLess(body.index("**RED.**"), body.index("**GREEN.**"))
        self.assertIn("which tests ran", body)
        self.assertIn("**stop**", body)
        self.assertIn("Never start phase N+1 on a failing phase N", body)

    def test_implement_reuses_allin_and_allin_commit_and_asks_before_committing(self):
        body = text("gt-implement")
        self.assertIn("<base_dir>/../../scripts/gt_allin.py", body)
        self.assertIn("<base_dir>/../../scripts/gt_allin_commit.py", body)
        self.assertIn("Commit this?", body)
        self.assertLess(body.index("--dry-run"), body.index("Commit this?"))
        self.assertTrue((SCRIPTS / "gt_allin.py").is_file())
        self.assertTrue((SCRIPTS / "gt_allin_commit.py").is_file())


class SurfacePointsAtTheNewVerbs(unittest.TestCase):
    def test_session_start_lines_name_the_new_verbs(self):
        src = (SCRIPTS / "gt_surface.py").read_text(encoding="utf-8")
        self.assertIn("/gt:gt-handle handoff", src)
        self.assertIn("/gt:gt-list tasks mine p1", src)
        self.assertNotIn("/gt:gt-handoff-handle", src)
        self.assertNotIn("/gt:gt-task-list", src)


if __name__ == "__main__":
    unittest.main()
