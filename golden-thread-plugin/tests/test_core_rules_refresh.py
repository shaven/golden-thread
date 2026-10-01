"""An EXISTING vault's Core-rule files converge to the release (0.17.11 release blocker).

Until 0.17.11 nothing refreshed a rule file a vault already had: install-core-rules skips
existing files and gt_upgrade had no step for it, so an upgraded vault went on injecting
the old rule-1 imperative every turn while the release's guard enforced queue-first.

Now, by content (the vault-tool rule): a vault copy byte-identical to some text gt shipped
is replaced; anything else is the owner's, kept and reported. Both paths that run it are
pinned: gt_upgrade's core-rules-refresh step and install.sh's vault_refresh refresh.
"""
from _harness import Sandbox, SCRIPTS, TEMPLATES, REPO, core_rules_dir

UPGRADE = SCRIPTS / "gt_upgrade.py"
REFRESH = SCRIPTS / "vault_refresh.py"
RULE1 = "core_concurrent_session_claim.md"
OLD = REPO / "golden-thread" / "0.17.10" / "templates" / "core-rules" / RULE1


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.vault = self.git_init(self.make_vault().resolve(), commit=False)
        self.rules = core_rules_dir(self.vault)
        self.assertNotEqual(OLD.read_bytes(), (TEMPLATES / "core-rules" / RULE1).read_bytes(),
                            "fixture: 0.17.10's rule 1 must differ from this release's")
        self.rule1 = self.rules / RULE1
        self.rule1.write_bytes(OLD.read_bytes())          # an unmodified 0.17.10 copy
        self.commit("vault")

    def git(self, *args):
        return self.run_cmd(["git", "-C", self.vault, *args])

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)

    def upgrade(self, *extra):
        p = self.py(UPGRADE, "run", "--vault", self.vault, *extra)
        self.assertNotIn("Traceback", p.stdout + p.stderr)
        return p

    def snapshot(self):
        return {f.name: f.read_bytes() for f in sorted(self.rules.glob("*.md"))}


class UpgradeStep(Base):
    def test_an_unmodified_old_shipped_rule_is_replaced(self):
        st = self.py(UPGRADE, "status", "--vault", self.vault).stdout
        self.assertIn("core-rules-refresh", st)
        p = self.upgrade()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.rule1.read_bytes(), (TEMPLATES / "core-rules" / RULE1).read_bytes())
        self.assertIn("Updated Core rule → %s" % RULE1, p.stdout)

    def test_a_locally_edited_rule_is_kept_and_reported(self):
        edited = OLD.read_bytes() + b"\n<!-- owner: my own wording -->\n"
        self.rule1.write_bytes(edited)
        self.commit("owner edit")
        p = self.upgrade()
        self.assertEqual(self.rule1.read_bytes(), edited, "an owner-edited rule was overwritten")
        self.assertIn("%s edited locally, not refreshed; diff against" % RULE1, p.stdout)
        self.assertIn("templates/core-rules/%s" % RULE1, p.stdout)
        # it must not hold the vault at "pending" forever
        st = self.py(UPGRADE, "status", "--vault", self.vault).stdout
        self.assertNotIn("core-rules-refresh", st)

    def test_a_new_rule_absent_from_the_vault_is_added(self):
        gone = self.rules / "core_verification_state.md"
        gone.unlink()
        self.commit("lose a rule")
        p = self.upgrade()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(gone.read_bytes(),
                         (TEMPLATES / "core-rules" / gone.name).read_bytes())

    def test_running_twice_changes_nothing(self):
        self.assertEqual(self.upgrade().returncode, 0)
        self.commit("upgraded")
        before = self.snapshot()
        p = self.upgrade()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("nothing pending", p.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        p = self.upgrade("--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Would update Core rule → %s" % RULE1, p.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")


class InstallRefreshPath(Base):
    """install.sh runs `vault_refresh.py refresh` on a git vault: the same convergence."""

    def refresh(self, *extra):
        p = self.py(REFRESH, "refresh", "--vault", self.vault, *extra)
        self.assertOk(p, "vault_refresh refresh failed")
        return p.stdout

    def test_refresh_replaces_old_shipped_rule_and_is_idempotent(self):
        out = self.refresh("--dry-run")
        self.assertEqual(self.rule1.read_bytes(), OLD.read_bytes(), "dry run wrote")
        self.assertIn("Would update Core rule → %s" % RULE1, out)
        out = self.refresh()
        self.assertIn("Updated Core rule → %s" % RULE1, out)
        self.assertEqual(self.rule1.read_bytes(), (TEMPLATES / "core-rules" / RULE1).read_bytes())
        before = self.snapshot()
        out = self.refresh()
        self.assertNotIn("Core rule", out)
        self.assertEqual(self.snapshot(), before)

    def test_refresh_keeps_an_edited_rule(self):
        edited = OLD.read_bytes() + b"\nowner\n"
        self.rule1.write_bytes(edited)
        out = self.refresh()
        self.assertEqual(self.rule1.read_bytes(), edited)
        self.assertIn("edited locally, not refreshed", out)


class EveryShippedRuleIsListed(Sandbox):
    def test_every_core_rule_in_every_version_dir_is_in_shipped_hashes(self):
        import hashlib
        import json
        listed = json.loads((TEMPLATES / "shipped-hashes.json").read_text(encoding="utf-8"))
        missing = [str(f.relative_to(REPO))
                   for f in sorted((REPO / "golden-thread").glob("*/templates/core-rules/*"))
                   if f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest()
                   not in listed.get("core-rules", {}).get(f.name, [])]
        self.assertEqual(missing, [], "run dev/shipped_hashes.py <release-dir>")


if __name__ == "__main__":
    import unittest
    unittest.main()
