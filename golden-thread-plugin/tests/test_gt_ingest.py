"""gt_ingest.py: the migration-candidate scanner and its classifiers.

Classifier functions are pure (no HOME at import), so they are tested in-process;
everything else goes through the CLI against a sandbox project and sandbox HOME.
"""
import json
import os
import shutil
import unittest

from _harness import Sandbox, SCRIPTS, load_module

INGEST = SCRIPTS / "gt_ingest.py"


class ClassifierTest(Sandbox):
    def setUp(self):
        super().setUp()
        old = os.environ.get("HOME")
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old is not None
                        else os.environ.pop("HOME", None))
        os.environ["HOME"] = str(self.home)
        self.m = load_module(INGEST, "gt_ingest_under_test")

    def test_memory_generic_knowledge_keywords(self):
        """Every keyword in the knowledge list must be reachable. 'infrastructure'
        contains 'structure', which the design rule tests first, so an
        infrastructure_*.md memory file is sent to design, never to knowledge."""
        for kw in ("auth", "platform", "networking", "identity", "orchestration", "infrastructure"):
            with self.subTest(kw=kw):
                self.assertEqual(self.m.classify_memory_file(f"{kw}_notes.md", "plain"), "knowledge",
                                 f"'{kw}' in a memory filename did not classify as knowledge")

    def test_author_files_are_not_knowledge(self):
        """Keywords match whole words: 'auth' is a knowledge keyword, but AUTHORS.md
        and an authors_notes.md memory file are about people, not authentication."""
        from pathlib import Path
        self.assertNotEqual(self.m.classify_doc_file(Path("AUTHORS.md"), ""), "knowledge")
        self.assertNotEqual(self.m.classify_memory_file("authors_notes.md", "plain"), "knowledge")
        self.assertEqual(self.m.classify_doc_file(Path("auth-flow.md"), ""), "knowledge")
        self.assertEqual(self.m.classify_memory_file("auth_tokens.md", "plain"), "knowledge")

    def test_memory_precedence(self):
        c = self.m.classify_memory_file
        self.assertEqual(c("feedback_platform.md", ""), "decisions")          # feedback first
        self.assertEqual(c("platform_architecture.md", ""), "design")         # design before knowledge
        self.assertEqual(c("golden-thread-notes.md", ""), "global_memory")
        self.assertEqual(c("golden_thread.md", ""), "global_memory")
        self.assertEqual(c("user.md", "never"), "skip")
        self.assertEqual(c("Schema.md", ""), "design")                        # case-insensitive

    def test_memory_content_fallback(self):
        c = self.m.classify_memory_file
        self.assertEqual(c("misc.md", "We never deploy on Friday."), "decisions")
        self.assertEqual(c("misc.md", "Discovered a gotcha in the cache."), "research")
        self.assertEqual(c("misc.md", "nothing notable"), "research")

    def test_doc_classifier(self):
        from pathlib import Path
        c = lambda n: self.m.classify_doc_file(Path(n), "")
        self.assertEqual(c("ARCHITECTURE.md"), "design")
        self.assertEqual(c("system-overview.md"), "design")
        for kw in ("auth", "platform", "networking", "identity", "orchestration",
                   "infrastructure", "knowledge"):
            with self.subTest(kw=kw):
                self.assertEqual(c(f"{kw}-guide.md"), "knowledge")
        self.assertEqual(c("backlog.md"), "ideas")
        self.assertEqual(c("future-ideas.md"), "ideas")
        self.assertEqual(c("CHANGELOG.md"), "skip")
        self.assertEqual(c("notes.md"), "research")

    def test_claude_md_section_classifier(self):
        c = self.m.classify_claude_md_section
        self.assertEqual(c("Key Decisions", ""), "decisions")
        self.assertEqual(c("Conventions", ""), "decisions")
        self.assertEqual(c("Golden Thread", ""), "skip")
        self.assertEqual(c("Memory", ""), "skip")
        self.assertEqual(c("Deploy", ""), "design")
        self.assertEqual(c("Anything else", ""), "decisions")


