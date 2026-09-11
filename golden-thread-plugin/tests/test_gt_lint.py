"""gt_lint.py: each check fires on a seeded defect, and a fresh vault is clean.

The fixture is a fresh vault whose Core-rule hooks are wired to fake scripts in the
sandbox HOME, so the baseline carries no core-unenforced noise. Each test seeds one
kind of defect and asserts it is reported under its check name.
"""
import json
import re
import shutil
import unittest

from _harness import Sandbox, SCRIPTS

LINT = SCRIPTS / "gt_lint.py"
VI = SCRIPTS / "vault_init.py"
FINDING = re.compile(r"^\[([a-z-]+)\] (.+)$", re.M)


class LintBase(Sandbox):
    def setUp(self):
        super().setUp()
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        hooks.mkdir(parents=True)
        for n in ("inject_core_rules.sh", "validate_response.sh", "guard_session_claims.sh"):
            (hooks / n).write_text("#!/bin/sh\nexit 0\n")
            (hooks / n).chmod(0o755)
        self.v = self.make_vault()

    # -- helpers ---------------------------------------------------------------
    def lint(self, *extra):
        proc = self.py(LINT, self.v, *extra)
        return proc, FINDING.findall(proc.stdout)

    def checks(self, findings):
        return {c for c, _ in findings}

    def assertFinding(self, findings, check, path, proc=None):
        self.assertIn((check, path), findings,
                      f"expected [{check}] {path}\n" + (proc.stdout if proc else str(findings)))

    def assertNoFinding(self, findings, check, path=None):
        hits = [f for f in findings if f[0] == check and (path is None or f[1] == path)]
        self.assertEqual(hits, [], f"unexpected {check} finding(s): {hits}")

    def w(self, rel, text):
        p = self.v / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def project(self, slug, *extra):
        proc = self.py(VI, "create-project", "--vault", self.v, "--name", slug, "--domain", "tools",
                       *extra)
        self.assertOk(proc)

    def fill_source(self, rel_dir):
        self.w(f"{rel_dir}/source.md",
               "# Source\n\n**Topology:** local\n\n| Role | Env | Host |\n|---|---|---|\n"
               "| app | local | laptop |\n")

    def index(self, *titles):
        self.w("index.md", "# Index\n\n" + "".join(f"- [[{t}]] — x\n" for t in titles))


# ---------------------------------------------------------------------------- baseline
class BaselineTest(LintBase):
    def test_fresh_vault_is_healthy(self):
        proc, findings = self.lint()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("healthy", proc.stdout)
        self.assertEqual(findings, [])

    def test_fresh_vault_with_new_project_reports_only_source_todo(self):
        self.project("alpha")
        self.project("beta", "--parent", "alpha")
        proc, findings = self.lint()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.checks(findings), {"source-todo"}, proc.stdout)
        self.assertIn(("source-todo", "Projects/alpha/source.md"), findings)
        self.assertIn(("source-todo", "Projects/alpha/beta/source.md"), findings)
        self.assertIn("source-todo: 4", proc.stdout)

    def test_filled_source_clears_source_todo(self):
        self.project("alpha")
        self.fill_source("Projects/alpha")
        proc, findings = self.lint()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_missing_vault_exits_1(self):
        proc = self.py(LINT, self.tmp / "nope")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("vault not found", proc.stderr)


