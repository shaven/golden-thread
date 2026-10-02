"""Every tool that takes a project slug resolves it the way gt_adr does (0.18.1).

0.18.1 gave gt_adr ONE shared resolver, gt_spool.resolve_project: a bare sub-project slug
means `Projects/<parent>/<slug>/`, a name that matches nothing is an error that creates
nothing, and a name two projects share names both instead of guessing. The other tools
still joined `Projects/` with the slug, so they missed sub-projects -- and the writing ones
(gt_demote, gt_handoff) could grow a stray top-level `Projects/<slug>/`. This module drives
each switched caller against one fixture vault: a top-level `parent`, its sub-project
`child`, and a name `dup` that is a sub-project of two parents.

Assertions read the FILESYSTEM wherever the tool writes, because the original bug reported
success while writing the wrong path.
"""
import json
import shutil

from _harness import Sandbox, SCRIPTS, TOOLS

VI = SCRIPTS / "vault_init.py"


class Fixture(Sandbox):
    def setUp(self):
        super().setUp()
        self.v = self.make_vault()
        self.create("parent")
        self.create("child", "--parent", "parent")
        self.create("other")
        self.create("dup", "--parent", "parent")
        self.create("dup", "--parent", "other")
        # gt_task drains through the broker installed beside the hooks
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for n in ("gt_broker.py", "gt_write_queue.py"):
            shutil.copy(SCRIPTS / n, hooks / n)

    def create(self, name, *extra):
        p = self.py(VI, "create-project", "--vault", self.v, "--name", name, "--domain", "t",
                    "--topology", "local", *extra)
        self.assertOk(p, "create-project %s failed" % name)

    def tree(self):
        return sorted(p.relative_to(self.v).as_posix() for p in self.v.rglob("*")
                      if "spool" not in p.parts and ".git" not in p.parts)

    def assertNoStray(self, name="child"):
        self.assertFalse((self.v / "Projects" / name).exists(),
                         "a top-level Projects/%s/ was created for a sub-project slug" % name)

    def assertRefused(self, p, name, both=False):
        out = p.stdout + p.stderr
        self.assertNotEqual(p.returncode, 0, "accepted %r:\n%s" % (name, out))
        self.assertIn(name, out, "the refusal does not name %r" % name)
        if both:
            self.assertIn("parent/dup", out)
            self.assertIn("other/dup", out)

    def unknown_and_ambiguous(self, run):
        """`run(name)` -> CompletedProcess. Unknown and ambiguous both refuse, create nothing."""
        before = self.tree()
        self.assertRefused(run("ghost"), "ghost")
        self.assertRefused(run("dup"), "dup", both=True)
        self.assertEqual(self.tree(), before, "a refused slug created or changed something")
        self.assertNoStray("ghost")


class GtTask(Fixture):
    def task(self, *args):
        return self.py(self.v / "Projects/golden-thread/tools/gt_task.py", *args,
                       env={"GT_TODAY": "2026-10-01"})

    def test_add_lands_in_the_subprojects_readme(self):
        p = self.task("add", "wire", "the", "thing", "--project", "child", "--vault", self.v)
        self.assertOk(p)
        self.assertIn("wire the thing", (self.v / "Projects/parent/child/README.md").read_text())
        self.assertNoStray()
        self.assertIn("added parent/child:", p.stdout, "the ID must carry the resolved path")

    def test_move_to_a_subproject_by_bare_slug(self):
        self.assertOk(self.task("add", "move", "me", "--project", "parent", "--vault", self.v))
        rows = json.loads(self.task("list", "parent", "--vault", self.v, "--json").stdout)
        tid = [r["id"] for r in rows if r["text"].startswith("move me")][0]
        self.assertOk(self.task("move", tid, "--to", "child", "--reason", "belongs there",
                              "--vault", self.v))
        self.assertIn("move me", (self.v / "Projects/parent/child/README.md").read_text())
        self.assertNoStray()

    def test_unknown_and_ambiguous(self):
        self.unknown_and_ambiguous(lambda n: self.task("add", "x", "--project", n,
                                                       "--vault", self.v))


class GtHandoff(Fixture):
    def ho(self, name, *args):
        return self.py(SCRIPTS / "gt_handoff.py", "--vault", self.v, "--project", name, *args)

    def test_facts_and_file_come_from_the_subproject(self):
        p = self.ho("child", "--json")
        self.assertOk(p)
        srcs = [f["source"] for f in json.loads(p.stdout)["facts"].values()]
        self.assertTrue(any(s.startswith("Projects/parent/child/") for s in srcs), srcs)
        self.assertOk(self.ho("child"))
        self.assertTrue(list((self.v / "Projects/parent/child/handoff").glob("*-handoff.md")),
                        "the handoff is not in the sub-project's handoff/ folder")
        self.assertNoStray()

    def test_unknown_and_ambiguous(self):
        self.unknown_and_ambiguous(lambda n: self.ho(n))


