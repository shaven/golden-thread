"""gt_sync.py and the gt-sync skill: vault pull / push / status across machines (gt 0.18.0).

Request 2026-09-24-cross-device-sync, with the triage correction that behind-origin needs a
fetch (gt_push_check only reports ahead). Fixtures are real git repos: a bare "origin", the
vault cloned from it, and a second clone standing in for another machine.
  * pull fast-forwards and reports the newest commit's subject and the files changed;
  * pull on a diverged vault STOPS without merging or rebasing (HEAD unchanged, no merge);
  * push passes the push check and pushes; with the push check failing (no upstream) it refuses;
  * push refuses when origin is ahead (pull first);
  * status shows ahead/behind counts and uncommitted changes;
  * --dry-run never fetches, pulls or pushes;
  * the SessionStart behind line: silent when sync_check is off, `cached` reads the refs on disk
    without fetching, `fetch` sees a new remote commit; gt_push_check prints it in its one report;
  * every git command names the vault (`git -C <vault>`) -- checked in the source;
  * the skill exists and its triggers do not collide.
"""
import json
import os
import re
import unittest

from _harness import Sandbox, SCRIPTS, GT

SYNC = SCRIPTS / "gt_sync.py"
PUSH = SCRIPTS / "gt_push_check.py"


class SyncBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.tmp / "seed"
        self.git_init(seed, commit=False)
        (seed / "index.md").write_text("# Index\n")
        self.git("-C", seed, "add", "-A")
        self.git("-C", seed, "commit", "-q", "-m", "seed")
        self.git("-C", seed, "remote", "add", "origin", str(self.origin))
        self.git("-C", seed, "push", "-q", "-u", "origin", "main")
        self.vault = self.tmp / "vault"
        self.other = self.tmp / "other"
        self.git("clone", "-q", str(self.origin), str(self.vault))
        self.git("clone", "-q", str(self.origin), str(self.other))
        self.config(vault_path=str(self.vault))

    def git(self, *args):
        p = self.run_cmd(["git", *args])
        self.assertEqual(p.returncode, 0, "git %s: %s" % (args, p.stderr))
        return p.stdout.strip()

    def commit(self, repo, name, text, msg):
        (repo / name).write_text(text)
        self.git("-C", repo, "add", "-A")
        self.git("-C", repo, "commit", "-q", "-m", msg)

    def head(self, repo):
        return self.git("-C", repo, "rev-parse", "HEAD")

    def sync(self, *args):
        return self.py(SYNC, *args, "--vault", self.vault)


class Pull(SyncBase):
    def test_fast_forward_reports_what_changed(self):
        self.commit(self.other, "Knowledge.md", "new fact\n", "add the new fact")
        self.git("-C", self.other, "push", "-q")
        p = self.sync("pull", "--json")
        self.assertOk(p)
        d = json.loads(p.stdout)
        self.assertEqual(d["result"], "pulled")
        self.assertEqual(d["newest"], "add the new fact")
        self.assertEqual(d["files_changed"], 1)
        self.assertEqual(self.head(self.vault), self.head(self.other))
        again = self.sync("pull")
        self.assertOk(again)
        self.assertIn("already up to date", again.stdout)

    def test_diverged_stops_without_merging(self):
        self.commit(self.other, "a.md", "remote\n", "remote side")
        self.git("-C", self.other, "push", "-q")
        self.commit(self.vault, "b.md", "local\n", "local side")
        before = self.head(self.vault)
        p = self.sync("pull")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("DIVERGED", p.stdout)
        self.assertIn("never merges or rebases", p.stdout)
        self.assertEqual(self.head(self.vault), before, "pull moved HEAD on a diverged vault")
        parents = self.git("-C", self.vault, "log", "-1", "--format=%P").split()
        self.assertEqual(len(parents), 1, "a merge commit was made")
        self.assertFalse((self.vault / "a.md").exists())

    def test_dry_run_does_not_fetch_or_pull(self):
        self.commit(self.other, "a.md", "remote\n", "remote side")
        self.git("-C", self.other, "push", "-q")
        before = self.git("-C", self.vault, "rev-parse", "origin/main")
        p = self.sync("pull", "--dry-run")
        self.assertOk(p)
        self.assertIn("already up to date", p.stdout)   # refs on disk know nothing yet
        self.assertIn("does not fetch", p.stdout)
        self.assertEqual(self.git("-C", self.vault, "rev-parse", "origin/main"), before)


