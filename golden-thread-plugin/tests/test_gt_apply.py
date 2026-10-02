"""gt_apply.py -- add-ons propose fixes, the host writes them (0.18.1).

Two requests, one write path:

2026-09-15-addon-writes-through-host
  * a well-formed proposal is listed with its add-on, file and diff;
  * addon_fixes off discards proposals; propose changes nothing until `apply`; apply writes a
    passing proposal and the file's check passes afterwards;
  * refused, file unchanged: a diff that no longer applies, a protected path (even though the
    module grants it), a file claimed by another live session (named), a file the add-on's
    findings did not name;
  * a fix that applies but does not make the re-check pass is `did-not-fix`, original bytes kept;
  * `undo` restores the exact original bytes; an applied fix is uncommitted and in `git diff`;
  * each apply, refusal and rollback is one `addon.fix` event in the vault's spool;
  * the add-on process cannot write the target during a check run.

2026-09-15-addon-fix-gatekeeper-hardening
  * compare-and-swap under a lock: a changed target is refused at write time; two racing
    applies on one file write at most once; a repo file outside the vault gets the same check;
  * the staged copy is re-checked by EVERY applicable checker: a fix that breaks another
    checker, or leaves the proposer's finding, is refused;
  * content rules: mode bit, create, delete, symlink, U+202E, U+200B, a scrub-term match
    (never printed), an oversize change; HTML: <script>, on-event attribute, new host --
    while adding alt="..." passes;
  * a non-first-party checker is capped at propose under addon_fixes apply, and says so.
"""
import json
import os
import subprocess
import unittest

from _harness import Sandbox, SCRIPTS, PYTHON
from _checkers import make_module

GT_CHECK = SCRIPTS / "gt_check.py"
GT_APPLY = SCRIPTS / "gt_apply.py"
VAULT_TOOLS = ("Projects", "golden-thread", "tools")

FIX = {"id": "html-fix", "globs": ["*.html"], "fixes": ["*.html"], "rules": ["html"]}
REMOVE = {"mark": "BAD-MARKER", "replace": ["BAD-MARKER", "good"]}
ORIGINAL = "<html>\n<p>BAD-MARKER</p>\n<p>other</p>\n</html>\n"


class ApplyBase(Sandbox):
    checkers = ((FIX, REMOVE),)
    first_party = True
    mode = "propose"

    def setUp(self):
        super().setUp()
        self.vault = self.make_vault()
        self.root = self.tmp / "plugins"
        make_module(self.root, [(dict(d), dict(c)) for d, c in self.checkers],
                    manifest=self.first_party)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "a.html").write_text(ORIGINAL)
        (self.repo / "b.html").write_text("<p>clean</p>\n")
        self.git_init(self.repo)
        self.setmode(self.mode)

    def setmode(self, mode, **extra):
        self.config(vault_path=str(self.vault), addon_fixes=mode, **extra)

    def check(self, *files, repo=None, env=None):
        repo = repo or self.repo
        return self.py(GT_CHECK, "run", *(files or ("a.html",)), "--plugin-root", self.root,
                       "--repo", repo, "--vault", self.vault, cwd=str(repo), env=env)

    def apply(self, *args, env=None):
        return self.py(GT_APPLY, *args, "--vault", self.vault, env=env)

    def pending(self):
        p = self.apply("list", "--json")
        self.assertOk(p)
        return json.loads(p.stdout)["proposals"]

    def only_id(self):
        props = self.pending()
        self.assertEqual(len(props), 1, props)
        return props[0]["id"]

    def bytes(self, rel="a.html", root=None):
        return ((root or self.repo) / rel).read_text()

    def propose_and_apply(self, env=None):
        self.check(env=env)
        pid = self.only_id()
        return pid, self.apply("apply", pid, env=env)

    def events(self):
        d = self.vault / "Projects" / "golden-thread" / "spool" / "events"
        rows = []
        for f in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
            rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return [r for r in rows if r["kind"] == "addon.fix"]


