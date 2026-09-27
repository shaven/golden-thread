"""templates/githooks: prepare-commit-msg, post-commit and pre-commit, end to end.

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

And one hook with the OPPOSITE contract, in PreCommitCredentialGate below:
  * pre-commit FAILS CLOSED -- a staged credential, an absent scanner or a scanner that
    errored all refuse the commit, because a credential in git history cannot be
    un-published while a missed attribution can be reconstructed. Its escapes
    (--no-verify, gt.secretsgate off, a baseline) are each tested, and the "off" state is
    asserted to announce itself.
"""
import json
import os
import shutil
import socket
import unittest

from _harness import Sandbox, PYTHON, SCRIPTS

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


class PreCommitCredentialGate(Sandbox):
    """templates/githooks/pre-commit — the ONLY hook here that fails CLOSED.

    The other two are bookkeeping and exit 0 on every path, because a missed attribution can
    be reconstructed later. A credential in git history cannot be un-published: it is in every
    clone and every backup immediately and permanently, and the only real remedy is rotating
    the credential. So "I could not check" must behave as "do not commit".

    These tests assert that asymmetry directly, because it is exactly the property a later
    edit would "tidy up" by making this hook consistent with its two neighbours.
    """

    # A deliberate fake, assembled so this test file is not itself a finding.
    FAKE = "AKIA" + "IOSFODNN7EXAMPLE"

    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git not installed")
        self.vault = self.make_vault().resolve()
        self.env["GIT_EDITOR"] = "true"
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args, ok=True, env=None):
        proc = self.run_cmd(["git", *args], cwd=self.vault, env=env)
        if ok:
            self.assertOk(proc, "git " + " ".join(str(a) for a in args))
        return proc

    def commits(self):
        out = self.run_cmd(["git", "rev-list", "--count", "HEAD"], cwd=self.vault).stdout
        return int(out.strip() or 0)

    def stage(self, rel, text):
        (self.vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.vault / rel).write_text(text)
        self.git("add", rel)

    def test_the_gate_is_wired_and_executable(self):
        """A hook that is not executable is skipped by git IN SILENCE."""
        hook = self.vault / ".githooks" / "pre-commit"
        self.assertTrue(hook.is_file(), "vault_init did not seed the credential gate")
        self.assertTrue(os.access(hook, os.X_OK), "pre-commit is not executable")

    def test_a_staged_credential_refuses_the_commit(self):
        before = self.commits()
        self.stage("conf.py", 'AWS_KEY = "%s"\n' % self.FAKE)
        proc = self.git("commit", "-m", "add conf", ok=False)
        self.assertNotEqual(proc.returncode, 0, "the commit was NOT blocked:\n" + proc.stdout)
        self.assertIn("COMMIT REFUSED", proc.stdout + proc.stderr)
        self.assertEqual(self.commits(), before, "a commit was created despite the refusal")

    def test_a_clean_commit_is_not_impeded(self):
        """A gate that blocks honest work is one people disable."""
        before = self.commits()
        self.stage("notes/ok.md", "nothing secret here\n")
        self.git("commit", "-q", "-m", "ok")
        self.assertEqual(self.commits(), before + 1)

    def test_the_refusal_never_prints_the_value(self):
        self.stage("conf.py", 'AWS_KEY = "%s"\n' % self.FAKE)
        proc = self.git("commit", "-m", "x", ok=False)
        self.assertNotIn(self.FAKE, proc.stdout + proc.stderr,
                         "the gate printed the credential it was protecting")

    def test_no_verify_is_the_documented_escape(self):
        """Git's own flag must keep working; a gate with no escape gets deleted instead."""
        before = self.commits()
        self.stage("conf.py", 'AWS_KEY = "%s"\n' % self.FAKE)
        self.git("commit", "-q", "--no-verify", "-m", "forced")
        self.assertEqual(self.commits(), before + 1)

    def test_turning_the_gate_off_is_never_silent(self):
        """A gate you have forgotten you disabled is indistinguishable from a working one."""
        self.git("config", "gt.secretsgate", "off")
        self.stage("conf.py", 'AWS_KEY = "%s"\n' % self.FAKE)
        proc = self.git("commit", "-m", "gate off")
        self.assertIn("OFF", proc.stdout + proc.stderr,
                      "the gate was off and said nothing about it")

    def test_an_absent_scanner_refuses_rather_than_passes(self):
        """The asymmetry as a test: unknown is not clean.

        The sandbox HOME holds no plugin cache, so clearing the override leaves the hook
        genuinely unable to resolve a scanner -- exactly a machine where gt is not installed.
        """
        before = self.commits()
        env = dict(self.env)
        env["GT_SECRETS_BIN"] = ""
        self.stage("notes/ok.md", "not a credential\n")
        proc = self.run_cmd(["git", "commit", "-m", "unchecked"], cwd=self.vault, env=env)
        self.assertNotEqual(proc.returncode, 0,
                            "a commit went through UNCHECKED:\n" + proc.stdout + proc.stderr)
        self.assertIn("not installed", proc.stdout + proc.stderr)
        self.assertEqual(self.commits(), before)

    def test_an_override_pointing_nowhere_refuses(self):
        """Falling back silently would make a typo in the path look like a clean scan."""
        env = dict(self.env)
        env["GT_SECRETS_BIN"] = str(self.tmp / "nope" / "gt_secrets.py")
        self.stage("notes/ok.md", "fine\n")
        proc = self.run_cmd(["git", "commit", "-m", "x"], cwd=self.vault, env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not exist", proc.stdout + proc.stderr)

    def test_committing_in_the_vault_does_not_leave_it_dirty(self):
        """The gate records its verdict in the vault -- but NOT when the repo it is guarding IS
        the vault.

        Writing a vault file from a vault pre-commit hook leaves an unstaged modification after
        every single commit: the report is written, the commit does not contain it, and the tree
        is dirty from then on, one line per commit forever. This shipped for about an hour on
        2026-09-27 and was caught by 18 test units asserting a vault is clean after an install --
        not by any test of the gate itself. This is that test, so the next person does not have
        to rediscover it from eighteen unrelated failures.
        """
        self.stage("notes/ok.md", "nothing secret\n")
        self.git("commit", "-q", "-m", "a normal vault commit")

        # An ABSOLUTE assertion, not a before/after comparison. The first version of this test
        # compared porcelain before and after and PASSED with the guard stripped out, because
        # setUp already makes a commit -- so the stray report was present in `before` too and
        # the difference cancelled. A test that cannot fail is worse than no test, and this one
        # was verified to fail with the guard removed.
        checks = self.vault / ".gt" / "checks"
        self.assertFalse(
            list(checks.glob("*.md")) if checks.is_dir() else [],
            "a check report was written into the vault being committed, so every commit in "
            "the vault now leaves it dirty")
        porcelain = self.run_cmd(["git", "status", "--porcelain"], cwd=self.vault).stdout
        self.assertNotIn(".gt/checks", porcelain, "the commit left a report behind:\n" + porcelain)

    def test_recording_is_opt_in_and_works_when_opted_in(self):
        """Two halves, and the second is the one that keeps the first honest.

        Off by default: a hook that writes to the vault on every commit in every repo on the
        machine is a side effect nobody asked for while typing `git commit`, and it leaves the
        vault dirty with unrelated work. gt's demo test caught this -- a commit in the demo
        vault was writing into the configured one.

        But a feature only ever tested in its OFF state is indistinguishable from a feature
        that does not work, so this also opts in and asserts the record actually lands.
        """
        other = self.tmp / "codeproj"
        other.mkdir()
        self.run_cmd(["git", "init", "-q", "-b", "main", str(other)])
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"),
                     ("core.hooksPath", str(self.vault / ".githooks"))):
            self.run_cmd(["git", "-C", str(other), "config", k, v])
        (other / "a.py").write_text("x = 1\n")
        self.run_cmd(["git", "-C", str(other), "add", "a.py"])

        # OFF: nothing is recorded in the configured vault
        self.run_cmd(["git", "-C", str(other), "commit", "-q", "-m", "one"])
        checks = self.vault / ".gt" / "checks"
        self.assertFalse(checks.is_dir() and list(checks.glob("*.md")),
                         "a commit in an unrelated repo wrote to the vault without being asked")

        # ON: the record lands
        self.run_cmd(["git", "-C", str(other), "config", "gt.checkreport", "on"])
        (other / "b.py").write_text("y = 2\n")
        self.run_cmd(["git", "-C", str(other), "add", "b.py"])
        self.run_cmd(["git", "-C", str(other), "commit", "-q", "-m", "two"])
        written = sorted(p.name for p in checks.glob("*.md")) if checks.is_dir() else []
        self.assertIn("secrets-commit.md", written,
                      "opted in, and still nothing was recorded — the feature only works "
                      "in its off state")

    def test_a_baseline_quiets_an_accepted_finding(self):
        """The supported way to accept a finding -- not --no-verify, and not switching off."""
        self.stage("conf.py", 'AWS_KEY = "%s"\n' % self.FAKE)
        self.git("commit", "-m", "blocked", ok=False)          # it blocks first
        base = self.vault / ".gt" / "secrets-baseline.json"
        base.parent.mkdir(parents=True, exist_ok=True)
        self.assertOk(self.run_cmd([
            PYTHON, str(SCRIPTS / "gt_secrets.py"), str(self.vault),
            "--write-baseline", str(base)]))
        before = self.commits()
        self.git("commit", "-q", "-m", "accepted")
        self.assertEqual(self.commits(), before + 1,
                         "a baselined finding still blocked the commit")


if __name__ == "__main__":
    unittest.main()
