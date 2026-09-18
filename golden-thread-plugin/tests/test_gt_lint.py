"""gt_lint.py: each check fires on a seeded defect, and a fresh vault is clean.

The fixture is a fresh vault whose Core-rule hooks are wired to fake scripts in the
sandbox HOME, so the baseline carries no core-unenforced noise. Each test seeds one
kind of defect and asserts it is reported under its check name.
"""
import json
import re
import shutil
import unittest

from _harness import Sandbox, SCRIPTS, ENFORCEMENT_HOOKS

LINT = SCRIPTS / "gt_lint.py"
VI = SCRIPTS / "vault_init.py"
FINDING = re.compile(r"^\[([a-z-]+)\] (.+)$", re.M)


class LintBase(Sandbox):
    def setUp(self):
        super().setUp()
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        hooks.mkdir(parents=True)
        for n in ENFORCEMENT_HOOKS:
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

    def test_a_missing_vault_does_not_look_like_findings(self):
        """Exit 1 means "the vault was read and here is what is wrong with it". A vault
        path that does not exist exited 1 too, so a caller that only reads the status
        could not tell a report from a vault nothing had opened -- and every caller that
        treats non-zero as "findings" reported findings that were never computed."""
        missing = self.py(LINT, self.tmp / "nope")
        self.assertIn("vault not found", missing.stderr)
        self.w("Knowledge/Lonely.md", "x\n")               # a vault WITH findings
        findings = self.py(LINT, self.v)
        self.assertEqual(findings.returncode, 1, findings.stdout)
        self.assertNotEqual(missing.returncode, findings.returncode,
                            "a vault that was never read reports the same exit code as "
                            "a vault that was read and linted")
        self.assertEqual(missing.returncode, 2)

    def test_queue_with_runbooks_is_refused_not_ignored(self):
        """--queue was read only after the --runbooks branch had exited: no file, no
        warning, exit 0. A caller that asked for a worklist got a silent success."""
        q = self.tmp / "queue.md"
        proc = self.py(LINT, "--vault", self.v, "--runbooks", "--queue", q)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("--queue", proc.stderr)
        self.assertFalse(q.exists())


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


