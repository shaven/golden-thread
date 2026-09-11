"""gt-wiki vault_init.py, wiki mode: builds a standalone LLM Wiki vault.

Contracts pinned here:
  * wiki mode lays down Sources/, Knowledge/, index.md, log.md, CLAUDE.md (domain
    filled) and Knowledge/_template.md, and nothing from flow mode;
  * it is idempotent and never overwrites an existing file;
  * the config is written once; a config naming a DIFFERENT vault is a conflict
    (exit 3) and is left byte-identical, as is a config that is not JSON.
"""
import json
import os
import unittest

from _harness import Sandbox, SCRIPTS, WIKI_SCRIPTS

INIT = WIKI_SCRIPTS / "vault_init.py"
PLACEHOLDER = "<YOUR DOMAIN HERE>"


class WikiVaultInitTest(Sandbox):
    def init(self, vault, *extra):
        return self.py(INIT, "wiki", "--vault", vault, *extra)

    def cfg_path(self):
        return self.home / ".claude" / "vault-config.json"

    def test_wiki_mode_builds_the_vault(self):
        v = self.tmp / "wiki"
        p = self.init(v, "--domain", "Pizza Ovens")
        self.assertOk(p)
        for d in ("Sources", "Knowledge"):
            self.assertTrue((v / d).is_dir(), d)
        for f in ("index.md", "log.md", "CLAUDE.md", "Knowledge/_template.md"):
            self.assertTrue((v / f).is_file(), f)
        claude = (v / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("Pizza Ovens", claude)
        self.assertNotIn(PLACEHOLDER, claude)
        log = (v / "log.md").read_text(encoding="utf-8")
        for op in ("ingest", "query", "lint", "refresh", "graduate", "retire", "relocate"):
            self.assertIn(op, log, "log stub does not state the closed vocabulary")
        self.assertFalse((v / "Projects").exists(), "wiki mode created flow-mode Projects/")
        # config written into the SANDBOX home, pointing at the absolute vault path
        cfg = json.loads(self.cfg_path().read_text(encoding="utf-8"))
        self.assertEqual(cfg["vault_path"], os.path.abspath(str(v)))

    @unittest.expectedFailure  # defect: 2026-09-11-wiki-vault-init-config-handling
    def test_without_domain_the_placeholder_stays_and_is_reported(self):
        v = self.tmp / "wiki"
        p = self.init(v)
        self.assertOk(p)
        self.assertIn(PLACEHOLDER, (v / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertIn("placeholder NOT filled", p.stdout)

    def test_second_run_is_idempotent_and_overwrites_nothing(self):
        v = self.tmp / "wiki"
        self.assertOk(self.init(v, "--domain", "D"))
        (v / "index.md").write_text("# Index\n\n- [[Mine]] — user content\n", encoding="utf-8")
        (v / "CLAUDE.md").write_text("hand edited\n", encoding="utf-8")
        p = self.init(v, "--domain", "Other", "--json")
        self.assertOk(p)
        rep = json.loads(p.stdout)
        self.assertTrue(rep["ok"])
        self.assertEqual([r for r in rep["results"] if r["action"] == "created"], [],
                         "second run created something")
        self.assertIn("user content", (v / "index.md").read_text(encoding="utf-8"))
        self.assertEqual((v / "CLAUDE.md").read_text(encoding="utf-8"), "hand edited\n")

    def test_explicit_config_path(self):
        v = self.tmp / "wiki"
        cfg = self.tmp / "elsewhere" / "cfg.json"
        self.assertOk(self.init(v, "--config", cfg))
        self.assertEqual(json.loads(cfg.read_text())["vault_path"], os.path.abspath(str(v)))
        self.assertFalse(self.cfg_path().exists(), "default config written despite --config")

    def test_config_for_another_vault_is_a_conflict_and_untouched(self):
        self.config(vault_path="/some/other/vault")
        before = self.cfg_path().read_bytes()
        p = self.init(self.tmp / "wiki", "--json")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        rep = json.loads(p.stdout)
        self.assertFalse(rep["ok"])
        conflict = [r for r in rep["results"] if r["action"] == "conflict"]
        self.assertEqual(len(conflict), 1)
        self.assertIn("/some/other/vault", conflict[0]["note"])
        self.assertEqual(self.cfg_path().read_bytes(), before)

    def test_invalid_json_config_is_a_conflict_and_untouched(self):
        self.cfg_path().write_text("{not json", encoding="utf-8")
        p = self.init(self.tmp / "wiki")
        self.assertEqual(p.returncode, 3)
        self.assertEqual(self.cfg_path().read_text(), "{not json")

    def test_config_already_pointing_here_is_skipped(self):
        v = self.tmp / "wiki"
        self.config(vault_path=os.path.abspath(str(v)), install_demo="no")
        before = self.cfg_path().read_bytes()
        self.assertOk(self.init(v))
        self.assertEqual(self.cfg_path().read_bytes(), before, "matching config was rewritten")

    @unittest.expectedFailure  # defect: 2026-09-11-wiki-vault-init-config-handling
    def test_config_holding_only_other_settings_is_not_a_vault_conflict(self):
        # install.sh documents install_demo=no in vault-config.json, which a user can
        # set before any vault exists. A config naming NO vault is not a second vault.
        self.config(install_demo="no")
        v = self.tmp / "wiki"
        p = self.init(v)
        self.assertOk(p, "a vault-config.json with no vault_path was reported as a conflict "
                         "with vault None")
        cfg = json.loads(self.cfg_path().read_text())
        self.assertEqual(cfg.get("vault_path"), os.path.abspath(str(v)))
        self.assertEqual(cfg.get("install_demo"), "no", "existing setting was dropped")

    @unittest.expectedFailure  # defect: 2026-09-11-wiki-vault-init-config-handling
    def test_same_vault_through_a_symlink_is_not_a_conflict_with_gt_init(self):
        # gt's vault_init.py records the RESOLVED path; a wiki init of the same vault
        # reached through a symlink (a ~/Dropbox link, macOS /var -> /private/var)
        # must recognise it as the same vault.
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        self.assertOk(self.py(SCRIPTS / "vault_init.py", "fresh", "--vault", link / "vault",
                              "--domain", "D"))
        p = self.init(link / "vault", "--domain", "D")
        self.assertOk(p, "wiki init reported the vault gt init had just configured as a "
                         "different vault (abspath vs resolved path)")


if __name__ == "__main__":
    unittest.main()
