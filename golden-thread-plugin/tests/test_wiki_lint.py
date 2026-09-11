"""gt-wiki wiki_lint.py: ten report-only health checks over an LLM Wiki vault.

Contracts pinned here:
  * a healthy vault exits 0 with zero findings; any finding exits 1;
  * each of the ten checks fires on the thing it names and not on its near misses
    (aliased / heading links, inline source lists, principle pages, code blocks);
  * lint-declines.md suppresses exact finding lines and counts them;
  * report-only: nothing in the vault changes unless --queue names a file;
  * a vault fresh from vault_init.py wiki lints clean.
"""
import datetime
import hashlib
import json
import unittest

from _harness import Sandbox, WIKI_SCRIPTS

LINT = WIKI_SCRIPTS / "wiki_lint.py"
INIT = WIKI_SCRIPTS / "vault_init.py"
TODAY = datetime.date.today().isoformat()


def page(title, body, sources=("Sources/src-one.md",), status="growing", updated=TODAY, extra=""):
    src = "".join(f'  - "[[{s}]]"\n' for s in sources)
    return (f"---\ntitle: {title}\ncategory: concept\nsources:\n{src}"
            f"created: {TODAY}\nupdated: {updated}\nstatus: {status}\n{extra}---\n\n{body}\n")


class WikiLintTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.v = self.tmp / "vault"
        (self.v / "Knowledge").mkdir(parents=True)
        (self.v / "Sources").mkdir()
        (self.v / "Sources" / "src-one.md").write_text("---\ntitle: one\n---\nraw\n", encoding="utf-8")
        self.write("Alpha Page", page("Alpha Page", "See [[Beta Page|beta]] and [[Beta Page#Details]]."))
        self.write("Beta Page", page("Beta Page", "Back to [[Alpha Page]]."))
        self.set_index("Alpha Page", "Beta Page")

    def write(self, name, text, folder="Knowledge"):
        (self.v / folder / f"{name}.md").write_text(text, encoding="utf-8")

    def set_index(self, *names):
        (self.v / "index.md").write_text(
            "# Index\n\n" + "".join(f"- [[{n}]] — s\n" for n in names), encoding="utf-8")

    def lint(self, *args):
        return self.py(LINT, self.v, "--json", *args)

    def findings(self, *args):
        p = self.lint(*args)
        self.assertIn(p.returncode, (0, 1), p.stderr)
        return p, json.loads(p.stdout)

    def tree_hash(self):
        h = hashlib.sha256()
        for f in sorted(self.v.rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(self.v)).encode() + b"\0" + f.read_bytes())
        return h.hexdigest()

    # ---------------------------------------------------------------------------
    def test_healthy_vault_is_clean(self):
        p, r = self.findings()
        self.assertEqual(p.returncode, 0, json.dumps(r["findings"], indent=1))
        self.assertEqual(r["total"], 0)
        self.assertEqual((r["pages"], r["sources"]), (2, 1))

    def test_every_check_fires(self):
        # One defect per check, each on its own page, then read every category.
        self.write("Gamma Page", page("Gamma Page", "Links [[Nowhere]]. Mentions alpha page in passing.",
                                      sources=("Sources/ghost.md",), status="draft",
                                      updated="2020-01-01", extra="expires_when: until v2 ships\n"))
        self.write("Delta", page("Delta", "Links [[Alpha Page]] one way.", sources=()))
        (self.v / "Sources" / "src-two.md").write_text(
            "---\ntitle: two\nsupersedes:\n  - Sources/src-one.md\n---\n", encoding="utf-8")
        self.set_index("Alpha Page", "Beta Page", "Delta", "Removed Page")
        p, r = self.findings()
        f = r["findings"]
        self.assertEqual(p.returncode, 1)
        expect = {
            "broken-links": "Gamma Page.md -> [[nowhere]]",
            "orphans": "Gamma Page.md",
            "missing-reciprocal": "Delta.md -> Alpha Page.md (no link back)",
            "unsourced": "Delta.md",
            "superseded-cited": "Alpha Page.md cites [[src-one]] superseded by [[src-two]]",
            "review-due": "Gamma Page.md (last touch 2020-01-01)",
            "index-mismatch": "index lists [[removed page]] but no file exists",
            "status-schema": "Gamma Page.md (status: 'draft')",
            "expiry-declared": "Gamma Page.md (expires_when: until v2 ships)",
            "unlinked-mention": 'Gamma Page.md mentions "Alpha Page" without linking [[Alpha Page]]',
        }
        self.assertEqual(set(f), set(expect), "check set changed")
        for check, item in expect.items():
            with self.subTest(check=check):
                self.assertIn(item, f[check])
        self.assertIn("Gamma Page.md cites missing source [[ghost]]", f["unsourced"])
        self.assertIn("Gamma Page.md missing from index.md", f["index-mismatch"])
        # near misses that must NOT fire
        self.assertFalse([x for x in f["broken-links"] if "beta page" in x],
                         "aliased / heading links reported broken")
        self.assertNotIn("Delta.md", f["orphans"], "an indexed page was called an orphan")

    def test_inline_source_list_resolves(self):
        self.write("Beta Page", page("Beta Page", "Back to [[Alpha Page]].", sources=())
                   .replace("sources:\n", 'sources: ["Sources/src-one.md"]\n'))
        _, r = self.findings()
        self.assertEqual(r["findings"]["unsourced"], [])

    def test_principles_are_exempt_from_review_due(self):
        old = "2019-06-01"
        self.write("Alpha Page", page("Alpha Page", "[[Beta Page]]", updated=old)
                   .replace("category: concept", "category: decision"))
        self.write("Beta Page", page("Beta Page", "[[Alpha Page]]", updated=old, extra="kind: principle\n"))
        _, r = self.findings()
        self.assertEqual(r["findings"]["review-due"], [])
        self.write("Beta Page", page("Beta Page", "[[Alpha Page]]", updated=old))
        _, r = self.findings()
        self.assertEqual(r["findings"]["review-due"], [f"Beta Page.md (last touch {old})"])
        _, r = self.findings("--days", "100000")
        self.assertEqual(r["findings"]["review-due"], [], "--days not honoured")

    def test_unlinked_mention_ignores_code_short_titles_and_links(self):
        self.write("Git", page("Git", "[[Alpha Page]] [[Beta Page]]"))
        self.write("Alpha Page", page("Alpha Page", "[[Beta Page]] uses git daily.\n\n"
                                      "```\nbeta page in code\n```\n"))
        self.write("Beta Page", page("Beta Page", "[[Alpha Page]] [[Git]]"))
        self.set_index("Alpha Page", "Beta Page", "Git")
        _, r = self.findings()
        self.assertEqual(r["findings"]["unlinked-mention"], [], r["findings"]["unlinked-mention"])

    def test_declines_suppress_exact_lines_and_are_counted(self):
        self.write("Beta Page", page("Beta Page", "No link back."))
        _, r = self.findings()
        finding = "Alpha Page.md -> Beta Page.md (no link back)"
        self.assertEqual(r["findings"]["missing-reciprocal"], [finding])
        (self.v / "lint-declines.md").write_text(
            f"# Declines\n\n- {finding} | one-way on purpose\n- some other line\n", encoding="utf-8")
        p, r = self.findings()
        self.assertEqual(r["findings"]["missing-reciprocal"], [])
        self.assertEqual(r["suppressed"], 1)
        self.assertEqual(p.returncode, 0, "a fully declined report still exits 1")

    def test_report_only_and_queue(self):
        self.write("Beta Page", page("Beta Page", "[[Alpha Page]]", updated="2020-02-02",
                                     extra="expires_when: never\n"))
        before = self.tree_hash()
        self.findings()
        self.py(LINT, self.v)  # text mode too
        self.assertEqual(self.tree_hash(), before, "lint modified the vault")
        q = self.tmp / "queue.md"
        self.findings("--queue", q)
        self.assertEqual(self.tree_hash(), before)
        text = q.read_text(encoding="utf-8")
        self.assertIn("Pending: 2", text)
        self.assertIn("- [ ] Beta Page.md (last touch 2020-02-02)", text)
        self.assertIn("- [ ] Beta Page.md (expires_when: never)", text)

    def test_text_report_and_usage(self):
        p = self.py(LINT, self.v)
        self.assertOk(p)
        self.assertIn("Findings: 0", p.stdout)
        self.assertIn("## unlinked-mention (0)", p.stdout)
        self.assertEqual(self.py(LINT).returncode, 2)

    def test_fresh_wiki_vault_lints_clean(self):
        v = self.tmp / "fresh"
        self.assertOk(self.py(INIT, "wiki", "--vault", v, "--domain", "D",
                              "--config", self.tmp / "cfg.json"))
        p = self.py(LINT, v, "--json")
        r = json.loads(p.stdout)
        self.assertEqual(r["total"], 0,
                         "a vault fresh from vault_init.py wiki fails its own lint: "
                         "Knowledge/_template.md is linted as a page -> "
                         + json.dumps({k: x for k, x in r["findings"].items() if x}))


if __name__ == "__main__":
    unittest.main()
