"""gt_lint.py 0.18.0 checks that file into the review queue: adr-expires, bundled-concept,
decision-candidate, memory-entity-orphan.

Each runs as part of a normal gt_lint run (no extra flag), writes through the one
--queue path every other check uses, and honours lint-declines.md.
"""
import json

from _harness import Sandbox, SCRIPTS, TOOLS, ENFORCEMENT_HOOKS

LINT = SCRIPTS / "gt_lint.py"
VI = SCRIPTS / "vault_init.py"
NEW = ("adr-expires", "bundled-concept", "decision-candidate", "memory-entity-orphan")


class Base(Sandbox):
    def setUp(self):
        super().setUp()
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        hooks.mkdir(parents=True)
        for n in ENFORCEMENT_HOOKS:
            (hooks / n).write_text("#!/bin/sh\nexit 0\n")
            (hooks / n).chmod(0o755)
        self.v = self.make_vault()

    def w(self, rel, text):
        p = self.v / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def project(self, slug, *extra):
        self.assertOk(self.py(VI, "create-project", "--vault", self.v, "--name", slug,
                              "--domain", "t", "--topology", "local", *extra))

    def lint(self, check=None, queue=None):
        args = [LINT, "--vault", self.v, "--json"] + (["--queue", queue] if queue else [])
        p = self.py(*args)
        self.assertIn(p.returncode, (0, 1), p.stderr)
        found = json.loads(p.stdout)["findings"]
        return [f for f in found if check is None or f["kind"] == check]

    def decline(self, *lines):
        self.w("lint-declines.md", "# Declines\n\n" + "".join("suppress: %s\n" % l for l in lines))


class FreshVault(Base):
    def test_a_fresh_vault_and_project_raise_none_of_the_new_checks(self):
        self.project("alpha")
        hits = [f for f in self.lint() if f["kind"] in NEW]
        self.assertEqual(hits, [], hits)


class AdrExpires(Base):
    def setUp(self):
        super().setUp()
        self.project("alpha")
        self.adr = lambda *a: self.py(TOOLS / "gt_adr.py", "--vault", self.v, *a)
        for args in (("--title", "Use SQLite", "--expires-when", "until upgrade"),
                     ("--title", "Pin v1.2", "--expires", "2020-01-01"),
                     ("--title", "Future pin", "--expires", "2099-01-01"),
                     ("--title", "Plain decision")):
            self.assertOk(self.adr("allocate", "alpha", *args))
        self.assertOk(self.adr("merge", "alpha"))

    def test_condition_always_past_date_only_and_plain_never(self):
        msgs = [f["message"] for f in self.lint("adr-expires")]
        self.assertEqual(len(msgs), 2, msgs)
        self.assertTrue(any("ADR-1" in m and "until upgrade" in m for m in msgs),
                        "the condition text must be in the queue entry: %s" % msgs)
        self.assertTrue(any("ADR-2" in m and "2020-01-01" in m for m in msgs), msgs)
        self.assertFalse(any("ADR-3" in m or "ADR-4" in m for m in msgs), msgs)

    def test_entry_reaches_the_review_queue_with_its_condition(self):
        q = self.tmp / "review-queue.md"
        self.lint(queue=q)
        text = q.read_text()
        self.assertIn("## adr-expires", text)
        self.assertIn("until upgrade", text)

    def test_one_adr_can_be_declined(self):
        self.decline("Projects/alpha/decisions.md:ADR-1")
        msgs = [f["message"] for f in self.lint("adr-expires")]
        self.assertEqual([m for m in msgs if "ADR-1" in m], [])
        self.assertTrue(any("ADR-2" in m for m in msgs))

    def test_a_superseded_adr_is_not_asked_about(self):
        self.assertOk(self.adr("allocate", "alpha", "--title", "Replace SQLite",
                               "--supersedes", "1"))
        self.assertOk(self.adr("merge", "alpha"))
        msgs = [f["message"] for f in self.lint("adr-expires")]
        self.assertFalse(any("ADR-1 " in m for m in msgs), msgs)

    def test_frontmatter_spelling_is_read_too(self):
        """`expires_when:` -- the Knowledge-page spelling -- written by hand in a slot."""
        self.assertOk(self.adr("allocate", "alpha", "--title", "Workaround"))
        slot = self.v / "Projects/golden-thread/spool/decisions/alpha/0005.md"
        slot.write_text(slot.read_text() + 'expires_when: "SDK 2.0 ships"\n')
        self.assertOk(self.adr("merge", "alpha"))
        msgs = [f["message"] for f in self.lint("adr-expires")]
        self.assertTrue(any("ADR-5" in m and "SDK 2.0 ships" in m for m in msgs), msgs)

    def test_allocate_rejects_a_malformed_date(self):
        p = self.adr("allocate", "alpha", "--title", "x", "--expires", "next week")
        self.assertEqual(p.returncode, 2)


