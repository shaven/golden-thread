"""gt_digest.py -- research-digest.md beside the append-only research.md
(0.18.1, 2026-09-24-research-digest).

Fixture: research.md with 25 sections, three [pinned]. Contract:
  * `write` produces research-digest.md through the write queue (Core rule 1)
  * the 20 most recent sections, newest first; sections 1-5 (oldest) absent from that block
  * the 3 pinned sections in the pinned block, whatever their age
  * research.md is byte-identical afterwards
  * the digest is listed in the project's memory/MEMORY.md after the first generation
  * after a 26th section is appended, `write` updates the digest and the oldest drops out
  * `check` says current only when the digest matches research.md as it is now
  * gt-work runs it; gt-open reads the digest when `check` says current
"""
import json
import re

from _harness import GT, Sandbox, SCRIPTS

DIGEST = SCRIPTS / "gt_digest.py"
PINNED = (2, 9, 17)


def research(n):
    parts = ["# Research — quokka\n"]
    for i in range(1, n + 1):
        tag = " [pinned]" if i in PINNED else ""
        parts.append("## 2026-09-%02d: finding number %d%s\n\nSummary line for finding %d.\n"
                     "\nMore detail that must not appear.\n" % (min(i, 28), i, tag, i))
    return "\n".join(parts)


class DigestBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.config(vault_path=str(self.vault))
        self.proj = self.vault / "Projects" / "quokka"
        (self.proj / "memory").mkdir(parents=True)
        (self.proj / "memory" / "MEMORY.md").write_text("# Memory Index\n\n- [A](a.md) — a\n")
        (self.proj / "research.md").write_text(research(25))

    def cli(self, *args):
        return self.py(DIGEST, *args, "--vault", str(self.vault), "--project", "quokka")

    def digest(self):
        return (self.proj / "research-digest.md").read_text()

    def block(self, text, title):
        m = re.search(r"^## %s.*?\n(.*?)(?=^## |\Z)" % re.escape(title), text, re.S | re.M)
        self.assertTrue(m, text)
        return [l for l in m.group(1).splitlines() if l.startswith("- ")]


class Digest(DigestBase):
    def test_write_produces_the_digest(self):
        before = (self.proj / "research.md").read_bytes()
        p = self.cli("write")
        self.assertOk(p)
        text = self.digest()
        self.assertEqual(before, (self.proj / "research.md").read_bytes(),
                         "research.md is append-only and must never be modified")
        latest = self.block(text, "Latest sections")
        self.assertEqual(20, len(latest))
        self.assertIn("finding number 25", latest[0])
        self.assertIn("finding number 6", latest[-1])
        nums = [int(re.search(r"finding number (\d+)", l).group(1)) for l in latest]
        self.assertEqual(list(range(25, 5, -1)), nums, "not newest-first")
        for old in range(1, 6):
            self.assertNotIn("finding number %d**" % old, "\n".join(latest))
        self.assertIn("Summary line for finding 25.", latest[0])
        self.assertNotIn("must not appear", text)
        pinned = self.block(text, "Pinned findings")
        self.assertEqual(3, len(pinned))
        self.assertEqual(["17", "9", "2"],
                         [re.search(r"finding number (\d+)", l).group(1) for l in pinned])

    def test_lines_are_cut_to_120_characters(self):
        (self.proj / "research.md").write_text("## 2026-10-01: long\n\n" + "x" * 300 + "\n")
        self.assertOk(self.cli("write"))
        line = self.block(self.digest(), "Latest sections")[0]
        summary = line.split(" — ", 1)[1]
        self.assertLessEqual(len(summary), 120)

    def test_listed_in_memory_index_once(self):
        self.assertOk(self.cli("write"))
        idx = (self.proj / "memory" / "MEMORY.md").read_text()
        self.assertEqual(1, idx.count("research-digest.md"))
        self.assertIn("- [A](a.md) — a", idx)
        with open(self.proj / "research.md", "a") as fh:
            fh.write("\n## 2026-10-02: another\n\nnew\n")
        self.assertOk(self.cli("write"))
        self.assertEqual(1, (self.proj / "memory" / "MEMORY.md").read_text()
                         .count("research-digest.md"))

    def test_no_memory_index_creates_one(self):
        (self.proj / "memory" / "MEMORY.md").unlink()
        self.assertOk(self.cli("write"))
        self.assertIn("research-digest.md", (self.proj / "memory" / "MEMORY.md").read_text())

    def test_regenerated_after_an_append_and_the_oldest_drops(self):
        self.assertOk(self.cli("write"))
        with open(self.proj / "research.md", "a") as fh:
            fh.write("\n## 2026-10-01: finding number 26\n\nSummary line for finding 26.\n")
        self.assertEqual(1, self.cli("check").returncode, "an appended section left it current")
        self.assertOk(self.cli("write"))
        latest = self.block(self.digest(), "Latest sections")
        self.assertIn("finding number 26", latest[0])
        self.assertEqual(20, len(latest))
        self.assertNotIn("finding number 6**", "\n".join(latest))
        self.assertEqual(0, self.cli("check").returncode)

    def test_check_states(self):
        self.assertEqual(1, self.cli("check").returncode)
        self.assertOk(self.cli("write"))
        p = self.cli("check", "--json")
        self.assertOk(p)
        self.assertTrue(json.loads(p.stdout)["current"])

    def test_dry_run_writes_nothing(self):
        p = self.cli("write", "--dry-run")
        self.assertOk(p)
        self.assertFalse((self.proj / "research-digest.md").exists())
        self.assertIn("would queue", p.stdout)

    def test_unchanged_research_is_a_no_op(self):
        self.assertOk(self.cli("write"))
        p = self.cli("write", "--json")
        self.assertOk(p)
        self.assertEqual([], json.loads(p.stdout)["writes"])

    def test_it_goes_through_the_queue(self):
        src = DIGEST.read_text()
        self.assertIn("wq.submit", src)
        self.assertNotIn("open(os.path.join(base, DIGEST), \"w\"", src)


class Skills(DigestBase):
    def test_gt_work_and_gt_open_use_it(self):
        work = (GT / "skills" / "gt-work" / "SKILL.md").read_text()
        self.assertIn("gt_digest.py write", work)
        opn = (GT / "skills" / "gt-open" / "SKILL.md").read_text()
        self.assertIn("gt_digest.py check", opn)
        self.assertIn("research-digest.md", opn)
