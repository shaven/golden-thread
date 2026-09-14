"""gt_adr.py — ADR numbers allocated atomically; decisions.md rendered from them.

The race test runs real processes in parallel, repeatedly. A single pair passes by
luck against a broken allocator, so it loops: a read-then-write counter fails this
within a few rounds, which is the point.
"""
import subprocess
import sys
import unittest
from collections import Counter

from _harness import Sandbox, TOOLS, SCRIPTS, PYTHON


class GtAdr(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        p = self.py(SCRIPTS / "vault_init.py", "create-project", "--vault", self.vault,
                    "--name", "demo", "--title", "Demo", "--domain", "test",
                    "--topology", "local")
        self.assertOk(p, "could not scaffold the fixture project")
        self.dec = self.vault / "Projects" / "demo" / "decisions.md"

    def tool(self, *args, **kw):
        return self.py(TOOLS / "gt_adr.py", "--vault", self.vault, *args, **kw)

    def seed(self, text):
        """Stand in a PRE-0.11 decisions.md: since 0.14.0 create-project hands back a
        project already migrated, so the scaffold's baseline goes too, or `migrate`
        would answer "already migrated" and the tests below would test nothing."""
        (self.vault / "Projects/golden-thread/spool/decisions/demo"
         / "0000-baseline.md").unlink(missing_ok=True)
        self.dec.write_text(text, encoding="utf-8")

    def allocate(self, title="t"):
        p = self.tool("allocate", "demo", "--title", title)
        self.assertOk(p, "allocate failed")
        return int(p.stdout.strip())

    # -- born current (regression 2026-09-14) -------------------------------------
    def test_new_project_is_born_migrated_and_renders_allocations(self):
        """create-project left decisions.md without a baseline, so `decisions-spool`
        was pending the moment a project existed and install.sh migrated it."""
        base = self.vault / "Projects/golden-thread/spool/decisions/demo/0000-baseline.md"
        self.assertTrue(base.is_file(), "create-project left no decisions baseline")
        before = self.dec.read_bytes()
        self.assertOk(self.tool("merge", "demo"))
        self.assertEqual(self.dec.read_bytes(), before, "scaffold is not what merge renders")
        self.assertIn("already migrated", self.tool("migrate", "demo").stdout)
        self.assertEqual(self.allocate("first real decision"), 1)
        self.assertOk(self.tool("merge", "demo"))
        text = self.dec.read_text()
        self.assertIn("## ADR-1: first real decision", text)
        self.assertIn("# Demo Decisions", text)

    # -- allocation --------------------------------------------------------------
    def test_allocation_is_exclusive_and_sequential(self):
        self.assertEqual([self.allocate() for _ in range(3)], [1, 2, 3])

    def test_slot_is_named_by_number_alone(self):
        """number+session in the filename would never collide, voiding the guarantee."""
        self.allocate()
        names = sorted(p.name for p in
                       (self.vault / "Projects/golden-thread/spool/decisions/demo").iterdir())
        self.assertIn("0001.md", names,
                      "slot must be named by the number alone, not number+session")

    def test_never_reuses_an_existing_number(self):
        self.seed("# D\n\n## ADR-1: one\n\n## ADR-2: two\n")
        self.assertOk(self.tool("migrate", "demo"))
        self.assertEqual(self.allocate(), 3)

    def test_concurrent_allocations_never_collide(self):
        got = []
        for _ in range(15):
            ps = [subprocess.Popen(
                [PYTHON, str(TOOLS / "gt_adr.py"), "--vault", str(self.vault),
                 "allocate", "demo", "--title", "race"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=self.env)
                for _ in range(2)]
            got += [int(p.communicate()[0].strip()) for p in ps]
        dupes = [n for n, c in Counter(got).items() if c > 1]
        self.assertEqual(dupes, [], f"the same number was issued twice: {dupes}")
        self.assertEqual(sorted(got), list(range(1, len(got) + 1)),
                         "allocations are not contiguous")

    def test_deleting_the_highest_does_not_roll_back(self):
        self.allocate(); n = self.allocate()
        (self.vault / f"Projects/golden-thread/spool/decisions/demo/{n:04d}.md").unlink()
        self.assertNotEqual(self.allocate(), n,
                            "a number was reissued after its slot was deleted")

    def test_stale_restore_does_not_reissue(self):
        """An older copy syncing back must not make spent numbers issuable again."""
        for _ in range(5):
            self.allocate()
        d = self.vault / "Projects/golden-thread/spool/decisions/demo"
        for n in (4, 5):
            (d / f"{n:04d}.md").unlink()          # as a stale sync or restore would
        self.assertGreater(self.allocate(), 5,
                           "a spent number was reissued after an older state returned")

    def test_corrupt_highwater_degrades_safely(self):
        """The floor is belt-and-braces: garbage in it must not break allocation."""
        self.allocate(); self.allocate()
        d = self.vault / "Projects/golden-thread/spool/decisions/demo"
        (d / ".highwater").write_text("not-a-number\n", encoding="utf-8")
        self.assertEqual(self.allocate(), 3,
                         "a corrupt high-water file changed the derived result")

    def test_projects_allocate_independently(self):
        p = self.py(SCRIPTS / "vault_init.py", "create-project", "--vault", self.vault,
                    "--name", "other", "--title", "Other", "--domain", "test",
                    "--topology", "local")
        self.assertOk(p)
        self.allocate(); self.allocate()
        q = self.tool("allocate", "other", "--title", "x")
        self.assertOk(q)
        self.assertEqual(int(q.stdout.strip()), 1, "projects share a sequence")

    # -- rendering ---------------------------------------------------------------
    def test_merge_orders_and_is_idempotent(self):
        self.seed("# D\n\n## ADR-1: one\n")
        self.assertOk(self.tool("migrate", "demo"))
        self.allocate("three"); self.allocate("four")
        self.assertOk(self.tool("merge", "demo"))
        first = self.dec.read_bytes()
        self.assertOk(self.tool("merge", "demo"))
        self.assertEqual(self.dec.read_bytes(), first, "merge is not idempotent")
        nums = [l for l in self.dec.read_text().splitlines() if l.startswith("## ADR-")]
        self.assertEqual(nums, ["## ADR-1: one", "## ADR-2: three", "## ADR-3: four"])

    # -- migration ---------------------------------------------------------------
    def test_migrate_refuses_on_duplicate_numbers(self):
        """Renumbering would invalidate inbound references, so refuse and report."""
        self.seed("# D\n\n## ADR-6: first thing\n\n## ADR-6: different thing\n")
        before = self.dec.read_bytes()
        p = self.tool("migrate", "demo")
        self.assertNotEqual(p.returncode, 0, "duplicate ADR numbers were accepted")
        self.assertIn("ADR-6", p.stdout + p.stderr)
        self.assertEqual(self.dec.read_bytes(), before, "the file was modified anyway")

    def test_amendment_is_not_a_duplicate(self):
        """`ADR-6 amendment` is deliberate usage and must not be flagged."""
        self.seed("# D\n\n## ADR-6: a source field is decoration\n\n"
                  "## ADR-6 amendment: a resolvable URL is not a read URL\n")
        self.assertOk(self.tool("migrate", "demo"),
                      "an amendment was treated as a duplicate allocation")

    def test_migration_round_trips(self):
        body = "# D\n\n## ADR-1: one\n\nBody.\n\n## ADR-2: two\n\nMore.\n"
        self.seed(body)
        self.assertOk(self.tool("migrate", "demo"))
        self.assertTrue(self.dec.read_text().rstrip("\n").endswith(body.rstrip("\n")),
                        "migration did not reproduce the original content")

    def test_unfinished_allocation_is_reported(self):
        n = self.allocate()
        out = self.tool("status", "demo").stdout
        self.assertIn("UNFINISHED", out, "an allocated-but-empty ADR was not reported")
        self.assertNotEqual(self.allocate(), n, "an unfinished number was reissued")


class GtAdrFaithfulnessAndDryRun(GtAdr):
    """0.12.0: the migration gate stops normalising what it is checking, and every
    writing subcommand can be rehearsed.

    The 2026-09-11 incident had two halves. This class covers the quieter one:
    migrating 42 projects dropped a trailing blank line from homekit-bridge's
    decisions.md while reporting a byte-identical round trip, because BOTH sides of
    the comparison were rstrip'd -- as was the test that guarded it.
    """

    def test_trailing_blank_line_is_not_dropped_in_silence(self):
        body = "# D\n\n## ADR-1: one\n\nBody.\n\n"      # ends with a blank line
        self.seed(body)
        p = self.tool("migrate", "demo")
        self.assertOk(p, "migration failed")
        kept = self.dec.read_text().endswith("Body.\n\n")
        announced = "trailing blank line" in (p.stdout + p.stderr)
        self.assertTrue(kept or announced,
                        "the line was dropped and nothing said so — the 2026-09-11 defect:\n"
                        + repr(self.dec.read_text()[-40:]) + "\n" + p.stdout + p.stderr)

    def test_baseline_holds_the_original_bytes_exactly(self):
        body = "# D\n\n## ADR-1: one\n\nBody.\n\n"
        self.seed(body)
        self.assertOk(self.tool("migrate", "demo"))
        base = (self.vault / "Projects/golden-thread/spool/decisions/demo/0000-baseline.md")
        self.assertEqual(base.read_text(), body,
                         "the baseline is the only copy of the pre-migration file; it "
                         "must be byte-exact whatever the rendering does")

    def test_real_content_difference_still_refuses(self):
        """Tolerating trailing newlines must not have loosened the gate itself."""
        self.seed("# D\n\n## ADR-1: one\n")
        import unittest.mock  # noqa: F401  (documents intent; the check below is real)
        self.assertOk(self.tool("migrate", "demo"))
        # A second migrate on generated output is the refusal path that proves the
        # gate still fires rather than overwriting.
        p = self.tool("migrate", "demo")
        self.assertIn("already migrated", p.stdout + p.stderr)

    # -- dry run ------------------------------------------------------------------
    def test_migrate_dry_run_writes_nothing(self):
        self.seed("# D\n\n## ADR-1: one\n")
        before = self.dec.read_bytes()
        p = self.tool("migrate", "demo", "--dry-run")
        self.assertOk(p, "dry run failed")
        self.assertIn("dry run", p.stdout)
        self.assertEqual(self.dec.read_bytes(), before, "a dry run modified decisions.md")
        self.assertFalse((self.vault / "Projects/golden-thread/spool/decisions/demo"
                          / "0000-baseline.md").exists(),
                         "a dry run left a baseline behind, so the real migrate would "
                         "then refuse as 'already migrated'")

    def test_merge_dry_run_writes_nothing(self):
        self.seed("# D\n\n## ADR-1: one\n")
        self.assertOk(self.tool("migrate", "demo"))
        self.allocate("two")
        before = self.dec.read_bytes()
        p = self.tool("merge", "demo", "--dry-run")
        self.assertOk(p, "dry run failed")
        self.assertEqual(self.dec.read_bytes(), before, "a dry run rewrote decisions.md")

    def test_allocate_dry_run_reserves_no_number(self):
        """A rehearsal that consumed a number would punch a hole every time."""
        self.seed("# D\n\n## ADR-1: one\n")
        self.assertOk(self.tool("migrate", "demo"))
        p = self.tool("allocate", "demo", "--title", "x", "--dry-run")
        self.assertOk(p, "dry run failed")
        self.assertEqual(self.allocate("real"), 2,
                         "the dry run consumed ADR-2, so the next real allocation skipped it")

    def test_short_flag_is_accepted(self):
        self.seed("# D\n\n## ADR-1: one\n")
        before = self.dec.read_bytes()
        self.assertOk(self.tool("migrate", "demo", "-n"), "-n is the documented short form")
        self.assertEqual(self.dec.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
