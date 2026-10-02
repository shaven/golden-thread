"""gt_optimize.py's memory additions (request 2026-09-16-memory-optimization-command, rescoped
from a new gt_memory command into gt-optimize): open cost per project, single-project globals,
a dated research.md archive, and an in-place supersede mark.

Contract:
  * Reporting writes nothing -- proved by a byte-identical tree.
  * Open cost is what /gt:gt-open loads, per project and in total.
  * A global-memory note naming exactly one project is reported (gt_lint's evidence).
  * --archive is a dry run unless --apply; with --apply the archive is written first, the
    research.md entries are replaced by one index line each, and archive + remainder hold
    every original entry (no-loss, asserted on content). A claimed research.md is refused,
    naming the holder.
  * --supersede marks an entry in place and never removes it.
  * No subcommand deletes a file or an entry under any flag combination.
"""
import datetime
import hashlib
import json
import os
import unittest

from _harness import Sandbox, SCRIPTS

OPT = SCRIPTS / "gt_optimize.py"

RESEARCH = """# Research — alpha

Preamble that is not an entry.

## 2024-02-01: first finding

The relay drops idle sockets after ten minutes of silence.

## 2024-11-15: second finding

The broker needs the vault named explicitly on every call.

## Notes without a date

Loose notes stay where they are.

## 2025-06-01: correction

The relay drops idle sockets after five minutes, not ten.

## 2026-09-01: recent finding

Fresh and staying put.
"""


