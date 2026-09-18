"""gt_edits.py: per-edit attribution (session + host) in <repo>/.git/gt-edits.jsonl.

safe_write.write() calls record() on every successful write; prepare-commit-msg
renders the ledger into `Session-Edit:` trailers and post-commit drains it (those
hooks are covered end to end in test_githooks.py). Here each function is pinned on
its own, in a subprocess, against the sandbox vault's git repo.
"""
import json
import os
import unittest

from _harness import Sandbox, PYTHON

SID = "abcdef12-3456-7890-abcd-ef1234567890"


class GtEditsTest(Sandbox):
    def setUp(self):
        super().setUp()
        # Resolved: the sandbox sits under /var, a symlink to /private/var on macOS,
        # and git reports the resolved git dir. record()'s only caller, safe_write,
        # resolves the target first, so tests do too.
        self.vault = self.make_vault().resolve()
        if not (self.vault / ".git").is_dir():
            self.skipTest("git not installed: vault was not initialised as a repo")
        self.tools = self.vault / "Projects" / "golden-thread" / "tools"
        self.ledger = self.vault / ".git" / "gt-edits.jsonl"
        self.env["GT_SESSION_ID"] = SID

    def ge(self, body, cwd=None):
        code = ("import json, os, sys\nsys.path.insert(0, %r)\n"
                "import gt_edits as ge, safe_write as sw\n" % str(self.tools)) + body
        proc = self.run_cmd([PYTHON, "-c", code], cwd=cwd or self.vault)
        self.assertOk(proc, "driver failed")
        out = proc.stdout.strip().splitlines()
        return json.loads(out[-1]) if out else None

    def rows(self):
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text().splitlines() if l.strip()]

    def seed(self, *rows):
        with open(self.ledger, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def git(self, *args):
        proc = self.run_cmd(["git", "-C", self.vault, *args])
        self.assertOk(proc, "git " + " ".join(args))
        return proc.stdout

    # -- record --------------------------------------------------------------
    def test_safe_write_records_session_host_task_and_relative_path(self):
        gs = self.tools / "gt_session.py"
        self.assertOk(self.py(gs, "--id", SID, "register", "--task", "tidy the notes",
                              env={"CLAUDE_PID": str(os.getpid())}))
        target = self.vault / "Knowledge" / "page.md"
        self.ge("print(json.dumps(sw.write(%r, 'hi\\n')))" % str(target))
        (row,) = self.rows()
        self.assertEqual(row["path"], os.path.join("Knowledge", "page.md"))
        self.assertEqual(row["session"], SID[:8])
        self.assertEqual(row["task"], "tidy the notes")
        self.assertEqual(row["strategy"], "atomic")
        self.assertTrue(row["host"] and not row["host"].endswith(".local"))

    def test_append_is_recorded_with_its_strategy(self):
        self.ge("sw.write(%r, 'x\\n', 'a')" % str(self.vault / "log.md"))
        self.assertEqual([(r["path"], r["strategy"]) for r in self.rows()], [("log.md", "direct")])

    def test_unknown_session_is_recorded_as_unknown(self):
        env = {k: v for k, v in self.env.items() if k != "GT_SESSION_ID"}
        self.env = env
        body = ("os.environ.clear(); os.environ.update(%r)\n"
                "print(json.dumps(ge.record(%r)))" % ({"PATH": os.environ.get("PATH", "")},
                                                      str(self.vault / "a.md")))
        row = self.ge(body)
        self.assertEqual(row["session"], "unknown")

    def test_writes_outside_a_repo_and_inside_dot_git_are_not_recorded(self):
        outside = self.tmp / "loose" / "f.md"
        self.assertIsNone(self.ge("print(json.dumps(ge.record(%r)))" % str(outside)))
        self.assertIsNone(self.ge("print(json.dumps(ge.record(%r)))" % str(self.vault / ".git" / "x")))
        self.assertEqual(self.rows(), [])

    def test_record_never_raises(self):
        self.assertIsNone(self.ge("print(json.dumps(ge.record(None)))"))

    def test_a_write_in_a_linked_worktree_is_recorded_relative_to_that_worktree(self):
        """_git_dir's comment says --git-dir is used "so this keeps working inside a worktree
        or a submodule". _repo_root then returned None unless the git dir's basename was
        literally `.git` -- which it never is in either case: a worktree's is
        `.git/worktrees/<name>`. So the path was recorded absolute and _task_for gave up,
        losing attribution in exactly the layout the comment says is handled."""
        self.git("commit", "-q", "--allow-empty", "-m", "base")
        wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", "-b", "side", str(wt))
        # Resolved, for the same reason setUp resolves the vault: the sandbox sits under /var,
        # a symlink to /private/var, and git answers with the resolved path.
        wt = wt.resolve()
        target = wt / "Knowledge" / "page.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        row = self.ge("print(json.dumps(ge.record(%r)))" % str(target), cwd=wt)
        self.assertIsNotNone(row, "a write inside a worktree was not recorded at all")
        self.assertEqual(row["path"], os.path.join("Knowledge", "page.md"),
                         "the path was not made relative to the worktree")

    # -- trailers ------------------------------------------------------------
    def test_trailers_one_per_path_session_host_newest_task_wins_and_staged_only(self):
        self.seed({"path": "a.md", "session": "s1", "host": "h", "task": "old"},
                  {"path": "a.md", "session": "s1", "host": "h", "task": "new"},
                  {"path": "a.md", "session": "s2", "host": "h", "task": None},
                  {"path": "b.md", "session": "s1", "host": "h", "task": "b"})
        got = self.ge("print(json.dumps(ge.trailers(%r, {'a.md'})))" % str(self.vault / ".git"))
        self.assertEqual(got, ["Session-Edit: a.md · s1 · h · new",
                               "Session-Edit: a.md · s2 · h"])
        allrows = self.ge("print(json.dumps(ge.trailers(%r)))" % str(self.vault / ".git"))
        self.assertEqual(len(allrows), 3)

    def test_malformed_ledger_lines_are_skipped(self):
        self.ledger.write_text('not json\n\n{"path": "a.md", "session": "s", "host": "h"}\n')
        got = self.ge("print(json.dumps(ge.trailers(%r)))" % str(self.vault / ".git"))
        self.assertEqual(got, ["Session-Edit: a.md · s · h"])

    def test_apply_to_message_appends_final_paragraph_and_is_idempotent(self):
        self.seed({"path": "a.md", "session": "s1", "host": "h", "task": "t"})
        msg = self.tmp / "MSG"
        msg.write_text("Subject line\n\nBody text.\n")
        g = str(self.vault / ".git")
        self.assertEqual(self.ge("print(ge.apply_to_message(%r, %r))" % (str(msg), g)), 1)
        self.assertEqual(msg.read_text(), "Subject line\n\nBody text.\n\nSession-Edit: a.md · s1 · h · t\n")
        self.assertEqual(self.ge("print(ge.apply_to_message(%r, %r))" % (str(msg), g)), 0)
        self.assertEqual(msg.read_text().count("Session-Edit:"), 1, "a second pass stacked a duplicate")

    def test_apply_to_message_ignores_a_copy_only_inside_comments(self):
        self.seed({"path": "a.md", "session": "s1", "host": "h"})
        msg = self.tmp / "MSG"
        msg.write_text("Subject\n# Session-Edit: a.md · s1 · h\n")
        self.ge("print(ge.apply_to_message(%r, %r))" % (str(msg), str(self.vault / ".git")))
        live = [l for l in msg.read_text().splitlines() if l.startswith("Session-Edit:")]
        self.assertEqual(live, ["Session-Edit: a.md · s1 · h"])

    def test_apply_to_message_with_nothing_staged_in_scope_leaves_message_alone(self):
        self.seed({"path": "b.md", "session": "s1", "host": "h"})
        msg = self.tmp / "MSG"
        msg.write_text("Subject\n")
        n = self.ge("print(ge.apply_to_message(%r, %r, {'a.md'}))" % (str(msg), str(self.vault / ".git")))
        self.assertEqual(n, 0)
        self.assertEqual(msg.read_text(), "Subject\n")

    # -- clear ---------------------------------------------------------------
    def test_clear_keeps_only_the_named_unstaged_paths(self):
        self.seed({"path": "a.md", "session": "s"}, {"path": "b.md", "session": "s"},
                  {"path": "b.md", "session": "t"})
        n = self.ge("print(ge.clear(%r, keep_unstaged={'b.md'}))" % str(self.vault / ".git"))
        self.assertEqual(n, 1)
        self.assertEqual(sorted(r["session"] for r in self.rows() if r["path"] == "b.md"), ["s", "t"])
        self.assertFalse([r for r in self.rows() if r["path"] == "a.md"])

    def test_cli_clear_keeps_entries_for_files_not_in_this_commit(self):
        # main() `clear` promises "Keep anything not in this commit", but passes
        # keep_unstaged=None when something is staged and set() otherwise; both
        # keep nothing, so the CLI always wipes the whole ledger.
        (self.vault / "a.md").write_text("a\n")
        (self.vault / "b.md").write_text("b\n")
        self.git("add", "a.md")
        self.seed({"path": "a.md", "session": "s", "host": "h"},
                  {"path": "b.md", "session": "s", "host": "h"})
        proc = self.py(self.tools / "gt_edits.py", "clear", cwd=self.vault)
        self.assertOk(proc)
        self.assertEqual([r["path"] for r in self.rows()], ["b.md"],
                         "`gt_edits.py clear` dropped attribution for b.md, which was not staged")

    # -- CLI -----------------------------------------------------------------
    def test_cli_show_trailers_and_outside_repo(self):
        (self.vault / "a.md").write_text("a\n")
        self.git("add", "a.md")
        self.seed({"ts": "2026-09-11T10:00:00", "path": "a.md", "session": "s1", "host": "h", "task": "t"},
                  {"ts": "2026-09-11T10:00:01", "path": "b.md", "session": "s1", "host": "h"})
        show = self.py(self.tools / "gt_edits.py", "show", cwd=self.vault)
        self.assertOk(show)
        self.assertIn("2 pending edit(s)", show.stdout)
        tr = self.py(self.tools / "gt_edits.py", "trailers", cwd=self.vault)
        self.assertOk(tr)
        self.assertEqual(tr.stdout.strip().splitlines(), ["Session-Edit: a.md · s1 · h · t"])
        loose = self.tmp / "loose"
        loose.mkdir()
        out = self.py(self.tools / "gt_edits.py", "show", cwd=loose)
        self.assertEqual(out.returncode, 1)
        self.assertIn("not a git repository", out.stdout)


if __name__ == "__main__":
    unittest.main()
