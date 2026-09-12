"""gt_paths.py -- how every Core-rule hook finds the vault and its core-rules folder.

Contract (from the module docstring):
  vault:       $GT_VAULT -> ~/.claude/vault-config.json:vault_path -> None
  core-rules:  config core_rules_path (vault-relative) -> search the vault for a
               'core-rules' dir holding core_rule_priority_model.md -> None
  record=True re-records a self-healed location in vault-config.json.

The module reads HOME at import time (CONFIG), so it is imported from a copy inside
the sandbox with HOME already pointed at the throwaway home. Nothing is imported from
the plugin tree, so no __pycache__ lands there.
"""
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from _harness import Sandbox, HOOKS, SCRIPTS, load_module

MODEL = "core_rule_priority_model.md"


def rule(level="core", enforcement="reminder", imperative=None, body="**Do the thing.**",
         inject=None):
    fm = ["---", "name: x", "metadata:", f"  level: {level}", f"  enforcement: {enforcement}"]
    if imperative is not None:
        fm.append(f'imperative: "{imperative}"')
    if inject is not None:
        fm.append(f"inject: {inject}")
    fm.append("---")
    return "\n".join(fm) + "\n\n" + body + "\n"


class GtPathsTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        # gt_paths.py ships from scripts/ and is INSTALLED into the hooks dir by
        # install.sh. Until 0.12.8 a stale second copy also sat in hooks/, and these
        # tests copied that one -- which is part of why the duplicate survived so
        # long: the suite depended on it. The hooks dir is a destination, not a source.
        shutil.copy2(SCRIPTS / "gt_paths.py", self.hooks / "gt_paths.py")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GT_")}
        env["HOME"] = str(self.home)
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.gp = load_module(self.hooks / "gt_paths.py", "gt_paths_under_test")
        self.cfg_path = self.home / ".claude" / "vault-config.json"

    def tearDown(self):
        self._env.stop()
        super().tearDown()

    # -- fixtures ---------------------------------------------------------------
    def vault(self, name="vault", core_rel="Projects/golden-thread/core-rules"):
        v = self.tmp / name
        (v / "Projects").mkdir(parents=True)
        if core_rel:
            c = v / core_rel
            c.mkdir(parents=True)
            (c / MODEL).write_text(rule(), encoding="utf-8")
            (c / "core_alpha.md").write_text(rule(), encoding="utf-8")
        return v

    def read_cfg(self):
        return json.loads(self.cfg_path.read_text(encoding="utf-8"))

    # -- the config is read from the sandbox, not the developer's home ------------
    def test_config_path_is_under_sandbox_home(self):
        self.assertEqual(self.gp.CONFIG, self.home / ".claude" / "vault-config.json")

    # -- find_vault ---------------------------------------------------------------
    def test_find_vault_from_config(self):
        v = self.vault()
        self.config(vault_path=str(v))
        self.assertEqual(self.gp.find_vault(), v)

    def test_gt_vault_env_wins_over_config(self):
        a, b = self.vault("a"), self.vault("b")
        self.config(vault_path=str(a))
        os.environ["GT_VAULT"] = str(b)
        self.assertEqual(self.gp.find_vault(), b)

    def test_gt_vault_env_that_is_not_a_dir_falls_back_to_config(self):
        a = self.vault("a")
        self.config(vault_path=str(a))
        os.environ["GT_VAULT"] = str(self.tmp / "no-such-dir")
        self.assertEqual(self.gp.find_vault(), a)

    def test_no_vault_when_config_missing_malformed_or_stale(self):
        self.assertIsNone(self.gp.find_vault(), "no config at all")
        self.cfg_path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.gp.find_vault(), "malformed config must not raise")
        self.config(vault_path=str(self.tmp / "gone"))
        self.assertIsNone(self.gp.find_vault(), "vault_path that no longer exists")
        self.assertIsNone(self.gp.find_core_rules())
        self.assertEqual(self.gp.core_rule_files(), [])

    # -- find_core_rules ----------------------------------------------------------
    def test_recorded_core_rules_path_is_used(self):
        v = self.vault()
        self.config(vault_path=str(v), core_rules_path="Projects/golden-thread/core-rules")
        self.assertEqual(self.gp.find_core_rules(), v / "Projects/golden-thread/core-rules")

    def test_stale_recorded_path_self_heals_by_marker_search(self):
        v = self.vault(core_rel="Projects/renamed-gt/core-rules")
        self.config(vault_path=str(v), core_rules_path="Projects/golden-thread/core-rules",
                    other_key="kept")
        before = self.cfg_path.read_text(encoding="utf-8")
        found = self.gp.find_core_rules(record=False)
        self.assertEqual(found, v / "Projects/renamed-gt/core-rules")
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), before,
                         "record=False must not rewrite vault-config.json")

        self.gp.find_core_rules(record=True)
        cfg = self.read_cfg()
        self.assertEqual(cfg["core_rules_path"], "Projects/renamed-gt/core-rules")
        self.assertEqual(cfg["vault_path"], str(v))
        self.assertEqual(cfg["other_key"], "kept", "re-recording must keep unrelated keys")

    def test_core_rules_dir_without_marker_file_is_ignored(self):
        v = self.vault(core_rel=None)
        decoy = v / "Projects" / "aaa" / "core-rules"      # sorts first, but no marker
        decoy.mkdir(parents=True)
        (decoy / "core_fake.md").write_text(rule(), encoding="utf-8")
        real = v / "Projects" / "zzz" / "core-rules"
        real.mkdir(parents=True)
        (real / MODEL).write_text(rule(), encoding="utf-8")
        self.config(vault_path=str(v))
        self.assertEqual(self.gp.find_core_rules(), real)

    def test_no_core_rules_anywhere(self):
        v = self.vault(core_rel=None)
        self.config(vault_path=str(v))
        self.assertIsNone(self.gp.find_core_rules(record=True))
        self.assertNotIn("core_rules_path", self.read_cfg(), "nothing found, nothing recorded")

    def test_explicit_vault_argument_is_honoured(self):
        v = self.vault()
        # no config at all: the caller hands the vault in
        self.assertEqual(self.gp.find_core_rules(v), v / "Projects/golden-thread/core-rules")

    # -- core_rule_files ----------------------------------------------------------
    def test_core_rule_files_only_core_md_sorted(self):
        v = self.vault()
        c = v / "Projects/golden-thread/core-rules"
        (c / "core_beta.md").write_text(rule(), encoding="utf-8")
        (c / "README.md").write_text("# readme\n", encoding="utf-8")
        (c / "enforcement.md").write_text("# wiring\n", encoding="utf-8")
        (c / "core_notes.txt").write_text("x\n", encoding="utf-8")
        (c / "core_dir.md").mkdir()
        names = [p.name for p in self.gp.core_rule_files(c)]
        self.assertEqual(names, ["core_alpha.md", "core_beta.md", MODEL])

    # -- parse_rule ---------------------------------------------------------------
    def test_parse_rule_explicit_imperative_and_nested_metadata(self):
        p = self.tmp / "core_x.md"
        p.write_text(rule(level="core", enforcement="validated",
                          imperative="Always do X.", body="**Not this one.**", inject="false"),
                     encoding="utf-8")
        r = self.gp.parse_rule(p)
        self.assertEqual(r["name"], "core_x")
        self.assertEqual(r["level"], "core")
        self.assertEqual(r["enforcement"], "validated")
        self.assertEqual(r["imperative"], "Always do X.", "quotes stripped, frontmatter wins")
        self.assertEqual(r["inject"], "false")

    def test_parse_rule_imperative_falls_back_to_first_bold_statement(self):
        p = self.tmp / "core_y.md"
        p.write_text(rule(body="# Title\n\n**Begin every reply\nwith the time.** More.\n\n"
                               "**Tier:** Core"), encoding="utf-8")
        self.assertEqual(self.gp.parse_rule(p)["imperative"], "Begin every reply with the time.")

    def test_parse_rule_without_frontmatter_has_no_level(self):
        p = self.tmp / "core_z.md"
        p.write_text("level: core\n\n**Body rule.**\n", encoding="utf-8")
        r = self.gp.parse_rule(p)
        self.assertNotIn("level", r, "keys are read from frontmatter only, not the body")
        self.assertEqual(r["imperative"], "Body rule.")

    def test_parse_rule_unreadable_file_is_empty(self):
        self.assertEqual(self.gp.parse_rule(self.tmp / "missing.md"), {})

    def test_parse_rule_reads_every_shipped_core_rule(self):
        # Every shipped rule except the model must yield level=core and an imperative;
        # otherwise the injector silently drops it.
        src = HOOKS.parent / "templates" / "core-rules"
        for p in sorted(src.glob("core_*.md")):
            if p.name == MODEL:
                continue
            with self.subTest(rule=p.name):
                r = self.gp.parse_rule(p)
                self.assertEqual(r.get("level"), "core")
                self.assertTrue(r.get("imperative"))
                self.assertIn(r.get("enforcement"), ("reminder", "validated"))


