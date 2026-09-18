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

from _harness import Sandbox, SCRIPTS, ENFORCEMENT_HOOKS, TEMPLATES

VI = SCRIPTS / "vault_init.py"
HOOK_NAMES = ENFORCEMENT_HOOKS


def vi_registrations():
    """The vault_init-owned rows of HOOK_REGISTRATIONS -- (event, script) PAIRS, not names.

    Read from gt_components rather than listed here, for the reason that file states about
    its own list: a second declaration of "what should be wired" is a second thing to forget.
    Since 0.16.2 one script (inject_core_rules.sh) is registered under two events, so a count
    of unique script names no longer equals a count of registrations.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gt_components_for_tests", str(SCRIPTS / "gt_components.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return [r for r in mod.HOOK_REGISTRATIONS
            if str(r.get("owner", "")).startswith("vault_init.py")]


class VaultInitBase(Sandbox):
    def vi(self, *args):
        return self.py(VI, *args)

    def vi_json(self, *args, expect=0):
        """`expect` is the exit code, and it is part of the contract, not a formality.

        A mode that either performs its move or does not -- rename, merge, archive -- exits 1
        when it did not and said why in an error or conflict row. main() used to end in a bare
        sys.exit(0), so every one of the refusals below reported success to its caller
        (corrected 2026-09-18). The scaffolding modes still exit 0 while recording advisory
        error rows, e.g. `fresh` before install.sh has put the hook scripts in place."""
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

    def test_fresh_vault_is_born_current(self):
        """Regression 2026-09-14: `fresh` wrote a plain log.md, so every new vault had the
        0.11.0 log-spool migration pending and install.sh migrated it straight away."""
        v = self.make_vault()
        up = self.py(SCRIPTS / "gt_upgrade.py", "status", "--vault", v)
        self.assertEqual(up.returncode, 0, up.stdout + up.stderr)
        self.assertIn("nothing pending", up.stdout)
        self.assertNotIn("pending step(s)", up.stdout)
        release = json.loads((SCRIPTS.parent / ".claude-plugin" / "plugin.json").read_text())["version"]
        stamp = json.loads((v / "Projects/golden-thread/.vault-version.json").read_text())
        self.assertEqual(stamp["gt"], release)
        # log.md is generated, and exactly as `gt_log.py migrate` would have produced it
        base = v / "Projects/golden-thread/spool/log/0000-baseline.md"
        self.assertTrue(base.is_file(), "no log spool baseline")
        tools = v / "Projects/golden-thread/tools"
        log = v / "log.md"
        self.assertTrue(log.read_text().rstrip("\n").endswith(base.read_text().rstrip("\n")))
        before = log.read_bytes()
        self.assertOk(self.py(tools / "gt_log.py", "--vault", v, "merge"))
        self.assertEqual(log.read_bytes(), before, "fresh log.md is not what merge renders")
        self.assertOk(self.py(tools / "gt_log.py", "--vault", v, "--id", "t", "add",
                              "2026-09-14 10:00 CDT [work] x — first entry"))
        self.assertIn("[work] x — first entry", log.read_text())
        # and a second `fresh` over it neither re-migrates nor refuses
        self.vi_json("fresh", "--vault", v, "--domain", "Test")
        self.assertIn("[work] x — first entry", log.read_text())
        self.assertIn("nothing pending",
                      self.py(SCRIPTS / "gt_upgrade.py", "status", "--vault", v).stdout)

    def test_fresh_dry_run_does_not_generate_the_log(self):
        v = self.tmp / "dry"
        res = self.vi_json("fresh", "--vault", v, "--domain", "Test", "--no-config", "--dry-run")
        self.assertTrue(any(r["action"] == "would-migrate" for r in res))
        self.assertFalse(v.exists())

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

    def test_fresh_without_installed_hooks_still_exits_zero(self):
        """The scaffold DID happen; the hooks are a later step install.sh owns.

        The exit code says whether the operation happened, and a `fresh` into a clean
        directory on a machine where gt has not been installed yet is the documented happy
        path -- so this advisory error row must not fail the run, while a rename onto a name
        already taken must (see RenameTest)."""
        proc = self.vi("fresh", "--vault", self.tmp / "hv", "--domain", "T")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        res = json.loads(proc.stdout)
        self.assertTrue([r for r in self.actions(res, "error")
                         if "hook scripts not installed" in r["note"]])
        self.assertTrue((self.tmp / "hv" / "Knowledge").is_dir(), "the vault was scaffolded")

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

    def test_connect_keeps_an_owner_core_hooks_path(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        v = self.tmp / "hp"
        v.mkdir()
        self.assertOk(self.run_cmd(["git", "-C", v, "init", "-q"]))
        self.assertOk(self.run_cmd(["git", "-C", v, "config", "core.hooksPath", "my-hooks"]))
        res = self.vi_json("connect", "--vault", v)
        hp = self.run_cmd(["git", "-C", v, "config", "--get", "core.hooksPath"]).stdout.strip()
        self.assertEqual(hp, "my-hooks", "connect overwrote the owner's core.hooksPath")
        self.assertTrue(any("yours" in (r.get("detail") or r.get("note") or "")
                            for r in self.actions(res, "skipped")), res)
        # and an unset one is still wired
        w = self.tmp / "hp2"
        w.mkdir()
        self.assertOk(self.run_cmd(["git", "-C", w, "init", "-q"]))
        self.vi_json("connect", "--vault", w)
        self.assertEqual(self.run_cmd(["git", "-C", w, "config", "--get",
                                       "core.hooksPath"]).stdout.strip(), ".githooks")

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


class ConnectMergeBaseTest(VaultInitBase):
    """connect records a merge base only when there is nothing to review (0.14.0).

    Recording the shipped template as the base of an EDITED document told gt_upgrade the
    owner had reviewed it against the release -- and since install.sh applies upgrades
    unattended, the next install would merge over edits nobody had looked at."""
    BASE = Path("Projects") / "golden-thread" / ".templates"

    def existing(self, protocol):
        v = self.tmp / "existing"
        (v / "Projects").mkdir(parents=True)
        (v / "Projects" / "PROTOCOL.md").write_bytes(protocol)
        return v

    def status(self, v):
        return self.py(SCRIPTS / "gt_upgrade.py", "status", "--vault", v)

    def test_connect_to_an_edited_document_records_no_base(self):
        v = self.existing((TEMPLATES / "PROTOCOL.md").read_bytes() + b"\n## Owner's section\n")
        self.vi_json("connect", "--vault", v)
        self.assertFalse((v / self.BASE / "PROTOCOL.md").exists(),
                         "connect recorded a base for a document nobody reviewed")
        st = self.status(v)
        self.assertEqual(st.returncode, 0, st.stdout + st.stderr)
        self.assertIn("needs a person: no merge base — review %s against the shipped template"
                      % (v / "Projects" / "PROTOCOL.md"), st.stdout)

    def test_connect_to_an_untouched_copy_records_the_base(self):
        shipped = (TEMPLATES / "PROTOCOL.md").read_bytes()
        v = self.existing(shipped)
        self.vi_json("connect", "--vault", v)
        self.assertEqual((v / self.BASE / "PROTOCOL.md").read_bytes(), shipped)
        self.assertNotIn("needs a person", self.status(v).stdout)

    def test_fresh_records_the_base(self):
        v = self.make_vault()
        for name in ("PROTOCOL.md", "CONVENTIONS.md"):
            self.assertEqual((v / self.BASE / name).read_bytes(),
                             (TEMPLATES / name).read_bytes(), name)


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
        # Counted per REGISTRATION, not per script. Since 0.16.2 inject_core_rules.sh is wired
        # twice -- UserPromptSubmit every turn, and SessionStart/compact to re-assert the rules
        # after a compaction discards hook-added context -- so the unique-script count (5) and
        # the registration count (6) diverged. Asserting against the script names would let a
        # dropped second registration pass as "everything already wired", which is the exact
        # failure vault_init's own comments describe shipping twice.
        wanted = vi_registrations()
        self.assertEqual(len([r for r in res if "already wired" in r["note"]]), len(wanted),
                         "a second run must report every enforcement registration already wired")

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

    # 0.15.0: install.sh runs this on every install. An owner's deliberate deletion came
    # back each time, silently. Removal is honoured when recorded in .gt-removed.
    def test_deleted_rule_is_recreated_and_reported_by_name(self):
        v = self.make_vault()
        rule = v / "Projects" / "golden-thread" / "core-rules" / "core_parallel_when_beneficial.md"
        rule.unlink()
        res = self.vi_json("install-core-rules", "--vault", v, "--no-hooks")
        self.assertTrue(rule.exists())
        self.assertIn(rule.resolve(), [Path(r["path"]).resolve() for r in self.actions(res, "created")])

    def test_rule_listed_in_gt_removed_stays_removed(self):
        v = self.make_vault()
        core = v / "Projects" / "golden-thread" / "core-rules"
        (core / "core_parallel_when_beneficial.md").unlink()
        (core / "core_timestamp_every_message.md").unlink()
        (core.parent / ".gt-removed").write_text(
            "# rules I took out\ncore_parallel_when_beneficial.md\n"
            "core_timestamp_every_message.md   # hooked\n")
        res = self.vi_json("install-core-rules", "--vault", v, "--no-hooks")
        self.assertFalse((core / "core_parallel_when_beneficial.md").exists(),
                         "a rule listed in .gt-removed was re-created")
        self.assertFalse((core / "core_timestamp_every_message.md").exists())
        kept = {Path(r["path"]).name: r["note"] for r in self.actions(res, "kept-removed")}
        self.assertIn("core_parallel_when_beneficial.md", kept)
        self.assertNotIn("WARNING", kept["core_parallel_when_beneficial.md"])
        self.assertIn("WARNING: its hook still enforces it", kept["core_timestamp_every_message.md"])

    def test_enforcement_section_listed_in_gt_removed_is_not_reinserted(self):
        v = self.bare_vault()
        self.vi_json("install-core-rules", "--vault", v, "--no-hooks")
        text = (v / "CLAUDE.md").read_text()
        start = text.index("## First: is enforcement active?")
        end = text.index("## How to Read This")
        (v / "CLAUDE.md").write_text(text[:start] + text[end:])
        (v / "Projects" / "golden-thread" / ".gt-removed").write_text("claude-md-enforcement-section\n")
        res = self.vi_json("install-core-rules", "--vault", v, "--no-hooks")
        self.assertNotIn("## First: is enforcement active?", (v / "CLAUDE.md").read_text())
        self.assertIn((v / "CLAUDE.md").resolve(),
                      [Path(r["path"]).resolve() for r in self.actions(res, "kept-removed")])


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

    def test_new_project_is_born_current(self):
        """Regression 2026-09-14: create-project wrote a plain decisions.md, so
        `decisions-spool` was pending as soon as a project existed."""
        v = self.make_vault()
        res = self.project(v, "alpha", "--title", "Alpha", "--domain", "tools")
        self.project(v, "beta", "--parent", "alpha", "--domain", "tools")
        self.assertFalse(self.actions(res, "error"), res)
        up = self.py(SCRIPTS / "gt_upgrade.py", "status", "--vault", v)
        self.assertEqual(up.returncode, 0, up.stdout + up.stderr)
        self.assertIn("nothing pending", up.stdout, up.stdout)
        spool = v / "Projects/golden-thread/spool/decisions"
        for rel in ("alpha", "alpha/beta"):
            self.assertTrue((spool / rel / "0000-baseline.md").is_file(), rel)
        tool = v / "Projects/golden-thread/tools/gt_adr.py"
        dec = v / "Projects/alpha/decisions.md"
        before = dec.read_bytes()
        self.assertOk(self.py(tool, "--vault", v, "merge", "alpha"))
        self.assertEqual(dec.read_bytes(), before, "decisions.md is not what merge renders")
        self.assertOk(self.py(tool, "--vault", v, "allocate", "alpha", "--title", "Pick X"))
        self.assertOk(self.py(tool, "--vault", v, "merge", "alpha"))
        self.assertIn("## ADR-1: Pick X", dec.read_text())
        self.assertIn("# Alpha Decisions", dec.read_text())

    def test_existing_decisions_md_is_never_migrated(self):
        """An existing decisions.md is the owner's ADR history; it belongs to gt_upgrade."""
        v = self.make_vault()
        p = v / "Projects" / "legacy"
        p.mkdir(parents=True)
        (p / "decisions.md").write_text("# Legacy\n\n## ADR-1: kept as is\n")
        before = (p / "decisions.md").read_bytes()
        res = self.project(v, "legacy")
        self.assertEqual((p / "decisions.md").read_bytes(), before)
        self.assertFalse((v / "Projects/golden-thread/spool/decisions/legacy").exists())
        self.assertFalse([r for r in res if r["path"].endswith("decisions.md")
                          and r["action"] != "skipped"], res)
        # and a re-run over a project this command created does not re-migrate it
        self.project(v, "alpha")
        dec = v / "Projects/alpha/decisions.md"
        dec_before = dec.read_bytes()
        res = self.project(v, "alpha")
        self.assertEqual(dec.read_bytes(), dec_before)
        self.assertFalse(self.actions(res, "error"), res)

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

    def test_rename_moves_the_decisions_spool(self):
        """Regression 2026-09-14: the spool stayed under the old name, so the renamed
        project read as unmigrated and its next ADR restarted at 1."""
        v = self.make_vault()
        self.project(v, "alpha", "--domain", "tools")
        self.project(v, "beta", "--parent", "alpha", "--domain", "tools")
        tool = v / "Projects/golden-thread/tools/gt_adr.py"
        for rel in ("alpha", "alpha", "alpha/beta"):
            self.assertOk(self.py(tool, "--vault", v, "allocate", rel, "--title", "x"))
        self.assertOk(self.py(tool, "--vault", v, "merge", "alpha"))
        res = self.vi_json("rename-project", "--vault", v, "--from", "alpha", "--to", "gamma")
        self.assertFalse(self.actions(res, "error"), res)
        spool = v / "Projects/golden-thread/spool/decisions"
        self.assertFalse((spool / "alpha").exists())
        self.assertTrue((spool / "gamma/0002.md").is_file())
        self.assertTrue((spool / "gamma/beta/0001.md").is_file())
        up = self.py(SCRIPTS / "gt_upgrade.py", "status", "--vault", v)
        self.assertIn("nothing pending", up.stdout, up.stdout)
        n = self.py(tool, "--vault", v, "allocate", "gamma", "--title", "next")
        self.assertEqual(n.stdout.strip(), "3")
        dec = v / "Projects/gamma/decisions.md"
        self.assertOk(self.py(tool, "--vault", v, "merge", "gamma"))
        self.assertEqual(len(re.findall(r"^## ADR-", dec.read_text(), re.M)), 3)

    def test_rename_rerecords_core_rules_path(self):
        v = self.make_vault()
        self.vi_json("rename-project", "--vault", v, "--from", "golden-thread", "--to", "gt-meta")
        self.assertEqual(self.cfg()["core_rules_path"], "Projects/gt-meta/core-rules")

    def test_rename_onto_existing_is_conflict_and_moves_nothing(self):
        v = self.make_vault()
        self.project(v, "alpha")
        self.project(v, "beta")
        res = self.vi_json("rename-project", "--vault", v, "--from", "alpha", "--to", "beta",
                           expect=1)
        self.assertTrue(self.actions(res, "conflict"))
        self.assertTrue((v / "Projects" / "alpha" / "idea.md").is_file())

    def test_rename_dry_run_changes_nothing(self):
        """Until 0.14.0 `rename-project --dry-run` really renamed the folder."""
        v = self.make_vault()
        self.project(v, "alpha", "--domain", "tools")
        (v / "Knowledge" / "Page.md").write_text("see Projects/alpha/design.md\n")
        def snapshot():
            return {str(f.relative_to(v)): f.read_bytes() for f in v.rglob("*") if f.is_file() and ".git" not in f.parts}
        before = snapshot()
        res = self.vi_json("rename-project", "--vault", v, "--from", "alpha", "--to", "gamma", "--dry-run")
        self.assertTrue(self.actions(res, "would-rename"), res)
        self.assertEqual(snapshot(), before, "a dry-run rename changed the vault")
        self.assertTrue((v / "Projects" / "alpha").is_dir())
        self.assertFalse((v / "Projects" / "gamma").exists())

    def test_rename_unknown_project_is_error(self):
        v = self.make_vault()
        res = self.vi_json("rename-project", "--vault", v, "--from", "ghost", "--to", "x",
                           expect=1)
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
        # hand-written decisions.md files stand in for PRE-0.11 (unmigrated) projects
        shutil.rmtree(v / "Projects/golden-thread/spool/decisions")
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

    # -- decisions spool (0.11.0): regression 2026-09-14, merged ADRs were deleted by
    #    the next `gt_adr.py merge` because they were appended to the RENDERED file.
    def spooled_pair(self):
        v = self.make_vault()
        self.project(v, "alpha", "--domain", "tools")
        self.project(v, "beta", "--domain", "tools")
        tool = v / "Projects/golden-thread/tools/gt_adr.py"
        for slug in ("alpha", "beta"):
            for i in (1, 2):
                n = self.py(tool, "--vault", v, "allocate", slug, "--title", f"{slug} choice {i}")
                self.assertOk(n)
                slot = v / f"Projects/golden-thread/spool/decisions/{slug}/{int(n.stdout):04d}.md"
                slot.write_text(slot.read_text() + f"\n{slug}-body-{i}\n")
            self.assertOk(self.py(tool, "--vault", v, "merge", slug))
        return v, tool

    def adrs(self, path):
        return [int(n) for n in re.findall(r"^## ADR-(\d+)(?!\s*amendment)", path.read_text(), re.M)]

    def assertCurrent(self, v):
        up = self.py(SCRIPTS / "gt_upgrade.py", "status", "--vault", v)
        self.assertOk(up)
        self.assertIn("nothing pending", up.stdout, up.stdout)

    def test_merged_adrs_survive_the_next_render(self):
        v, tool = self.spooled_pair()
        res = self.vi_json("merge-project", "--vault", v, "--from", "beta", "--into", "alpha",
                           "--date", "2026-09-14")
        self.assertFalse(self.actions(res, "error"), res)
        dec = v / "Projects/alpha/decisions.md"
        self.assertEqual(self.adrs(dec), [1, 2, 3, 4])
        self.assertOk(self.py(tool, "--vault", v, "merge", "alpha"))
        text = dec.read_text()
        self.assertEqual(self.adrs(dec), [1, 2, 3, 4], "re-render lost the merged ADRs:\n" + text)
        for s in ("alpha choice 1", "alpha-body-2", "## ADR-3: beta choice 1 *(was beta ADR-1)*",
                  "## ADR-4: beta choice 2 *(was beta ADR-2)*", "beta-body-2"):
            self.assertIn(s, text)
        spool = v / "Projects/golden-thread/spool/decisions"
        self.assertFalse((spool / "beta").exists(), "beta's spool left behind")
        self.assertIn("merged_into: alpha", (v / "Projects/beta/README.md").read_text())
        self.assertCurrent(v)
        n = self.py(tool, "--vault", v, "allocate", "alpha", "--title", "after")
        self.assertEqual(n.stdout.strip(), "5")

    def test_legacy_source_merges_without_loss(self):
        v, a, b = self.two_projects()          # both unmigrated
        tool = v / "Projects/golden-thread/tools/gt_adr.py"
        res = self.vi_json("merge-project", "--vault", v, "--from", "beta", "--into", "alpha",
                           "--date", "2026-09-11")
        self.assertFalse(self.actions(res, "error"), res)
        self.assertOk(self.py(tool, "--vault", v, "merge", "alpha"))
        text = (a / "decisions.md").read_text()
        self.assertEqual(self.adrs(a / "decisions.md"), [1, 2, 3])
        self.assertIn("## ADR-3: Use widgets *(was beta ADR-1)*", text)
        self.assertIn("because", text)
        self.assertFalse((v / "Projects/golden-thread/spool/decisions/beta").exists())
        self.assertCurrent(v)

    def test_legacy_source_into_migrated_destination(self):
        v, tool = self.spooled_pair()
        b = v / "Projects/beta"
        shutil.rmtree(v / "Projects/golden-thread/spool/decisions/beta")
        (b / "decisions.md").write_text("# beta\n\n## ADR-1: Old one\n\nold-body\n\n"
                                        "## ADR-1 amendment: tweak\n\ntweak-body\n")
        res = self.vi_json("merge-project", "--vault", v, "--from", "beta", "--into", "alpha")
        self.assertFalse(self.actions(res, "error"), res)
        self.assertOk(self.py(tool, "--vault", v, "merge", "alpha"))
        text = (v / "Projects/alpha/decisions.md").read_text()
        self.assertEqual(self.adrs(v / "Projects/alpha/decisions.md"), [1, 2, 3])
        for s in ("## ADR-3: Old one *(was beta ADR-1)*", "old-body", "tweak-body"):
            self.assertIn(s, text)
        self.assertCurrent(v)

    def test_failure_midway_leaves_both_projects_intact(self):
        v, tool = self.spooled_pair()
        spool = v / "Projects/golden-thread/spool/decisions"

        def snap():
            return {str(p.relative_to(v)): p.read_bytes() for p in
                    list((v / "Projects/alpha").rglob("*")) + list((v / "Projects/beta").rglob("*"))
                    + list(spool.rglob("*")) if p.is_file() and p.name != ".highwater"}
        before = snap()
        proc = self.py(VI, "merge-project", "--vault", v, "--from", "beta", "--into", "alpha",
                       env={"GT_TEST_FAULT": "merge-decisions"})
        # 1: the merge did not happen and the run says so. It exited 0 through 0.16.4, which
        # told the caller a merge that had been rolled back had succeeded.
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        res = json.loads(proc.stdout)
        self.assertTrue(self.actions(res, "error"), res)
        self.assertEqual(snap(), before, "a failed merge changed the projects")
        self.assertOk(self.py(tool, "--vault", v, "merge", "alpha"))
        self.assertEqual(self.adrs(v / "Projects/alpha/decisions.md"), [1, 2])

    def test_merge_dry_run_and_legacy_failure_change_nothing(self):
        """Regression 2026-09-14: `merge-project --dry-run` really moved memory notes and
        deleted the source's decisions.md. And a failed merge into an UNMIGRATED
        destination must undo the destination's migration too."""
        v, a, b = self.two_projects()

        def snap():
            return {str(p.relative_to(v)): p.read_bytes() for p in (v / "Projects").rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts}
        before = snap()
        res = self.vi_json("merge-project", "--vault", v, "--from", "beta", "--into", "alpha",
                           "--dry-run")
        self.assertFalse(self.actions(res, "error"), res)
        self.assertEqual(snap(), before, "a dry-run merge changed the vault")
        proc = self.py(VI, "merge-project", "--vault", v, "--from", "beta", "--into", "alpha",
                       env={"GT_TEST_FAULT": "merge-decisions"})
        self.assertTrue(self.actions(json.loads(proc.stdout), "error"), proc.stdout)
        self.assertEqual(snap(), before, "a failed merge changed the vault")

    def test_merge_moves_sub_project_spools(self):
        v, tool = self.spooled_pair()
        self.project(v, "kid", "--parent", "beta", "--domain", "tools")
        self.assertOk(self.py(tool, "--vault", v, "allocate", "beta/kid", "--title", "kid one"))
        res = self.vi_json("merge-project", "--vault", v, "--from", "beta", "--into", "alpha")
        self.assertFalse(self.actions(res, "error"), res)
        spool = v / "Projects/golden-thread/spool/decisions"
        self.assertTrue((spool / "alpha/kid/0001.md").is_file())
        self.assertFalse((spool / "beta").exists())
        self.assertCurrent(v)

    def test_merge_into_itself_or_missing_is_error(self):
        v = self.make_vault()
        self.project(v, "alpha")
        res = self.vi_json("merge-project", "--vault", v, "--from", "alpha", "--into", "alpha",
                           expect=1)
        self.assertTrue(self.actions(res, "error"))
        res = self.vi_json("merge-project", "--vault", v, "--from", "ghost", "--into", "alpha",
                           expect=1)
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
        res = self.vi_json("archive-project", "--vault", v, "--slug", "ghost", expect=1)
        self.assertTrue(self.actions(res, "error"))


