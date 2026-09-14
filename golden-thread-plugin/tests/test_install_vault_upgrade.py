"""install.sh applies pending vault upgrades when the vault is clean (0.14.0).

Owner decision 2026-09-14, "Apply when clean", for Requirement R1 (an install from any
older release equals a fresh install of the newest). Contracts pinned here:
  * a vault THIS install created gets an initial commit (with a one-off identity when
    git has none configured, global config untouched), then its pending steps applied;
  * an existing clean git vault: pending steps applied after a backup tarball, results
    left UNCOMMITTED, "Review and commit the vault" printed;
  * a vault with uncommitted changes: nothing applied, the owner's file byte-identical,
    the reason and the exact command printed;
  * a vault that is not a git repo: pending reported only, with that reason;
  * a conflicting doc merge: document untouched, "Run /gt:gt-upgrade to finish";
  * an owner document with NO merge base: untouched and reported on every install, never
    applied, no base recorded;
  * "clean" is decided from a snapshot taken BEFORE the install writes into the vault, so
    the install's own refreshes (a replaced vault tool) do not block the upgrade -- and
    they are never staged or committed;
  * gt_upgrade failing or crashing mid-run: the install still exits 0 with a reason, the
    backup exists and no file is left half-written;
  * nothing pending: "Vault upgrades: none pending", and the vault is not re-stamped.
"""
import os
import shutil
import subprocess

from _harness import GIT_ID, TEMPLATES
from test_install_prune import PruneBase

BASE = ("Projects", "golden-thread", ".templates", "PROTOCOL.md")
LOCAL = "\n## A section this vault added\n"


