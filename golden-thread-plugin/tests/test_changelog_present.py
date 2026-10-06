"""CHANGELOG.md exists, is not empty, and has a heading for the gt release under test.

On 2026-10-04 an edit truncated CHANGELOG.md to 0 bytes and 3799 tests passed on Linux and on
Windows against it: nothing in the suite read the file. These tests fail on an empty or missing
changelog, on one that lost its title, and on one with no `## gt <version>` heading for the
release the suite is testing.
"""
import re
import unittest

from _harness import GT, REPO

CHANGELOG = REPO.parent / "CHANGELOG.md"
HEADING = re.compile(r"^## gt (\d+\.\d+\.\d+)\b.*$", re.M)


def problems(text, version):
    """Why `text` is not a usable changelog for `version` ([] when it is)."""
    out = []
    if not text.strip():
        return ["CHANGELOG.md is empty"]
    if not text.lstrip().startswith("# Changelog"):
        out.append("CHANGELOG.md does not start with the '# Changelog' title")
    versions = [m.group(1) for m in HEADING.finditer(text)]
    if version not in versions:
        out.append(f"CHANGELOG.md has no '## gt {version}' heading "
                   f"(newest headings: {', '.join(versions[:3]) or 'none'})")
    else:
        m = next(m for m in HEADING.finditer(text) if m.group(1) == version)
        nxt = HEADING.search(text, m.end())
        body = text[m.end():nxt.start() if nxt else len(text)]
        if not body.strip():
            out.append(f"the '## gt {version}' section of CHANGELOG.md is empty")
    return out


class ChangelogPresent(unittest.TestCase):
    def test_the_changelog_exists_and_names_the_release_under_test(self):
        self.assertTrue(CHANGELOG.is_file(), f"{CHANGELOG} is missing")
        text = CHANGELOG.read_text(encoding="utf-8")
        self.assertEqual(problems(text, GT.name), [])
        # the file holds the whole history, not one entry: a truncated-then-appended changelog
        # has the current heading and nothing else
        self.assertGreaterEqual(len(HEADING.findall(text)), 3,
                                "CHANGELOG.md holds fewer than three gt releases")

    def test_the_check_catches_what_went_wrong(self):
        good = "# Changelog\n\nintro\n\n## gt 9.9.9 — unreleased\n\n### a change\n\ntext\n"
        self.assertEqual(problems(good, "9.9.9"), [])
        self.assertEqual(problems("", "9.9.9"), ["CHANGELOG.md is empty"])
        self.assertEqual(problems(" \n\n", "9.9.9"), ["CHANGELOG.md is empty"])
        self.assertIn("no '## gt 9.9.10' heading", problems(good, "9.9.10")[0])     # not a prefix
        self.assertIn("no '## gt 9.9.9' heading",
                      problems(good.replace("## gt 9.9.9", "## gt 9.9.8"), "9.9.9")[0])
        self.assertIn("title", problems(good.replace("# Changelog\n", ""), "9.9.9")[0])
        empty = "# Changelog\n\n## gt 9.9.9 — unreleased\n\n## gt 9.9.8 — 2026-01-01\n\nx\n"
        self.assertIn("section of CHANGELOG.md is empty", problems(empty, "9.9.9")[0])


if __name__ == "__main__":
    unittest.main()