PAGE = """---
title: {title}
category: {category}
tags: [{tags}]
sources: []
created: 2026-09-01
updated: 2026-09-01
status: growing
---

# {title}

{body}
"""


class BundledConcept(Base):
    def page(self, name, title, tags, heads, category="reference"):
        body = "".join("## %s\n\ntext\n\n" % h for h in heads)
        self.w("Knowledge/%s.md" % name, PAGE.format(title=title, tags=tags, body=body,
                                                     category=category))
        return "Knowledge/%s.md" % name

    def hits(self):
        return {f["path"]: f for f in self.lint("bundled-concept")}

    def test_divergent_page_fires_with_its_heading_list(self):
        rel = self.page("SQLite Schema Migration", "SQLite Schema Migration", "sqlite, migration",
                        ["What this is", "Running migrations", "Testing migrations", "Rollback",
                         "Production hygiene"])
        h = self.hits()
        self.assertIn(rel, h)
        self.assertIn("Rollback", h[rel]["message"])
        self.assertIn("Consider splitting", h[rel]["message"])

    def test_coherent_page_does_not_fire(self):
        rel = self.page("Widget Cache", "Widget Cache", "cache",
                        ["Widget cache layout", "Cache eviction", "Widget warmup",
                         "Cache sizing", "History"])
        self.assertNotIn(rel, self.hits())

    def test_three_headings_never_fire(self):
        rel = self.page("Three", "Alpha", "x", ["One", "Two", "Three"])
        self.assertNotIn(rel, self.hits())

    def test_decision_pages_are_exempt(self):
        rel = self.page("Dec", "Alpha", "x", ["A", "B", "C", "D", "E", "F"], category="decision")
        self.assertNotIn(rel, self.hits())

    def test_exactly_two_on_topic_is_bundled_three_is_coherent(self):
        """The case the request left open: 2 sharing fires, 3 sharing does not."""
        two = self.page("Two", "Gizmo Builds", "gizmo",
                        ["Gizmo setup", "Builds on CI", "Lunch menu", "Parking"])
        three = self.page("Three", "Gizmo Builds", "gizmo",
                          ["Gizmo setup", "Builds on CI", "Gizmo release", "Parking"])
        h = self.hits()
        self.assertIn(two, h)
        self.assertNotIn(three, h)

    def test_declined_page_is_skipped(self):
        rel = self.page("Mixed", "Alpha", "x", ["One", "Two", "Three", "Four"])
        self.assertIn(rel, self.hits())
        self.decline(rel)
        self.assertNotIn(rel, self.hits())


DESIGN = """# Alpha Design

## We chose the queue

We chose a queue over polling because the broker drops idle sockets.
This paragraph says nothing in particular about anything.
The retry loop is deliberately bounded to three attempts.
```
we decided inside a code block, which is not prose
```
We use SQLite instead of Postgres for the single-writer case.
"""


