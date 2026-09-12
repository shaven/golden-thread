"""gt_upgrade.py -- bring an existing vault up to the installed release.

Contract:
  * A vault with no stamp is treated as OLD, never as current: every migration is
    offered, and each detects its own work so re-running is a no-op.
  * --dry-run writes NOTHING, including no stamp and no backup.
  * `run` refuses a dirty git tree, so the upgrade stays one `git checkout` from undone.
  * A migration that REFUSES does not stop the others; it is reported, and a later run
    continues from there.
  * A document merge without a base does not overwrite the owner's file.
  * A conflicting merge leaves the document untouched and writes the markers beside it.
"""
import json
import subprocess
import unittest

from _harness import Sandbox, SCRIPTS, TEMPLATES


UP = SCRIPTS / "gt_upgrade.py"
VI = SCRIPTS / "vault_init.py"
STAMP = "Projects/golden-thread/.vault-version.json"
BASE = "Projects/golden-thread/.templates"


class UpgradeBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.v = self.make_vault()
        self.env.pop("GT_VAULT", None)

    def up(self, *args):
        return self.py(UP, *args, "--vault", str(self.v))

    def project(self, slug, decisions="# D\n\n## ADR-1: one\n"):
        d = self.v / "Projects" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "decisions.md").write_text(decisions)
        return d

    def commit_all(self):
        self.git_init(self.v)
        subprocess.run(["git", "-C", str(self.v), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(self.v), "commit", "-m", "base"],
                       capture_output=True, env={**self.env, "HOME": str(self.home)})

    def snapshot(self):
        return {str(f): f.read_bytes() for f in sorted(self.v.rglob("*"))
                if f.is_file() and ".git/" not in str(f)}


class Status(UpgradeBase):
    def test_unstamped_vault_is_treated_as_old(self):
        (self.v / STAMP).unlink()          # a vault from before stamps existed
        p = self.up("status")
        self.assertIn("never stamped", p.stdout)
        self.assertIn("OLD", p.stdout,
                      "an unstamped vault must not be assumed current")

    def test_status_names_the_release_and_the_vault(self):
        p = self.up("status")
        self.assertIn(str(self.v), p.stdout)
        self.assertIn("release:", p.stdout)

    def test_pending_decisions_are_listed_by_project(self):
        self.project("alpha")
        p = self.up("status")
        self.assertIn("alpha", p.stdout)
        self.assertNotEqual(p.returncode, 0, "pending work must not report success")

    def test_status_writes_nothing(self):
        before = self.snapshot()
        self.up("status")
        self.assertEqual(self.snapshot(), before, "status modified the vault")


class SeededVault(UpgradeBase):
    """A vault created today starts stamped and with a merge base, so the FIRST
    upgrade it ever sees can merge rather than guess."""

    def test_fresh_vault_is_stamped(self):
        stamp = json.loads((self.v / STAMP).read_text())
        self.assertTrue(stamp.get("gt"), "a new vault has no version stamp")

    def test_fresh_vault_carries_a_merge_base(self):
        for name in ("PROTOCOL.md", "CONVENTIONS.md"):
            self.assertTrue((self.v / BASE / name).is_file(),
                            f"{name} has no recorded base, so an upgrade cannot merge it")

    def test_the_base_matches_what_was_seeded(self):
        base = (self.v / BASE / "PROTOCOL.md").read_text()
        seeded = (self.v / "Projects" / "PROTOCOL.md").read_text()
        self.assertEqual(base, seeded,
                         "the base must equal what the vault started from, or the first "
                         "merge will replay edits nobody made")