# ---------------------------------------------------------------------------- ADRs
class AdrCollisionTest(LintBase):
    TWO_SIXES = ("# Decisions\n\n## ADR-6: keep the ledger in git\n\nbody\n\n"
                 "## ADR-6: move the ledger to sqlite\n\nbody\n")

    def test_collision_in_a_sub_project_is_examined(self):
        """`Projects/*/decisions.md` is one level deep. Sub-projects live deeper, and
        every one of them was silently exempt from the check whose entire subject is
        two decisions answering to one name."""
        self.project("alpha")
        self.fill_source("Projects/alpha")
        self.project("beta", "--parent", "alpha")
        self.fill_source("Projects/alpha/beta")
        self.w("Projects/alpha/beta/decisions.md", self.TWO_SIXES)
        proc, f = self.lint()
        self.assertFinding(f, "adr-collision", "Projects/alpha/beta/decisions.md", proc)
        self.assertIn("alpha/beta", proc.stdout)

    def test_collision_at_the_top_level_still_reported(self):
        self.project("alpha")
        self.fill_source("Projects/alpha")
        self.w("Projects/alpha/decisions.md", self.TWO_SIXES)
        _, f = self.lint()
        self.assertFinding(f, "adr-collision", "Projects/alpha/decisions.md")

    def test_amendment_exclusion_is_case_insensitive(self):
        """The code promises `## ADR-6 amendment:` is deliberate usage and excluded,
        because "flagging it would train people to ignore the check" -- but the pattern
        carried re.M only, so Amendment:, AMENDMENT: and (amendment): were all reported
        as collisions. The promise is what is tested here, in the spellings people write."""
        self.project("alpha")
        self.fill_source("Projects/alpha")
        for n, form in enumerate(("amendment:", "Amendment:", "AMENDMENT:", "(amendment):")):
            self.w("Projects/alpha/decisions.md",
                   f"# Decisions\n\n## ADR-{n}: the decision\n\nbody\n\n"
                   f"## ADR-{n} {form} what changed\n\nbody\n")
            proc, f = self.lint()
            self.assertNoFinding(f, "adr-collision", "Projects/alpha/decisions.md")

    def test_a_second_real_allocation_after_an_amendment_is_still_a_collision(self):
        self.project("alpha")
        self.fill_source("Projects/alpha")
        self.w("Projects/alpha/decisions.md",
               "# Decisions\n\n## ADR-6: one\n\n## ADR-6 Amendment: fine\n\n## ADR-6: two\n")
        _, f = self.lint()
        self.assertFinding(f, "adr-collision", "Projects/alpha/decisions.md")


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

    def hooks_dir(self):
        return self.home / ".claude" / "golden-thread" / "hooks"

    def wire(self, **events):
        """settings.json wiring one command per event. A bare name is resolved to the
        installed hook script; anything else is written through verbatim."""
        hooks = {}
        for event, command in events.items():
            cmd = str(self.hooks_dir() / command) if command.endswith(".sh") \
                and "/" not in command else command
            hooks[event] = [{"hooks": [{"type": "command", "command": cmd}]}]
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}))

    def test_core_unenforced_when_the_event_is_wired_to_an_unrelated_hook(self):
        """THE defect this check exists to catch, one level up from where it looked.

        wired_hook_events() marked an event wired as soon as any block under it had a
        non-empty `hooks` list, and never read `command`. So a vault with somebody
        else's UserPromptSubmit hook -- a linter, a logger, anything -- and
        inject_core_rules.sh nowhere in settings.json was reported as enforcing the
        entire Core tier. That is the project's own 0.9.5 failure restated: the hook
        existed, install.sh copied it, the check reported clean, and nothing had
        registered the event.
        """
        self.wire(UserPromptSubmit="/usr/local/bin/somebody-elses-hook.sh",
                  Stop="/usr/local/bin/somebody-elses-hook.sh")
        proc, f = self.lint()
        cr = self.v / "Projects/golden-thread/core-rules"
        rules = [p.name for p in sorted(cr.glob("core_*.md")) if "level: core" in p.read_text()]
        self.assertTrue(rules, "the fixture vault ships no core rules")
        for n in rules:
            self.assertFinding(f, "core-unenforced", f"Projects/golden-thread/core-rules/{n}", proc)
        self.assertFinding(f, "core-unenforced", "Projects/golden-thread/core-rules", proc)
        self.assertIn("the event is wired, but to something else", proc.stdout)

    def test_core_enforced_when_the_real_script_is_wired_with_arguments(self):
        """The command is matched on the script it runs, not on one exact string, so a
        wrapper or an argument does not read as an uninstalled mechanism."""
        self.wire(UserPromptSubmit="bash %s --quiet" % (self.hooks_dir() / "inject_core_rules.sh"),
                  Stop=str(self.hooks_dir() / "validate_response.sh"))
        proc, f = self.lint()
        self.assertNoFinding(f, "core-unenforced")

    def test_core_unenforced_partial_wiring(self):
        """Only UserPromptSubmit wired: validated rules (need Stop) are flagged,
        reminder rules are not, and the folder-level 'inert' finding is absent."""
        self.wire(UserPromptSubmit="inject_core_rules.sh")
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

    def test_a_bare_word_does_not_silence_a_link_vault_wide(self):
        """Suppression scope used to differ per check: broken-link also matched a bare
        link TARGET against the whole vault, so one word silenced that link in every
        file, while other checks honoured only the relative path. A one-word line does
        not look like a vault-wide rule, so it no longer is one -- a link is declined
        scoped to the file it is in."""
        self.index("One", "Two")
        self.w("Knowledge/One.md", "[[Gone]]\n")
        self.w("Knowledge/Two.md", "[[Gone]]\n")
        self.declines("suppress: Gone")
        proc, f = self.lint()
        self.assertFinding(f, "broken-link", "Knowledge/One.md", proc)
        self.assertFinding(f, "broken-link", "Knowledge/Two.md", proc)
        self.declines("suppress: Knowledge/One.md:[[Gone]]")
        proc, f = self.lint()
        self.assertNoFinding(f, "broken-link", "Knowledge/One.md")
        self.assertFinding(f, "broken-link", "Knowledge/Two.md", proc)

    def test_a_bare_file_name_suppresses_that_file_in_every_check(self):
        """The documented bare-filename form, now honoured by every check rather than
        by some of them."""
        self.project("alpha")
        self.fill_source("Projects/alpha")
        self.w("Projects/alpha/decisions.md",
               "## ADR-1: one\n\n## ADR-1: two\n")
        self.w("Knowledge/Old.md", "---\nstatus: stale\n---\n[[Nowhere]]\n")
        self.index("Old")
        self.declines("suppress: decisions.md", "suppress: Old.md")
        proc, f = self.lint()
        self.assertNoFinding(f, "adr-collision")
        self.assertNoFinding(f, "stale")
        self.assertNoFinding(f, "broken-link")

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

    def test_a_clean_run_rewrites_the_queue_instead_of_leaving_a_stale_one(self):
        """`if args.queue and findings:` meant a healthy vault never rewrote the file,
        so review-queue.md went on listing findings that had been fixed -- and gt-open
        reports its pending count from exactly that file."""
        q = self.tmp / "queue.md"
        self.w("Knowledge/Lonely.md", "x\n")
        self.assertEqual(self.lint("--queue", q)[0].returncode, 1)
        self.assertIn("Knowledge/Lonely.md", q.read_text())
        self.index("Lonely")                                  # fix both findings
        proc, _ = self.lint("--queue", q)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertTrue(q.exists(), "the queue was left behind by a clean run")
        self.assertNotIn("Knowledge/Lonely.md", q.read_text(),
                         "a clean run left the previous run's findings in the worklist")
        self.assertIn("No open findings", q.read_text())


