"""skill_lint.py: trigger-collision audit over SKILL.md descriptions (rule 2)."""
import unittest

from _harness import Sandbox, SCRIPTS, GT, WIKI

SL = SCRIPTS / "skill_lint.py"


class SkillLintTest(Sandbox):
    def skill(self, root, name, description, quote='"'):
        d = self.tmp / root / "skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {quote}{description}{quote}\n---\n\n# {name}\n")
        return self.tmp / root

    def lint(self, *roots):
        return self.py(SL, *roots)

    def test_collision_exits_2_and_names_the_pair(self):
        r = self.skill("p", "alpha", "Does A. Use when the user says: tidy the notes, sort things.")
        self.skill("p", "beta", "Does B. Use when the user says: tidy the notes, file things.")
        proc = self.lint(r)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("TRIGGER COLLISIONS — 1 pair(s)", proc.stdout)
        self.assertIn("alpha  <->  beta", proc.stdout)
        self.assertIn('shared trigger: "tidy the notes"', proc.stdout)
        self.assertNotIn('"sort things"', proc.stdout)

    def test_distinct_triggers_pass(self):
        r = self.skill("p", "alpha", "A. Use when the user says: tidy the notes, sort things.")
        self.skill("p", "beta", "B. Use when: file the inbox, route a thought.")
        proc = self.lint(r)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("Checked 2 skills across 1 root(s).", proc.stdout)
        self.assertIn("Rule 2 holds", proc.stdout)

    def test_normalisation_case_and_trailing_period(self):
        r = self.skill("p", "alpha", "A. Use when the user says: Lint The Vault, x-ray it")
        self.skill("p", "beta", "B. Use when the user says: lint the vault.")
        proc = self.lint(r)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn('"lint the vault"', proc.stdout)

    def test_stopwords_and_short_phrases_never_collide(self):
        r = self.skill("p", "alpha", "A. Use when the user says: check the vault, go, help, tidy up")
        self.skill("p", "beta", "B. Use when the user says: check the vault, go, help, file stuff")
        proc = self.lint(r)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_trigger_list_ends_at_first_sentence(self):
        r = self.skill("p", "alpha", "A. Use when the user says: tidy up. Also mentions refresh the wiki.")
        self.skill("p", "beta", "B. Use when the user says: refresh the wiki.")
        proc = self.lint(r)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_skill_without_triggers_is_listed(self):
        r = self.skill("p", "alpha", "Does A without any trigger list.")
        self.skill("p", "beta", "B. Use when the user says: file stuff.")
        proc = self.lint(r)
        self.assertEqual(proc.returncode, 0)
        tail = proc.stdout.split("No advertised triggers", 1)[1]
        self.assertIn("alpha", tail)
        self.assertNotIn("beta", tail)

    def test_collisions_across_roots(self):
        r1 = self.skill("p1", "alpha", "A. Use when the user says: sweep the attic.")
        r2 = self.skill("p2", "beta", "B. Use when the user says: sweep the attic.")
        proc = self.lint(r1, r2)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("across 2 root(s)", proc.stdout)

    def test_no_skills_found(self):
        (self.tmp / "empty").mkdir()
        proc = self.lint(self.tmp / "empty")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("No SKILL.md files found", proc.stdout)

    def test_shipped_plugins_have_no_collisions(self):
        proc = self.lint(GT, WIKI)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_unquoted_description_is_still_audited(self):
        """`description: text` (plain YAML scalar) is as valid as a quoted one and is
        the common form in Claude Code skills. The regex accepts only double-quoted
        descriptions, so two unquoted skills with the same trigger pass as clean."""
        r = self.skill("p", "alpha", "A. Use when the user says: sweep the attic.", quote="")
        self.skill("p", "beta", "B. Use when the user says: sweep the attic.", quote="")
        proc = self.lint(r)
        self.assertEqual(proc.returncode, 2,
                         "unquoted descriptions were read as empty and the collision was missed:\n"
                         + proc.stdout)



class ForkedSkills(Sandbox):
    """0.20.0: `context: fork` runs a skill in a subagent with none of the conversation, in the
    background by default -- it cannot ask the owner. A forked skill that asks is refused."""

    def skill(self, name, front, body):
        d = self.tmp / "p" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: %s\ndescription: \"%s. Use when: %s now.\"\n"
                                    "%s---\n\n%s\n" % (name, name, name, front, body))
        return self.tmp / "p"

    def test_a_forked_skill_that_asks_the_owner_is_refused(self):
        r = self.skill("gt-alpha", "context: fork\nagent: general-purpose\n",
                       "Run the report.\n\nAsk \"Apply it now?\" and wait.")
        proc = self.py(SL, r)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("FORKED SKILLS THAT ASK THE OWNER", proc.stdout)
        self.assertIn("SKILL.md:", proc.stdout)

    def test_a_forked_skill_that_only_reports_and_an_unforked_one_that_asks_pass(self):
        r = self.skill("gt-alpha", "context: fork\n", "Run the report and summarise it.")
        self.skill("gt-beta", "", "Ask \"Apply it now?\"")
        proc = self.py(SL, r)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_none_of_the_heavy_skills_forks_and_the_release_passes(self):
        for name in ("gt-ingest", "gt-lint", "gt-validate", "gt-optimize", "gt-review"):
            head = (GT / "skills" / name / "SKILL.md").read_text().split("\n---", 1)[0]
            self.assertNotIn("context: fork", head, name)
        proc = self.py(SL, GT)
        self.assertNotIn("FORKED SKILLS THAT ASK THE OWNER", proc.stdout)


if __name__ == "__main__":
    unittest.main()