class VaultUpgradeBase(PruneBase):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git not installed")

    @property
    def up(self):
        return self.src / "scripts" / "gt_upgrade.py"

    @property
    def backups(self):
        return sorted((self.home / ".claude" / "golden-thread" / "backups").glob("vault-*.tar.gz"))

    def git(self, v, *args):
        return self.run_cmd(["git", "-C", v, *args])

    def porcelain(self, v):
        return self.git(v, "status", "--porcelain").stdout.strip()

    def head(self, v):
        return self.git(v, "rev-parse", "HEAD").stdout.strip()

    def commit(self, v, msg="fixture"):
        self.assertOk(self.git(v, "add", "-A"))
        self.assertOk(self.git(v, "commit", "-q", "--allow-empty", "-m", msg))

    def current_vault(self):
        """An existing vault, committed, fully upgraded, and clean after one install."""
        v = self.make_vault()
        self.config(vault_path=str(v))
        self.assertOk(self.py(self.up, "run", "--vault", v, "--allow-dirty"))
        self.commit(v)
        self.install()                            # anything the installer seeds, committed
        self.commit(v, "after first install")
        self.assertEqual(self.porcelain(v), "", "fixture: vault not clean")
        self.assertIn("nothing pending", self.py(self.up, "status", "--vault", v).stdout)
        return v

    def make_doc_merge_pending(self, v):
        """The release changed PROTOCOL.md since this vault's base; the vault has its own edit.

        -> (protocol path, the line the release added)."""
        shipped = (TEMPLATES / "PROTOCOL.md").read_text(encoding="utf-8").splitlines(True)
        idx = next(i for i in range(len(shipped) // 3, len(shipped))
                   if shipped[i].strip() and not shipped[i].startswith("#"))
        base = "".join(shipped[:idx] + shipped[idx + 1:])
        v.joinpath(*BASE).write_text(base, encoding="utf-8")
        protocol = v / "Projects" / "PROTOCOL.md"
        protocol.write_text(base + LOCAL, encoding="utf-8")
        return protocol, shipped[idx]


class FreshInstallCreatesAndCommits(VaultUpgradeBase):
    def test_created_vault_gets_an_initial_commit_and_upgrades_applied(self):
        target = self.tmp / "newvault"
        env = {k: v for k, v in self.env.items() if k not in GIT_ID}
        env["GIT_CONFIG_NOSYSTEM"] = "1"          # no identity from anywhere
        p = subprocess.run(["bash", str(self.repo / "install.sh"), "--vault", str(target)],
                           env=env, capture_output=True, text=True, timeout=300)
        self.assertOk(p, "install.sh --vault failed")
        log = self.git(target, "log", "--format=%an|%s").stdout.strip().splitlines()
        self.assertEqual(len(log), 1, log)
        self.assertIn("Golden Thread vault created by install.sh %s" % self.src.name, log[0])
        self.assertIn("Committed the new vault", p.stdout)
        self.assertFalse((self.home / ".gitconfig").exists(), "global git config was written")
        self.assertIn("Vault upgrades: none pending", p.stdout)
        self.assertIn("nothing pending", self.py(self.up, "status", "--vault", target).stdout)
        # Born current (regression 2026-09-14): the install must not migrate the vault it
        # just created and leave the result uncommitted.
        self.assertNotIn("Vault upgrades — applying", p.stdout)
        self.assertNotIn("Uncommitted for your review", p.stdout)
        self.assertEqual(self.porcelain(target), "", "the new vault was left with uncommitted files")

    def test_a_second_install_does_not_commit_again_or_touch_a_clean_vault(self):
        target = self.tmp / "newvault"
        self.install("--vault", str(target))
        self.commit(target, "reviewed")
        head = self.head(target)
        p = self.install()
        self.assertNotIn("Committed the new vault", p.stdout)
        self.assertIn("Vault upgrades: none pending", p.stdout)
        self.assertEqual(self.head(target), head)
        self.assertEqual(self.porcelain(target), "", "an install with nothing pending dirtied the vault")


class CleanVaultIsUpgraded(VaultUpgradeBase):
    def test_pending_step_applied_backed_up_and_left_uncommitted(self):
        v = self.current_vault()
        protocol, added = self.make_doc_merge_pending(v)
        self.commit(v, "owner work")
        head, before = self.head(v), len(self.backups)
        p = self.install()
        text = protocol.read_text(encoding="utf-8")
        self.assertIn(added, text, "the release's change was not merged in")
        self.assertIn(LOCAL.strip(), text, "the owner's edit was lost")
        self.assertEqual(len(self.backups), before + 1, "no backup tarball")
        self.assertEqual(self.head(v), head, "the installer committed the upgrade")
        self.assertNotEqual(self.porcelain(v), "")
        self.assertIn("doc-merge", p.stdout)
        self.assertIn("Vault upgrades: none pending", p.stdout)
        self.assertIn('Review and commit the vault: git -C "%s" status' % v, p.stdout)


class InstallRefreshesDoNotBlock(VaultUpgradeBase):
    def test_clean_vault_with_a_stale_tool_still_gets_the_pending_step(self):
        v = self.current_vault()
        protocol, added = self.make_doc_merge_pending(v)
        tool = v / "Projects" / "golden-thread" / "tools" / "gt_tasks.py"
        tool.write_text(tool.read_text(encoding="utf-8") + "\n# an older copy\n", encoding="utf-8")
        self.commit(v, "owner work")
        os.utime(tool, (1_000_000, 1_000_000))      # older than the template: install replaces it
        head = self.head(v)
        p = self.install()
        self.assertIn("Vault tool REPLACED → gt_tasks.py", p.stdout)
        self.assertNotIn("not applied", p.stdout, "the install's own refresh blocked the upgrade")
        self.assertIn(added, protocol.read_text(encoding="utf-8"), "pending step not applied")
        self.assertIn("this install's own refreshes", p.stdout)
        self.assertIn("Uncommitted for your review: this install's vault file refreshes AND "
                      "the applied upgrades", p.stdout)
        self.assertIn('Review and commit the vault: git -C "%s" status' % v, p.stdout)
        self.assertEqual(self.head(v), head, "the installer committed")
        self.assertEqual(self.git(v, "diff", "--cached", "--name-only").stdout.strip(), "",
                         "the installer staged files")
        dirty = self.porcelain(v)
        self.assertIn("gt_tasks.py", dirty)
        self.assertIn("PROTOCOL.md", dirty)


class NoBaseDocumentNeedsAPerson(VaultUpgradeBase):
    def test_owner_edited_doc_without_a_base_survives_two_installs(self):
        v = self.current_vault()
        v.joinpath(*BASE).unlink()
        protocol = v / "Projects" / "PROTOCOL.md"
        protocol.write_bytes(protocol.read_bytes() + b"\n## Owner's own section\n")
        self.commit(v, "owner edit, from before bases existed")
        before, n = protocol.read_bytes(), len(self.backups)
        for attempt in (1, 2):
            p = self.install()
            self.assertEqual(protocol.read_bytes(), before, "install %d changed the document" % attempt)
            self.assertFalse(v.joinpath(*BASE).exists(), "install %d recorded a base" % attempt)
            self.assertIn("needs a person: no merge base — review %s against the shipped "
                          "template, then run /gt:gt-upgrade" % protocol, p.stdout)
            self.assertIn("Vault upgrades: none pending", p.stdout)
            self.assertNotIn("applying", p.stdout)
            self.assertEqual(self.porcelain(v), "", "install %d dirtied the vault" % attempt)
        self.assertEqual(len(self.backups), n, "gt_upgrade run was invoked")


class DirtyVaultIsLeftAlone(VaultUpgradeBase):
    def test_uncommitted_user_work_blocks_and_is_byte_identical(self):
        v = self.current_vault()
        protocol, _ = self.make_doc_merge_pending(v)
        self.commit(v, "owner work")
        mine = v / "Projects" / "my-draft.md"
        mine.write_bytes(b"half-written thought\r\nno newline at end")
        before_doc, before_mine, n = protocol.read_bytes(), mine.read_bytes(), len(self.backups)
        p = self.install()
        self.assertEqual(mine.read_bytes(), before_mine)
        self.assertEqual(protocol.read_bytes(), before_doc, "an upgrade was applied to a dirty vault")
        self.assertEqual(len(self.backups), n, "gt_upgrade ran on a dirty vault")
        self.assertIn("Vault upgrades pending, not applied — the vault has uncommitted changes "
                      "(yours are never touched).", p.stdout)
        self.assertIn("doc-merge", p.stdout)
        self.assertIn("/gt:gt-upgrade", p.stdout)
        self.assertIn('python3 "%s" --vault "%s" run' % (self.up, v), p.stdout)


class NonGitVaultIsReportedOnly(VaultUpgradeBase):
    def test_pending_reported_with_the_reason(self):
        v = self.make_vault()
        shutil.rmtree(v / ".git")
        # A pre-0.11 vault: a fresh one is born current since 0.14.0, so put its log.md
        # back to the un-migrated form to make log-spool pending.
        spool = v / "Projects" / "golden-thread" / "spool" / "log"
        (v / "log.md").write_bytes((spool / "0000-baseline.md").read_bytes())
        shutil.rmtree(spool)
        self.config(vault_path=str(v))
        p = self.install()
        self.assertIn("not a git repository", p.stdout)
        self.assertIn("log-spool", p.stdout)
        self.assertFalse((v / "Projects" / "golden-thread" / "spool" / "log").exists(),
                         "an upgrade was applied to a non-git vault")
        self.assertEqual(self.backups, [])
        self.assertFalse((v / ".git").exists(), "the installer created a git repo")


class ConflictNeedsAPerson(VaultUpgradeBase):
    def test_conflicting_merge_leaves_the_document_and_says_how_to_finish(self):
        v = self.current_vault()
        v.joinpath(*BASE).write_text("\n".join("base line %d" % i for i in range(40)) + "\n")
        protocol = v / "Projects" / "PROTOCOL.md"
        protocol.write_text("\n".join("vault line %d" % i for i in range(40)) + "\n")
        self.commit(v, "diverged")
        before = protocol.read_bytes()
        p = self.install()
        self.assertEqual(protocol.read_bytes(), before, "a conflicting merge rewrote the document")
        self.assertIn("CONFLICT", p.stdout)
        self.assertIn("Run /gt:gt-upgrade to finish", p.stdout)
        self.assertTrue((v / "Projects" / "PROTOCOL.md.merge-conflict").is_file())

    def test_a_second_install_does_not_redo_the_conflict(self):
        v = self.current_vault()
        v.joinpath(*BASE).write_text("\n".join("base line %d" % i for i in range(40)) + "\n")
        protocol = v / "Projects" / "PROTOCOL.md"
        protocol.write_text("\n".join("vault line %d" % i for i in range(40)) + "\n")
        self.commit(v, "diverged")
        self.install()
        conflict = v / "Projects" / "PROTOCOL.md.merge-conflict"
        self.assertTrue(conflict.is_file())
        snap = {f: f.read_bytes() for f in sorted(v.rglob("*")) if f.is_file() and ".git" not in f.parts}
        status, n = self.git(v, "status", "--porcelain", "--untracked-files=all").stdout, len(self.backups)
        p = self.install()
        self.assertEqual(self.git(v, "status", "--porcelain", "--untracked-files=all").stdout, status,
                         "the second install changed the vault")
        self.assertEqual({f: f.read_bytes() for f in sorted(v.rglob("*"))
                          if f.is_file() and ".git" not in f.parts}, snap,
                         "the second install rewrote vault files")
        self.assertEqual(len(self.backups), n, "gt_upgrade run was invoked again")
        self.assertIn("Vault upgrades: none pending", p.stdout)
        self.assertIn("needs a person: conflict awaiting you: %s — resolve, then /gt:gt-upgrade"
                      % conflict, p.stdout)


class FailuresNeverFailTheInstall(VaultUpgradeBase):
    def test_gt_upgrade_that_cannot_run(self):
        self.current_vault()
        self.up.write_text("raise SystemExit(3)\n")
        self.manifest()
        p = self.install()                      # asserts exit 0
        self.assertIn("could not run", p.stdout)
        self.assertIn("Restart Claude Code", p.stdout)

    def test_a_crash_mid_run_leaves_a_backup_and_no_half_written_file(self):
        v = self.current_vault()
        protocol, _ = self.make_doc_merge_pending(v)
        self.commit(v, "owner work")
        before = protocol.read_bytes()
        # The real script under another name; the shipped name crashes it hard (not an
        # Exception, so gt_upgrade's per-step handler cannot catch it) inside doc-merge.
        os.replace(self.up, self.up.with_name("_gt_upgrade_real.py"))
        self.up.write_text(
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
            "import _gt_upgrade_real as m\n"
            "if 'run' in sys.argv:\n"
            "    def boom(vault, dry):\n"
            "        raise KeyboardInterrupt('simulated crash')\n"
            "    m.MIGRATIONS = tuple((a, b, c, d, boom if b == 'doc-merge' else e)\n"
            "                         for a, b, c, d, e in m.MIGRATIONS)\n"
            "raise SystemExit(m.main())\n")
        self.manifest()
        p = self.install()                      # asserts exit 0
        self.assertIn("Vault upgrade stopped", p.stdout)
        self.assertIn("backed up before anything changed", p.stdout)
        self.assertTrue(self.backups, "no backup tarball")
        self.assertEqual(protocol.read_bytes(), before, "a crashed merge left a changed document")
        self.assertEqual([f for f in v.rglob("*.merging")], [], "a merge temp file was left")
        self.assertIn("Restart Claude Code", p.stdout)


class NothingPending(VaultUpgradeBase):
    def test_current_vault_says_none_pending_and_stays_clean(self):
        v = self.current_vault()
        p = self.install()
        self.assertIn("Vault upgrades: none pending", p.stdout)
        self.assertNotIn("applying", p.stdout)
        self.assertEqual(self.porcelain(v), "")

    def test_no_vault_prints_no_upgrade_line(self):
        p = self.install()
        self.assertNotIn("Vault upgrades", p.stdout)