class Push(SyncBase):
    def test_push_after_the_check_passes(self):
        self.commit(self.vault, "r.md", "finding\n", "session write-back")
        d = self.sync("push", "--dry-run")
        self.assertOk(d)
        self.assertIn("would run", d.stdout)
        self.assertNotEqual(self.git("-C", self.vault, "rev-parse", "origin/main"),
                            self.head(self.vault), "--dry-run pushed")
        p = self.sync("push", "--json")
        self.assertOk(p)
        out = json.loads(p.stdout)
        self.assertEqual(out["result"], "pushed")
        self.assertIn("AHEAD of", out["push_check"])
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "main"),
                         self.head(self.vault))

    def test_refused_when_the_push_check_fails(self):
        self.git("-C", self.vault, "checkout", "-q", "-b", "loose")
        self.commit(self.vault, "r.md", "x\n", "on a branch with no upstream")
        p = self.sync("push")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("REFUSED", p.stdout)
        self.assertIn("NO UPSTREAM", p.stdout, "the push check's own text is not shown")
        self.assertNotIn("loose", self.git("--git-dir", str(self.origin), "branch"))

    def test_refused_when_origin_is_ahead(self):
        self.commit(self.other, "a.md", "remote\n", "remote side")
        self.git("-C", self.other, "push", "-q")
        self.commit(self.vault, "b.md", "local\n", "local side")
        p = self.sync("push")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("pull first", p.stdout)

    def test_nothing_to_push(self):
        p = self.sync("push")
        self.assertOk(p)
        self.assertIn("nothing to push", p.stdout)


class Status(SyncBase):
    def test_ahead_behind_and_dirty(self):
        self.commit(self.other, "a.md", "remote\n", "remote side")
        self.git("-C", self.other, "push", "-q")
        self.commit(self.vault, "b.md", "local\n", "local side")
        (self.vault / "c.md").write_text("uncommitted\n")
        p = self.sync("status", "--json")
        self.assertOk(p)
        d = json.loads(p.stdout)
        self.assertEqual((d["ahead"], d["behind"], d["dirty"]), (1, 1, 1))
        self.assertTrue(d["fetched"])
        h = self.sync("status")
        self.assertIn("1 ahead, 1 behind; 1 uncommitted", h.stdout)
        self.assertIn("DIVERGED", h.stdout)

    def test_not_a_repo(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        p = self.py(SYNC, "status", "--vault", plain)
        self.assertEqual(p.returncode, 2)


class SessionStartLine(SyncBase):
    def remote_moves(self):
        self.commit(self.other, "a.md", "remote\n", "remote side")
        self.git("-C", self.other, "push", "-q")

    def test_off_by_default_and_silent(self):
        self.remote_moves()
        p = self.py(PUSH, "check", str(self.vault))
        self.assertOk(p)
        self.assertNotIn("GOLDEN THREAD sync", p.stdout)
        self.assertIn("GOLDEN THREAD push", p.stdout)

    def test_cached_does_not_fetch(self):
        self.remote_moves()
        self.config(vault_path=str(self.vault), sync_check="cached")
        p = self.py(PUSH, "check", str(self.vault))
        self.assertOk(p)
        self.assertIn("GOLDEN THREAD sync: vault not behind", p.stdout)
        # now let the refs on disk learn about it
        self.git("-C", self.vault, "fetch", "-q")
        p = self.py(PUSH, "check", str(self.vault))
        self.assertIn("1 commit(s) BEHIND origin/main", p.stdout)

    def test_fetch_mode_sees_the_remote_commit_in_one_hook_object(self):
        self.remote_moves()
        self.config(vault_path=str(self.vault), sync_check="fetch")
        p = self.py(PUSH, "check", str(self.vault), "--hook")
        self.assertOk(p)
        objs = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
        self.assertEqual(len(objs), 1, "two hook JSON objects on one stdout: %r" % p.stdout)
        msg = objs[0]["systemMessage"]
        self.assertIn("GOLDEN THREAD push", msg)
        self.assertIn("BEHIND origin/main (fetched just now)", msg)

    def test_fetch_failure_is_bounded_and_said(self):
        self.git("-C", self.vault, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        self.config(vault_path=str(self.vault), sync_check="fetch")
        p = self.py(SYNC, "behind", "--vault", self.vault)
        self.assertOk(p)
        self.assertIn("fetch failed", p.stdout)


class Contract(unittest.TestCase):
    def test_every_git_call_names_the_vault(self):
        src = SYNC.read_text(encoding="utf-8")
        calls = re.findall(r'\["git",[^\]]*\]', src)
        self.assertTrue(calls)
        for c in calls:
            self.assertIn('"-C"', c, c)
        self.assertNotRegex(src, r'git\(\s*vault\s*,\s*"(merge|rebase)"')
        self.assertNotIn('"config"', src)

    def test_the_skill(self):
        text = (GT / "skills" / "gt-sync" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: gt-sync", text)
        for verb in ("status", "pull", "push"):
            self.assertIn("gt_sync.py %s" % verb, text)
        self.assertIn("--vault", text)

    def test_it_installs_beside_the_push_check(self):
        import sys
        sys.path.insert(0, str(SCRIPTS))
        from _harness import load_module
        comp = load_module(SCRIPTS / "gt_components.py", "gt_components_sync_test")
        self.assertIn("gt_sync.py", comp.HOOK_DIR_SCRIPTS)


if __name__ == "__main__":
    unittest.main()
