"""gt-wiki wiki_log.py: the deterministic writer for log.md and index.md.

Contracts pinned here:
  * `log` accepts only the closed vocabulary and appends, never rewrites;
  * `index` replaces an existing `[[Page]]` entry IN PLACE (even when --section
    names another section), adds a new one under --section, appends otherwise;
  * a failed command leaves the file byte-identical.
"""
import datetime
import unittest

from _harness import Sandbox, WIKI_SCRIPTS

LOG = WIKI_SCRIPTS / "wiki_log.py"
OPS = ("ingest", "query", "lint", "refresh", "graduate", "retire", "relocate")

INDEX = """# Index

Every Knowledge page gets one line here.

## Tools

- [[Alpha]] — first tool
- [[Alphabet Soup|soup]] — aliased entry

## Concepts

- [[Gamma]] — a concept
"""


class WikiLogTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.log = self.vault / "log.md"
        self.index = self.vault / "index.md"
        self.log.write_text("# Log\n\nexisting entry\n", encoding="utf-8")
        self.index.write_text(INDEX, encoding="utf-8")

    def wl(self, *args):
        return self.py(LOG, self.vault, *args)

    # -- log ----------------------------------------------------------------------
    def test_log_appends_dated_entry_with_bullets(self):
        p = self.wl("log", "ingest", "Some Source", "--line", "first", "--line", "second")
        self.assertOk(p)
        today = datetime.date.today().isoformat()
        text = self.log.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Log\n\nexisting entry\n"), "log.md was rewritten, not appended")
        self.assertIn(f"## [{today}] ingest | Some Source\n- first\n- second\n", text)
        self.assertIn(f"## [{today}] ingest | Some Source", p.stdout)

    def test_every_op_in_the_closed_vocabulary_is_accepted(self):
        for op in OPS:
            with self.subTest(op=op):
                self.assertOk(self.wl("log", op, f"t-{op}"))
                self.assertIn(f"] {op} | t-{op}", self.log.read_text(encoding="utf-8"))

    def test_ops_outside_the_vocabulary_are_rejected_and_log_untouched(self):
        before = self.log.read_bytes()
        for op in ("update", "delete", "Ingest", "ingest ", "edit"):
            with self.subTest(op=op):
                p = self.wl("log", op, "Title")
                self.assertNotEqual(p.returncode, 0, f"op {op!r} was accepted")
                self.assertIn("op must be one of", p.stderr)
                self.assertEqual(self.log.read_bytes(), before, f"op {op!r} wrote to log.md")

    def test_log_without_trailing_newline_does_not_glue_entries(self):
        self.log.write_text("# Log\nlast line without newline", encoding="utf-8")
        self.assertOk(self.wl("log", "lint", "Weekly"))
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertIn("last line without newline", lines)
        self.assertTrue(any(l.startswith("## [") and l.endswith("lint | Weekly") for l in lines))

    def test_log_is_created_when_absent(self):
        self.log.unlink()
        self.assertOk(self.wl("log", "query", "Q"))
        self.assertIn("query | Q", self.log.read_text(encoding="utf-8"))

    # -- index --------------------------------------------------------------------
    def test_new_entry_is_added_under_the_named_section(self):
        p = self.wl("index", "Beta", "second tool", "--section", "Tools")
        self.assertOk(p)
        text = self.index.read_text(encoding="utf-8")
        tools = text.split("## Tools")[1].split("## Concepts")[0]
        self.assertIn("- [[Beta]] — second tool", tools)
        # the section's existing entries keep their order, the new one lands last
        self.assertLess(tools.index("[[Alpha]]"), tools.index("[[Beta]]"))
        self.assertLess(tools.index("[[Alphabet Soup"), tools.index("[[Beta]]"))
        self.assertIn("- [[Gamma]] — a concept", text.split("## Concepts")[1])
        self.assertIn("added to Tools", p.stdout)

    def test_new_entry_under_last_section_and_empty_section(self):
        self.index.write_text("# Index\n\n## Empty\n## Last\n\n- [[X]] — x\n", encoding="utf-8")
        self.assertOk(self.wl("index", "Y", "why", "--section", "Last"))
        self.assertOk(self.wl("index", "Z", "zed", "--section", "Empty"))
        text = self.index.read_text(encoding="utf-8")
        empty = text.split("## Empty")[1].split("## Last")[0]
        last = text.split("## Last")[1]
        self.assertIn("- [[Z]] — zed", empty)
        self.assertIn("- [[X]] — x\n- [[Y]] — why", last)

    def test_new_entry_without_section_is_appended(self):
        self.assertOk(self.wl("index", "Delta", "fourth"))
        self.assertTrue(self.index.read_text(encoding="utf-8").endswith("- [[Delta]] — fourth\n"))

    def test_existing_entry_replaced_in_place_not_moved(self):
        before = self.index.read_text(encoding="utf-8").splitlines()
        p = self.wl("index", "Gamma", "rewritten summary", "--section", "Tools")
        self.assertOk(p)
        after = self.index.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(before), len(after), "replacement changed the line count")
        i = before.index("- [[Gamma]] — a concept")
        self.assertEqual(after[i], "- [[Gamma]] — rewritten summary")
        self.assertEqual(sum("[[Gamma]]" in l for l in after), 1, "entry was duplicated")
        self.assertIn("replaced", p.stdout)

    def test_aliased_entry_is_replaced_and_prefix_page_is_not(self):
        self.assertOk(self.wl("index", "Alphabet Soup", "new soup"))
        self.assertOk(self.wl("index", "Alph", "a prefix of two titles"))
        text = self.index.read_text(encoding="utf-8")
        self.assertIn("- [[Alphabet Soup]] — new soup", text)
        self.assertNotIn("aliased entry", text)
        self.assertIn("- [[Alpha]] — first tool", text, "a page whose title is a prefix clobbered [[Alpha]]")
        self.assertIn("- [[Alph]] — a prefix of two titles", text)

    def test_title_with_regex_metacharacters(self):
        self.assertOk(self.wl("index", "C++ (lang) [draft]?", "first", "--section", "Concepts"))
        self.assertOk(self.wl("index", "C++ (lang) [draft]?", "second"))
        text = self.index.read_text(encoding="utf-8")
        self.assertEqual(text.count("[[C++ (lang) [draft]?]]"), 1)
        self.assertIn("— second", text)

    def test_missing_section_fails_and_leaves_index_untouched(self):
        before = self.index.read_bytes()
        p = self.wl("index", "Beta", "x", "--section", "Nope")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("section '## Nope' not found", p.stderr)
        self.assertEqual(self.index.read_bytes(), before)

    def test_section_name_is_not_prefix_matched(self):
        before = self.index.read_bytes()
        p = self.wl("index", "Beta", "x", "--section", "Tool")
        self.assertNotEqual(p.returncode, 0, "'Tool' matched the '## Tools' heading")
        self.assertEqual(self.index.read_bytes(), before)

    def test_replacing_an_entry_keeps_backslashes_in_the_summary_literal(self):
        # A summary is free text: a Windows path or a regex in it is ordinary content.
        summary = r"matches \d+ in C:\temp\new"
        p = self.wl("index", "Gamma", summary)
        self.assertOk(p, "replacing an entry whose new summary holds a backslash crashed "
                         "(wiki_log.py passes the summary to re.sub as a replacement template)")
        self.assertIn("- [[Gamma]] — " + summary + "\n", self.index.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