class ModesTest(ApplyBase):
    def test_a_proposal_is_listed_with_addon_file_and_diff(self):
        self.check()
        p = self.apply("list")
        self.assertOk(p)
        self.assertIn("fx", p.stdout)
        self.assertIn("a.html", p.stdout)
        self.assertIn("-<p>BAD-MARKER</p>", p.stdout)
        self.assertIn("+<p>good</p>", p.stdout)

    def test_off_discards_and_changes_nothing(self):
        self.setmode("off")
        p = self.check()
        self.assertIn("discarded", p.stdout)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.bytes(), ORIGINAL)

    def test_propose_changes_nothing_until_apply(self):
        self.check()
        self.assertEqual(self.bytes(), ORIGINAL)
        p = self.apply("apply", self.only_id())
        self.assertOk(p)
        self.assertIn("<p>good</p>", self.bytes())
        self.assertEqual(self.check().returncode, 0, "the file's check must pass afterwards")

    def test_apply_mode_writes_and_the_check_then_passes(self):
        self.setmode("apply")
        self.check()
        self.assertIn("<p>good</p>", self.bytes())
        self.assertEqual(self.check().returncode, 0)

    def test_undo_restores_the_exact_original_bytes(self):
        pid, p = self.propose_and_apply()
        self.assertOk(p)
        self.assertOk(self.apply("undo", pid))
        self.assertEqual((self.repo / "a.html").read_bytes(), ORIGINAL.encode())

    def test_an_applied_fix_is_left_uncommitted(self):
        log0 = self.run_cmd(["git", "-C", self.repo, "rev-list", "--count", "HEAD"]).stdout
        _pid, p = self.propose_and_apply()
        self.assertOk(p)
        diff = self.run_cmd(["git", "-C", self.repo, "diff", "--name-only"]).stdout.split()
        self.assertEqual(diff, ["a.html"])
        self.assertEqual(self.run_cmd(["git", "-C", self.repo, "rev-list", "--count",
                                       "HEAD"]).stdout, log0)

    def test_each_outcome_records_one_event(self):
        _pid, p = self.propose_and_apply()
        self.assertOk(p)
        (self.repo / "a.html").write_text(ORIGINAL)        # again, so a second proposal exists
        self.check()
        pid2 = self.only_id()
        (self.repo / "a.html").write_text(ORIGINAL + "<!-- edited -->\n")
        self.assertEqual(self.apply("apply", pid2).returncode, 1)
        ev = self.events()
        self.assertEqual(len(ev), 2, ev)
        self.assertTrue(all("fx" in e["note"] and "a.html" in e["note"] for e in ev))
        self.assertEqual(sorted(e["note"].rsplit("-> ", 1)[1] for e in ev),
                         ["applied", "refused"])


class RefusalsTest(ApplyBase):
    def test_a_stale_repo_file_is_refused_and_left_alone(self):
        self.check()
        edited = ORIGINAL + "<!-- edited after the check -->\n"
        (self.repo / "a.html").write_text(edited)
        p = self.apply("apply", self.only_id())
        self.assertEqual(p.returncode, 1)
        self.assertIn("changed since", p.stdout)
        self.assertEqual(self.bytes(), edited)

    def test_a_protected_path_is_refused_even_when_granted(self):
        (self.repo / "core-rules").mkdir()
        (self.repo / "core-rules" / "x.html").write_text(ORIGINAL)
        self.check("core-rules/x.html")
        p = self.apply("apply", self.only_id())
        self.assertEqual(p.returncode, 1)
        self.assertIn("core-rules/ is a protected path", p.stdout)
        self.assertEqual(self.bytes("core-rules/x.html"), ORIGINAL)

    def test_a_file_claimed_by_another_live_session_is_refused_by_name(self):
        notes = self.vault / "Notes"
        notes.mkdir()
        (notes / "page.html").write_text(ORIGINAL)
        tool = self.vault.joinpath(*VAULT_TOOLS) / "gt_session.py"
        self.assertOk(self.py(tool, "--vault", self.vault, "--id", "other-session", "register",
                              "--files", "Notes/page.html",
                              env={"CLAUDE_PID": str(os.getpid())}))
        self.check("Notes/page.html", repo=self.vault)
        p = self.apply("apply", self.only_id())
        self.assertEqual(p.returncode, 1)
        self.assertIn("other-session", p.stdout)
        self.assertEqual((notes / "page.html").read_text(), ORIGINAL)

    def test_did_not_fix_is_rolled_back(self):
        root = self.tmp / "p2"
        make_module(root, [(FIX, {"mark": "BAD-MARKER", "replace": ["<p>other", "<p>else"]})])
        self.root = root
        self.check()
        p = self.apply("apply", self.only_id())
        self.assertEqual(p.returncode, 1)
        self.assertIn("did-not-fix", p.stdout)
        self.assertEqual(self.bytes(), ORIGINAL)


