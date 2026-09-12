"""vault_init.py: scaffold, connect, Core-rule install and project lifecycle.

Every run happens in a throwaway HOME; the hooks directory the tool looks for
(~/.claude/golden-thread/hooks) is faked inside that HOME when a test needs wiring.
"""
import json
import pathlib
import re
import shutil
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, ENFORCEMENT_HOOKS

VI = SCRIPTS / "vault_init.py"
HOOK_NAMES = ENFORCEMENT_HOOKS


class VaultInitBase(Sandbox):
    def vi(self, *args):
        return self.py(VI, *args)

    def vi_json(self, *args, expect=0):
        proc = self.vi(*args)
        self.assertEqual(proc.returncode, expect,
                         f"vault_init {args[0]} exit {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return json.loads(proc.stdout)

    def fake_hooks(self):
        d = self.home / ".claude" / "golden-thread" / "hooks"
        d.mkdir(parents=True, exist_ok=True)
        for n in HOOK_NAMES:
            (d / n).write_text("#!/bin/sh\nexit 0\n")
            (d / n).chmod(0o755)
        return d

    def cfg(self):
        return json.loads((self.home / ".claude" / "vault-config.json").read_text())

    def project(self, v, slug, *extra):
        return self.vi_json("create-project", "--vault", v, "--name", slug, *extra)

    @staticmethod
    def actions(results, action):
        return [r for r in results if r["action"] == action]


# ---------------------------------------------------------------------------- fresh
class FreshTest(VaultInitBase):
    def test_fresh_scaffolds_a_complete_vault(self):
        v = self.make_vault()
        for d in ("Knowledge", "Sources", "global-memory", "Projects",
                  "Projects/golden-thread/core-rules", "Projects/golden-thread/tools", ".githooks"):
            self.assertTrue((v / d).is_dir(), d)
        for f in ("CLAUDE.md", "index.md", "log.md", "review-queue.md", "INBOX.md", "TASKS.md",
                  "lint-declines.md", "global-memory/MEMORY.md", "Projects/README.md",
                  "Projects/CONVENTIONS.md", "Projects/PROTOCOL.md", "Projects/INFRASTRUCTURE.md",
                  "Projects/golden-thread/README.md",
                  "Projects/golden-thread/core-rules/core_rule_priority_model.md",
                  "Projects/golden-thread/tools/gt_tasks.py"):
            self.assertTrue((v / f).is_file(), f)
        cfg = self.cfg()
        self.assertEqual(cfg["vault_path"], str(v.resolve()))
        self.assertEqual(cfg["core_rules_path"], "Projects/golden-thread/core-rules")
        # domain substituted into the templates
        self.assertIn("Test", (v / "index.md").read_text())
        self.assertNotIn("{{DOMAIN}}", (v / "CLAUDE.md").read_text())
        # global CLAUDE.md in the sandbox HOME points at this vault
        g = (self.home / ".claude" / "CLAUDE.md").read_text()
        self.assertIn("## Golden Thread", g)
        self.assertIn(f"{v.resolve()}/global-memory/MEMORY.md", g)
        # git repo with attribution hooks pointed at
        if shutil.which("git"):
            hp = self.run_cmd(["git", "-C", v, "config", "--get", "core.hooksPath"])
            self.assertEqual(hp.stdout.strip(), ".githooks")

    def test_fresh_is_idempotent_and_preserves_edits(self):
        v = self.make_vault()
        (v / "index.md").write_text("# my own index\n")
        before = (v / "CLAUDE.md").read_text()
        res = self.vi_json("fresh", "--vault", v, "--domain", "Other")
        created = {Path(r["path"]).name for r in self.actions(res, "created")}
        for name in ("CLAUDE.md", "index.md", "log.md", "INBOX.md", "README.md"):
            self.assertNotIn(name, created, f"{name} re-created on second run")
        self.assertEqual((v / "index.md").read_text(), "# my own index\n")
        self.assertEqual((v / "CLAUDE.md").read_text(), before)
        g = (self.home / ".claude" / "CLAUDE.md").read_text()
        self.assertEqual(g.count("## Golden Thread"), 1, "global CLAUDE.md section duplicated")

    def test_fresh_appends_to_existing_global_claude_md(self):
        (self.home / ".claude" / "CLAUDE.md").write_text("# Mine\n\nkeep me\n")
        self.make_vault()
        g = (self.home / ".claude" / "CLAUDE.md").read_text()
        self.assertTrue(g.startswith("# Mine\n\nkeep me"))
        self.assertIn("## Golden Thread", g)

    def test_fresh_conflict_exits_3_and_creates_nothing(self):
        a = self.make_vault("a")
        b = self.tmp / "b"
        proc = self.vi("fresh", "--vault", b, "--domain", "X")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        res = json.loads(proc.stdout)
        self.assertTrue(self.actions(res, "conflict"))
        self.assertFalse(b.exists(), "conflicting fresh run still scaffolded the new vault")
        self.assertEqual(self.cfg()["vault_path"], str(a.resolve()))

    def test_fresh_without_installed_hooks_reports_error_and_writes_no_settings(self):
        res = self.vi_json("fresh", "--vault", self.tmp / "v", "--domain", "T")
        errs = [r for r in self.actions(res, "error") if "hook scripts not installed" in r["note"]]
        self.assertTrue(errs, "missing hook scripts not reported")
        self.assertFalse((self.home / ".claude" / "settings.json").exists())

    def test_fresh_with_installed_hooks_wires_settings(self):
        hooks = self.fake_hooks()
        self.make_vault()
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        for event, name in (("UserPromptSubmit", "inject_core_rules.sh"),
                            ("Stop", "validate_response.sh"),
                            ("PreToolUse", "guard_session_claims.sh")):
            cmds = [h["command"] for b in s["hooks"][event] for h in b["hooks"]]
            self.assertIn(str(hooks / name), cmds, event)


# ---------------------------------------------------------------------------- connect
class ConnectTest(VaultInitBase):
    def test_connect_existing_folder_seeds_only_what_is_missing(self):
        v = self.tmp / "existing"
        (v / "notes").mkdir(parents=True)
        (v / "notes" / "mine.md").write_text("hands off\n")
        res = self.vi_json("connect", "--vault", v)
        self.assertEqual(self.cfg()["vault_path"], str(v.resolve()))
        self.assertTrue((v / "INBOX.md").is_file())
        self.assertTrue((v / "Projects" / "golden-thread" / "tools" / "gt_tasks.py").is_file())
        self.assertEqual((v / "notes" / "mine.md").read_text(), "hands off\n")
        self.assertFalse(self.actions(res, "error"), res)

    def test_connect_missing_directory_exits_1(self):
        proc = self.vi("connect", "--vault", self.tmp / "nope")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse((self.home / ".claude" / "vault-config.json").exists())

    def test_connect_switches_to_a_different_vault(self):
        """The conflict message (and gt-init's SKILL.md) says: run 'connect' mode to
        switch. connect must therefore succeed when the config points elsewhere."""
        self.make_vault("a")
        b = self.tmp / "b"
        b.mkdir()
        proc = self.vi("connect", "--vault", b)
        self.assertEqual(proc.returncode, 0,
                         "connect refused to switch vaults -- it hits the same exit-3 conflict "
                         "that tells the user to use connect to switch:\n" + proc.stdout)
        self.assertEqual(self.cfg()["vault_path"], str(b.resolve()))


# ---------------------------------------------------------------------------- core rules
class InstallCoreRulesTest(VaultInitBase):
    def bare_vault(self):
        v = self.tmp / "old-vault"
        v.mkdir()
        (v / "CLAUDE.md").write_text("# Old vault\n\nintro\n\n## How to Read This\n\nstuff\n")
        return v

    def test_no_hooks_copies_rules_retrofits_check_and_leaves_settings(self):
        self.fake_hooks()
        v = self.bare_vault()
        res = self.vi_json("install-core-rules", "--vault", v, "--no-hooks")
        core = v / "Projects" / "golden-thread" / "core-rules"
        self.assertTrue((core / "core_rule_priority_model.md").is_file())
        self.assertTrue(len(list(core.glob("core_*.md"))) >= 5)
        text = (v / "CLAUDE.md").read_text()
        self.assertIn("## First: is enforcement active?", text)
        self.assertLess(text.index("## First: is enforcement active?"),
                        text.index("## How to Read This"), "check not inserted before first H2")
        self.assertEqual(self.cfg()["core_rules_path"], "Projects/golden-thread/core-rules")
        self.assertFalse((self.home / ".claude" / "settings.json").exists(),
                         "--no-hooks touched settings.json")
        self.assertFalse(self.actions(res, "error"), res)

    def test_settings_flag_wires_custom_file_idempotently(self):
        hooks = self.fake_hooks()
        v = self.bare_vault()
        settings = self.tmp / "custom-settings.json"
        stale = str(v / "hooks" / "inject_core_rules.sh")      # old vault-internal copy
        settings.write_text(json.dumps({
            "model": "keep",
            "hooks": {
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": stale}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}],
            }}))
        self.vi_json("install-core-rules", "--vault", v, "--settings", settings)
        s = json.loads(settings.read_text())
        self.assertEqual(s["model"], "keep")
        ups = [h["command"] for b in s["hooks"]["UserPromptSubmit"] for h in b["hooks"]]
        self.assertEqual(ups, [str(hooks / "inject_core_rules.sh")], "stale copy not replaced")
        stop = [h["command"] for b in s["hooks"]["Stop"] for h in b["hooks"]]
        self.assertIn("/usr/bin/true", stop, "unrelated hook dropped")
        self.assertIn(str(hooks / "validate_response.sh"), stop)
        self.assertIn("PreToolUse", s["hooks"])
        self.assertFalse((self.home / ".claude" / "settings.json").exists(),
                         "--settings still wrote the default settings.json")
        first = settings.read_text()
        res = self.vi_json("install-core-rules", "--vault", v, "--settings", settings)
        self.assertEqual(settings.read_text(), first, "second run changed settings")
        self.assertEqual(len([r for r in res if "already wired" in r["note"]]), len(HOOK_NAMES),
                         "a second run must report every enforcement hook already wired")

    def test_missing_hook_scripts_is_an_error_not_a_silent_wire(self):
        v = self.bare_vault()
        settings = self.tmp / "s.json"
        res = self.vi_json("install-core-rules", "--vault", v, "--settings", settings)
        self.assertTrue(any("hook scripts not installed" in r["note"] for r in res))
        self.assertFalse(settings.exists())

    def test_relocated_core_rules_are_found_not_duplicated(self):
        v = self.make_vault()
        old = v / "Projects" / "golden-thread" / "core-rules"
        new = v / "Projects" / "meta" / "core-rules"
        new.parent.mkdir(parents=True)
        old.rename(new)
        cfg = self.cfg()
        cfg.pop("core_rules_path")
        self.config(**cfg)
        self.vi_json("install-core-rules", "--vault", v, "--no-hooks")
        self.assertFalse(old.exists(), "a second core-rules folder was created")
        self.assertEqual(self.cfg()["core_rules_path"], "Projects/meta/core-rules")


# ---------------------------------------------------------------------------- create-project
class CreateProjectTest(VaultInitBase):
    def test_create_project_scaffold_and_frontmatter(self):
        v = self.make_vault()
        self.project(v, "alpha", "--title", "Alpha Thing", "--tags", "a,b", "--domain", "tools",
                     "--topology", "local")
        p = v / "Projects" / "alpha"
        for f in ("README.md", "idea.md", "design.md", "decisions.md", "research.md",
                  "source.md", "runbook.md", "CLAUDE.md", "memory/MEMORY.md"):
            self.assertTrue((p / f).is_file(), f)
        readme = (p / "README.md").read_text()
        self.assertTrue(readme.startswith("---\ntype: project\nslug: alpha\ndomain: tools\n"))
        self.assertIn("tags: [tools, a, b]", readme)
        self.assertIn("topology: local", readme)
        self.assertIn("# Alpha Thing", readme)
        self.assertIn("**Topology:** local", (p / "source.md").read_text())
        self.assertNotIn("{{", (p / "CLAUDE.md").read_text())

    def test_row_lands_inside_master_index_table(self):
        v = self.make_vault()
        self.project(v, "alpha", "--domain", "tools")
        lines = (v / "Projects" / "README.md").read_text().splitlines()
        i = next(i for i, l in enumerate(lines) if "`alpha`" in l)
        self.assertTrue(lines[i - 1].startswith("|"), "row not inside the table")
        self.assertIn("| tools | idea |", lines[i])
        # second create is a skip, not a duplicate row
        self.project(v, "alpha", "--domain", "tools")
        text = (v / "Projects" / "README.md").read_text()
        self.assertEqual(text.count("`alpha`"), 1)

    def test_rerun_never_overwrites(self):
        v = self.make_vault()
        self.project(v, "alpha")
        (v / "Projects" / "alpha" / "idea.md").write_text("my brain dump\n")
        res = self.project(v, "alpha", "--title", "Changed")
        self.assertEqual((v / "Projects" / "alpha" / "idea.md").read_text(), "my brain dump\n")
        self.assertFalse([r for r in self.actions(res, "created") if r["path"].endswith(".md")])

    def test_project_dir_gets_section_once(self):
        v = self.make_vault()
        code = self.tmp / "code"
        code.mkdir()
        (code / "CLAUDE.md").write_text("# Code\n\nexisting rules\n")
        self.project(v, "alpha", "--project-dir", code)
        self.project(v, "alpha", "--project-dir", code)
        text = (code / "CLAUDE.md").read_text()
        self.assertIn("existing rules", text)
        self.assertEqual(text.count("## Golden Thread — alpha"), 1)
        self.assertIn(f"{v.resolve()}/Projects/alpha/memory/MEMORY.md", text)

    def test_fleet_link_for_bastion_topologies(self):
        v = self.make_vault()
        self.project(v, "b1", "--topology", "bastion-jump")
        self.assertIn("[[INFRASTRUCTURE]]", (v / "Projects/b1/source.md").read_text())
        self.project(v, "b2", "--topology", "remote", "--fleet", "My Fleet")
        self.assertIn("[[My Fleet]]", (v / "Projects/b2/source.md").read_text())
        self.project(v, "b3", "--topology", "local")
        self.assertIn("**Fleet:** n/a", (v / "Projects/b3/source.md").read_text())

    def test_invalid_topology_is_a_usage_error(self):
        v = self.make_vault()
        proc = self.vi("create-project", "--vault", v, "--name", "x", "--topology", "cloud")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((v / "Projects" / "x").exists())

    def test_parent_creates_nested_sub_project(self):
        v = self.make_vault()
        self.project(v, "alpha", "--domain", "tools")
        self.project(v, "beta", "--parent", "alpha", "--domain", "tools")
        sub = v / "Projects" / "alpha" / "beta"
        self.assertTrue((sub / "idea.md").is_file())
        self.assertIn("\nparent: alpha\n", (sub / "README.md").read_text())
        self.assertFalse((v / "Projects" / "beta").exists())
        self.assertIn("(beta/)", (v / "Projects" / "alpha" / "README.md").read_text())

    def test_parent_row_lands_in_parents_status_table(self):
        """register_in_master_index promises the row lands in the list 'instead of after
        whatever section happens to be last'. For --parent the list is the parent's
        '| Sub-project |' Status table; the row must appear before '## Tasks'."""
        v = self.make_vault()
        self.project(v, "alpha", "--domain", "tools")
        self.project(v, "beta", "--parent", "alpha", "--domain", "tools")
        text = (v / "Projects" / "alpha" / "README.md").read_text()
        row_at = text.index("(beta/)")
        self.assertLess(row_at, text.index("## Tasks"),
                        "sub-project row was appended at the end of the parent README "
                        "(under '## Related') instead of into its Status table")


# ---------------------------------------------------------------------------- rename
class RenameTest(VaultInitBase):
    def test_rename_moves_folder_and_repoints_references(self):
        v = self.make_vault()
        self.project(v, "alpha", "--domain", "tools")
        self.project(v, "beta", "--parent", "alpha", "--domain", "tools")
        (v / "Knowledge" / "Page.md").write_text("see Projects/alpha/design.md and `alpha`\n")
        res = self.vi_json("rename-project", "--vault", v, "--from", "alpha", "--to", "gamma")
        self.assertFalse(self.actions(res, "error"), res)
        self.assertFalse((v / "Projects" / "alpha").exists())
        g = v / "Projects" / "gamma"
        self.assertTrue((g / "idea.md").is_file())
        self.assertIn("\nslug: gamma\n", (g / "README.md").read_text())
        self.assertIn("\nparent: gamma\n", (g / "beta" / "README.md").read_text(),
                      "sub-project still names the old parent")
        idx = (v / "Projects" / "README.md").read_text()
        self.assertIn("(gamma/)", idx)
        self.assertIn("`gamma`", idx)
        self.assertNotIn("`alpha`", idx)
        page = (v / "Knowledge" / "Page.md").read_text()
        self.assertIn("Projects/gamma/design.md", page)
        self.assertIn("`gamma`", page)

    def test_rename_rerecords_core_rules_path(self):
        v = self.make_vault()
        self.vi_json("rename-project", "--vault", v, "--from", "golden-thread", "--to", "gt-meta")
        self.assertEqual(self.cfg()["core_rules_path"], "Projects/gt-meta/core-rules")

    def test_rename_onto_existing_is_conflict_and_moves_nothing(self):
        v = self.make_vault()
        self.project(v, "alpha")
        self.project(v, "beta")
        res = self.vi_json("rename-project", "--vault", v, "--from", "alpha", "--to", "beta")
        self.assertTrue(self.actions(res, "conflict"))
        self.assertTrue((v / "Projects" / "alpha" / "idea.md").is_file())

    def test_rename_unknown_project_is_error(self):
        v = self.make_vault()
        res = self.vi_json("rename-project", "--vault", v, "--from", "ghost", "--to", "x")
        self.assertTrue(self.actions(res, "error"))
        self.assertFalse((v / "Projects" / "x").exists())


# ---------------------------------------------------------------------------- merge
class MergeTest(VaultInitBase):
    def two_projects(self):
        v = self.make_vault()
        self.project(v, "alpha", "--domain", "tools")
        self.project(v, "beta", "--domain", "tools")
        a, b = v / "Projects" / "alpha", v / "Projects" / "beta"
        (a / "decisions.md").write_text("# alpha Decisions\n\n## ADR-1: First\n\nx\n\n## ADR-2: Second\n\ny\n")
        (b / "decisions.md").write_text("# beta Decisions\n\n## ADR-1: Use widgets\n\nbecause\n")
        (b / "research.md").write_text("# beta Research\n\n## 2026-01-01: found a thing\n\nbeta-finding\n")
        (b / "design.md").write_text("# beta Design\n\nbeta architecture\n")
        (b / "idea.md").write_text("# beta\n\nthe original beta idea\n")
        (b / "memory" / "note_b.md").write_text("a beta note\n")
        (b / "memory" / "shared.md").write_text("beta version\n")
        (a / "memory" / "shared.md").write_text("alpha version\n")
        (b / "memory" / "MEMORY.md").write_text(
            "# beta Memory Index\n\n- [note_b](note_b.md) — a note\n- [shared](shared.md) — s\n")
        return v, a, b

    def test_merge_folds_content_and_leaves_a_tombstone(self):
        v, a, b = self.two_projects()
        res = self.vi_json("merge-project", "--vault", v, "--from", "beta", "--into", "alpha",
                           "--date", "2026-09-11")
        self.assertFalse(self.actions(res, "error"), res)
        self.assertEqual((a / "memory" / "note_b.md").read_text(), "a beta note\n")
        self.assertEqual((a / "memory" / "shared.md").read_text(), "alpha version\n")
        self.assertEqual((a / "memory" / "shared__from_beta.md").read_text(), "beta version\n")
        idea = (a / "memory" / "idea_beta.md").read_text()
        self.assertIn("the original beta idea", idea)
        mem = (a / "memory" / "MEMORY.md").read_text()
        self.assertIn("note_b.md", mem)
        self.assertIn("idea_beta.md", mem)
        dec = (a / "decisions.md").read_text()
        self.assertIn("## ADR-3: Use widgets *(was beta ADR-1)*", dec)
        self.assertIn("## ADR-1: First", dec)
        self.assertIn("beta-finding", (a / "research.md").read_text())
        design = (a / "design.md").read_text()
        self.assertIn("NEEDS REVIEW", design)
        self.assertIn("beta architecture", design)
        # tombstone
        self.assertEqual([p.name for p in b.iterdir()], ["README.md"])
        tomb = (b / "README.md").read_text()
        self.assertIn("merged_into: alpha", tomb)
        self.assertNotIn("`beta`", (v / "Projects" / "README.md").read_text())
        rq = (v / "review-queue.md").read_text()
        self.assertIn("### Merge beta -> alpha (2026-09-11)", rq)
        self.assertIn("Name clash: `shared.md`", rq)
        self.assertIn("Reconcile `alpha/design.md`", rq)

    def test_merge_loses_nothing(self):
        """Docstring: 'Nothing is deleted'. Open tasks in the source README and files
        merge does not know about must survive somewhere in the vault."""
        v, a, b = self.two_projects()
        readme = (b / "README.md").read_text().replace(
            "## Tasks\n", "## Tasks\n\n- [ ] ship-the-beta-widget [p:: 1]\n")
        (b / "README.md").write_text(readme)
        (b / "notes-extra.md").write_text("UNIQUE-EXTRA-CONTENT\n")
        self.vi_json("merge-project", "--vault", v, "--from", "beta", "--into", "alpha",
                     "--date", "2026-09-11")
        corpus = "\n".join(p.read_text(errors="replace") for p in v.rglob("*")
                           if p.is_file() and ".git" not in p.parts)
        lost = [s for s in ("ship-the-beta-widget", "UNIQUE-EXTRA-CONTENT") if s not in corpus]
        self.assertEqual(lost, [], "merge-project deleted source content it did not carry over: "
                         + ", ".join(lost))

    def test_merge_into_itself_or_missing_is_error(self):
        v = self.make_vault()
        self.project(v, "alpha")
        res = self.vi_json("merge-project", "--vault", v, "--from", "alpha", "--into", "alpha")
        self.assertTrue(self.actions(res, "error"))
        res = self.vi_json("merge-project", "--vault", v, "--from", "ghost", "--into", "alpha")
        self.assertTrue(self.actions(res, "error"))
        self.assertTrue((v / "Projects" / "alpha" / "idea.md").is_file())


# ---------------------------------------------------------------------------- archive
class ArchiveTest(VaultInitBase):
    def test_archive_sets_stage_banner_and_index(self):
        v = self.make_vault()
        self.project(v, "alpha", "--domain", "tools")
        self.vi_json("archive-project", "--vault", v, "--slug", "alpha",
                     "--reason", "Replaced by beta.", "--date", "2026-09-11")
        readme = (v / "Projects" / "alpha" / "README.md").read_text()
        fm = re.match(r"^---\n(.*?)\n---\n", readme, re.S).group(1)
        self.assertIn("stage: archived", fm)
        self.assertIn("archived: 2026-09-11", fm)
        self.assertIn("**Archived 2026-09-11.** Replaced by beta.", readme)
        self.assertLess(readme.index("# alpha"), readme.index("**Archived"), "banner above H1")
        row = next(l for l in (v / "Projects" / "README.md").read_text().splitlines() if "`alpha`" in l)
        self.assertEqual(row.split("|")[4].strip(), "archived")
        # nothing deleted, and re-archiving does not stack banners
        self.assertTrue((v / "Projects" / "alpha" / "idea.md").is_file())
        self.vi_json("archive-project", "--vault", v, "--slug", "alpha", "--date", "2026-09-12")
        self.assertEqual((v / "Projects" / "alpha" / "README.md").read_text().count("**Archived"), 1)

    def test_archive_unknown_is_error(self):
        v = self.make_vault()
        res = self.vi_json("archive-project", "--vault", v, "--slug", "ghost")
        self.assertTrue(self.actions(res, "error"))


class DryRunTest(Sandbox):
    """--dry-run must touch NOTHING. A rehearsal that misses one write site is worse
    than no rehearsal, because it is believed: that is the 2026-09-11 incident in
    miniature, where the careful path silently wrote the live vault.
    """

    def snapshot(self, root):
        """Every path under root, with its bytes — the evidence that nothing moved."""
        out = {}
        for f in sorted(pathlib.Path(root).rglob("*")):
            out[str(f)] = f.read_bytes() if f.is_file() else b"<dir>"
        return out

    def test_fresh_dry_run_creates_no_vault(self):
        target = self.tmp / "would-be-vault"
        p = self.py(VI, "fresh", "--vault", target, "--domain", "Test", "--dry-run")
        self.assertOk(p, "dry run failed")
        self.assertFalse(target.exists(), "--dry-run created the vault anyway")
        self.assertFalse((self.home / ".claude" / "vault-config.json").exists(),
                         "--dry-run claimed the global config")
        self.assertIn("would-", p.stdout, "a dry run must still report what it would do")

    def test_create_project_dry_run_changes_nothing(self):
        v = self.make_vault()
        before = self.snapshot(v)
        p = self.py(VI, "create-project", "--vault", v, "--name", "ghost",
                    "--title", "Ghost", "--domain", "test", "--topology", "local",
                    "--dry-run")
        self.assertOk(p, "dry run failed")
        self.assertEqual(self.snapshot(v), before,
                         "--dry-run modified the vault; compare the reported paths")
        self.assertFalse((v / "Projects" / "ghost").exists())

    def test_dry_run_then_real_run_produces_the_same_paths(self):
        """The rehearsal must predict the real thing, or it is decoration."""
        v = self.make_vault()
        dryp = self.py(VI, "create-project", "--vault", v, "--name", "twin",
                       "--title", "Twin", "--domain", "test", "--topology", "local",
                       "--dry-run")
        self.assertOk(dryp)
        planned = {r["path"] for r in json.loads(dryp.stdout)
                   if r["action"].startswith("would-")}
        realp = self.py(VI, "create-project", "--vault", v, "--name", "twin",
                        "--title", "Twin", "--domain", "test", "--topology", "local")
        self.assertOk(realp)
        done = {r["path"] for r in json.loads(realp.stdout)
                if r["action"] in ("created", "updated")}
        missed = planned - done
        self.assertFalse(missed, f"the dry run promised paths the real run never wrote: {missed}")

    def test_install_core_rules_dry_run_leaves_settings_alone(self):
        v = self.make_vault()
        settings = self.home / ".claude" / "settings.json"
        before = settings.read_bytes() if settings.exists() else None
        p = self.py(VI, "install-core-rules", "--vault", v, "--dry-run")
        self.assertOk(p, "dry run failed")
        after = settings.read_bytes() if settings.exists() else None
        self.assertEqual(after, before, "--dry-run rewrote settings.json")

    def test_short_flag_is_accepted(self):
        target = self.tmp / "nope"
        self.assertOk(self.py(VI, "fresh", "--vault", target, "--domain", "T", "-n"))
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
