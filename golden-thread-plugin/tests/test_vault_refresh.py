"""scripts/vault_refresh.py — install.sh's in-vault refreshes never destroy the owner's files.

A fresh-context validation of 0.15.0 (2026-09-14) reproduced, against vaults built by
older releases, every loss pinned below. Each test states the owner's edit first and
asserts it survives -- the negative case -- before the refresh that is allowed to happen:
  * an UNCOMMITTED edit to a .githooks/ hook was overwritten with no backup anywhere;
  * an owner-edited vault tool OLDER on disk than the release's copy was replaced as
    "stale" (and a pristine older-release copy seeded after the release was unpacked
    was reported "AHEAD" instead of updated);
  * a custom core.hooksPath was overwritten with .githooks;
  * the backup install.sh relied on was taken after those writes, so it lacked them.
And the list those decisions rest on, templates/shipped-hashes.json, must cover every
tool and hook text in the tree, or a pristine copy would read as an owner edit.
"""
import hashlib
import json
import os
import tarfile

from _harness import Sandbox, SCRIPTS, TEMPLATES, REPO

REFRESH = SCRIPTS / "vault_refresh.py"


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.git_init(self.make_vault().resolve(), commit=False)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "vault")
        self.tools = self.vault / "Projects" / "golden-thread" / "tools"
        self.hooks = self.vault / ".githooks"

    def git(self, *args):
        return self.run_cmd(["git", "-C", self.vault, *args])

    def refresh(self, *extra):
        p = self.py(REFRESH, "refresh", "--vault", self.vault, *extra)
        self.assertOk(p, "vault_refresh refresh failed")
        return p.stdout

    @property
    def backups(self):
        return self.home / ".claude" / "golden-thread" / "backups"


class ShippedHashesCoverTheTree(Base):
    def test_every_template_tool_and_hook_in_every_version_dir_is_listed(self):
        listed = json.loads((TEMPLATES / "shipped-hashes.json").read_text(encoding="utf-8"))
        missing = []
        for tdir in sorted((REPO / "golden-thread").glob("*/templates")):
            for kind in ("tools", "githooks"):
                for f in sorted((tdir / kind).glob("*")):
                    if f.is_file() and f.suffix != ".pyc" and \
                            sha(f.read_bytes()) not in listed.get(kind, {}).get(f.name, []):
                        missing.append(str(f.relative_to(REPO)))
        self.assertEqual(missing, [], "not in shipped-hashes.json -- run "
                                      "dev/shipped_hashes.py <release-dir>")

    def test_the_generator_check_passes(self):
        p = self.py(REPO / "dev" / "shipped_hashes.py", TEMPLATES.parent, "--check")
        self.assertOk(p, "templates/shipped-hashes.json is stale")


class GitHooks(Base):
    def test_uncommitted_hook_edit_is_kept(self):
        hook = self.hooks / "post-commit"
        edited = hook.read_bytes() + b"\n# owner: also notify my tool\n"
        hook.write_bytes(edited)                       # not committed
        out = self.refresh()
        self.assertEqual(hook.read_bytes(), edited, "an uncommitted hook edit was overwritten")
        self.assertIn("Git hook KEPT → .githooks/post-commit has uncommitted changes", out)

    def test_committed_hook_edit_is_kept_like_a_tool(self):
        # Validator 2026-09-14: committed hook edits were replaced (backed up) while an
        # owner-edited vault tool was kept. One rule now: only a shipped copy is replaced.
        hook = self.hooks / "post-commit"
        edited = hook.read_bytes() + b"\n# owner edit, committed\n"
        hook.write_bytes(edited)
        self.git("commit", "-qam", "owner hook edit")
        out = self.refresh()
        self.assertEqual(hook.read_bytes(), edited, "a committed hook edit was replaced")
        self.assertIn("Git hook MODIFIED LOCALLY → .githooks/post-commit", out)
        self.assertNotIn("REPLACED", out)

    def test_an_earlier_shipped_hook_is_updated(self):
        hook = self.hooks / "post-commit"
        old = hook.read_bytes() + b"\n# an earlier release\n"
        hook.write_bytes(old)
        self.git("commit", "-qam", "older shipped hook")
        from _harness import load_module
        mod = load_module(REFRESH, "vault_refresh_hooks_update")
        known = mod.shipped()
        known["githooks"].setdefault("post-commit", set()).add(mod.sha(hook))
        said = []
        mod.refresh_githooks(self.vault, known["githooks"], False, said.append)
        self.assertEqual(hook.read_bytes(), (TEMPLATES / "githooks" / "post-commit").read_bytes())
        self.assertIn("Updated git hook → .githooks/post-commit (was an earlier release's copy)",
                      said)
        self.assertTrue(os.access(hook, os.X_OK))

    def test_pristine_hook_is_left_quiet(self):
        out = self.refresh()
        self.assertNotIn("Git hook", out)
        self.assertFalse(self.backups.exists() and list(self.backups.glob("githook-*")))