class VaultMarkdownTest(ApplyBase):
    """A vault Markdown file is written through the write queue and broker (Core rule 1),
    never directly -- and an undo goes the same way."""
    checkers = (({"id": "md-fix", "globs": ["*.md"], "fixes": ["*.md"]},
                 {"mark": "BAD-MARKER", "replace": ["BAD-MARKER", "good"]}),)

    def test_a_vault_markdown_fix_goes_through_the_queue(self):
        page = self.vault / "Notes" / "page.md"
        page.parent.mkdir()
        page.write_text("# Page\n\nBAD-MARKER\n")
        self.check("Notes/page.md", repo=self.vault)
        pid = self.only_id()
        p = self.apply("apply", pid)
        self.assertOk(p)
        self.assertEqual(page.read_text(), "# Page\n\ngood\n")
        queued = list((self.vault / "Projects" / "golden-thread" / "spool").rglob("*.json*"))
        self.assertTrue(any("page.md" in q.read_text(errors="replace") for q in queued
                            if q.is_file()), "no trace of the write in the queue/broker spool")
        self.assertOk(self.apply("undo", pid))
        self.assertEqual(page.read_text(), "# Page\n\nBAD-MARKER\n")


class OutOfFindingsTest(ApplyBase):
    checkers = ((FIX, {"mark": "BAD-MARKER", "replace": ["clean", "cleaner"],
                       "propose_file": "b.html"}),)

    def test_a_file_the_findings_did_not_name_is_refused(self):
        self.check("a.html", "b.html")
        p = self.apply("apply", self.only_id())
        self.assertEqual(p.returncode, 1)
        self.assertIn("not a file the add-on's findings named", p.stdout)
        self.assertEqual(self.bytes("b.html"), "<p>clean</p>\n")


class AddonCannotWriteTest(ApplyBase):
    checkers = (({"id": "writer", "globs": ["*.html"], "fixes": ["*.html"]},
                 {"behaviour": "mutate"}),)
    mode = "apply"

    def test_a_write_during_the_run_is_a_violation_and_the_file_is_unchanged(self):
        p = self.py(GT_CHECK, "run", "a.html", "--json", "--plugin-root", self.root,
                    "--vault", self.vault, cwd=str(self.repo))
        r = json.loads(p.stdout)["results"][0]
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("read-only violation", r["reason"])
        self.assertEqual(self.bytes(), ORIGINAL)