def digest(root):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            h.update(os.path.relpath(p, root).encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


def entries(text):
    """-> every `## ` block in text, as stripped strings."""
    out, cur = [], None
    for line in text.split("\n"):
        if line.startswith("## "):
            cur = [line]
            out.append(cur)
        elif cur is not None:
            cur.append(line)
    return ["\n".join(b).strip() for b in out]


class MemoryOptimizeTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.vault = self.tmp / "vault"
        for d in ("global-memory", "Knowledge", "Projects/alpha/memory", "Projects/beta/memory"):
            (self.vault / d).mkdir(parents=True)
        self.write("Projects/alpha/README.md", "# Alpha\n\nVision line.\n")
        self.write("Projects/beta/README.md", "# Beta\n")
        self.write("Projects/alpha/research.md", RESEARCH)
        self.write("Projects/alpha/memory/MEMORY.md", "- [Note](note.md) — a note\n")
        self.write("Projects/alpha/memory/note.md", "A note that is not loaded at open.\n" * 50)
        self.write("Projects/CONVENTIONS.md", "conventions\n")
        self.write("Projects/PROTOCOL.md", "protocol\nrules\n")

    def write(self, rel, text):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def read(self, rel):
        return (self.vault / rel).read_text(encoding="utf-8")

    def opt(self, *args, env=None):
        return self.py(OPT, "--vault", str(self.vault), *args, env=env)

    def vault_json(self, *args):
        r = self.opt("--only", "vault", "--json", *args)
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        return json.loads(r.stdout)

    # -- read-only ----------------------------------------------------------------------
    def test_report_writes_nothing(self):
        before = digest(self.vault)
        for extra in ([], ["--json"], ["--cost"], ["--only", "vault", "--cost", "--json"],
                      ["--project", "alpha"]):
            self.opt(*extra)
        self.assertEqual(digest(self.vault), before)

    # -- open cost ----------------------------------------------------------------------
    def test_open_cost_is_what_gt_open_loads(self):
        cost = self.vault_json()["open_cost"]
        alpha = next(p for p in cost["projects"] if p["project"] == "alpha")
        want = {"README.md": len(self.read("Projects/alpha/README.md").split("\n")),
                "research.md": len(RESEARCH.split("\n")),
                "memory/MEMORY.md": len(self.read("Projects/alpha/memory/MEMORY.md")
                                        .split("\n"))}
        self.assertEqual(alpha["files"], want, "memory notes are indexed, never loaded")
        self.assertEqual(alpha["lines"], sum(want.values()))
        self.assertEqual(cost["total_lines"], sum(p["lines"] for p in cost["projects"]))
        self.assertEqual(cost["shared"]["lines"], 2 + 3)
        r = self.opt("--only", "vault", "--cost")
        self.assertIn("OPEN COST", r.stdout)
        self.assertIn("all projects", r.stdout)

    def test_a_long_research_md_is_costed_as_gt_open_skims_it(self):
        body = "".join("## 2026-01-%02d: entry %d\n\n%s\n\n" % (i + 1, i, "text line\n" * 20)
                       for i in range(20))
        self.write("Projects/alpha/research.md", "# R\n\n" + body)
        alpha = next(p for p in self.vault_json()["open_cost"]["projects"]
                     if p["project"] == "alpha")
        self.assertTrue(alpha["research_skimmed"])
        self.assertLess(alpha["files"]["research.md"], len(body.split("\n")))

    # -- single-project globals ---------------------------------------------------------
    def test_a_global_naming_one_project_is_reported(self):
        self.write("global-memory/only-alpha.md", "The alpha deploy needs a warm cache.\n")
        self.write("global-memory/both.md", "alpha and beta share the relay.\n")
        self.write("global-memory/none.md", "Use absolute dates in memory.\n")
        self.write("global-memory/word.md", "The alphabet soup is not a project.\n")
        f = [x for x in self.vault_json()["findings"] if x["kind"] == "single-project-global"]
        self.assertEqual([x["path"] for x in f], ["global-memory/only-alpha.md"])
        self.assertEqual(f[0]["class"], "judgement")
        self.assertIn("--demote global-memory/only-alpha.md", f[0]["message"])

    # -- archive ------------------------------------------------------------------------
    def test_archive_dry_run_names_entries_and_changes_nothing(self):
        before = digest(self.vault)
        for extra in ([], ["--dry-run"]):
            r = self.opt("--archive", "--project", "alpha", "--before", "2025-12-31", *extra)
            self.assertEqual(r.returncode, 0, r.stderr)
            for title in ("first finding", "second finding", "correction"):
                self.assertIn(title, r.stdout)
            self.assertNotIn("recent finding", r.stdout)
            self.assertIn("research-archive-2024.md", r.stdout)
        self.assertEqual(digest(self.vault), before)

    def test_archive_moves_old_entries_and_loses_nothing(self):
        r = self.opt("--archive", "--project", "alpha", "--before", "2025-12-31", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        a24 = self.read("Projects/alpha/research-archive-2024.md")
        a25 = self.read("Projects/alpha/research-archive-2025.md")
        res = self.read("Projects/alpha/research.md")
        self.assertIn("## 2024-02-01: first finding", a24)
        self.assertIn("## 2024-11-15: second finding", a24)
        self.assertIn("## 2025-06-01: correction", a25)
        for moved in ("first finding", "second finding", "correction"):
            self.assertNotIn("## %s" % moved, res)
        index = [ln for ln in res.split("\n") if ln.startswith("- 20")]
        self.assertEqual(len(index), 3, res)
        self.assertTrue(any("research-archive-2024#2024-02-01: first finding" in ln
                            for ln in index))
        self.assertIn("## 2026-09-01: recent finding", res)
        self.assertIn("## Notes without a date", res)
        self.assertIn("Preamble that is not an entry.", res)
        # NO LOSS, on content: every original entry is present, verbatim, in the union.
        union = a24 + "\n" + a25 + "\n" + res
        for e in entries(RESEARCH):
            self.assertIn(e, union, "entry lost: %s" % e.split("\n")[0])

    def test_a_second_archive_appends_and_keeps_one_index_section(self):
        self.opt("--archive", "--project", "alpha", "--before", "2024-06-01", "--apply")
        r = self.opt("--archive", "--project", "alpha", "--before", "2025-01-01", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        a24 = self.read("Projects/alpha/research-archive-2024.md")
        self.assertIn("first finding", a24)
        self.assertIn("second finding", a24)
        res = self.read("Projects/alpha/research.md")
        self.assertEqual(res.count("## Archived entries"), 1)
        self.assertEqual(len([ln for ln in res.split("\n") if ln.startswith("- 20")]), 2)

    def test_archive_refuses_a_research_md_another_live_session_claims(self):
        d = self.vault / "Projects" / "golden-thread" / "sessions"
        d.mkdir(parents=True)
        (d / "other.md").write_text(
            "---\nsession_id: other-session\nstatus: active\nlast_execution: %s\n---\n"
            "# What this session has open\n- `Projects/alpha/research.md`\n"
            % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        before = digest(self.vault)
        r = self.opt("--archive", "--project", "alpha", "--before", "2025-12-31", "--apply",
                     env={"CLAUDE_SESSION_ID": "me"})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("other-session", r.stderr)
        self.assertEqual(digest(self.vault), before)

    def test_archive_never_touches_an_undated_heading(self):
        self.opt("--archive", "--project", "alpha", "--before", "2099-01-01", "--apply")
        self.assertIn("## Notes without a date\n\nLoose notes stay where they are.",
                      self.read("Projects/alpha/research.md"))

    # -- supersede ----------------------------------------------------------------------
    def test_supersede_marks_in_place_and_keeps_the_entry(self):
        r = self.opt("--supersede", "--file", "Projects/alpha/research.md",
                     "--entry", "2024-02-01: first finding", "--by", "2025-06-01: correction",
                     "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = self.read("Projects/alpha/research.md")
        block = [e for e in entries(res) if e.startswith("## 2024-02-01")][0]
        self.assertIn("superseded_by: [[research#2025-06-01: correction]]", block)
        self.assertIn("ten minutes of silence", block, "the superseded entry must stay")
        # Everything else is untouched.
        for e in entries(RESEARCH):
            if not e.startswith("## 2024-02-01"):
                self.assertIn(e, res)

    def test_supersede_dry_run_writes_nothing_and_ambiguity_refuses(self):
        before = digest(self.vault)
        r = self.opt("--supersede", "--file", "Projects/alpha/research.md",
                     "--entry", "2024-02-01: first finding", "--by", "2025-06-01: correction")
        self.assertEqual(r.returncode, 0)
        self.assertIn("would mark", r.stdout)
        r = self.opt("--supersede", "--file", "Projects/alpha/research.md",
                     "--entry", "no such heading", "--by", "2025-06-01: correction", "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(digest(self.vault), before)

    # -- interface ----------------------------------------------------------------------
    def test_mutating_actions_accept_vault_and_dry_run_and_never_infer_the_vault(self):
        h = self.py(OPT, "--help")
        for flag in ("--vault", "--dry-run", "--archive", "--supersede", "--apply"):
            self.assertIn(flag, h.stdout)
        r = self.py(OPT, "--archive", "--project", "alpha", "--before", "2025-01-01")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--vault", r.stderr)
        self.assertEqual(self.opt("--archive", "--project", "alpha", "--before", "2025-01-01",
                                  "--apply", "--dry-run").returncode, 2)

    def test_no_flag_combination_deletes_a_file_or_shrinks_one_except_by_archive(self):
        self.write("global-memory/only-alpha.md", "The alpha deploy needs a warm cache.\n")
        files = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        runs = [[], ["--json"], ["--cost"], ["--only", "vault"],
                ["--archive", "--project", "alpha", "--before", "2025-12-31"],
                ["--supersede", "--file", "Projects/alpha/research.md", "--entry",
                 "2026-09-01: recent finding", "--by", "Notes without a date", "--apply"],
                ["--archive", "--project", "alpha", "--before", "2025-12-31", "--apply"]]
        for args in runs:
            self.opt(*args)
        for p, old in files.items():
            self.assertTrue(p.exists(), "%s was deleted" % p)
            if p.name == "research.md":
                continue
            self.assertGreaterEqual(len(p.read_bytes()), len(old), "%s shrank" % p)
        moved = "".join(p.read_text(encoding="utf-8")
                        for p in (self.vault / "Projects/alpha").glob("research-archive-*.md"))
        for e in entries(RESEARCH):
            if e.split("\n")[0] not in self.read("Projects/alpha/research.md"):
                self.assertIn(e, moved, "an entry left research.md without reaching an archive")


if __name__ == "__main__":
    unittest.main()
