"""A slug is a name, not an address (0.18.1).

Two bugs, one root: a sub-project created with `create-project --parent P` lives at
`Projects/P/<slug>/`, but gt_adr joined `Projects/` with the bare slug (rendering a
sub-project's ADRs into a new top-level folder while reporting success), and the
generated CLAUDE.md pointed its reader at `Projects/<slug>/`, which does not exist.

Every assertion here reads the FILESYSTEM, not the tool's stdout: the original bug
printed "updated" while writing the wrong path.
"""
from _harness import Sandbox, TOOLS, SCRIPTS

VI = SCRIPTS / "vault_init.py"
SPOOL = "Projects/golden-thread/spool/decisions"


class SubprojectBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.v = self.make_vault()
        self.create("parent")
        self.create("child", "--parent", "parent")

    def create(self, name, *extra):
        p = self.py(VI, "create-project", "--vault", self.v, "--name", name, "--domain", "t",
                    "--topology", "local", *extra)
        self.assertOk(p, "create-project %s failed" % name)
        return p

    def adr(self, *args):
        return self.py(TOOLS / "gt_adr.py", "--vault", self.v, *args)


class AdrSubprojectResolution(SubprojectBase):
    def test_allocate_reserves_into_the_subprojects_spool(self):
        p = self.adr("allocate", "child", "--title", "first")
        self.assertOk(p)
        self.assertTrue((self.v / SPOOL / "parent/child/0001.md").is_file(),
                        "the slot is not in the sub-project's spool folder")
        self.assertFalse((self.v / SPOOL / "child").exists(),
                         "a flat spool folder was created for a sub-project slug")

    def test_merge_renders_inside_the_parent_and_creates_no_top_level_folder(self):
        self.assertOk(self.adr("allocate", "child", "--title", "Use widgets"))
        self.assertOk(self.adr("merge", "child"))
        dec = self.v / "Projects/parent/child/decisions.md"
        self.assertIn("## ADR-1: Use widgets", dec.read_text())
        self.assertFalse((self.v / "Projects/child").exists(),
                         "merge created a top-level Projects/child/ folder")

    def test_merge_twice_is_idempotent_and_leaves_one_file(self):
        self.assertOk(self.adr("allocate", "child", "--title", "t"))
        self.assertOk(self.adr("merge", "child"))
        dec = self.v / "Projects/parent/child/decisions.md"
        before = dec.read_bytes()
        self.assertOk(self.adr("merge", "child"))
        self.assertEqual(dec.read_bytes(), before)
        found = sorted(p.relative_to(self.v).as_posix()
                       for p in (self.v / "Projects").rglob("decisions.md")
                       if "spool" not in p.parts and "child" in p.parts)
        self.assertEqual(found, ["Projects/parent/child/decisions.md"])

    def test_unknown_slug_is_refused_by_name_and_creates_nothing(self):
        before = sorted(p for p in self.v.rglob("*"))
        for cmd in (("allocate", "ghost", "--title", "x"), ("merge", "ghost"),
                    ("status", "ghost"), ("lineage", "ghost", "x")):
            p = self.adr(*cmd)
            self.assertNotEqual(p.returncode, 0, "%s accepted an unknown slug" % cmd[0])
            self.assertIn("ghost", p.stderr, "the refusal does not name the slug")
        self.assertEqual(sorted(p for p in self.v.rglob("*")), before,
                         "an unknown slug created something")

    def test_merge_refuses_a_folder_without_readme(self):
        """A path that names a folder (not a project) is never rendered into."""
        (self.v / "Projects/parent/notes").mkdir()
        p = self.adr("merge", "parent/notes")
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.v / "Projects/parent/notes/decisions.md").exists())

    def test_top_level_project_is_unchanged(self):
        self.assertOk(self.adr("allocate", "parent", "--title", "top"))
        self.assertTrue((self.v / SPOOL / "parent/0001.md").is_file())
        self.assertOk(self.adr("merge", "parent"))
        self.assertIn("## ADR-1: top", (self.v / "Projects/parent/decisions.md").read_text())
        self.assertNotIn("ADR-1: top", (self.v / "Projects/parent/child/decisions.md").read_text())

    def test_path_form_still_accepted(self):
        """vault_init and gt_upgrade pass `parent/child`; that keeps working."""
        self.assertOk(self.adr("allocate", "parent/child", "--title", "x"))
        self.assertTrue((self.v / SPOOL / "parent/child/0001.md").is_file())

    def test_ambiguous_slug_is_refused(self):
        self.create("other")
        self.create("child", "--parent", "other")
        p = self.adr("allocate", "child", "--title", "x")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("more than one", p.stderr)
        self.assertOk(self.adr("allocate", "other/child", "--title", "x"))

    def test_flat_spool_from_the_old_bug_is_moved_and_rendered_in_place(self):
        """A vault that already has the bug's flat spool needs no migration: the spool
        entry is authoritative and a corrected merge renders it in the right place."""
        flat = self.v / SPOOL / "child"
        flat.mkdir(parents=True)
        (flat / "0001.md").write_text("<!-- allocated by old -->\n## ADR-1: from the bug\nbody\n")
        stray = self.v / "Projects/child"
        stray.mkdir()
        (stray / "decisions.md").write_text("stray\n")
        p = self.adr("merge", "child")
        self.assertOk(p)
        self.assertFalse(flat.exists(), "the flat spool was not moved")
        self.assertTrue((self.v / SPOOL / "parent/child/0001.md").is_file())
        self.assertIn("ADR-1: from the bug",
                      (self.v / "Projects/parent/child/decisions.md").read_text())
        self.assertIn("pre-0.18.1", p.stderr, "the stray folder was not named")
        self.assertEqual((stray / "decisions.md").read_text(), "stray\n",
                         "the stray folder must be left for its owner to delete")

    def test_flat_and_nested_spools_both_holding_adrs_is_refused(self):
        self.assertOk(self.adr("allocate", "child", "--title", "real"))
        flat = self.v / SPOOL / "child"
        flat.mkdir(parents=True)
        (flat / "0001.md").write_text("<!-- allocated by old -->\n## ADR-1: other\n")
        p = self.adr("merge", "child")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertTrue((flat / "0001.md").is_file())