# ------------------------------------------------------- the queue is a worklist, not a projection
class QueueIsHumanWorkTest(LintBase):
    """--queue writes a file a PERSON works through between runs (skills/gt-lint points it
    at <vault>/review-queue.md, which gt-open reports on and gt-promote appends to). A bare
    truncating write destroyed every tick with nothing said, and could tear the file.

    Unlike TASKS.md there is no upstream copy of a tick -- findings are recomputed from the
    vault and the writer hard-codes `- [ ]` -- so ticks are carried across, not just backed up.
    """

    DIGESTS = "Projects/golden-thread/.lint-queue-digests"
    BACKUPS = "Projects/golden-thread/backups"

    def setUp(self):
        super().setUp()
        self.w("Knowledge/Lonely.md", "x\n")     # one page, two findings: index-gap + orphan
        self.q = self.tmp / "queue.md"

    def run_lint(self):
        return self.lint("--queue", self.q)[0]

    def backups(self):
        d = self.v / self.BACKUPS
        return sorted(d.glob("queue.md.*")) if d.exists() else []

    def tick_everything(self):
        self.q.write_text(self.q.read_text(encoding="utf-8").replace("- [ ] ", "- [x] "),
                          encoding="utf-8")

    def test_an_untouched_queue_regenerates_silently_with_no_backup(self):
        """The warning has to be rare enough to mean something."""
        self.run_lint()                                  # seeds the queue and the receipt
        proc = self.run_lint()                           # nothing touched it since
        self.assertNotIn("not what this tool last generated", proc.stdout + proc.stderr)
        self.assertEqual(self.backups(), [],
                         "an untouched worklist must not accumulate a backup on every run")

    def test_a_ticked_item_survives_the_next_run(self):
        self.run_lint()
        self.tick_everything()
        before = self.q.read_text(encoding="utf-8")
        self.assertIn("- [x] `Knowledge/Lonely.md`", before)
        proc = self.run_lint()
        after = self.q.read_text(encoding="utf-8")
        self.assertNotIn("- [ ] `Knowledge/Lonely.md`", after,
                         "a tick was silently erased: the queue is the only copy of it")
        self.assertEqual(after.count("- [x] `Knowledge/Lonely.md`"),
                         before.count("- [x] `Knowledge/Lonely.md`"))
        out = proc.stdout + proc.stderr
        # A tick is a change this tool CARRIES, so it is not an unexplained edit. The
        # comparison ignores ticks: warning on them meant the "only in that copy"
        # message fired on every run after anyone worked the list, always benignly,
        # which is how a warning stops being read.
        self.assertNotIn("not what this tool last generated", out)
        self.assertEqual(self.backups(), [],
                         "ticking an item made a backup on every subsequent run")

    def test_a_note_typed_beside_a_tick_is_still_reported(self):
        """Ignoring ticks must not mean ignoring what is next to them."""
        self.run_lint()
        self.tick_everything()
        self.q.write_text(self.q.read_text(encoding="utf-8") + "\n- a note I typed here\n",
                          encoding="utf-8")
        proc = self.run_lint()
        self.assertIn("not what this tool last generated", proc.stdout + proc.stderr)
        self.assertEqual(len(self.backups()), 1)

    def test_backups_are_bounded_and_never_overwrite_each_other(self):
        """Nothing pruned the folder, and the name was second-resolution while
        shutil.copy2 overwrites -- so two runs in one second destroyed a copy and a
        broken receipt grew the folder for ever."""
        self.run_lint()
        for i in range(15):
            self.q.write_text(self.q.read_text(encoding="utf-8") + f"\n- note {i}\n",
                              encoding="utf-8")
            self.run_lint()
        kept = self.backups()
        self.assertEqual(len(kept), 10, f"backups unbounded: {len(kept)} copies")
        bodies = [p.read_text(encoding="utf-8") for p in kept]
        self.assertEqual(len(set(bodies)), len(bodies), "two runs wrote the same backup name")
        self.assertTrue(any("note 14" in b for b in bodies), "the newest copy was pruned")

    def test_an_unwritable_receipt_still_reports_where_the_copy_went(self):
        """The receipt was written AFTER os.replace, unguarded. An unwritable one raised
        after the queue had been replaced AND copied aside, so `return notes` never ran:
        the user saw a traceback about a file they had not asked about, and never the
        line naming the copy they now needed."""
        self.q.write_text("# Vault Review Queue\n\nsomething from before\n", encoding="utf-8")
        (self.v / self.DIGESTS).mkdir(parents=True)      # a directory: cannot be written
        proc = self.run_lint()
        out = proc.stdout + proc.stderr
        self.assertNotIn("Traceback", out)
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("kept a copy", out)
        self.assertIn("could not record the generation receipt", out)
        kept = self.backups()
        self.assertEqual(len(kept), 1, f"expected exactly one copy, got {kept}")
        self.assertIn("something from before", kept[0].read_text(encoding="utf-8"))
        self.assertIn("- [ ] `Knowledge/Lonely.md`", self.q.read_text(encoding="utf-8"))

    def test_a_hand_edited_queue_is_backed_up_and_reported(self):
        self.run_lint()
        self.q.write_text(self.q.read_text(encoding="utf-8") + "\n- a note I typed here\n",
                          encoding="utf-8")
        proc = self.run_lint()
        out = proc.stdout + proc.stderr
        self.assertIn("not what this tool last generated", out)
        self.assertIn("kept a copy", out, "a backup nobody is told about is not a backup")
        kept = self.backups()
        self.assertEqual(len(kept), 1, f"expected exactly one copy, got {kept}")
        self.assertIn("a note I typed here", kept[0].read_text(encoding="utf-8"))

    def test_a_pre_existing_queue_with_no_receipt_is_kept_not_overwritten(self):
        self.q.write_text("# Vault Review Queue\n\nsomething from before this check existed\n",
                          encoding="utf-8")
        proc = self.run_lint()
        out = proc.stdout + proc.stderr
        self.assertIn("no previous digest recorded", out)
        kept = self.backups()
        self.assertEqual(len(kept), 1, f"expected exactly one copy, got {kept}")
        self.assertIn("something from before", kept[0].read_text(encoding="utf-8"))

    def test_a_tick_on_a_finding_that_is_gone_does_not_come_back(self):
        self.w("Knowledge/Other.md", "x\n")                # a second defect keeps the queue alive
        self.run_lint()
        self.tick_everything()
        self.index("Lonely")                               # fixes Lonely's index-gap and orphan
        self.run_lint()
        text = self.q.read_text(encoding="utf-8")
        self.assertNotIn("Knowledge/Lonely.md", text,
                         "a fixed finding must not be resurrected by its old tick")
        self.assertIn("- [x] `Knowledge/Other.md`", text,
                      "the still-open finding keeps the tick the person made")

    def test_the_receipt_is_recorded_per_queue_path(self):
        self.run_lint()
        d = self.v / self.DIGESTS
        self.assertTrue(d.is_file(), "no generation receipt written")
        data = json.loads(d.read_text(encoding="utf-8"))
        self.assertIn(str(self.q.resolve()), data, data)
        self.assertRegex(data[str(self.q.resolve())], r"^[0-9a-f]{64}$")

    def test_no_temp_file_is_left_behind(self):
        """The write is atomic (tmp + fsync + replace) so a torn worklist never exists --
        but a leftover tmp beside it would be its own mess."""
        self.run_lint()
        self.run_lint()
        self.assertEqual(list(self.q.parent.glob("queue.md.gt-tmp*")), [])


