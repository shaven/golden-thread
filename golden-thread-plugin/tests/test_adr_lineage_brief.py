"""0.18.0: decision lineage (gt_adr.py lineage), entity lookup (gt_entities.py) and the
repo brief (gt_brief.py) -- the three readers of the structured ADR fields and of
memory `entities:`.
"""
from _harness import Sandbox, SCRIPTS, TOOLS

VI = SCRIPTS / "vault_init.py"

DECISIONS = """# Demo Decisions

## ADR-1: Store session tokens in a local file
- **Date**: 2026-08-14
- **Decision**: auth tokens live in a local file
- **Context**: simplest thing that works

## ADR-2: Unrelated logging choice
- **Decision**: log to stdout

## ADR-4: Rotate auth tokens via the broker API
- **Date**: 2026-09-11
- **Supersedes**: ADR-1
- **Decision**: rotate through the broker
- **Context**: compliance requirement on token storage scope

## ADR-9: Use the PKCE flow for auth
- **Date**: 2026-09-20
- **Supersedes**: ADR-4
- **Decision**: PKCE flow, no stored secret
- **Context**: broker API deprecated
- **Rejected alternatives**:
  - broker API (deprecated)
  - local file (compliance failure)

## ADR-10: Pin the SDK
- **Decision**: stay on 1.2
- **Expires when**: 1.3 fixes the breaking change
- **Rejected alternatives**: upgrading now (breaks the build)
"""


class Fixture(Sandbox):
    def setUp(self):
        super().setUp()
        self.v = self.make_vault()
        self.assertOk(self.py(VI, "create-project", "--vault", self.v, "--name", "demo",
                              "--title", "Demo", "--domain", "t", "--topology", "local"))
        self.proj = self.v / "Projects/demo"

    def seed_decisions(self, text=DECISIONS):
        (self.v / "Projects/golden-thread/spool/decisions/demo/0000-baseline.md").unlink()
        (self.proj / "decisions.md").write_text(text, encoding="utf-8")
        self.assertOk(self.adr("migrate", "demo"))

    def adr(self, *a):
        return self.py(TOOLS / "gt_adr.py", "--vault", self.v, *a)


class Lineage(Fixture):
    def setUp(self):
        super().setUp()
        self.seed_decisions()

    def test_full_chain_oldest_first_with_current_and_reasons(self):
        p = self.adr("lineage", "demo", "auth")
        self.assertOk(p)
        out = p.stdout
        i1, i4, i9 = (out.index("ADR-%d (" % n) for n in (1, 4, 9))
        self.assertTrue(i1 < i4 < i9, out)
        self.assertNotIn("ADR-2 (", out, "an unrelated ADR joined the chain")
        self.assertIn("ADR-1 (2026-08-14) — Store session tokens in a local file", out)
        self.assertIn("Superseded by ADR-4 because: compliance requirement", out)
        self.assertIn("Superseded by ADR-9 because: broker API deprecated", out)
        self.assertIn("ADR-9 (2026-09-20) — Use the PKCE flow for auth — current", out)
        self.assertIn("broker API (deprecated); local file (compliance failure)", out)
        self.assertEqual(out.count("— current"), 1, out)

    def test_a_match_anywhere_in_the_chain_returns_the_whole_chain(self):
        out = self.adr("lineage", "demo", "PKCE").stdout
        self.assertIn("ADR-1 (", out)
        self.assertIn("ADR-9 (", out)

    def test_no_match(self):
        p = self.adr("lineage", "demo", "nonexistent topic")
        self.assertOk(p)
        self.assertIn("No decisions found for topic 'nonexistent topic'", p.stdout)

    def test_single_adr_is_current(self):
        out = self.adr("lineage", "demo", "logging").stdout
        self.assertIn("ADR-2 (date unknown) — Unrelated logging choice — current", out)

    def test_allocate_writes_the_supersedes_field_lineage_reads(self):
        p = self.adr("allocate", "demo", "--title", "Drop PKCE for mTLS auth",
                     "--supersedes", "ADR-9")
        self.assertOk(p)
        n = int(p.stdout.strip())
        slot = self.v / ("Projects/golden-thread/spool/decisions/demo/%04d.md" % n)
        self.assertIn("- **Supersedes**: ADR-9", slot.read_text())
        out = self.adr("lineage", "demo", "auth").stdout
        self.assertIn("Superseded by ADR-%d" % n, out)
        self.assertIn("ADR-%d (" % n, out)
        self.assertNotIn("for auth — current", out.split("ADR-%d (" % n)[0])

    def test_supersedes_must_name_an_existing_adr(self):
        p = self.adr("allocate", "demo", "--title", "x", "--supersedes", "77")
        self.assertEqual(p.returncode, 2)
        self.assertIn("ADR-77", p.stderr)

    def test_lineage_is_read_only(self):
        before = sorted((q, q.stat().st_mtime_ns) for q in self.v.rglob("*") if q.is_file())
        self.adr("lineage", "demo", "auth")
        after = sorted((q, q.stat().st_mtime_ns) for q in self.v.rglob("*") if q.is_file())
        self.assertEqual(before, after)


MEM = """---
name: {name}
description: "{name}"
metadata:
  type: project
{ent}---

Body of {name}.
"""