class GtPathsCliTest(Sandbox):
    """`python3 gt_paths.py` -- the self-heal the injector runs before every turn."""

    def setUp(self):
        super().setUp()
        self.hooks = self.home / ".claude" / "golden-thread" / "hooks"
        self.hooks.mkdir(parents=True)
        # gt_paths.py ships from scripts/ and is INSTALLED into the hooks dir by
        # install.sh. Until 0.12.8 a stale second copy also sat in hooks/, and these
        # tests copied that one -- which is part of why the duplicate survived so
        # long: the suite depended on it. The hooks dir is a destination, not a source.
        shutil.copy2(SCRIPTS / "gt_paths.py", self.hooks / "gt_paths.py")
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def run_cli(self):
        proc = self.py(self.hooks / "gt_paths.py")
        self.assertOk(proc)
        return json.loads(proc.stdout)

    def test_reports_real_vault_and_its_rules(self):
        v = self.make_vault()
        out = self.run_cli()
        self.assertEqual(Path(out["vault"]).resolve(), v.resolve())
        self.assertTrue(out["core_rules"].endswith("Projects/golden-thread/core-rules"))
        self.assertIn("core_timestamp_every_message", out["rules"])
        self.assertIn("core_rule_priority_model", out["rules"])

    def test_cli_records_a_moved_core_rules_folder(self):
        v = self.make_vault()
        old = v / "Projects" / "golden-thread" / "core-rules"
        new = v / "Projects" / "memory-system" / "core-rules"
        new.parent.mkdir(parents=True)
        old.rename(new)
        out = self.run_cli()
        self.assertTrue(out["core_rules"].endswith("Projects/memory-system/core-rules"))
        cfg = json.loads((self.home / ".claude" / "vault-config.json").read_text())
        self.assertEqual(cfg["core_rules_path"], "Projects/memory-system/core-rules")

    def test_no_vault_prints_nulls_and_writes_nothing(self):
        out = self.run_cli()
        self.assertEqual(out, {"vault": None, "core_rules": None, "rules": []})
        self.assertFalse((self.home / ".claude" / "vault-config.json").exists())


if __name__ == "__main__":
    unittest.main()