class DecisionCandidate(Base):
    def setUp(self):
        super().setUp()
        self.project("alpha")
        self.w("Projects/alpha/design.md", DESIGN)

    def test_three_hits_with_line_sentence_and_title(self):
        hits = self.lint("decision-candidate")
        lines = sorted(f["line"] for f in hits)
        self.assertEqual(lines, [5, 7, 11], hits)
        by = {f["line"]: f for f in hits}
        self.assertIn('"we chose"', by[5]["message"])
        self.assertIn("broker drops idle sockets", by[5]["message"])
        self.assertIn("line 7", by[7]["message"])
        self.assertIn("we use ... instead of", by[11]["message"])
        self.assertIn('--title "We chose a queue over polling', by[5]["proposed_fix"])
        self.assertTrue(all(f["path"] == "Projects/alpha/design.md" for f in hits))

    def test_research_md_is_scanned_too(self):
        self.w("Projects/alpha/research.md", "# R\n\n## 2026-01-01: x\n\nThis is intentional.\n")
        self.assertIn("Projects/alpha/research.md",
                      {f["path"] for f in self.lint("decision-candidate")})

    def test_one_line_declined_by_hash_survives_the_line_moving(self):
        hit = [f for f in self.lint("decision-candidate") if f["line"] == 7][0]
        key = hit["proposed_fix"].split("suppress: ")[1].split("`")[0]
        self.decline(key)
        self.w("Projects/alpha/design.md", "# moved\n\n" + DESIGN)
        lines = sorted(f["line"] for f in self.lint("decision-candidate"))
        self.assertEqual(lines, [7, 13], "the declined line came back after moving")

    def test_one_line_declined_by_number(self):
        self.decline("Projects/alpha/design.md:L5")
        self.assertEqual(sorted(f["line"] for f in self.lint("decision-candidate")), [7, 11])

    def test_no_adr_is_written(self):
        before = (self.v / "Projects/alpha/decisions.md").read_bytes()
        self.lint(queue=self.tmp / "q.md")
        self.assertEqual((self.v / "Projects/alpha/decisions.md").read_bytes(), before)
        self.assertFalse(list((self.v / "Projects/golden-thread/spool/decisions/alpha")
                              .glob("0001.md")))

    def test_phrases_are_configurable_through_gt_settings(self):
        self.config(vault_path=str(self.v))
        self.assertOk(self.py(SCRIPTS / "gt_settings.py", "set", "decision_signals",
                              "-deliberately;+says nothing"))
        lines = sorted(f["line"] for f in self.lint("decision-candidate"))
        self.assertEqual(lines, [5, 6, 11])

    def test_off_silences_the_check(self):
        self.config(vault_path=str(self.v), decision_signals="off")
        self.assertEqual(self.lint("decision-candidate"), [])

    def test_queue_entry_carries_the_line(self):
        q = self.tmp / "review-queue.md"
        self.lint(queue=q)
        self.assertIn("line 5:", q.read_text())


MEM = """---
name: {name}
description: "{name}"
metadata:
  type: project
{ent}---

{body}
"""


class MemoryEntityOrphan(Base):
    def setUp(self):
        super().setUp()
        self.project("alpha")

    def mem(self, name, body, entities=None):
        ent = "entities:\n" + "".join("  - %s\n" % e for e in entities) if entities else ""
        return self.w("Projects/alpha/memory/%s.md" % name,
                      MEM.format(name=name, ent=ent, body=body))

    def hits(self):
        return {f["path"]: f for f in self.lint("memory-entity-orphan")}

    def test_undeclared_name_mentioned_four_times_is_suggested(self):
        self.mem("tagged", "about the auth-service", ["auth-service"])
        self.mem("tokensvc", "TOKENSVC issues tokens. TOKENSVC rotates. TOKENSVC is slow. Ask TOKENSVC.")
        h = self.hits()
        rel = "Projects/alpha/memory/tokensvc.md"
        self.assertIn(rel, h)
        self.assertIn("TOKENSVC (4×)", h[rel]["message"])
        self.assertIn("entities: [TOKENSVC]", h[rel]["proposed_fix"])

    def test_a_declared_name_is_suggested_wherever_it_recurs(self):
        self.mem("tagged", "x", ["auth-service"])
        self.mem("untagged", "The auth-service restarts; auth-service logs; auth-service.")
        self.assertIn("Projects/alpha/memory/untagged.md", self.hits())

    def test_declared_entities_are_not_suggested_again(self):
        self.mem("ok", "TOKENSVC TOKENSVC TOKENSVC TOKENSVC", ["TOKENSVC"])
        self.assertNotIn("Projects/alpha/memory/ok.md", self.hits())

    def test_files_without_entities_are_otherwise_unaffected(self):
        self.mem("quiet", "A note mentioning TOKENSVC once.")
        self.mem("tagged", "x", ["auth-service"])
        self.assertEqual(self.hits(), {})

    def test_guesses_wait_until_the_project_adopts_the_field(self):
        """Measured flood: before any memory file declares entities, no guesses."""
        self.mem("tokensvc", "TOKENSVC TOKENSVC TOKENSVC TOKENSVC")
        self.assertEqual(self.hits(), {})

    def test_one_name_can_be_declined(self):
        self.mem("tagged", "x", ["auth-service"])
        self.mem("tokensvc", "TOKENSVC TOKENSVC TOKENSVC TOKENSVC")
        self.decline("Projects/alpha/memory/tokensvc.md:TOKENSVC")
        self.assertEqual(self.hits(), {})


if __name__ == "__main__":
    import unittest
    unittest.main()