class VaultTools(Base):
    def test_owner_edited_tool_older_on_disk_is_kept(self):
        tool = self.tools / "gt_tasks.py"
        edited = tool.read_bytes() + b"\n# OWNER LOCAL FIX\n"
        tool.write_bytes(edited)
        os.utime(tool, (1_000_000, 1_000_000))   # older than any template: 0.14.0 replaced it
        out = self.refresh()
        self.assertEqual(tool.read_bytes(), edited, "the owner's edit to a vault tool was replaced")
        self.assertIn("Vault tool MODIFIED LOCALLY → gt_tasks.py matches no version gt has "
                      "shipped; kept.", out)

    def test_earlier_release_copy_newer_on_disk_is_updated_not_ahead(self):
        listed = json.loads((TEMPLATES / "shipped-hashes.json").read_text(encoding="utf-8"))
        tool = self.tools / "gt_tasks.py"
        text = tool.read_bytes() + b"\n# an earlier release's text\n"
        listed["tools"]["gt_tasks.py"].append(sha(text))     # gt shipped this text once
        hashes = self.tmp / "shipped.json"
        hashes.write_text(json.dumps(listed), encoding="utf-8")
        tool.write_bytes(text)                  # written now: NEWER on disk than the template
        out = self.refresh("--shipped", hashes)
        self.assertNotIn("AHEAD", out)
        self.assertIn("Vault tool UPDATED → gt_tasks.py", out)
        self.assertEqual(tool.read_bytes(), (TEMPLATES / "tools" / "gt_tasks.py").read_bytes())

    def test_dry_run_writes_nothing(self):
        tool = self.tools / "gt_log.py"
        tool.unlink()
        out = self.refresh("--dry-run")
        self.assertFalse(tool.exists())
        self.assertIn("Seeded vault tool → gt_log.py", out)


class HooksPath(Base):
    def test_owner_hooks_path_is_left_alone(self):
        self.git("config", "core.hooksPath", "my-hooks")
        out = self.refresh()
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), "my-hooks")
        self.assertIn("core.hooksPath is 'my-hooks' (yours)", out)

    def test_unset_hooks_path_is_wired(self):
        self.git("config", "--unset", "core.hooksPath")
        out = self.refresh()
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), ".githooks")
        self.assertIn("Wired vault git attribution", out)


class BackupBeforeWrites(Base):
    def test_backup_holds_the_originals_and_is_kept_only_when_something_changed(self):
        rule = next((self.vault / "Projects" / "golden-thread" / "core-rules").glob("core_*.md"))
        original = rule.read_bytes()
        bak = self.backups / "install-vault-files-test.tar.gz"
        self.assertOk(self.py(REFRESH, "backup", "--vault", self.vault, "--out", bak))
        # untouched -> pruned
        p = self.py(REFRESH, "prune", "--vault", self.vault, "--backup", bak)
        self.assertOk(p)
        self.assertFalse(bak.exists(), "a backup of an untouched vault was left behind")
        # changed -> kept, with the original inside
        self.assertOk(self.py(REFRESH, "backup", "--vault", self.vault, "--out", bak))
        rule.write_bytes(b"replaced by an install\n")
        p = self.py(REFRESH, "prune", "--vault", self.vault, "--backup", bak)
        self.assertTrue(bak.exists())
        self.assertIn("changed: %s" % rule.relative_to(self.vault), p.stdout)
        with tarfile.open(bak) as tf:
            self.assertEqual(tf.extractfile(str(rule.relative_to(self.vault))).read(), original)


if __name__ == "__main__":
    import unittest
    unittest.main()