# ---------------------------------------------------------------------------- --json
class JsonOutputTest(LintBase):
    def test_json_parses_and_matches_text_mode(self):
        self.project("alpha")
        self.w("Knowledge/Lonely.md", "x [[Nowhere]]\n")
        text_proc, text_findings = self.lint()
        json_proc = self.py(LINT, self.v, "--json")
        self.assertEqual(json_proc.returncode, text_proc.returncode)
        data = json.loads(json_proc.stdout)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["vault"], str(self.v.resolve()))
        for f in data["findings"]:
            self.assertTrue({"kind", "path", "line", "message"} <= set(f), f)
        text_counts = {}
        for kind, _ in text_findings:
            text_counts[kind] = text_counts.get(kind, 0) + 1
        self.assertEqual(data["counts"], text_counts)
        self.assertEqual(sorted((f["kind"], f["path"]) for f in data["findings"]),
                         sorted(text_findings))

    def test_json_finding_shape_matches_the_documented_schema(self):
        """The docstring listed {kind,path,line,message} -- omitting proposed_fix, which
        is always emitted, and advertising `line`, which no default-mode check ever
        sets. It now says both, and this is the assertion behind that sentence."""
        self.w("Knowledge/Lonely.md", "x [[Nowhere]]\n")
        data = json.loads(self.py(LINT, self.v, "--json").stdout)
        self.assertTrue(data["findings"])
        for f in data["findings"]:
            self.assertEqual(set(f), {"kind", "path", "line", "message", "proposed_fix"}, f)
            self.assertIsNone(f["line"], f)
            self.assertTrue(f["proposed_fix"], f)

    def test_json_clean_vault(self):
        proc = self.py(LINT, "--vault", self.v, "--json")
        self.assertOk(proc)
        data = json.loads(proc.stdout)
        self.assertEqual((data["findings"], data["counts"]), ([], {}))