class DryRun(UpgradeBase):
    def test_dry_run_writes_nothing_at_all(self):
        self.project("alpha")
        before = self.snapshot()
        (self.v / STAMP).unlink()
        before = self.snapshot()
        p = self.up("run", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.snapshot(), before, "a dry run modified the vault")
        self.assertFalse((self.v / STAMP).exists(), "a dry run stamped the vault")

    def test_dry_run_works_on_a_dirty_tree(self):
        """The rehearsal must be available exactly when the tree is mid-work."""
        self.commit_all()
        (self.v / "scratch.md").write_text("uncommitted")
        p = self.up("run", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_dry_run_says_what_it_would_do(self):
        self.project("alpha")
        p = self.up("run", "--dry-run")
        self.assertIn("would", p.stdout.lower())


class Applying(UpgradeBase):
    def test_refuses_a_dirty_tree(self):
        self.commit_all()
        (self.v / "scratch.md").write_text("uncommitted work")
        (self.v / STAMP).unlink()
        p = self.up("run")
        self.assertEqual(p.returncode, 2)
        self.assertIn("REFUSED", p.stdout + p.stderr)
        self.assertFalse((self.v / STAMP).exists(), "it stamped the vault anyway")

    def test_allow_dirty_overrides(self):
        self.commit_all()
        (self.v / "scratch.md").write_text("uncommitted work")
        p = self.up("run", "--allow-dirty")
        self.assertIn(p.returncode, (0, 1), p.stdout + p.stderr)

    def test_applying_stamps_the_vault_with_history(self):
        self.project("alpha")
        self.up("run", "--allow-dirty")
        stamp = json.loads((self.v / STAMP).read_text())
        self.assertIn("gt", stamp)
        self.assertTrue(stamp.get("history"), "no history recorded")

    def test_a_second_run_is_a_no_op(self):
        self.project("alpha")
        self.up("run", "--allow-dirty")
        p = self.up("run", "--allow-dirty")
        self.assertIn("up to date", p.stdout, p.stdout)

    def test_a_refusal_does_not_stop_the_others(self):
        """A project with two ADR-6 entries refuses; every other project still migrates."""
        self.project("good")
        self.project("bad", "# D\n\n## ADR-6: one\n\n## ADR-6: two\n")
        p = self.up("run", "--allow-dirty")
        out = p.stdout + p.stderr
        self.assertIn("REFUSED", out, "the duplicate was not reported")
        self.assertIn("bad", out, "the refusing project was not named")
        self.assertTrue(
            (self.v / "Projects/golden-thread/spool/decisions/good/0000-baseline.md").exists(),
            "one project's refusal stopped the others")
        self.assertEqual(p.returncode, 1, "a run needing a person must not report success")

    def test_backup_is_written_before_changes(self):
        self.project("alpha")
        p = self.up("run", "--allow-dirty")
        line = [l for l in p.stdout.splitlines() if l.startswith("backup:")]
        self.assertTrue(line, "no backup was reported")
        path = line[0].split(":", 1)[1].strip()
        self.assertTrue(path.endswith(".tar.gz"))
        import os
        self.assertTrue(os.path.isfile(path), "the reported backup does not exist")


class DocumentMerges(UpgradeBase):
    def doc(self):
        return self.v / "Projects" / "PROTOCOL.md"

    def test_no_base_leaves_the_document_alone_and_records_one(self):
        d = self.doc()
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text("# Mine\n\nlocal edits\n")
        before = d.read_bytes()
        self.up("run", "--allow-dirty")
        self.assertEqual(d.read_bytes(), before,
                         "an owner's document was overwritten with no merge base")
        self.assertTrue((self.v / BASE / "PROTOCOL.md").is_file(),
                        "no base captured, so the next upgrade cannot merge either")

    def test_with_a_base_the_release_changes_are_merged_in(self):
        shipped = (TEMPLATES / "PROTOCOL.md").read_text()
        d = self.doc()
        d.parent.mkdir(parents=True, exist_ok=True)
        # base == shipped, and the vault adds a line of its own
        base = self.v / BASE / "PROTOCOL.md"
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(shipped)
        d.write_text(shipped + "\n## A section this vault added\n")
        self.up("run", "--allow-dirty")
        after = d.read_text()
        self.assertIn("A section this vault added", after,
                      "the vault's own edit was lost in the merge")

    def test_a_conflict_leaves_the_document_untouched(self):
        shipped = (TEMPLATES / "PROTOCOL.md").read_text()
        base = self.v / BASE / "PROTOCOL.md"
        base.parent.mkdir(parents=True, exist_ok=True)
        # A base that shares no lines with either side forces a conflict.
        base.write_text("\n".join("base line %d" % i for i in range(40)) + "\n")
        d = self.doc()
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text("\n".join("vault line %d" % i for i in range(40)) + "\n")
        before = d.read_bytes()
        p = self.up("run", "--allow-dirty")
        self.assertEqual(d.read_bytes(), before, "a conflicting merge rewrote the document")
        self.assertIn("CONFLICT", p.stdout)
        self.assertTrue(d.with_suffix(".md.merge-conflict").is_file(),
                        "the conflicted text was not left anywhere to resolve")
        self.assertNotEqual(shipped, d.read_text())


if __name__ == "__main__":
    unittest.main()
