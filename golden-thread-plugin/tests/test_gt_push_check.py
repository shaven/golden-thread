"""gt_push_check.py: does the vault hold commits that exist on this disk only?

Every repo here is built in the sandbox; the "remote" is a bare repo beside it, so
nothing touches the network. States: no vault, not a repo, detached HEAD, no remote,
no upstream, upstream configured but ref gone, in sync, ahead (today / weeks old),
worktree (.git is a file), git missing, policy off, --hook JSON.
"""
import json
import os
import shutil
import time
import unittest

from _harness import Sandbox, SCRIPTS

TOOL = SCRIPTS / "gt_push_check.py"


class PushCheck(Sandbox):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git not installed")
        self.vault = self.tmp / "vault"

    def git(self, *args, cwd=None, env=None):
        p = self.run_cmd(["git", "-C", cwd or self.vault, *args], env=env)
        self.assertOk(p, "git " + " ".join(map(str, args)))
        return p.stdout.strip()

    def commit(self, msg, days_ago=0):
        env = {}
        if days_ago:
            stamp = "%d +0000" % (time.time() - days_ago * 86400)
            env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        (self.vault / "note.md").write_text(msg + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg, env=env)

    def repo_with_remote(self, track=True):
        self.git_init(self.vault)
        self.commit("first")
        bare = self.tmp / "origin.git"
        self.run_cmd(["git", "init", "-q", "--bare", bare])
        self.git("remote", "add", "origin", str(bare))
        self.git("push", "-q", *(["-u"] if track else []), "origin", "main")
        return bare

    def check(self, *extra, **kw):
        p = self.py(TOOL, "check", *extra, **kw)
        self.assertOk(p, "push check must never fail a session start")
        return p.stdout

    # -- nothing to check ----------------------------------------------------------
    def test_no_config(self):
        self.assertIn("no vault path configured", self.check())

    def test_configured_vault_missing(self):
        self.config(vault_path=str(self.tmp / "gone"))
        self.assertIn("no vault path configured", self.check())

    def test_vault_not_a_repo(self):
        self.vault.mkdir()
        self.config(vault_path=str(self.vault))
        self.assertIn("no vault git repo", self.check())

    def test_git_missing(self):
        self.vault.mkdir()
        empty = self.tmp / "emptybin"
        empty.mkdir()
        out = self.check(self.vault, env={"PATH": str(empty)})
        self.assertIn("git is not installed", out)

    # -- the loud cases ------------------------------------------------------------
    def test_no_remote(self):
        self.git_init(self.vault)
        self.commit("second")
        self.config(vault_path=str(self.vault))
        out = self.check()
        self.assertIn("vault branch 'main' has NO REMOTE", out)
        self.assertIn("2 commit(s) exist on this disk only", out)
        self.assertIn("remote add origin", out)

    def test_no_upstream(self):
        self.repo_with_remote(track=False)
        self.config(vault_path=str(self.vault))
        out = self.check()
        self.assertIn("has NO UPSTREAM", out)
        self.assertIn("push -u origin main", out)
        self.assertNotIn("in sync", out)

    def test_upstream_configured_but_ref_gone(self):
        self.repo_with_remote()
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        self.config(vault_path=str(self.vault))
        out = self.check()
        self.assertIn("configured to track origin/main", out)
        self.assertIn("does not exist locally", out)
        self.assertIn("fetch origin", out)

    def test_detached_head(self):
        self.git_init(self.vault)
        self.commit("second")
        self.git("checkout", "-q", "--detach", "HEAD")
        self.config(vault_path=str(self.vault))
        self.assertIn("detached HEAD", self.check())

    # -- upstream present ----------------------------------------------------------
    def test_in_sync(self):
        self.repo_with_remote()
        self.config(vault_path=str(self.vault))
        self.assertEqual(self.check().strip(),
                         "GOLDEN THREAD push: vault in sync with origin/main.")

    def test_ahead_today(self):
        self.repo_with_remote()
        self.commit("a")
        self.commit("b")
        self.config(vault_path=str(self.vault))
        out = self.check()
        self.assertIn("vault is 2 commit(s) AHEAD of origin/main, all from today", out)
        self.assertIn('push with: git -C "%s" push' % self.vault, out)

    def test_ahead_age_is_the_oldest_commit(self):
        self.repo_with_remote()
        self.commit("old", days_ago=20)
        self.commit("mid", days_ago=5)
        self.commit("new")
        self.config(vault_path=str(self.vault))
        out = self.check()
        self.assertIn("3 commit(s) AHEAD", out)
        self.assertIn("oldest 20 days old", out)

    def test_worktree_with_git_file_is_a_repo(self):
        self.repo_with_remote()
        self.commit("ahead")
        wt = self.tmp / "wt"
        self.git("worktree", "add", "-q", "-b", "side", str(wt), "HEAD")
        self.assertTrue((wt / ".git").is_file())
        self.git("branch", "-q", "--set-upstream-to=origin/main", "side", cwd=wt)
        out = self.check(wt)
        self.assertNotIn("no vault git repo", out)
        self.assertIn("1 commit(s) AHEAD of origin/main", out)

    # -- resolution and delivery ---------------------------------------------------
    def test_explicit_arg_and_gt_vault_override_config(self):
        self.repo_with_remote()
        other = self.tmp / "plain"
        other.mkdir()
        self.config(vault_path=str(other))
        self.assertIn("no vault git repo", self.check())
        self.assertIn("in sync", self.check(env={"GT_VAULT": str(self.vault)}))
        self.assertIn("in sync", self.check(self.vault))
        # a GT_VAULT that does not exist is ignored, not trusted
        self.assertIn("no vault git repo",
                      self.check(env={"GT_VAULT": str(self.tmp / "nope")}))

    def test_policy_off(self):
        self.git_init(self.vault)
        self.config(vault_path=str(self.vault), push_check="off")
        self.assertEqual(self.check(), "")
        self.assertEqual(self.check("--hook"), "")

    def test_hook_json(self):
        self.git_init(self.vault)
        self.config(vault_path=str(self.vault))
        d = json.loads(self.check("--hook"))
        self.assertIn("NO REMOTE", d["systemMessage"])
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(d["hookSpecificOutput"]["additionalContext"], d["systemMessage"])

    def test_usage(self):
        p = self.py(TOOL)
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage", p.stdout)


if __name__ == "__main__":
    unittest.main()