class EntityLookup(Fixture):
    def setUp(self):
        super().setUp()
        mem = self.proj / "memory"
        idx = ["# Memory Index", ""]
        for name, ents in (("rotation", "[auth-service, TOKENSVC]"), ("outage", "[auth-service]"),
                           ("limits", "\n  - Auth-Service\n  - quotas"), ("plain1", None),
                           ("plain2", None)):
            ent = "" if ents is None else "entities: %s\n" % ents
            (mem / ("%s.md" % name)).write_text(MEM.format(name=name, ent=ent))
            idx.append("- [%s](%s.md) — what %s is about" % (name, name, name))
        (mem / "MEMORY.md").write_text("\n".join(idx) + "\n")

    def look(self, *a):
        return self.py(SCRIPTS / "gt_entities.py", "--vault", self.v, *a)

    def test_only_tagged_files_load_case_insensitive_substring(self):
        p = self.look("lookup", "AUTH", "--project", "demo")
        self.assertOk(p)
        for name in ("rotation", "outage", "limits"):
            self.assertIn("Body of %s." % name, p.stdout)
            self.assertIn("what %s is about" % name, p.stdout, "MEMORY.md description missing")
        for name in ("plain1", "plain2"):
            self.assertNotIn("Body of %s." % name, p.stdout)

    def test_no_match(self):
        p = self.look("lookup", "nonexistent", "--project", "demo")
        self.assertOk(p)
        self.assertIn("No memory files tagged with entity 'nonexistent'", p.stdout)

    def test_list_and_unknown_project(self):
        out = self.look("list", "--project", "demo").stdout
        self.assertIn("TOKENSVC: rotation.md", out)
        p = self.look("list", "--project", "ghost")
        self.assertEqual(p.returncode, 2)
        self.assertIn("ghost", p.stderr)


IDEA = """# Demo

A widget service that turns orders into invoices; breaking it stops billing.

More brain dump below, see [[Some Page]] and Projects/demo/research.md.
"""

SOURCE = """# Demo — Source

**Topology:** remote
**Repo:** https://example.invalid/demo.git
**Fleet:** [[INFRASTRUCTURE]]

## Deployment targets

| Role | Env | Host | Site | Path | Notes |
|---|---|---|---|---|---|
| app | prod | web-a | demo.example.invalid | /srv/demo | primary |
| app | staging | web-b | | /srv/demo | |

## File plan

| Source (repo path) | Kind | Targets | Deployed path | Notes |
|---|---|---|---|---|
|  |  |  |  |  |
"""

BRIEF_DECISIONS = """# Demo Decisions

## ADR-1: Invoices are generated nightly
- **Decision**: one batch at 02:00, never on demand
- **Rejected alternatives**: on-demand generation (locks the orders table)

## ADR-2: Store PDFs on local disk
- **Decision**: PDFs under /srv/demo/pdf

## ADR-3: Store PDFs in object storage
- **Supersedes**: ADR-2
- **Decision**: PDFs in the bucket; see [[Storage Notes]]
- **Rejected alternatives**:
  - local disk (lost on rebuild)

## ADR-4: Pin the PDF library
- **Decision**: stay on 2.x
- **Expires when**: 3.x supports fonts
"""


class Brief(Fixture):
    def brief(self, *a):
        return self.py(SCRIPTS / "gt_brief.py", "--vault", self.v, "demo", *a)

    def test_full_project(self):
        (self.proj / "idea.md").write_text(IDEA)
        (self.proj / "source.md").write_text(SOURCE)
        self.seed_decisions(BRIEF_DECISIONS)
        before = sorted((q, q.stat().st_mtime_ns) for q in self.v.rglob("*") if q.is_file())
        p = self.brief("--repo", str(self.tmp / "repo"))
        self.assertOk(p)
        out = p.stdout
        body, _, trailer = out.partition("## Deeper context")
        self.assertIn("A widget service that turns orders into invoices", body)
        self.assertIn("Invoices are generated nightly", body)
        self.assertIn("Store PDFs in object storage", body)
        self.assertNotIn("Store PDFs on local disk", body, "a superseded ADR is not a constraint")
        self.assertNotIn("Pin the PDF library", body, "an expiring ADR is not an invariant")
        self.assertIn("web-a", body)
        self.assertIn("Topology: remote", body)
        self.assertIn("- on-demand generation (locks the orders table)", body)
        self.assertIn("- local disk (lost on rebuild)", body)
        # Self-contained: no vault path, no wikilink, no ADR number above the trailer.
        for bad in ("Projects/", "[[", "ADR-", "Knowledge/", "INFRASTRUCTURE"):
            self.assertNotIn(bad, body, "vault reference %r in the substantive section" % bad)
        self.assertIn("vault-config.json", trailer)
        self.assertIn("Projects/demo/", trailer)
        after = sorted((q, q.stat().st_mtime_ns) for q in self.v.rglob("*") if q.is_file())
        self.assertEqual(before, after, "gt_brief wrote into the vault")
        self.assertFalse((self.tmp / "repo").exists(), "gt_brief wrote into the repo")

    def test_only_idea_md_says_what_is_missing(self):
        (self.proj / "idea.md").write_text(IDEA)
        (self.proj / "source.md").unlink()
        p = self.brief()
        self.assertOk(p)
        self.assertIn("A widget service", p.stdout)
        self.assertIn("no ADRs", p.stderr)
        self.assertIn("no source.md", p.stderr)

    def test_unknown_project(self):
        p = self.py(SCRIPTS / "gt_brief.py", "--vault", self.v, "ghost")
        self.assertEqual(p.returncode, 2)
        self.assertIn("ghost", p.stderr)


if __name__ == "__main__":
    import unittest
    unittest.main()