# ---------------------------------------------------------------------------- knowledge
class KnowledgeChecksTest(LintBase):
    def test_index_gap(self):
        self.index("Linker")
        self.w("Knowledge/Linker.md", "see [[Unindexed]]\n")
        self.w("Knowledge/Unindexed.md", "body\n")
        _, f = self.lint()
        self.assertFinding(f, "index-gap", "Knowledge/Unindexed.md")
        self.assertNoFinding(f, "index-gap", "Knowledge/Linker.md")
        self.assertNoFinding(f, "orphan", "Knowledge/Unindexed.md")

    def test_orphan(self):
        self.w("Knowledge/Lonely.md", "nobody links here\n")
        _, f = self.lint()
        self.assertFinding(f, "orphan", "Knowledge/Lonely.md")
        self.assertFinding(f, "index-gap", "Knowledge/Lonely.md")

    def test_orphan_is_not_hidden_by_a_longer_title_in_the_index(self):
        """check_index_gap was fixed so 'Quote API' does not count as indexed because
        'Schwab Quote API' is listed. check_orphans still does a bare substring test,
        so the same page is reported as an index-gap but never as an orphan."""
        self.index("Schwab Quote API")
        self.w("Knowledge/Schwab Quote API.md", "body\n")
        self.w("Knowledge/Quote API.md", "nobody links here\n")
        proc, f = self.lint()
        self.assertFinding(f, "index-gap", "Knowledge/Quote API.md", proc)
        self.assertFinding(f, "orphan", "Knowledge/Quote API.md", proc)

    def test_broken_link(self):
        self.index("Page")
        self.w("Knowledge/Page.md", "see [[Nowhere Page]] and [[Page#Section]] and [[Page|alias]]\n")
        proc, f = self.lint()
        self.assertFinding(f, "broken-link", "Knowledge/Page.md", proc)
        self.assertIn("[[Nowhere Page]]", proc.stdout)
        self.assertEqual(proc.stdout.count("[broken-link]"), 1, proc.stdout)

    def test_links_that_resolve(self):
        self.project("alpha")
        self.fill_source("Projects/alpha")
        self.project("beta", "--parent", "alpha")
        self.fill_source("Projects/alpha/beta")
        self.index("Page")
        self.w("Knowledge/Page.md",
               "[[alpha]] [[alpha/beta]] [[beta/decisions]] [[alpha/beta/design]] "
               "[[Knowledge/Page]] [[INFRASTRUCTURE]] `[[in code]]`\n\n```\n[[fenced]]\n```\n")
        proc, f = self.lint()
        self.assertNoFinding(f, "broken-link")
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_embedded_attachment_is_not_a_broken_link(self):
        """Obsidian vaults embed attachments with ![[file.png]]. The file exists, so
        the link resolves -- but link_targets() registers only .md files."""
        self.index("Diagrams")
        self.w("Knowledge/Diagrams.md", "The flow:\n\n![[flow.png]]\n")
        (self.v / "Knowledge" / "flow.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        proc, f = self.lint()
        self.assertNoFinding(f, "broken-link", "Knowledge/Diagrams.md")

    def test_escaped_pipe_alias_in_a_table_is_not_a_broken_link(self):
        """Inside a Markdown table Obsidian requires the alias pipe escaped:
        [[Target Page\\|alias]]. The regex keeps the backslash in the target."""
        self.index("Table", "Target Page")
        self.w("Knowledge/Target Page.md", "body\n")
        self.w("Knowledge/Table.md", "| Link | Note |\n|---|---|\n| [[Target Page\\|alias]] | x |\n")
        proc, f = self.lint()
        self.assertNoFinding(f, "broken-link", "Knowledge/Table.md")

    def test_superseded_cited(self):
        self.index("Cites")
        self.w("Sources/2026-01-01 old.md", "old\n")
        self.w("Sources/2026-02-01 new.md", "---\nsupersedes: [\"Sources/2026-01-01 old.md\"]\n---\nnew\n")
        self.w("Knowledge/Cites.md", "---\nsources:\n  - Sources/2026-01-01 old.md\n---\nbody\n")
        proc, f = self.lint()
        self.assertFinding(f, "superseded-cited", "Knowledge/Cites.md", proc)
        self.assertIn("2026-02-01 new.md", proc.stdout)

    def test_stale(self):
        self.index("Old")
        self.w("Knowledge/Old.md", "---\nstatus: stale\n---\nbody\n")
        self.index("Old", "Fresh")
        self.w("Knowledge/Fresh.md", "---\nstatus: current\n---\nbody\n")
        _, f = self.lint()
        self.assertFinding(f, "stale", "Knowledge/Old.md")
        self.assertNoFinding(f, "stale", "Knowledge/Fresh.md")


# ---------------------------------------------------------------------------- memory
class MemoryChecksTest(LintBase):
    def test_memory_unlisted_top_level_and_sub_project(self):
        self.project("alpha")
        self.fill_source("Projects/alpha")
        self.project("beta", "--parent", "alpha")
        self.fill_source("Projects/alpha/beta")
        self.w("Projects/alpha/memory/listed.md", "x\n")
        self.w("Projects/alpha/memory/MEMORY.md", "# idx\n\n- [listed](listed.md) — x\n")
        self.w("Projects/alpha/memory/stray.md", "x\n")
        self.w("Projects/alpha/beta/memory/stray2.md", "x\n")
        _, f = self.lint()
        self.assertFinding(f, "memory-unlisted", "Projects/alpha/memory/stray.md")
        self.assertFinding(f, "memory-unlisted", "Projects/alpha/beta/memory/stray2.md")
        self.assertNoFinding(f, "memory-unlisted", "Projects/alpha/memory/listed.md")

    def test_global_gap_bloat_and_scope_leak(self):
        self.project("alpha")
        self.fill_source("Projects/alpha")
        self.w("global-memory/unlisted.md", "fact\n")
        self.w("global-memory/big.md", "".join(f"line {i}\n" for i in range(31)))
        self.w("global-memory/ok.md", "".join(f"line {i}\n" for i in range(30)))
        self.w("global-memory/leak.md", "Deploys only go through the alpha box.\n")
        self.w("global-memory/MEMORY.md",
               "# G\n\n- [big](big.md)\n- [ok](ok.md)\n- [leak](leak.md)\n")
        _, f = self.lint()
        self.assertFinding(f, "global-gap", "global-memory/unlisted.md")
        self.assertNoFinding(f, "global-gap", "global-memory/big.md")
        self.assertFinding(f, "memory-bloat", "global-memory/big.md")
        self.assertNoFinding(f, "memory-bloat", "global-memory/ok.md")
        self.assertFinding(f, "global-scope-leak", "global-memory/leak.md")
        self.assertNoFinding(f, "global-scope-leak", "global-memory/ok.md")


# ---------------------------------------------------------------------------- projects
class ProjectChecksTest(LintBase):
    def test_bastion_without_fleet_link(self):
        self.project("b")
        self.w("Projects/b/source.md",
               "**Topology:** bastion-jump\n\n| Role | Env |\n|---|---|\n| web | prod |\n")
        proc, f = self.lint()
        self.assertFinding(f, "source-todo", "Projects/b/source.md")
        self.assertIn("links no fleet definition", proc.stdout)

    def test_frontmatter_problems(self):
        for slug in ("nofm", "wrongslug", "todo", "missing"):
            self.project(slug)
            self.fill_source(f"Projects/{slug}")
        self.w("Projects/nofm/README.md", "# nofm\n")
        self.w("Projects/wrongslug/README.md",
               "---\ntype: project\nslug: other\ndomain: tools\nstage: idea\n---\n# x\n")
        self.w("Projects/todo/README.md",
               "---\ntype: project\nslug: todo\ndomain: TODO\nstage: idea\n---\n# x\n")
        self.w("Projects/missing/README.md", "---\ntype: project\nslug: missing\n---\n# x\n")
        proc, f = self.lint()
        for slug in ("nofm", "wrongslug", "todo", "missing"):
            self.assertFinding(f, "frontmatter", f"Projects/{slug}/README.md", proc)
        self.assertIn("declares slug 'other'", proc.stdout)
        self.assertIn("todo has no domain set", proc.stdout)
        self.assertIn("frontmatter missing: domain, stage", proc.stdout)
        self.assertIn("nofm/README.md has no property frontmatter", proc.stdout)

    def test_project_missing(self):
        self.project("alpha")
        self.fill_source("Projects/alpha")
        self.index("Links")
        self.w("Knowledge/Links.md",
               "[ok](../Projects/alpha/) [gone](../Projects/ghost/) [rel](alpha/)\n")
        proc, f = self.lint()
        self.assertFinding(f, "project-missing", "Knowledge/Links.md", proc)
        self.assertIn("'ghost/'", proc.stdout)
        self.assertNotIn("'alpha/'", proc.stdout)


# ---------------------------------------------------------------------------- core rules
class CoreChecksTest(LintBase):
    def test_core_misplaced_and_no_enforcement(self):
        self.index("Rogue", "Bare")
        self.w("Knowledge/Rogue.md", "---\nlevel: core\nenforcement: reminder\n---\n**Do it.**\n")
        self.w("Projects/golden-thread/core-rules/core_bare.md", "---\nlevel: core\n---\n**Bare.**\n")
        _, f = self.lint()
        self.assertFinding(f, "core-misplaced", "Knowledge/Rogue.md")
        self.assertNoFinding(f, "core-no-enforcement", "Knowledge/Rogue.md")
        self.assertFinding(f, "core-no-enforcement", "Projects/golden-thread/core-rules/core_bare.md")
        self.assertNoFinding(f, "core-misplaced", "Projects/golden-thread/core-rules/core_bare.md")

    def test_core_unenforced_when_nothing_is_wired(self):
        (self.home / ".claude" / "settings.json").write_text("{}")
        proc, f = self.lint()
        self.assertEqual(self.checks(f), {"core-unenforced"}, proc.stdout)
        self.assertFinding(f, "core-unenforced", "Projects/golden-thread/core-rules")
        rules = sorted((self.v / "Projects/golden-thread/core-rules").glob("core_*.md"))
        core = [r for r in rules if "level: core" in r.read_text()]
        for r in core:
            self.assertFinding(f, "core-unenforced", f"Projects/golden-thread/core-rules/{r.name}")

    def test_core_unenforced_partial_wiring(self):
        """Only UserPromptSubmit wired: validated rules (need Stop) are flagged,
        reminder rules are not, and the folder-level 'inert' finding is absent."""
        (self.home / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "x"}]}]}}))
        proc, f = self.lint()
        self.assertNoFinding(f, "core-unenforced", "Projects/golden-thread/core-rules")
        cr = self.v / "Projects/golden-thread/core-rules"

        def enforcement(p):
            fm = re.match(r"^---\s*\n(.*?)\n---", p.read_text(), re.S)
            m = fm and re.search(r"^\s*enforcement:\s*(\w+)", fm.group(1), re.M)
            return m.group(1) if m else None
        validated = [p.name for p in cr.glob("core_*.md") if enforcement(p) == "validated"]
        reminder = [p.name for p in cr.glob("core_*.md") if enforcement(p) == "reminder"]
        self.assertTrue(validated and reminder, "template rules changed shape")
        for n in validated:
            self.assertFinding(f, "core-unenforced", f"Projects/golden-thread/core-rules/{n}", proc)
        for n in reminder:
            self.assertNoFinding(f, "core-unenforced", f"Projects/golden-thread/core-rules/{n}")