class SubprojectClaudeMd(SubprojectBase):
    def test_subproject_claude_md_names_its_real_path(self):
        text = (self.v / "Projects/parent/child/CLAUDE.md").read_text()
        self.assertIn("Projects/parent/child/", text)
        self.assertNotIn("Projects/child/", text)
        # The assertion that would have caught it: the path it names RESOLVES.
        import re
        paths = re.findall(r"`(Projects/[^`]+)/`", text)
        self.assertTrue(paths, "CLAUDE.md names no vault path at all")
        for rel in paths:
            self.assertTrue((self.v / rel).is_dir(), "CLAUDE.md points at %s, not a folder" % rel)

    def test_top_level_claude_md_names_its_path(self):
        self.create("solo")
        text = (self.v / "Projects/solo/CLAUDE.md").read_text()
        self.assertIn("`Projects/solo/`", text)
        self.assertTrue((self.v / "Projects/solo").is_dir())

    def test_no_placeholder_survives_anywhere_in_the_template(self):
        for rel in ("Projects/parent/child/CLAUDE.md", "Projects/parent/CLAUDE.md"):
            self.assertNotIn("{{", (self.v / rel).read_text(), "%s kept a placeholder" % rel)

    def test_rerun_leaves_a_hand_edited_claude_md_untouched(self):
        p = self.v / "Projects/parent/child/CLAUDE.md"
        p.write_text(p.read_text() + "\nSENTINEL line written by hand\n")
        self.create("child", "--parent", "parent")
        self.assertIn("SENTINEL line written by hand", p.read_text())

    def test_parent_given_as_a_subproject_slug_nests_under_it(self):
        """--parent is resolved the same way: a grandchild lands inside the child."""
        self.create("grand", "--parent", "child")
        self.assertTrue((self.v / "Projects/parent/child/grand/README.md").is_file())
        self.assertFalse((self.v / "Projects/child").exists())
        self.assertIn("Projects/parent/child/grand/",
                      (self.v / "Projects/parent/child/grand/CLAUDE.md").read_text())


if __name__ == "__main__":
    import unittest
    unittest.main()
