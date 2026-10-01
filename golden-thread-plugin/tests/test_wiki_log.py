"""gt-wiki wiki_log.py: the deterministic writer for log.md and index.md.

Contracts pinned here:
  * `log` accepts only the closed vocabulary and appends, never rewrites;
  * `index` replaces an existing `[[Page]]` entry IN PLACE (even when --section
    names another section), adds a new one under --section, appends otherwise;
  * a failed command leaves the file byte-identical;
  * inside a Golden Thread vault (0.2.4) it writes nothing itself: `log` goes through
    gt_log.py (log.md is generated), `index` through the write queue and broker.
"""
import datetime
import json
import re
import shutil
import unittest

from _harness import Sandbox, WIKI_SCRIPTS, SCRIPTS, TOOLS

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


class WikiLogInGtVaultTest(Sandbox):
    """gt-wiki 0.2.4: inside a Golden Thread vault wiki_log.py writes nothing itself (Core
    rule 1). log.md is generated, so `log` goes through gt_log.py; `index` becomes one
    write-queue request, drained at once by gt_broker.py, or deposited for the next drain
    when gt's scripts cannot be found."""

    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.gt = self.vault / "Projects" / "golden-thread"
        (self.gt / "tools").mkdir(parents=True)
        (self.gt / "sessions").mkdir()
        (self.gt / "README.md").write_text("# golden-thread\n\n## Tasks\n", encoding="utf-8")
        for tool in TOOLS.glob("*.py"):
            shutil.copy(tool, self.gt / "tools" / tool.name)
        self.index = self.vault / "index.md"
        self.index.write_text(INDEX, encoding="utf-8")
        self.queue = self.gt / "spool" / "queue"

    def wl(self, *args, scripts=None):
        return self.py(LOG, self.vault, *args,
                       env={"GT_CORE_SCRIPTS": str(scripts or SCRIPTS),
                            "CLAUDE_CODE_SESSION_ID": "wiki-test"})

    def pending(self):
        return sorted(self.queue.glob("*.json")) if self.queue.is_dir() else []

    # -- log ----------------------------------------------------------------------
    def test_log_goes_through_gt_log_and_log_md_is_generated(self):
        p = self.wl("log", "ingest", "Some Source", "--line", "first", "--line", "second")
        self.assertOk(p)
        today = datetime.date.today().isoformat()
        spooled = "".join(f.read_text(encoding="utf-8")
                          for f in (self.gt / "spool" / "log").glob("*.md"))
        self.assertRegex(spooled, re.escape(today) + r".* \[ingest\] Some Source — first; second")
        text = (self.vault / "log.md").read_text(encoding="utf-8")
        self.assertIn("[ingest] Some Source — first; second", text)
        self.assertNotIn("## [", text, "the standalone-wiki entry shape was written to log.md")

    def test_log_without_gt_log_writes_nothing_and_fails(self):
        for f in (self.gt / "tools").glob("*.py"):
            f.unlink()
        p = self.wl("log", "lint", "Weekly", scripts=self.tmp / "no-gt")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("NOT written", p.stderr)
        self.assertIn("[lint] Weekly", p.stderr)
        self.assertFalse((self.vault / "log.md").exists())

    def test_closed_vocabulary_still_applies(self):
        p = self.wl("log", "update", "Title")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("op must be one of", p.stderr)

    # -- index --------------------------------------------------------------------
    def test_new_entry_is_queued_as_append_and_drained_into_the_section(self):
        p = self.wl("index", "Beta", "second tool", "--section", "Tools")
        self.assertOk(p)
        self.assertIn("[apply]", p.stdout)
        tools = self.index.read_text(encoding="utf-8").split("## Tools")[1].split("## Concepts")[0]
        self.assertLess(tools.index("[[Alphabet Soup"), tools.index("- [[Beta]] — second tool"))
        self.assertEqual(self.pending(), [], "the request was left in the queue")
        log = "".join(f.read_text() for f in (self.gt / "spool" / "broker").glob("log-*.jsonl"))
        self.assertIn('"append"', log)

    def test_existing_entry_is_replaced_in_its_section(self):
        p = self.wl("index", "Gamma", "rewritten summary", "--section", "Tools")
        self.assertOk(p)
        text = self.index.read_text(encoding="utf-8")
        self.assertIn("- [[Gamma]] — rewritten summary", text.split("## Concepts")[1])
        self.assertEqual(text.count("[[Gamma]]"), 1, "entry was duplicated")
        self.assertIn("- [[Alpha]] — first tool", text.split("## Tools")[1])

    def test_backslashes_and_metacharacters_stay_literal(self):
        summary = r"matches \d+ in C:\temp\new"
        self.assertOk(self.wl("index", "C++ (lang) [draft]?", "first", "--section", "Concepts"))
        self.assertOk(self.wl("index", "C++ (lang) [draft]?", summary))
        text = self.index.read_text(encoding="utf-8")
        self.assertEqual(text.count("[[C++ (lang) [draft]?]]"), 1)
        self.assertIn("- [[C++ (lang) [draft]?]] — " + summary + "\n", text)

    def test_missing_section_queues_nothing(self):
        before = self.index.read_bytes()
        p = self.wl("index", "Beta", "x", "--section", "Tool")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("section '## Tool' not found", p.stderr)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(self.pending(), [])

    def test_entry_above_the_first_section_is_refused(self):
        self.index.write_text("# Index\n\n- [[Top]] — preamble entry\n\n## Tools\n", encoding="utf-8")
        before = self.index.read_bytes()
        p = self.wl("index", "Top", "new")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not inside a `## ` section", p.stderr)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(self.pending(), [])

    def test_without_gt_scripts_the_request_is_deposited_for_the_next_drain(self):
        before = self.index.read_bytes()
        p = self.wl("index", "Gamma", "later", scripts=self.tmp / "no-gt")
        self.assertOk(p)
        self.assertIn("next `gt_broker.py drain`", p.stdout)
        self.assertEqual(self.index.read_bytes(), before, "index.md was written directly")
        [req_file] = self.pending()
        req = json.loads(req_file.read_text(encoding="utf-8"))
        self.assertEqual((req["op"], req["section"], req["path"], req["session"]),
                         ("replace-section", "Concepts", "index.md", "wiki-test"))
        # gt's own validator accepts it, and the broker applies it
        d = self.py(SCRIPTS / "gt_broker.py", "drain", "--vault", self.vault)
        self.assertOk(d)
        self.assertIn("- [[Gamma]] — later", self.index.read_text(encoding="utf-8"))
        self.assertEqual(self.pending(), [])


if __name__ == "__main__":
    unittest.main()
