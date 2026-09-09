#!/usr/bin/env python3
"""
build-docs.py — keep the .html/.pdf distributables honest about their .md sources.

WHY THIS EXISTS
---------------
Nothing in this repo built .html from .md. The .html files were authored by hand
beside the markdown, so a markdown edit never reached the distributable and
*nothing reported the gap*. They drifted independently, each stuck at a different
release: on 2026-09-09 MANUAL.html and ONBOARDING.html both still declared v0.9.6
while their markdown was at 0.9.11, MANUAL.html still said "fourteen skills", and
golden-thread-docs.* had been frozen at gt 0.6.0 for six releases -- listing 11
skills against the shipped 15 and one Core rule against the shipped 7.

The defect was never the drift. It was that the drift was silent.

WHAT THIS DOES
--------------
    ./build-docs.py                 # check for drift, write nothing  (default)
    ./build-docs.py --build         # regenerate .html bodies from .md
    ./build-docs.py --build MANUAL  # regenerate one document
    ./build-docs.py --pdf           # print the render commands (does not run them)

Check mode compares *content*, not markup: version strings, headings, and skill
mentions. It deliberately does NOT diff the HTML, because these files were written
by different tools -- MANUAL.html is bespoke, golden-thread-docs.html carries
`generator: pandoc 3.10.1` -- and a markup diff would report three healthy files as
broken every run. A check that cries wolf is a check people stop running.

Check mode uses **only the standard library**, so it runs on any machine. Only
--build needs `markdown` (3.4.4 on the Mac; absent on strader81, localshares and
shadminpc, as is pandoc).

WHAT IT WILL NOT TOUCH
----------------------
golden-thread-developer-guide.html has no .md at all -- the HTML *is* the source.
It is listed in HTML_ONLY and is never rebuilt, only reported. Rebuilding it would
destroy the document.
"""

import argparse
import html
import os
import re
import sys
from html.parser import HTMLParser

REPO = os.path.dirname(os.path.abspath(__file__))

# (markdown source, html target, pdf target, pdf renderer)
# Renderers are the two pipelines recorded in the vault's golden-thread/source.md.
DOCS = [
    ("MANUAL.md",              "MANUAL.html",              "MANUAL.pdf",              "weasyprint"),
    ("ONBOARDING.md",          "ONBOARDING.html",          "ONBOARDING.pdf",          "chrome"),
    ("OBSIDIAN-WORKFLOW.md",   "OBSIDIAN-WORKFLOW.html",   "OBSIDIAN-WORKFLOW.pdf",   "chrome"),
    ("golden-thread-docs.md",  "golden-thread-docs.html",  "golden-thread-docs.pdf",  "chrome"),
]

# HTML with no markdown source. Never rebuilt -- there is nothing to rebuild from.
HTML_ONLY = ["golden-thread-developer-guide.html"]

# Deliberate post-render overrides: things the markdown cannot express because they
# exist to cooperate with a specific stylesheet. Applied by --build, and exempted
# from the drift check so they are never "corrected" back into a bug.
#
# Each entry MUST carry the reason. An unexplained override is indistinguishable
# from a mistake, and the next person will helpfully undo it -- which is exactly
# what happened here on 2026-09-09.
POST_BUILD = {
    "MANUAL.html": [
        # MANUAL.html's stylesheet injects a kicker: h1::before { content: "GOLDEN
        # THREAD" }. Leaving the markdown's full title in the <h1> renders as
        # "GOLDEN THREAD / Golden Thread - User Manual" -- the title twice. Commit
        # 4ed67fd shortened the h1 to "User Manual" for exactly this reason, and a
        # naive regeneration from markdown puts the duplication straight back.
        (
            r"(<h1[^>]*>)Golden Thread\s*[-—]\s*User Manual(</h1>)",
            r"\1User Manual\2",
        ),
    ],
}

# Headings whose absence from the .html is expected, because a POST_BUILD override
# above deliberately changed them. Derived from POST_BUILD so the two cannot drift.
KNOWN_DIVERGENCES = {"MANUAL.html": ["Golden Thread"]}


class TextExtractor(HTMLParser):
    """Visible text only. Skips script/style so CSS class names cannot match."""

    SKIP = {"script", "style"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self):
        return " ".join("".join(self.parts).split())