# ---------------------------------------------------------------------------- attribution
class AttributionTest(LintBase):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git not installed")

    def test_unset_hookspath(self):
        self.run_cmd(["git", "-C", self.v, "config", "--unset", "core.hooksPath"])
        _, f = self.lint()
        self.assertFinding(f, "attribution-unwired", ".git/config")

    def test_non_executable_hook(self):
        (self.v / ".githooks" / "post-commit").chmod(0o644)
        (self.v / ".githooks" / "prepare-commit-msg").unlink()
        proc, f = self.lint()
        self.assertFinding(f, "attribution-unwired", ".githooks/post-commit", proc)
        self.assertFinding(f, "attribution-unwired", ".githooks/prepare-commit-msg", proc)
        self.assertIn("not executable", proc.stdout)


# ---------------------------------------------------------------------------- suppression / queue
class SuppressionAndQueueTest(LintBase):
    def declines(self, *lines):
        p = self.v / "lint-declines.md"
        p.write_text(p.read_text() + "\n" + "\n".join(lines) + "\n")

    def test_suppress_whole_file_and_single_link(self):
        self.index("Page")
        self.w("Knowledge/Lonely.md", "x\n")
        self.w("Knowledge/Page.md", "[[Gone One]] [[Gone Two]]\n")
        self.declines("suppress: Knowledge/lonely.md", "suppress: Knowledge/Page.md:[[Gone One]]")
        proc, f = self.lint()
        self.assertNoFinding(f, "orphan", "Knowledge/Lonely.md")
        self.assertNoFinding(f, "index-gap", "Knowledge/Lonely.md")
        self.assertIn("[[Gone Two]]", proc.stdout)
        self.assertNotIn("[[Gone One]]", proc.stdout)

    def test_suppress_scope_leak_for_one_slug(self):
        self.project("alpha")
        self.fill_source("Projects/alpha")
        self.w("global-memory/leak.md", "the alpha box\n")
        self.w("global-memory/MEMORY.md", "# G\n\n- [leak](leak.md)\n")
        self.declines("suppress: global-memory/leak.md:alpha")
        proc, f = self.lint()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_template_suppressions_cover_shipped_placeholders(self):
        # fresh index.md carries [[Page Title]] as an example; it must not surface
        _, f = self.lint()
        self.assertNoFinding(f, "broken-link")

    def test_queue_written_grouped_by_check(self):
        self.w("Knowledge/Lonely.md", "x\n")
        q = self.tmp / "queue.md"
        proc, _ = self.lint("--queue", q)
        self.assertEqual(proc.returncode, 1)
        text = q.read_text()
        self.assertIn("## index-gap (1 items)", text)
        self.assertIn("## orphan (1 items)", text)
        self.assertIn("- [ ] `Knowledge/Lonely.md`", text)
        self.assertIn("Review queue written to", proc.stdout)

    def test_queue_not_written_when_clean(self):
        q = self.tmp / "queue.md"
        proc, _ = self.lint("--queue", q)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(q.exists())


if __name__ == "__main__":
    unittest.main()