class CliTest(Sandbox):
    def scan(self, proj, *extra):
        proc = self.py(INGEST, proj, "--json", *extra)
        self.assertOk(proc)
        return json.loads(proc.stdout)

    def by_name(self, cands):
        return {c["filename"]: c for c in cands}

    def test_missing_project_exits_1_with_json_error(self):
        proc = self.py(INGEST, self.tmp / "nope", "--json")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error", json.loads(proc.stdout))

    def test_root_docs_classified_and_boilerplate_skipped(self):
        p = self.tmp / "proj"
        p.mkdir()
        (p / "README.md").write_text("# readme\n")
        (p / "ARCHITECTURE.md").write_text("layers\n")
        (p / "auth-flow.md").write_text("tokens\n")
        (p / "backlog.md").write_text("later\n")
        (p / "notes.md").write_text("misc\n")
        (p / "empty.md").write_text("   \n")
        got = self.by_name(self.scan(p))
        self.assertNotIn("README.md", got)
        self.assertNotIn("empty.md", got, "empty preview should be filtered out")
        self.assertEqual(got["ARCHITECTURE.md"]["suggested_dest"], "design")
        self.assertEqual(got["auth-flow.md"]["suggested_dest"], "knowledge")
        self.assertEqual(got["backlog.md"]["suggested_dest"], "ideas")
        self.assertEqual(got["notes.md"]["suggested_dest"], "research")
        for c in got.values():
            self.assertEqual(c["type"], "doc")
            self.assertEqual(c["confidence"], "low")

    def test_claude_md_sections(self):
        p = self.tmp / "proj"
        p.mkdir()
        (p / "CLAUDE.md").write_text(
            "# Proj\n\npreamble\n\n## Key Decisions\n\nuse sqlite\n\n## Golden Thread\n\nvault stuff\n"
            "\n### Deploy\n\nrsync to prod\n")
        got = self.by_name(self.scan(p))
        self.assertEqual(got["CLAUDE.md § Key Decisions"]["suggested_dest"], "decisions")
        self.assertEqual(got["CLAUDE.md § Deploy"]["suggested_dest"], "design")
        self.assertNotIn("CLAUDE.md § Golden Thread", got)
        self.assertNotIn("CLAUDE.md", got, "CLAUDE.md must not also be listed as a doc")

    def test_tech_stack_detection(self):
        p = self.tmp / "proj"
        p.mkdir()
        (p / "package.json").write_text(json.dumps(
            {"engines": {"node": ">=20"}, "dependencies": {"express": "4", "zod": "3"}}))
        (p / "requirements.txt").write_text("requests\n")
        got = self.by_name(self.scan(p))
        ts = got["tech-stack (detected)"]
        self.assertEqual(ts["suggested_dest"], "design")
        self.assertIn("Node.js >=20", ts["content_preview"])
        self.assertIn("express", ts["content_preview"])
        self.assertIn("Python", ts["content_preview"])

    def test_git_log_only_for_repos(self):
        p = self.tmp / "proj"
        p.mkdir()
        (p / "x.txt").write_text("x")
        self.assertNotIn("git log (last 20 commits)", self.by_name(self.scan(p)))
        if not shutil.which("git"):
            self.skipTest("git not installed")
        self.git_init(p)
        got = self.by_name(self.scan(p))
        self.assertIn("init", got["git log (last 20 commits)"]["content_preview"])

    def test_human_readable_output(self):
        p = self.tmp / "proj"
        p.mkdir()
        (p / "ARCHITECTURE.md").write_text("layers\n")
        proc = self.py(INGEST, p)
        self.assertOk(proc)
        self.assertIn("=== DESIGN (1 items) ===", proc.stdout)
        self.assertIn("[low] ARCHITECTURE.md", proc.stdout)

    def test_finds_claude_code_memory_dir(self):
        """Claude Code stores a project's memory at ~/.claude/projects/<encoded>/memory,
        where <encoded> is the absolute path with '/' replaced by '-' -- INCLUDING the
        leading '-' (e.g. /Users/me/proj -> -Users-me-proj). encode_project_path()
        strips that leading dash, so the memory directory is never found."""
        p = self.tmp / "proj"
        p.mkdir()
        encoded = str(p.resolve()).replace("/", "-")
        self.assertTrue(encoded.startswith("-"))
        mem = self.home / ".claude" / "projects" / encoded / "memory"
        mem.mkdir(parents=True)
        (mem / "networking_gotchas.md").write_text("the VPN drops idle sockets\n")
        got = self.by_name(self.scan(p))
        self.assertIn("networking_gotchas.md", got,
                      "memory file under the Claude Code encoded path was not scanned")
        self.assertEqual(got["networking_gotchas.md"]["suggested_dest"], "knowledge")
        self.assertEqual(got["networking_gotchas.md"]["confidence"], "high")


if __name__ == "__main__":
    unittest.main()
