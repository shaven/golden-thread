"""gt_upgrade.py -- bring an existing vault up to the installed release.

Contract:
  * A vault with no stamp is treated as OLD, never as current: every migration is
    offered, and each detects its own work so re-running is a no-op.
  * --dry-run writes NOTHING, including no stamp and no backup.
  * `run` refuses a dirty git tree, so the upgrade stays one `git checkout` from undone.
  * A migration that REFUSES does not stop the others; it is reported, and a later run
    continues from there.
  * A document merge without a base does not overwrite the owner's file, records no base,
    and is reported as needing a person rather than as a pending step.
  * A conflicting merge leaves the document untouched and writes the markers beside it.
  * A conflict already written for the same base, template and document is not redone:
    a second run writes nothing and reports "conflict awaiting you".
"""
import json
import shutil
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
        """SUPERSEDED ASSERTION, inverted 2026-09-12.

        This asserted `returncode != 0` for a vault with pending work, on the reading
        that "pending work must not report success". The request that changed it
        (2026-09-12-upgrade-status-exit-code) made the better argument: `status`
        SUCCEEDED -- it looked and found pending steps, which is the normal answer for a
        vault that has not been upgraded. Exiting 1 rendered every interactive
        /gt:gt-upgrade run as a tool error, which trains the reader to ignore a check
        whose entire job is to be read.

        Nothing branched on the code: gt_doctor computes pending itself and the skill
        reads the printed output (verified before changing it). The printed content is
        asserted here unchanged, which is what callers actually consume.
        """
        self.project("alpha")
        p = self.up("status")
        self.assertIn("alpha", p.stdout)
        self.assertIn("pending step(s)", p.stdout)
        self.assertIn("Rehearse:", p.stdout, "the actionable hint must survive")
        self.assertEqual(p.returncode, 0,
                         "finding pending steps is a successful status, not a failure")

    def test_up_to_date_vault_also_exits_zero(self):
        p = self.up("status")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_missing_vault_still_fails(self):
        """The exit code has to stay meaningful for real failures."""
        p = self.py(UP, "status", "--vault", str(self.tmp / "no-such-vault"))
        self.assertNotEqual(p.returncode, 0,
                            "a vault that does not exist is a genuine failure")

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

    # 0.15.0: a conversion the installer applies unattended says WHICH files it changed and
    # what that means, not just "migrated 1 project(s)".
    def test_decisions_conversion_names_each_file_and_its_note(self):
        self.project("alpha", "# D\n\n## ADR-1: one\n\n\n")      # trailing blank lines
        p = self.up("run", "--allow-dirty")
        self.assertIn("Projects/alpha/decisions.md — migrated alpha", p.stdout)
        self.assertIn("now GENERATED from spool/decisions/", p.stdout)

    def test_a_rule_listed_in_gt_removed_is_not_re_added(self):
        core = self.v / "Projects" / "golden-thread" / "core-rules"
        (core / "core_parallel_when_beneficial.md").unlink()
        self.assertIn("core_parallel_when_beneficial.md", self.up("status").stdout)
        (core.parent / ".gt-removed").write_text("core_parallel_when_beneficial.md\n")
        self.assertNotIn("core_parallel_when_beneficial.md", self.up("status").stdout)
        self.up("run", "--allow-dirty")
        self.assertFalse((core / "core_parallel_when_beneficial.md").exists(),
                         "a rule the owner listed as removed was re-added")