class GtDemote(Fixture):
    def dem(self, name):
        return self.py(SCRIPTS / "gt_demote.py", "--vault", self.v, "--file",
                       "global-memory/kelvin.md", "--to", "project-memory", "--project", name,
                       "--apply")

    def setUp(self):
        super().setUp()
        (self.v / "global-memory" / "kelvin.md").write_text("# Kelvin\n\nonly child needs it\n")

    def test_moves_into_the_subprojects_memory(self):
        self.assertOk(self.dem("child"))
        self.assertTrue((self.v / "Projects/parent/child/memory/kelvin.md").is_file())
        self.assertNoStray()

    def test_unknown_and_ambiguous(self):
        self.unknown_and_ambiguous(self.dem)


class GtClose(Fixture):
    def test_reports_on_the_subproject(self):
        p = self.py(SCRIPTS / "gt_close.py", "project", "child", "--vault", self.v, "--json")
        self.assertOk(p)
        self.assertEqual(json.loads(p.stdout)["project"], "parent/child")

    def test_archive_by_bare_slug_archives_the_subproject(self):
        self.assertOk(self.py(SCRIPTS / "gt_close.py", "project", "child", "--vault", self.v,
                              "--archive"))
        self.assertIn("stage: archived", (self.v / "Projects/parent/child/README.md").read_text())
        self.assertNotIn("stage: archived", (self.v / "Projects/parent/README.md").read_text())

    def test_unknown_and_ambiguous(self):
        self.unknown_and_ambiguous(lambda n: self.py(SCRIPTS / "gt_close.py", "project", n,
                                                     "--vault", self.v))


class VaultInitProjectOps(Fixture):
    def vi(self, *args):
        return self.py(VI, *args, "--vault", self.v)

    def test_archive_project_picks_the_subproject(self):
        self.assertOk(self.vi("archive-project", "--slug", "child"))
        self.assertIn("stage: archived", (self.v / "Projects/parent/child/README.md").read_text())

    def test_rename_project_renames_the_subproject_in_place(self):
        self.assertOk(self.vi("rename-project", "--from", "child", "--to", "kid"))
        self.assertTrue((self.v / "Projects/parent/kid/README.md").is_file())
        self.assertFalse((self.v / "Projects/parent/child").exists())
        self.assertNoStray("kid")

    def test_ambiguous_names_are_refused_not_guessed(self):
        before = {p: (self.v / p).read_bytes() for p in
                  ("Projects/parent/dup/README.md", "Projects/other/dup/README.md")}
        for args in (("archive-project", "--slug", "dup"),
                     ("rename-project", "--from", "dup", "--to", "dupe")):
            p = self.vi(*args)
            self.assertIn("parent/dup", p.stdout + p.stderr, args)
            self.assertIn("other/dup", p.stdout + p.stderr, args)
        for rel, b in before.items():
            self.assertEqual((self.v / rel).read_bytes(), b, "%s was changed" % rel)
        self.assertFalse((self.v / "Projects/parent/dupe").exists())
        self.assertFalse((self.v / "Projects/other/dupe").exists())

    def test_a_non_project_folder_of_that_name_is_not_a_project(self):
        """`rglob(slug)[0]` took ANY folder called `slug` -- a memory/ for instance."""
        p = self.vi("archive-project", "--slug", "memory")
        self.assertIn("not found", p.stdout + p.stderr)


class ReadOnlyCallers(Fixture):
    """Tools that only read: a bare sub-project slug works, unknown/ambiguous refuse."""

    def run_tool(self, name, script, *args):
        return self.py(SCRIPTS / script, "--vault", self.v, "--project", name, *args)

    CASES = (
        ("gt_catchup.py", ("--brief", "--json")),
        ("gt_memory_check.py", ("--json",)),
        ("gt_promote_detect.py", ("--json",)),
        ("gt_optimize.py", ("--only", "vault")),
    )

    def test_bare_subproject_slug_is_accepted(self):
        for script, extra in self.CASES:
            p = self.run_tool("child", script, *extra)
            self.assertIn(p.returncode, (0, 1), "%s refused child:\n%s%s"
                          % (script, p.stdout, p.stderr))
            self.assertNotIn("no project", p.stdout + p.stderr, script)
        self.assertNoStray()

    def test_unknown_and_ambiguous(self):
        for script, extra in self.CASES:
            with self.subTest(script=script):
                self.unknown_and_ambiguous(lambda n: self.run_tool(n, script, *extra))

    def test_digest_writes_beside_the_subprojects_research(self):
        p = self.py(SCRIPTS / "gt_digest.py", "write", "--vault", self.v, "--project", "child",
                    "--dry-run", "--json")
        self.assertOk(p)
        paths = [w["path"] for w in json.loads(p.stdout)["writes"]]
        self.assertIn("Projects/parent/child/research-digest.md", paths)
        self.unknown_and_ambiguous(lambda n: self.py(
            SCRIPTS / "gt_digest.py", "check", "--vault", self.v, "--project", n))

    def test_handoff_status_filters_on_the_subproject(self):
        st = SCRIPTS / "gt_handoff_status.py"
        self.assertOk(self.py(st, "list", "--vault", self.v, "--project", "child"))
        self.unknown_and_ambiguous(lambda n: self.py(st, "list", "--vault", self.v,
                                                     "--project", n))


if __name__ == "__main__":
    import unittest
    unittest.main()