class RaceTest(ApplyBase):
    checkers = ((FIX, REMOVE),
                (dict(FIX, id="html-fix-two"),
                 {"mark": "BAD-MARKER", "rule": "marker2", "replace": ["BAD-MARKER", "fine"]}))

    def test_two_concurrent_applies_write_at_most_once(self):
        self.check()
        ids = [p["id"] for p in self.pending()]
        self.assertEqual(len(ids), 2)
        procs = [subprocess.Popen([PYTHON, str(GT_APPLY), "apply", i, "--vault", str(self.vault)],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for i in ids]
        codes = sorted(p.wait(timeout=180) for p in procs)
        self.assertEqual(codes, [0, 1])
        text = self.bytes()
        self.assertEqual(("<p>good</p>" in text) + ("<p>fine</p>" in text), 1, text)


class IndependentRecheckTest(ApplyBase):
    checkers = ((FIX, {"mark": "BAD-MARKER", "replace": ["BAD-MARKER", "FORBIDDEN"]}),
                ({"id": "guard", "globs": ["*.html"]}, {"mark": "FORBIDDEN", "rule": "forbid"}))

    def test_a_fix_that_breaks_another_checker_is_refused(self):
        _pid, p = self.propose_and_apply()
        self.assertEqual(p.returncode, 1)
        self.assertIn("fx/guard", p.stdout)
        self.assertEqual(self.bytes(), ORIGINAL)


class ContentRulesTest(ApplyBase):
    def refused(self, cfg, expect, env=None, **settings):
        root = self.tmp / ("p-%d" % len(list(self.tmp.iterdir())))
        make_module(root, [(FIX, dict({"mark": "BAD-MARKER"}, **cfg))])
        self.root = root
        if settings:
            self.setmode("propose", **settings)
        pid, p = self.propose_and_apply(env=env)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(expect, p.stdout)
        self.assertEqual(self.bytes(), ORIGINAL)
        self.apply("list")                                  # leaves state for the next case
        return p

    HDR = "diff --git a/a.html b/a.html\n"

    def test_mode_bit(self):
        self.refused({"raw_diff": self.HDR + "old mode 100644\nnew mode 100755\n"},
                     "executable bit")

    def test_create(self):
        self.refused({"raw_diff": "--- /dev/null\n+++ b/a.html\n@@ -0,0 +1 @@\n+x\n"},
                     "creates a file")

    def test_delete(self):
        self.refused({"raw_diff": "--- a/a.html\n+++ /dev/null\n@@ -1,4 +0,0 @@\n"
                                  + "".join("-" + l for l in ORIGINAL.splitlines(True))},
                     "deletes a file")

    def test_symlink(self):
        self.refused({"raw_diff": self.HDR + "old mode 100644\nnew mode 120000\n"}, "symlink")

    def test_bidi_override(self):
        self.refused({"replace": ["BAD-MARKER", "ok‮"]}, "U+202E")

    def test_zero_width(self):
        self.refused({"replace": ["BAD-MARKER", "o​k"]}, "U+200B")

    def test_scrub_term_is_refused_without_being_printed(self):
        terms = self.tmp / "terms.txt"
        terms.write_text("zebrafrobnicator\n")
        p = self.refused({"replace": ["BAD-MARKER", "zebrafrobnicator"]}, "scrub term",
                         env={"GT_SCRUB_TERMS": str(terms)})
        self.assertNotIn("zebrafrobnicator", p.stdout + p.stderr)

    def test_oversize(self):
        self.refused({"replace": ["BAD-MARKER", "x" * 3000]}, "size", addon_fix_size_limit="1k")

    def test_html_script(self):
        self.refused({"replace": ["BAD-MARKER", "<script>x()</script>"]}, "<script>")

    def test_html_on_event(self):
        self.refused({"replace": ["<p>BAD-MARKER", '<p onclick="x()">ok']}, "on-event")

    def test_html_new_host(self):
        self.refused({"replace": ["BAD-MARKER", '<a href="https://new-host.example/">x</a>']},
                     "host")

    def test_adding_alt_passes(self):
        root = self.tmp / "alt"
        make_module(root, [(FIX, {"mark": "BAD-MARKER",
                                  "replace": ["<p>BAD-MARKER</p>", '<img alt="ok" src="a.png">']})])
        self.root = root
        _pid, p = self.propose_and_apply()
        self.assertOk(p)
        self.assertIn('alt="ok"', self.bytes())


class TierCapTest(ApplyBase):
    first_party = False
    mode = "apply"

    def test_a_non_first_party_proposal_is_capped_to_propose(self):
        p = self.check()
        self.assertIn("capped to propose", p.stdout)
        self.assertEqual(self.bytes(), ORIGINAL)
        self.assertEqual(len(self.pending()), 1)


if __name__ == "__main__":
    unittest.main()