class DocumentMerges(UpgradeBase):
    def doc(self):
        return self.v / "Projects" / "PROTOCOL.md"

    def test_no_base_is_left_alone_never_captured_and_never_pending(self):
        # 0.14.0: capturing the owner's CURRENT file as the base made the next run's merge
        # (base == ours) take the release's side everywhere -- the owner's edits lost.
        d = self.doc()
        d.parent.mkdir(parents=True, exist_ok=True)
        (self.v / BASE / "PROTOCOL.md").unlink()
        d.write_text("# Mine\n\nlocal edits\n")
        before = d.read_bytes()
        for _ in range(2):
            p = self.up("run", "--allow-dirty")
            self.assertEqual(d.read_bytes(), before,
                             "an owner's document was overwritten with no merge base")
            self.assertFalse((self.v / BASE / "PROTOCOL.md").exists(),
                             "a base was recorded without a person reviewing the document")
            self.assertIn("needs a person: no merge base — review %s against the shipped "
                          "template, then run /gt:gt-upgrade" % d, p.stdout)
        st = self.up("status")
        self.assertNotIn("doc-merge", st.stdout, "a no-base document offered as an appliable step")
        self.assertIn("needs a person: no merge base", st.stdout)

    def test_record_base_is_explicit_and_records_the_shipped_template(self):
        d = self.doc()
        (self.v / BASE / "PROTOCOL.md").unlink()
        d.write_text("# Mine\n\nlocal edits\n")
        before = d.read_bytes()
        p = self.up("run", "--allow-dirty", "--record-base", "PROTOCOL.md")
        self.assertOk(p)
        self.assertEqual((self.v / BASE / "PROTOCOL.md").read_text(),
                         (TEMPLATES / "PROTOCOL.md").read_text())
        self.assertEqual(d.read_bytes(), before)
        self.assertNotIn("needs a person", self.up("status").stdout)

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

    def test_an_owner_edit_already_merged_is_not_pending_again(self):
        # base == shipped and the vault carries its own edit: the release has nothing
        # left to give, so status must not offer doc-merge (it did, forever, before 0.14.0).
        shipped = (TEMPLATES / "PROTOCOL.md").read_text()
        base = self.v / BASE / "PROTOCOL.md"
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(shipped)
        d = self.doc()
        d.write_text(shipped + "\n## A section this vault added\n")
        self.assertNotIn("doc-merge", self.up("status").stdout)
        # ...while a base the release has moved past still is.
        base.write_text(shipped.replace("\n", "\n\n", 1))
        self.assertIn("doc-merge", self.up("status").stdout)

    def test_a_dry_run_writes_no_working_copy_inside_the_vault(self):
        """A rehearsal must not write, not even for an instant.

        `_merge3` copied the document to `<doc>.merging` BESIDE IT and removed it in a
        `finally` -- so a dry run created a file in the vault, and a crash or a kill between
        the copy and the cleanup left it there. An after-the-fact snapshot cannot see a file
        that is deleted again, so this watches from INSIDE the merge: a `git` on PATH that
        lists the document's directory on every call and then delegates to the real one. The
        .merging copy exists exactly while `git merge-file` is running."""
        real_git = shutil.which("git")
        if not real_git:
            self.skipTest("git not installed")
        shipped = (TEMPLATES / "PROTOCOL.md").read_text()
        base = self.v / BASE / "PROTOCOL.md"
        base.parent.mkdir(parents=True, exist_ok=True)
        # A base the release has moved past, so a merge is genuinely pending.
        base.write_text(shipped.replace("\n", "\n\n", 1))
        d = self.doc()
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text(shipped + "\n## A section this vault added\n")
        self.assertIn("doc-merge", self.up("status").stdout, "no merge to rehearse")

        seen = self.tmp / "seen.txt"
        bindir = self.tmp / "spybin"
        bindir.mkdir()
        spy = bindir / "git"
        spy.write_text('#!/bin/sh\nls -a "%s" >> "%s"\nexec "%s" "$@"\n'
                       % (d.parent, seen, real_git))
        spy.chmod(0o755)

        before = self.snapshot()
        p = self.py(UP, "run", "--vault", str(self.v), "--dry-run",
                    env={"PATH": "%s:%s" % (bindir, self.env.get("PATH", "/usr/bin:/bin"))})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(seen.is_file(), "the spy git was never called; the test proves nothing")
        self.assertNotIn(".merging", seen.read_text(),
                         "a dry run created a working copy inside the vault")
        self.assertEqual(self.snapshot(), before, "a dry run modified the vault")

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

    def conflicting(self):
        base = self.v / BASE / "PROTOCOL.md"
        base.write_text("\n".join("base line %d" % i for i in range(40)) + "\n")
        d = self.doc()
        d.write_text("\n".join("vault line %d" % i for i in range(40)) + "\n")
        return d

    def git_status(self):
        return subprocess.run(["git", "-C", str(self.v), "status", "--porcelain",
                               "--untracked-files=all"], capture_output=True, text=True).stdout

    def test_a_conflict_awaiting_its_owner_is_not_redone(self):
        d = self.conflicting()
        self.commit_all()
        first = self.up("run")
        self.assertIn("CONFLICT", first.stdout, first.stdout + first.stderr)
        conflict = d.with_suffix(".md.merge-conflict")
        awaiting = ("needs a person: conflict awaiting you: %s — resolve, then /gt:gt-upgrade"
                    % conflict)
        self.assertIn(awaiting, first.stdout)
        before, status_before = self.snapshot(), self.git_status()
        second = self.up("run", "--allow-dirty")
        self.assertEqual(self.snapshot(), before, "a second run rewrote files for the same conflict")
        self.assertEqual(self.git_status(), status_before)
        self.assertNotIn("backup:", second.stdout, "the awaiting conflict was treated as pending")
        self.assertIn(awaiting, second.stdout)
        st = self.up("status")
        self.assertIn("nothing pending", st.stdout)
        self.assertIn(awaiting, st.stdout)

    def test_a_changed_document_makes_the_conflict_pending_again(self):
        d = self.conflicting()
        self.up("run", "--allow-dirty")
        self.assertNotIn("doc-merge", self.up("status").stdout)
        d.write_text(d.read_text() + "the owner is working on it\n")
        st = self.up("status")
        self.assertIn("doc-merge", st.stdout, "new inputs must be merged again")
        self.assertNotIn("conflict awaiting you", st.stdout)

    def test_record_base_settles_a_resolved_conflict(self):
        d = self.conflicting()
        self.up("run", "--allow-dirty")
        conflict = d.with_suffix(".md.merge-conflict")
        p = self.up("run", "--allow-dirty", "--record-base", "PROTOCOL.md")
        self.assertIn("resolve it into", p.stdout, "record-base ignored an unresolved conflict")
        d.write_text("the owner's resolution\n")
        conflict.unlink()
        resolved = d.read_bytes()
        p = self.up("run", "--allow-dirty", "--record-base", "PROTOCOL.md")
        self.assertIn("recorded the", p.stdout, p.stdout + p.stderr)
        self.assertEqual((self.v / BASE / "PROTOCOL.md").read_text(),
                         (TEMPLATES / "PROTOCOL.md").read_text())
        self.assertEqual(d.read_bytes(), resolved)
        st = self.up("status")
        self.assertIn("nothing pending", st.stdout)
        self.assertNotIn("needs a person", st.stdout)