# ---------------------------------------------------------------------------- --runbooks
SHARED = "Export AWS_PROFILE=deploy before running terraform apply in any environment"


class RunbooksTest(LintBase):
    def runbooks(self, *extra):
        return self.py(LINT, "--vault", self.v, "--runbooks", *extra)

    def snapshot(self):
        return {str(p.relative_to(self.v)): p.read_bytes()
                for p in sorted(self.v.rglob("*")) if p.is_file()}

    def test_one_runbook_is_nothing_to_compare(self):
        self.w("Projects/alpha/runbook.md", f"# Run\n\n- {SHARED}\n")
        proc = self.runbooks()
        self.assertOk(proc)
        self.assertIn("nothing to compare", proc.stdout)
        data = json.loads(self.runbooks("--json").stdout)
        self.assertEqual(data["findings"], [])

    def test_duplicate_line_reported_once_with_both_paths(self):
        self.w("Projects/alpha/runbook.md", f"# Alpha\n\n- {SHARED}\n")
        self.w("Projects/beta/gamma/runbook.md",
               f"# Gamma\n\nintro line that is long enough to count here\n\n1.   {SHARED}\n")
        proc = self.runbooks()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(proc.stdout.count("[runbook-duplicate]"), 1, proc.stdout)
        data = json.loads(self.runbooks("--json").stdout)
        self.assertEqual(data["counts"], {"runbook-duplicate": 1})
        (f,) = data["findings"]
        self.assertEqual(f["kind"], "runbook-duplicate")
        self.assertEqual(sorted((l["path"], l["line"]) for l in f["locations"]),
                         [("Projects/alpha/runbook.md", 3), ("Projects/beta/gamma/runbook.md", 5)])

    def test_near_identical_lines_cluster(self):
        self.w("Projects/alpha/runbook.md", f"{SHARED}\n")
        self.w("Projects/beta/runbook.md", f"{SHARED}.\n")
        data = json.loads(self.runbooks("--json").stdout)
        self.assertEqual(len(data["findings"]), 1, data)

    def test_distinct_lines_and_headings_not_clustered(self):
        self.w("Projects/alpha/runbook.md",
               "# A heading that is long and identical in both files\n"
               "Restart the ingest worker with systemctl after rotating keys\n"
               "- short same line\n```\n```\n")
        self.w("Projects/beta/runbook.md",
               "# A heading that is long and identical in both files\n"
               "Check the dashboard for queue depth before paging anyone\n"
               "- short same line\n```\n```\n")
        proc = self.runbooks()
        self.assertOk(proc)
        self.assertNotIn("[runbook-duplicate]", proc.stdout)

    def test_detector_writes_nothing(self):
        self.w("Projects/alpha/runbook.md", f"- {SHARED}\n")
        self.w("Projects/beta/runbook.md", f"- {SHARED}\n")
        before = self.snapshot()
        self.runbooks()
        self.runbooks("--json")
        self.assertEqual(self.snapshot(), before)


class RunbookSkillTest(unittest.TestCase):
    def test_skill_lint_passes(self):
        import subprocess
        from _harness import GT, PYTHON
        proc = subprocess.run([PYTHON, str(SCRIPTS / "skill_lint.py"), str(GT)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        skill = (GT / "skills" / "gt-runbook-lint" / "SKILL.md").read_text()
        self.assertIn("gt_lint.py --vault", skill)
        self.assertIn("--runbooks", skill)


if __name__ == "__main__":
    unittest.main()