def html_text(path):
    p = TextExtractor()
    p.feed(read(path))
    return p.text()


def md_text(path):
    """Markdown with the syntax filed off -- enough for content comparison.

    Two things this must NOT do, both learned by getting them wrong:
      - strip hyphens. "gt-route" would become "gt route" and every skill name
        would read as missing from the markdown.
      - drop fenced-code *content*. The session-pattern blocks are fenced, and
        they are exactly where skill names appear. Remove the ``` markers only.
    """
    t = read(path)
    t = re.sub(r"^\s*```.*$", " ", t, flags=re.M)                 # fence markers, keep content
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)              # links -> their text
    t = re.sub(r"^\s{0,3}#{1,6}\s*", " ", t, flags=re.M)          # heading markers
    t = re.sub(r"^\s*[-*+]\s+", " ", t, flags=re.M)               # list bullets
    t = re.sub(r"^\s*>\s?", " ", t, flags=re.M)                   # blockquote markers
    t = re.sub(r"^\s*\|?[\s:|-]{6,}\|?\s*$", " ", t, flags=re.M)  # table rule rows
    t = t.replace("|", " ").replace("`", " ").replace("*", " ")
    t = re.sub(r"(?<!\w)_+|_+(?!\w)", " ", t)                     # emphasis, keeps snake_case
    return " ".join(t.split())


def read(path):
    with open(os.path.join(REPO, path), encoding="utf-8") as f:
        return f.read()


VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")
SKILL_RE = re.compile(r"gt-[a-z][a-z-]+")


def check_one(md, htm):
    """Return a list of human-readable drift findings."""
    findings = []
    mt, ht = md_text(md), html_text(htm)
    allowed = KNOWN_DIVERGENCES.get(htm, [])

    # 1. Version strings the markdown claims but the HTML never mentions.
    #    This is the failure that started it: html at v0.9.6, md at v0.9.12.
    mv, hv = set(VERSION_RE.findall(mt)), set(VERSION_RE.findall(ht))
    for v in sorted(mv - hv):
        findings.append(f"version {v} in .md, absent from .html")
    for v in sorted(hv - mv):
        findings.append(f"version {v} in .html, absent from .md  (stale claim?)")

    # 2. Skills named in one and not the other -- catches a shipped skill the
    #    distributable never learned about.
    ms, hs = set(SKILL_RE.findall(mt)), set(SKILL_RE.findall(ht))
    for s in sorted(ms - hs):
        findings.append(f"{s} in .md, missing from .html")
    for s in sorted(hs - ms):
        findings.append(f"{s} in .html, missing from .md")

    # 3. Headings present in the markdown but nowhere in the rendered text.
    #    Backticks are stripped from the heading first: the markdown writes
    #    `/gt:gt-init` and the rendered text carries /gt:gt-init without them.
    for line in read(md).splitlines():
        if line.startswith("#"):
            h = " ".join(line.lstrip("#").replace("`", "").split())
            if len(h) > 6 and h not in ht and h not in allowed:
                findings.append(f'heading absent from .html: "{h[:58]}"')

    # 4. Internal links that point at no anchor. WeasyPrint fails the PDF render
    #    on these ("No anchor #x for internal URI reference"); Chrome renders them
    #    silently dead, which is worse. Found the hard way when a rebuild dropped
    #    every heading id and only the WeasyPrint pipeline complained.
    raw = read(htm)
    ids = set(re.findall(r'id="([^"]+)"', raw))
    for target in sorted(set(re.findall(r'href="#([^"]+)"', raw))):
        if target not in ids:
            findings.append(f"dead internal link: #{target} has no matching id")

    return findings


