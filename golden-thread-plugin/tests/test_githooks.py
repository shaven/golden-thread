"""templates/githooks: prepare-commit-msg + post-commit, end to end in a sandbox vault.

vault_init seeds `.githooks/` and sets core.hooksPath, so a real `git commit` in the
sandbox vault runs the seeded hooks against the seeded gt_edits.py. Writes go through
the vault's safe_write, exactly as a session's would.

Contracts:
  * a commit carries one `Session-Edit:` trailer per staged file the ledger names,
    in the final paragraph so git parses it as a trailer;
  * post-commit drains what was committed and RETAINS attribution for edits that
    were not part of the commit;
  * an aborted commit costs nothing (the ledger is only drained after success);
  * amend/merge/squash are skipped; every failure path exits 0 (fails open).
"""
import json
import os
import shutil
import socket
import unittest

from _harness import Sandbox, PYTHON

SID = "abcdef12-3456-7890-abcd-ef1234567890"
HOST = socket.gethostname()
HOST = HOST[:-6] if HOST.endswith(".local") else HOST


class GitHooksTest(Sandbox):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git not installed")
        self.vault = self.make_vault().resolve()
        self.tools = self.vault / "Projects" / "golden-thread" / "tools"
        self.ledger = self.vault / ".git" / "gt-edits.jsonl"
        self.env["GT_SESSION_ID"] = SID
        self.env["GIT_EDITOR"] = "true"
        (self.vault / "tracked.md").write_text("v1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        if self.ledger.exists():
            self.ledger.unlink()

    # -- helpers -------------------------------------------------------------
    def git(self, *args, cwd=None, ok=True):
        proc = self.run_cmd(["git", *args], cwd=cwd or self.vault)
        if ok:
            self.assertOk(proc, "git " + " ".join(args))
        return proc

    def write(self, rel, text):
        code = ("import sys; sys.path.insert(0, %r); import safe_write as sw; "
                "sw.write(%r, %r)" % (str(self.tools), str(self.vault / rel), text))
        self.assertOk(self.run_cmd([PYTHON, "-c", code], cwd=self.vault))

    def trailer(self, rel):
        return "Session-Edit: %s · %s · %s" % (rel, SID[:8], HOST)

    def last_message(self):
        return self.git("log", "-1", "--format=%B").stdout

    def parsed_trailers(self):
        return self.git("log", "-1", "--format=%(trailers:key=Session-Edit,valueonly)").stdout.split("\n")

    def ledger_paths(self):
        if not self.ledger.exists():
            return []
        return sorted(json.loads(l)["path"] for l in self.ledger.read_text().splitlines() if l.strip())

    # -- tests ---------------------------------------------------------------
    def test_hooks_are_wired_by_vault_init(self):
        self.assertEqual(self.git("config", "core.hooksPath").stdout.strip(), ".githooks")
        for name in ("prepare-commit-msg", "post-commit"):
            self.assertTrue(os.access(self.vault / ".githooks" / name, os.X_OK), name + " not executable")

    def test_commit_carries_parsed_trailer_and_drains_ledger(self):
        self.write("notes/a.md", "a\n")
        self.assertEqual(self.ledger_paths(), ["notes/a.md"])
        self.git("add", "notes/a.md")
        self.git("commit", "-q", "-m", "add a")
        msg = self.last_message()
        self.assertTrue(msg.startswith("add a\n\n"), msg)
        self.assertIn(self.trailer("notes/a.md"), msg)
        self.assertTrue(any(v.startswith("notes/a.md · ") for v in self.parsed_trailers()),
                        "git does not parse the Session-Edit line as a trailer:\n" + msg)
        self.assertEqual(self.ledger_paths(), [], "post-commit did not drain the committed entry")

    def test_only_staged_files_get_trailers_and_unstaged_tracked_edits_are_kept(self):
        self.write("a.md", "a\n")
        self.write("tracked.md", "v2\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "only a")
        msg = self.last_message()
        self.assertIn(self.trailer("a.md"), msg)
        self.assertNotIn("tracked.md", msg, "a trailer claimed a file that is not in the commit")
        self.assertEqual(self.ledger_paths(), ["tracked.md"])

        self.git("add", "tracked.md")
        self.git("commit", "-q", "-m", "now tracked")
        self.assertIn(self.trailer("tracked.md"), self.last_message())
        self.assertEqual(self.ledger_paths(), [])

    def test_new_file_left_out_of_the_commit_keeps_its_attribution(self):
        # commit-clear retains only paths in `git diff --name-only`, which lists
        # TRACKED modifications. A brand-new file written through safe_write but not
        # in this commit is untracked, so its entry is dropped and its eventual
        # commit carries no Session-Edit trailer.
        self.write("a.md", "a\n")
        self.write("later.md", "later\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "only a")
        self.assertIn("later.md", self.ledger_paths(),
                      "post-commit dropped attribution for a new file that was not committed")
        self.git("add", "later.md")
        self.git("commit", "-q", "-m", "later")
        self.assertIn(self.trailer("later.md"), self.last_message())

    def test_empty_commit_claims_no_pending_edits(self):
        # With nothing staged, apply-msg passes `_staged(g) or None`, i.e. NO
        # restriction, so every pending edit is claimed by a commit that has none of
        # them -- the "message a lie" case trailers() exists to prevent.
        self.write("pending.md", "p\n")
        self.git("commit", "-q", "--allow-empty", "-m", "empty")
        self.assertNotIn("Session-Edit", self.last_message(),
                         "an empty commit was stamped with a trailer for an uncommitted file")

    def test_amend_does_not_stack_trailers(self):
        self.write("a.md", "a\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "-m", "add a")
        self.write("a.md", "a2\n")
        self.git("add", "a.md")
        self.git("commit", "-q", "--amend", "--no-edit")
        self.assertEqual(self.last_message().count(self.trailer("a.md")), 1)

    def test_aborted_commit_keeps_the_ledger(self):
        self.write("a.md", "a\n")
        self.git("add", "a.md")
        editor = self.tmp / "empty-editor.sh"
        editor.write_text("#!/bin/sh\n: > \"$1\"\n")
        editor.chmod(0o755)
        proc = self.run_cmd(["git", "commit"], cwd=self.vault, env={"GIT_EDITOR": str(editor)})
        self.assertNotEqual(proc.returncode, 0, "the empty-message commit was not aborted")
        self.assertEqual(self.ledger_paths(), ["a.md"], "an aborted commit drained the ledger")

    def test_verbose_editor_commit_keeps_the_trailer(self):
        # With commit.verbose (or `git commit -v`) the message file ends with a
        # "# ----- >8 -----" scissors line and the diff; git discards EVERYTHING
        # below it. apply_to_message appends at the very end, so the trailer is cut,
        # and post-commit then drains the ledger: the attribution is gone for good.
        self.write("a.md", "a\n")
        self.git("add", "a.md")
        editor = self.tmp / "subject-editor.sh"
        editor.write_text('#!/bin/sh\nprintf "subject\\n" | cat - "$1" > "$1.n" && mv "$1.n" "$1"\n')
        editor.chmod(0o755)
        proc = self.run_cmd(["git", "-c", "commit.verbose=true", "commit", "-q"],
                            cwd=self.vault, env={"GIT_EDITOR": str(editor)})
        self.assertOk(proc)
        self.assertEqual(self.last_message().splitlines()[0], "subject")
        self.assertIn(self.trailer("a.md"), self.last_message(),
                      "the trailer was written below the verbose scissors line and discarded "
                      "(ledger now: %s)" % self.ledger_paths())

    def test_commit_from_a_subdirectory(self):
        self.write("Knowledge/p.md", "p\n")
        self.git("add", "p.md", cwd=self.vault / "Knowledge")
        self.git("commit", "-q", "-m", "from subdir", cwd=self.vault / "Knowledge")
        self.assertIn(self.trailer("Knowledge/p.md"), self.last_message())

    def test_fails_open_on_garbage_ledger(self):
        (self.vault / "a.md").write_text("a\n")
        self.git("add", "a.md")
        self.ledger.write_text("{{{ not json\n")
        self.git("commit", "-q", "-m", "still commits")
        self.assertEqual(self.last_message().strip(), "still commits")

    def test_fails_open_when_the_tool_is_missing(self):
        self.write("a.md", "a\n")
        self.git("add", "a.md")
        tool = self.tools / "gt_edits.py"
        aside = self.tmp / "gt_edits.py.aside"
        shutil.move(str(tool), str(aside))
        try:
            proc = self.git("commit", "-q", "-m", "no tool", ok=False)
        finally:
            shutil.move(str(aside), str(tool))
        self.assertOk(proc, "a missing gt_edits.py blocked the commit")
        self.assertEqual(self.last_message().strip(), "no tool")
        self.assertEqual(self.ledger_paths(), ["a.md"], "ledger lost while the tool was absent")


if __name__ == "__main__":
    unittest.main()