class MergesThatRemoveYourLines(UpgradeBase):
    """A clean merge that would remove lines from the owner's document is held for a person.

    2026-09-14, the owner's vault: a pre-0.14 release recorded PROTOCOL.md itself as the
    base, so base == ours and the "clean" merge WAS the template -- the owner's "Write that
    down" section and every wikilink gone, reported as "would merge cleanly (-16 lines)",
    and install.sh applies pending steps unattended on a clean vault. The same shape arises
    when a release removes a section the owner still has. Neither is decided unattended."""

    OWNER = "# Protocol\n\n## Write that down\n\nthe owner's own rule\n\n[[a-wikilink]]\n"

    def doc(self):
        return self.v / "Projects" / "PROTOCOL.md"

    def base(self):
        return self.v / BASE / "PROTOCOL.md"

    def base_is_ours(self):
        self.base().write_text(self.OWNER)
        self.doc().write_text(self.OWNER)
        return self.doc()

    def test_a_base_recorded_from_the_owners_file_never_replaces_the_document(self):
        d = self.base_is_ours()
        before = d.read_bytes()
        st = self.up("status")
        self.assertNotIn("doc-merge", st.stdout, "a line-removing merge offered as appliable")
        self.assertIn("needs a person: merge held", st.stdout, st.stdout)
        self.assertIn("identical to your", st.stdout)
        for _ in range(2):
            p = self.up("run", "--allow-dirty")
            self.assertEqual(d.read_bytes(), before, "the owner's document was replaced")
            self.assertFalse(d.with_suffix(".md.merge-conflict").exists())
            self.assertIn("needs a person: merge held", p.stdout)
        self.assertEqual(self.base().read_text(), self.OWNER, "run rewrote the base")

    def test_an_add_only_merge_against_a_base_copied_from_the_owner_is_held(self):
        # Validator 2026-09-14: the owner deleted a section; the base was recorded from that
        # already-edited file, so the merge only ADDED the template's section back -- and
        # was applied, because nothing was removed.
        shipped = (TEMPLATES / "PROTOCOL.md").read_text()
        lines = shipped.splitlines(True)
        cut = next(i for i in range(len(lines) // 2, len(lines)) if lines[i].startswith("#"))
        owner = "".join(lines[:cut])          # the owner removed everything from here on
        self.assertNotEqual(owner, shipped)
        self.base().write_text(owner)
        self.doc().write_text(owner)
        before = self.doc().read_bytes()
        self.assertNotIn("doc-merge", self.up("status").stdout)
        p = self.up("run", "--allow-dirty")
        self.assertEqual(self.doc().read_bytes(), before, "a deleted section was re-inserted")
        self.assertIn("needs a person: merge held", p.stdout, p.stdout)

    def test_a_clean_merge_keeps_crlf_line_endings(self):
        from _harness import load_module
        gu = load_module(SCRIPTS / "gt_upgrade.py", "gt_upgrade_crlf")
        d = self.tmp / "crlf"
        d.mkdir()
        base, shipped, target = d / "base.md", d / "shipped.md", d / "doc.md"
        base.write_bytes(b"# T\r\n\r\none\r\ntwo\r\n")
        shipped.write_bytes(b"# T\r\n\r\none\r\ntwo\r\nthree\r\n")    # the release adds
        target.write_bytes(b"<!-- mine -->\r\n# T\r\n\r\none\r\ntwo\r\n")  # the owner adds
        rc, _msg = gu._merge3(target, base, shipped, False)
        self.assertEqual(rc, 0)
        self.assertEqual(target.read_bytes(),
                         b"<!-- mine -->\r\n# T\r\n\r\none\r\ntwo\r\nthree\r\n",
                         "the merge changed the document's line endings")

    def test_a_release_removing_a_section_the_owner_kept_is_held(self):
        shipped = (TEMPLATES / "PROTOCOL.md").read_text()
        self.base().write_text(shipped + "\n## A section the release dropped\n")
        d = self.doc()
        # the owner's edit at the top, far from the release's removal: a clean merge
        d.write_text("<!-- mine -->\n" + shipped + "\n## A section the release dropped\n")
        before = d.read_bytes()
        self.assertNotIn("doc-merge", self.up("status").stdout)
        p = self.up("run", "--allow-dirty")
        self.assertEqual(d.read_bytes(), before)
        self.assertIn("A section the release dropped", p.stdout, "the removed line was not shown")

    def test_dry_run_reports_lines_added_and_removed_not_a_net_count(self):
        shipped = (TEMPLATES / "PROTOCOL.md").read_text().splitlines(True)
        idx = next(i for i in range(len(shipped) // 3, len(shipped))
                   if shipped[i].strip() and not shipped[i].startswith("#"))
        base = "".join(shipped[:idx] + shipped[idx + 1:])
        self.base().write_text(base)
        self.doc().write_text(base + "\n## Mine\n")
        p = self.up("run", "--dry-run")
        self.assertIn("would merge cleanly (+1 added, -0 removed from your document)", p.stdout,
                      p.stdout)

    def test_record_base_repairs_a_base_recorded_from_the_owners_file(self):
        d = self.base_is_ours()
        before = d.read_bytes()
        p = self.up("run", "--allow-dirty", "--record-base", "PROTOCOL.md")
        self.assertOk(p)
        self.assertEqual(self.base().read_text(), (TEMPLATES / "PROTOCOL.md").read_text())
        self.assertEqual(d.read_bytes(), before)
        saved = list((self.home / ".claude" / "golden-thread" / "backups").glob("PROTOCOL.md.base.*"))
        self.assertEqual(len(saved), 1, "the replaced base was not backed up")
        self.assertEqual(saved[0].read_text(), self.OWNER)
        st = self.up("status")
        self.assertIn("nothing pending", st.stdout)
        self.assertNotIn("needs a person", st.stdout)

    def test_record_base_still_keeps_a_base_that_merges_without_loss(self):
        shipped = (TEMPLATES / "PROTOCOL.md").read_text()
        kept = shipped.replace("\n", "\n\n", 1)
        self.base().write_text(kept)
        self.doc().write_text(shipped + "\n## Mine\n")
        p = self.up("run", "--allow-dirty", "--dry-run", "--record-base", "PROTOCOL.md")
        self.assertIn("already has a merge base", p.stdout)
        self.assertEqual(self.base().read_text(), kept)

    def test_accept_merge_applies_a_held_merge_when_a_person_says_so(self):
        self.base_is_ours()
        p = self.up("run", "--allow-dirty", "--accept-merge", "PROTOCOL.md")
        self.assertOk(p)
        self.assertEqual(self.doc().read_text(), (TEMPLATES / "PROTOCOL.md").read_text())
        self.assertNotIn("needs a person", self.up("status").stdout)


if __name__ == "__main__":
    unittest.main()
