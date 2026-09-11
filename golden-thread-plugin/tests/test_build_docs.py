"""build-docs.py: drift check (default, stdlib-only) and --build between .md and .html.

build-docs.py resolves every document relative to ITS OWN directory, so each test
copies the script into a temp dir with either synthetic docs or a copy of the real
ones. The real docs are never read for writing and --build never runs on the repo.

Contracts pinned here:
  * check mode writes nothing, needs no third-party module, exits 0 clean / 1 drift;
  * it reports versions, skill names and headings present on one side only, and
    in-document links with no anchor;
  * the MANUAL h1 shortening done by --build is a known divergence, not drift;
  * --build regenerates a body the check then accepts.
"""
import hashlib
import importlib.util
import shutil
import unittest

from _harness import Sandbox, REPO, PYTHON

SCRIPT = REPO / "build-docs.py"
DOCS = ("MANUAL", "ONBOARDING", "OBSIDIAN-WORKFLOW", "golden-thread-docs")

MD = """# {title}

Written against gt v0.9.13 (release 0.9.13). Run `/gt:gt-init` first, then gt-open.

## Getting started

Some text with a [link](#getting-started).
"""

HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>.gt-fake-skill {{}}</style></head>
<body>
<h1 id="top">{h1}</h1>
<p>Written against gt v0.9.13 (release 0.9.13). Run /gt:gt-init first, then gt-open.</p>
<h2 id="getting-started">Getting started</h2>
<p>Some text with a <a href="#getting-started">link</a>.</p>
</body></html>
"""


class BuildDocsTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.d = self.tmp / "docs"
        self.d.mkdir()
        shutil.copy2(SCRIPT, self.d / "build-docs.py")

    def synthetic(self):
        for name in DOCS:
            title = f"{name} Guide"
            (self.d / f"{name}.md").write_text(MD.format(title=title), encoding="utf-8")
            (self.d / f"{name}.html").write_text(HTML.format(h1=title), encoding="utf-8")

    def real_copy(self):
        for name in DOCS:
            for ext in (".md", ".html"):
                shutil.copy2(REPO / f"{name}{ext}", self.d / f"{name}{ext}")

    def check(self, *args, stdlib_only=True):
        # -S: no site-packages, so a check that needed `markdown` would fail here.
        flags = ["-S"] if stdlib_only else []
        return self.run_cmd([PYTHON, *flags, self.d / "build-docs.py", *args])

    def tree(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.d.iterdir() if p.is_file()}

    def edit(self, name, old, new):
        p = self.d / name
        text = p.read_text(encoding="utf-8")
        self.assertIn(old, text, f"fixture: {old!r} not in {name}")
        p.write_text(text.replace(old, new, 1), encoding="utf-8")

    # ------------------------------------------------------------------------------
    def test_clean_docs_pass_and_nothing_is_written(self):
        self.synthetic()
        before = self.tree()
        p = self.check()
        self.assertOk(p)
        for name in DOCS:
            self.assertIn(f"ok    {name}.html", p.stdout)
        self.assertIn("golden-thread-developer-guide.html", p.stdout)
        self.assertEqual(self.tree(), before, "check mode wrote a file")

    def test_drift_in_a_copy_of_the_real_docs_is_reported(self):
        self.real_copy()
        baseline = self.check()
        self.assertIn(baseline.returncode, (0, 1), baseline.stderr)
        self.assertNotIn("Traceback", baseline.stderr)
        with open(self.d / "MANUAL.md", "a", encoding="utf-8") as f:
            f.write("\n## A section added after the render\n\nNow at 7.7.7 with gt-brandnew.\n")
        before = self.tree()
        p = self.check("MANUAL")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("MANUAL.html  <- MANUAL.md", p.stdout)
        self.assertIn("version 7.7.7 in .md, absent from .html", p.stdout)
        self.assertIn("gt-brandnew in .md, missing from .html", p.stdout)
        self.assertIn('heading absent from .html: "A section added after the render"', p.stdout)
        self.assertNotIn("ONBOARDING", p.stdout, "the `only` filter was ignored")
        self.assertEqual(self.tree(), before)

    def test_each_kind_of_drift(self):
        self.synthetic()
        # Both mentions change: with v-prefixed versions now visible (defect 2026-09-11-
        # build-docs-blind-spots), leaving "gt v0.9.13" behind would keep 0.9.13 in the html.
        self.edit("ONBOARDING.html", "release 0.9.13", "release 0.9.6")            # stale version
        self.edit("ONBOARDING.html", "gt v0.9.13", "gt v0.9.6")
        self.edit("OBSIDIAN-WORKFLOW.html", "then gt-open.", "then gt-open and gt-extra.")
        self.edit("golden-thread-docs.html", 'href="#getting-started"', 'href="#nowhere"')
        p = self.check()
        self.assertEqual(p.returncode, 1)
        out = p.stdout
        self.assertIn("ok    MANUAL.html", out)
        self.assertIn("version 0.9.13 in .md, absent from .html", out)
        self.assertIn("version 0.9.6 in .html, absent from .md  (stale claim?)", out)
        self.assertIn("gt-extra in .html, missing from .md", out)
        self.assertIn("dead internal link: #nowhere has no matching id", out)
        self.assertIn("3 document(s) drifted", out)

    def test_v_prefixed_version_drift_is_detected(self):
        # The docstring's founding case: "html at v0.9.6, md at v0.9.12". Docs write
        # versions as `v0.9.13`; `\b\d+` finds no word boundary between "v" and "0".
        self.synthetic()
        self.edit("ONBOARDING.html", "gt v0.9.13", "gt v0.9.6")
        p = self.check("ONBOARDING")
        self.assertEqual(p.returncode, 1, "a stale v-prefixed version in the .html was not "
                                          "reported: VERSION_RE never matches 'v0.9.6'\n" + p.stdout)
        self.assertIn("version 0.9.6 in .html, absent from .md", p.stdout)

    def test_css_text_is_not_read_as_content(self):
        self.synthetic()  # every .html carries `.gt-fake-skill` inside <style>
        p = self.check()
        self.assertNotIn("gt-fake-skill", p.stdout)

    def test_only_filter_and_pdf_recipe(self):
        self.synthetic()
        p = self.check("nosuchdoc")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no document matches", p.stderr)
        before = self.tree()
        p = self.check("--pdf")
        self.assertOk(p)
        self.assertIn("--no-pdf-header-footer", p.stdout)
        self.assertEqual(self.tree(), before)

    def test_manual_h1_shortening_is_a_known_divergence(self):
        # --build rewrites MANUAL's h1 "Golden Thread — User Manual" to "User Manual"
        # (POST_BUILD) and KNOWN_DIVERGENCES is meant to exempt exactly that. The
        # exemption must not depend on the full title surviving somewhere else on the
        # page, such as <title>.
        self.synthetic()
        (self.d / "MANUAL.md").write_text(MD.format(title="Golden Thread — User Manual"), encoding="utf-8")
        (self.d / "MANUAL.html").write_text(HTML.format(h1="User Manual"), encoding="utf-8")
        p = self.check("MANUAL")
        self.assertOk(p, "the POST_BUILD h1 override is reported as drift: KNOWN_DIVERGENCES "
                         "lists 'Golden Thread' but the heading is compared by exact match")

    def test_emphasis_in_a_heading_is_not_drift(self):
        # "compares content, not markup": a heading written `## Why *not* X` renders
        # as "Why not X". Backticks are already stripped; emphasis is markup too.
        self.synthetic()
        self.edit("ONBOARDING.md", "## Getting started", "## Getting *started* now")
        self.edit("ONBOARDING.html", ">Getting started</h2>", ">Getting <em>started</em> now</h2>")
        p = self.check("ONBOARDING")
        self.assertOk(p, "emphasis markers in a markdown heading are reported as a missing heading")

    def test_build_regenerates_and_then_checks_clean(self):
        if importlib.util.find_spec("markdown") is None:
            self.skipTest("--build needs the markdown module")
        self.synthetic()
        for name in DOCS:  # start from drifted bodies
            (self.d / f"{name}.html").write_text(
                '<html><head><title>keep-me</title></head><body class="b">\nold body\n</body></html>',
                encoding="utf-8")
        (self.d / "MANUAL.md").write_text(MD.format(title="Golden Thread — User Manual"), encoding="utf-8")
        self.assertEqual(self.check(stdlib_only=False).returncode, 1)
        p = self.check("--build", stdlib_only=False)
        self.assertOk(p)
        manual = (self.d / "MANUAL.html").read_text(encoding="utf-8")
        self.assertIn("<title>keep-me</title>", manual, "head was not preserved")
        self.assertIn('<body class="b">', manual)
        self.assertNotIn("old body", manual)
        self.assertRegex(manual, r"<h1[^>]*>User Manual</h1>", "POST_BUILD override not applied")
        self.assertIn('id="getting-started"', manual, "headings lost their ids")
        self.assertIn("1 override(s) applied", p.stdout)
        # the build's own output must satisfy the check (except the known divergence,
        # tested separately above), otherwise build and check disagree
        c = self.check(stdlib_only=False)
        for name in DOCS[1:]:
            self.assertIn(f"ok    {name}.html", c.stdout)


if __name__ == "__main__":
    unittest.main()
