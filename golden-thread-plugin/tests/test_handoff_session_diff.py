"""gt-handoff's `## What Changed This Session` (0.18.1, 2026-09-24-session-vault-diff).

  * gt_session.py register (the VAULT tool -- session registration lives there, not in
    scripts/) records the vault's `git rev-parse HEAD` as `start_commit`
  * gt_handoff.py reads it from the session file and appends the section, listing new memory
    files (with frontmatter description), research.md headings appended, new ADRs (spool slots
    and decisions.md), design files touched, Knowledge pages created/updated
  * --since-commit overrides; no changes -> "No vault changes this session."
  * the section is labelled derived-from-git and `self-verified`
"""
import json
import re
import shutil

from _harness import GT, Sandbox, TOOLS

HANDOFF = GT / "scripts" / "gt_handoff.py"
SESSION = TOOLS / "gt_session.py"
SID = "11111111-2222-3333-4444-555555555555"


class SessionDiff(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.proj = self.vault / "Projects" / "alpha"
        self.proj.mkdir(parents=True)
        (self.proj / "README.md").write_text("# Alpha\n\nAlpha does a thing.\n\n## Tasks\n\n- [ ] x\n")
        (self.proj / "research.md").write_text("# Research\n\n## 2026-09-01: old finding\n\nold\n")
        (self.proj / "decisions.md").write_text("# Decisions\n\n## ADR-1: Old choice\n\nx\n")
        (self.vault / "Knowledge").mkdir()
        (self.vault / "Knowledge" / "Old Page.md").write_text("# Old\n")
        self.git_init(self.vault)
        self.env["GT_VAULT"] = str(self.vault)
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)

    def git(self, *args):
        p = self.run_cmd(["git", "-C", str(self.vault), *args])
        self.assertOk(p)
        return p.stdout.strip()

    def commit(self, msg="work"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def register(self):
        p = self.py(SESSION, "register", "--vault", str(self.vault), "--id", SID, "--task", "t")
        self.assertOk(p)
        f = next((self.vault / "Projects" / "golden-thread" / "sessions").glob(SID + "_*.md"))
        return f

    def do_work(self):
        mem = self.proj / "memory"
        mem.mkdir()
        (mem / "wombat-fact.md").write_text(
            "---\nname: Wombat fact\ndescription: wombats are faster than kestrels\n---\nbody\n")
        (mem / "MEMORY.md").write_text("- [Wombat](wombat-fact.md)\n")
        with open(self.proj / "research.md", "a") as fh:
            fh.write("\n## 2026-10-01: kestrel throughput measured\n\n40%\n")
        spool = self.vault / "Projects" / "golden-thread" / "spool" / "decisions" / "alpha"
        spool.mkdir(parents=True)
        (spool / "0002.md").write_text("<!-- allocated by x -->\n## ADR-2: Use the queue\n\nbody\n")
        (self.proj / "design.md").write_text("# Design\n")
        (self.vault / "Knowledge" / "New Page.md").write_text("# New\n")
        with open(self.vault / "Knowledge" / "Old Page.md", "a") as fh:
            fh.write("more\n")
        self.commit()

    def handoff(self, *args):
        p = self.py(HANDOFF, "--vault", str(self.vault), "--project", "alpha", "--dry-run", *args)
        self.assertOk(p)
        return p.stdout

    def section(self, text):
        m = re.search(r"^## What Changed This Session\n(.*?)(?=^## )", text, re.S | re.M)
        self.assertTrue(m, "no What Changed section:\n" + text)
        return m.group(1)

    def test_register_records_start_commit(self):
        head = self.git("rev-parse", "HEAD")
        f = self.register()
        self.assertIn("start_commit: %s" % head, f.read_text())

    def test_reregister_keeps_the_original_start_commit(self):
        head = self.git("rev-parse", "HEAD")
        self.register()
        self.do_work()
        p = self.py(SESSION, "register", "--vault", str(self.vault), "--id", SID, "--resume")
        self.assertOk(p)
        f = next((self.vault / "Projects" / "golden-thread" / "sessions").glob(SID + "_*.md"))
        self.assertIn("start_commit: %s" % head, f.read_text())

    def test_the_section_lists_each_kind_of_change(self):
        self.register()
        self.do_work()
        sec = self.section(self.handoff("--session", SID))
        self.assertIn("self-verified", sec)
        self.assertIn("git diff", sec)
        self.assertIn("**New memory files (1):**", sec)
        self.assertIn('Projects/alpha/memory/wombat-fact.md — "wombats are faster than kestrels"',
                      sec)
        self.assertNotIn("MEMORY.md", sec)
        self.assertIn("**Updated research entries (1):**", sec)
        self.assertIn("2026-10-01: kestrel throughput measured", sec)
        self.assertNotIn("old finding", sec)
        self.assertIn('**New ADRs (1):**', sec)
        self.assertIn('ADR-2: Use the queue', sec)
        self.assertIn("**Updated design files (1):**", sec)
        self.assertIn("Knowledge pages (1 new, 1 updated)", sec)
        self.assertIn("Knowledge/New Page.md — created", sec)
        self.assertIn("Knowledge/Old Page.md — updated", sec)

    def test_session_id_from_the_environment(self):
        self.register()
        self.do_work()
        self.env["CLAUDE_CODE_SESSION_ID"] = SID
        self.assertIn("ADR-2", self.section(self.handoff()))

    def test_since_commit_overrides(self):
        self.do_work()
        first = self.git("rev-list", "--max-parents=0", "HEAD")
        sec = self.section(self.handoff("--since-commit", first))
        self.assertIn("ADR-2", sec)
        self.assertIn(first[:12], sec)

    def test_no_changes_says_so(self):
        self.register()
        sec = self.section(self.handoff("--session", SID))
        self.assertIn("No vault changes this session.", sec)

    def test_no_start_commit_means_no_section_not_a_guess(self):
        out = self.handoff("--session", "nobody")
        self.assertNotIn("What Changed This Session", out)

    def test_json_carries_the_changes(self):
        self.register()
        self.do_work()
        p = self.py(HANDOFF, "--vault", str(self.vault), "--project", "alpha", "--json",
                    "--session", SID)
        self.assertOk(p)
        ch = json.loads(p.stdout)["vault_changes"]["changes"]
        self.assertEqual(1, len(ch["memory"]))