# ---------------------------------------------------------------------------- events
class LifecycleEventsTest(VaultInitBase):
    """Each lifecycle operation emits one gt_events event -- only when it happened."""

    def events(self, v):
        f = v / "Projects/golden-thread/events.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def kinds(self, v):
        return [e["kind"] for e in self.events(v)]

    def test_create_rename_merge_archive_each_emit_one_event(self):
        v = self.make_vault()
        self.project(v, "alpha")
        self.project(v, "alpha")                       # re-run creates nothing: no event
        self.project(v, "kid", "--parent", "alpha")
        created = [e for e in self.events(v) if e["kind"] == "create"]
        self.assertEqual([(e["item"], e["project"]) for e in created],
                         [("Projects/alpha", "alpha"), ("Projects/alpha/kid", "alpha/kid")])
        self.project(v, "beta")
        self.vi_json("rename-project", "--vault", v, "--from", "beta", "--to", "gamma")
        self.vi_json("merge-project", "--vault", v, "--from", "gamma", "--into", "alpha")
        self.vi_json("archive-project", "--vault", v, "--slug", "alpha", "--reason", "done")
        evs = {e["kind"]: e for e in self.events(v)}
        self.assertEqual((evs["rename"]["from"], evs["rename"]["to"], evs["rename"]["project"]),
                         ("Projects/beta", "Projects/gamma", "gamma"))
        self.assertEqual((evs["merge"]["item"], evs["merge"]["to"], evs["merge"]["project"]),
                         ("Projects/gamma", "Projects/alpha", "alpha"))
        self.assertEqual((evs["archive"]["item"], evs["archive"]["note"]),
                         ("Projects/alpha", "archive-project: done"))
        self.assertEqual(sorted(self.kinds(v)),
                         ["archive", "create", "create", "create", "merge", "rename"])

    def test_dry_runs_and_failed_operations_emit_nothing(self):
        v = self.make_vault()
        self.project(v, "alpha")
        self.project(v, "beta")
        before = self.kinds(v)
        for args in (("create-project", "--vault", v, "--name", "ghost"),
                     ("rename-project", "--vault", v, "--from", "beta", "--to", "gamma"),
                     ("merge-project", "--vault", v, "--from", "beta", "--into", "alpha"),
                     ("archive-project", "--vault", v, "--slug", "alpha")):
            self.vi_json(*args, "--dry-run")
        self.vi_json("rename-project", "--vault", v, "--from", "nope", "--to", "x", expect=1)
        self.vi_json("rename-project", "--vault", v, "--from", "beta", "--to", "alpha", expect=1)
        self.vi_json("merge-project", "--vault", v, "--from", "alpha", "--into", "alpha",
                     expect=1)
        self.vi_json("archive-project", "--vault", v, "--slug", "ghost", expect=1)
        self.assertEqual(self.kinds(v), before, "a rehearsal or a refusal emitted an event")

    def test_event_failure_does_not_fail_the_operation(self):
        v = self.make_vault()
        spool = v / "Projects/golden-thread/spool/events"
        spool.parent.mkdir(parents=True, exist_ok=True)
        spool.write_text("blocks the spool directory\n")
        proc = self.vi("create-project", "--vault", v, "--name", "alpha")
        self.assertOk(proc)
        json.loads(proc.stdout)                        # stdout is still the JSON report
        self.assertIn("NOT recorded", proc.stderr)
        self.assertTrue((v / "Projects/alpha/README.md").is_file())


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
        self.assertFalse((v / "Projects/golden-thread/spool/decisions/ghost").exists())
        self.assertTrue(any(r["action"] == "would-migrate"
                            and r["path"].endswith("ghost/decisions.md")
                            for r in json.loads(p.stdout)),
                        "the dry run did not report the decisions.md migration")

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