def build_one(md, htm):
    """Re-render the <body> of htm from md, preserving its head and any header."""
    try:
        import markdown
    except ImportError:
        sys.exit(
            "--build needs the `markdown` module (pip install markdown).\n"
            "Check mode needs nothing and works without it."
        )

    old = read(htm)
    # Keep everything through <body>, plus a pandoc-style title block if present.
    m = re.search(r"(.*?<body[^>]*>\s*(?:<header\b.*?</header>\s*)?)", old, re.S)
    if not m:
        return f"SKIP {htm}: no <body> found"
    head = m.group(1)

    # `toc` is not optional: it is what puts id= on the headings. Without it
    # python-markdown emits bare <h2>, every in-document [text](#anchor) link
    # dangles, and WeasyPrint fails the render with "No anchor #... for internal
    # URI reference". pandoc emitted those ids, so dropping the extension
    # silently breaks links the previous renderer had made work.
    body = markdown.markdown(
        read(md),
        extensions=["tables", "fenced_code", "sane_lists", "attr_list", "toc"],
    )
    out = head + body + "\n</body>\n</html>\n"

    applied = 0
    for pattern, repl in POST_BUILD.get(htm, []):
        out, n = re.subn(pattern, repl, out)
        applied += n
        if n == 0:
            print(f"    WARNING {htm}: override matched nothing -- {pattern[:52]}")

    with open(os.path.join(REPO, htm), "w", encoding="utf-8") as f:
        f.write(out)
    extra = f", {applied} override(s) applied" if applied else ""
    return f"rebuilt {htm} from {md}{extra}"


PDF_HELP = """\
PDF rendering is not automated here on purpose: one pipeline needs a host this
script cannot assume it can reach, and getting the flags wrong leaks a path into
a shipped file. Run these by hand, then audit.

  MANUAL.pdf  --  WeasyPrint 69.0, strader81 (the only box with it; NOT on $PATH)
    scp MANUAL.html strader81:/tmp/
    ssh strader81 '~/.local/bin/weasyprint /tmp/MANUAL.html /tmp/MANUAL.pdf'
    scp strader81:/tmp/MANUAL.pdf .

  the rest  --  Chrome headless, locally
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
      --headless --disable-gpu --no-pdf-header-footer \\
      --print-to-pdf="$PWD/NAME.pdf" "file://$PWD/NAME.html"

  --no-pdf-header-footer is not optional. Chrome's default footer embeds the
  source file:// URL, which once put /Users/shaven inside three shipped PDFs.

AUDIT AFTERWARDS -- grep cannot read a PDF. The text is font-subset encoded and
grep returns a false clean; a scan once passed six files while two contained a
leaked string. Use pypdf (strader81 has 6.14.2, the only one on the fleet) and
always assert a control string that must be present. If the control comes back
zero the check is blind, not clean.

  Leak strings to assert absent:  /Users  work-laptop  CloudOps  Clops
  Control string to assert present:  Golden Thread

CHECK FOR THE SECTION, NOT THE STRING. This stylesheet uppercases headings unless
text-transform is pinned off, and commands are case-sensitive. A search for
"gt-farm" once came back empty against a PDF that contained it as /GT:GT-FARM,
which read as two dropped sections rather than the styling bug it was. A
case-sensitive absence is not evidence of missing content.
"""


def main():
    ap = argparse.ArgumentParser(
        description="Check, or rebuild, the .html distributables against their .md sources.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Default is check-only. Nothing is written unless you pass --build.",
    )
    ap.add_argument("--build", action="store_true", help="rewrite .html bodies from .md")
    ap.add_argument("--pdf", action="store_true", help="print the PDF render + audit recipe")
    ap.add_argument("only", nargs="?", help="limit to one document (substring match)")
    args = ap.parse_args()

    if args.pdf:
        print(PDF_HELP)
        return 0

    docs = [d for d in DOCS if not args.only or args.only.lower() in d[0].lower()]
    if not docs:
        sys.exit(f"no document matches {args.only!r}")

    if args.build:
        for md, htm, _, _ in docs:
            print(" ", build_one(md, htm))
        print("\nRebuilt. Re-render the PDFs next:  ./build-docs.py --pdf")
        return 0

    drifted = 0
    for md, htm, _, _ in docs:
        findings = check_one(md, htm)
        if findings:
            drifted += 1
            print(f"\n{htm}  <- {md}")
            for f in findings:
                print(f"    {f}")
        else:
            print(f"ok    {htm}")

    for h in HTML_ONLY:
        print(f"--    {h}  (no .md source; HTML is the source, never rebuilt)")

    if drifted:
        print(f"\n{drifted} document(s) drifted. Fix the .md, then: ./build-docs.py --build")
    else:
        print("\nAll documents match their markdown.")
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
