"""gt_demo.sh: start / end / clean [--dry-run] / remove, run from a fake INSTALLED
plugin copy under the sandbox HOME against a sandbox git vault with a bare upstream.

The safety contract under test:
  * clean never resets past commits that are already on a remote
  * clean never destroys uncommitted work outside Projects/demo-pizzabot and the
    demo source
  * remove commits ONLY the demo's own paths and records install_demo=no
"""
import json
import shutil
import unittest

from _harness import Sandbox, GT

DEMO_REL = "Projects/demo-pizzabot"


class DemoBase(Sandbox):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git not installed")
        # fake installed plugin
        self.install = (self.home / ".claude" / "plugins" / "cache" / "golden-thread-plugin"
                        / "gt" / GT.name)
        for sub in ("scripts", "templates", "skills"):
            shutil.copytree(GT / sub, self.install / sub)
        self.script = self.install / "scripts" / "gt_demo.sh"
        src = sorted((GT / "templates" / "demo-pizzabot" / "sources").glob("*.md"))
        self.source_rel = f"Sources/{src[0].name}"
        self.template_readme = (GT / "templates/demo-pizzabot/project/README.md").read_text()
        # vault with history and a bare upstream
        self.v = self.tmp / "vault"
        self.v.mkdir()
        (self.v / "notes.md").write_text("user notes\n")
        (self.v / "Projects").mkdir()
        (self.v / "Projects" / "README.md").write_text("# Projects\n")
        (self.v / "Sources").mkdir()
        (self.v / "Sources" / "keep.md").write_text("keep\n")
        self.git_init(self.v)
        (self.v / "notes.md").write_text("user notes v2\n")
        self.git("commit", "-qam", "second")
        self.upstream = self.tmp / "upstream.git"
        self.run_cmd(["git", "init", "-q", "--bare", self.upstream])
        self.git("remote", "add", "origin", str(self.upstream))
        self.assertOk(self.git("push", "-q", "-u", "origin", "main"))
        self.config(vault_path=str(self.v))
        self.snapshot_file = self.home / ".claude" / "gt-demo-snapshot"

    # -- helpers ---------------------------------------------------------------
    def git(self, *args):
        return self.run_cmd(["git", "-C", self.v, *args])

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def demo(self, *args, script=None):
        return self.sh(script or self.script, *args, cwd=self.tmp)

    def start(self):
        proc = self.demo("start")
        self.assertOk(proc, "demo start")
        return proc

    def commit_demo(self, msg="demo work"):
        self.assertOk(self.git("add", "--", DEMO_REL, self.source_rel))
        self.assertOk(self.git("commit", "-qm", msg))

    def status(self):
        return self.git("status", "--porcelain").stdout


class StartEndTest(DemoBase):
    def test_start_snapshots_and_installs(self):
        sha = self.head()
        proc = self.start()
        self.assertIn("status=ready", proc.stdout)
        self.assertEqual(self.snapshot_file.read_text().strip(), sha)
        self.assertEqual((self.v / DEMO_REL / "README.md").read_text(), self.template_readme)
        self.assertTrue((self.v / self.source_rel).is_file())
        self.assertEqual(self.head(), sha, "start must not commit")
        again = self.demo("start")
        self.assertEqual(again.returncode, 1)
        self.assertIn("already started", again.stdout)

    def test_end_reports_commits_and_uncommitted(self):
        self.start()
        self.commit_demo("pizza commit")
        (self.v / "notes.md").write_text("edited\n")
        proc = self.demo("end")
        self.assertOk(proc)
        self.assertIn("pizza commit", proc.stdout)
        self.assertIn(f"{DEMO_REL}/README.md", proc.stdout)
        self.assertIn("Uncommitted changes:", proc.stdout)
        self.assertIn("notes.md", proc.stdout)

    def test_end_and_clean_without_start(self):
        for cmd in ("end", "clean"):
            proc = self.demo(cmd)
            self.assertEqual(proc.returncode, 1, cmd)

    def test_usage_and_preconditions(self):
        self.assertEqual(self.demo("bogus").returncode, 1)
        (self.home / ".claude" / "vault-config.json").unlink()
        proc = self.demo("start")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("could not read a vault_path", proc.stdout)
        plain = self.tmp / "not-git"
        plain.mkdir()
        self.config(vault_path=str(plain))
        proc = self.demo("start")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not a git repo", proc.stdout)
        self.assertFalse(self.snapshot_file.exists())


class CleanTest(DemoBase):
    def test_dry_run_changes_nothing(self):
        sha = self.head()
        self.start()
        self.commit_demo("pizza commit")
        after = self.head()
        (self.v / DEMO_REL / "README.md").write_text("scribbled\n")
        proc = self.demo("clean", "--dry-run")
        self.assertOk(proc)
        self.assertIn("dry run", proc.stdout)
        self.assertIn("pizza commit", proc.stdout)
        self.assertIn("status=clean-possible", proc.stdout)
        self.assertEqual(self.head(), after)
        self.assertEqual(self.snapshot_file.read_text().strip(), sha)
        self.assertEqual((self.v / DEMO_REL / "README.md").read_text(), "scribbled\n")

    def test_clean_resets_unpushed_demo_commits_and_rearms(self):
        sha = self.head()
        self.start()
        self.commit_demo()
        (self.v / DEMO_REL / "README.md").write_text("demo edited this\n")
        self.git("commit", "-qam", "more demo")
        (self.v / "scratch.txt").write_text("untracked user file\n")
        proc = self.demo("clean")
        self.assertOk(proc)
        self.assertIn("status=clean", proc.stdout)
        self.assertEqual(self.head(), sha)
        self.assertEqual((self.v / DEMO_REL / "README.md").read_text(), self.template_readme)
        self.assertFalse(self.snapshot_file.exists())
        self.assertEqual((self.v / "scratch.txt").read_text(), "untracked user file\n")
        self.assertEqual((self.v / "notes.md").read_text(), "user notes v2\n")

    def test_clean_with_no_commits_just_reinstalls(self):
        sha = self.head()
        self.start()
        (self.v / DEMO_REL / "README.md").write_text("scribbled\n")
        proc = self.demo("clean")
        self.assertOk(proc)
        self.assertIn("No commits to undo", proc.stdout)
        self.assertEqual(self.head(), sha)
        self.assertEqual((self.v / DEMO_REL / "README.md").read_text(), self.template_readme)

    def test_clean_refuses_to_reset_past_pushed_commits(self):
        self.start()
        self.commit_demo("pushed demo commit")
        self.assertOk(self.git("push", "-q"))
        pushed = self.head()
        proc = self.demo("clean")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("REFUSED", proc.stdout)
        self.assertIn("already on the remote", proc.stdout)
        self.assertEqual(self.head(), pushed)
        self.assertTrue(self.snapshot_file.exists(), "snapshot must survive a refusal")
        dry = self.demo("clean", "--dry-run")
        self.assertEqual(dry.returncode, 2, "dry run must report the same refusal")

    @unittest.expectedFailure  # defect: 2026-09-11-demo-clean-guard-holes
    def test_clean_refuses_commits_pushed_to_a_remote_without_upstream_tracking(self):
        """The pushed-commit guard only consults @{upstream}. A commit pushed with a
        plain `git push origin main` (no -u) is on the remote all the same, but with
        no upstream configured the guard is skipped and the reset goes through."""
        self.git("branch", "--unset-upstream")
        self.start()
        self.commit_demo("pushed demo commit")
        self.assertOk(self.git("push", "-q", "origin", "main"))
        pushed = self.head()
        proc = self.demo("clean")
        self.assertEqual(self.head(), pushed,
                         "clean reset past a commit that is on origin/main:\n" + proc.stdout)
        self.assertEqual(proc.returncode, 2)

    def test_clean_refuses_foreign_uncommitted_changes(self):
        self.start()
        self.commit_demo()
        after = self.head()
        (self.v / "notes.md").write_text("unsaved user work\n")
        (self.v / "Sources" / "new.md").write_text("staged user work\n")
        self.git("add", "Sources/new.md")
        proc = self.demo("clean")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("uncommitted changes outside the demo", proc.stdout)
        self.assertIn("notes.md", proc.stdout)
        self.assertIn("Sources/new.md", proc.stdout)
        self.assertEqual(self.head(), after)
        self.assertEqual((self.v / "notes.md").read_text(), "unsaved user work\n")
        self.assertEqual((self.v / "Sources" / "new.md").read_text(), "staged user work\n")
        self.assertTrue(self.snapshot_file.exists())

    def test_clean_allows_uncommitted_changes_inside_demo_project(self):
        sha = self.head()
        self.start()
        self.commit_demo()
        (self.v / DEMO_REL / "README.md").write_text("mid-demo edit\n")
        proc = self.demo("clean")
        self.assertOk(proc)
        self.assertEqual(self.head(), sha)

    @unittest.expectedFailure  # defect: 2026-09-11-demo-clean-guard-holes
    def test_clean_allows_uncommitted_change_to_demo_source(self):
        """The demo source is one of the demo's own paths. Its name contains spaces,
        so `git status --porcelain` prints it quoted and the exact comparison in
        foreign_changes() never matches: the demo's own file is treated as foreign
        and clean refuses."""
        sha = self.head()
        self.start()
        self.commit_demo()
        with open(self.v / self.source_rel, "a") as fh:
            fh.write("\nannotated during the demo\n")
        proc = self.demo("clean")
        self.assertEqual(proc.returncode, 0,
                         "clean refused over the demo's own source file:\n" + proc.stdout)
        self.assertEqual(self.head(), sha)

    def test_clean_refuses_when_history_was_rewritten(self):
        self.start()
        self.commit_demo()
        # rewrite: move HEAD to a commit that does not descend from the snapshot
        self.git("reset", "-q", "--hard", "HEAD~2")
        (self.v / "other.md").write_text("x\n")
        self.git("add", "other.md")
        self.git("commit", "-qm", "diverged")
        diverged = self.head()
        proc = self.demo("clean")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("not a descendant of the snapshot", proc.stdout)
        self.assertEqual(self.head(), diverged)

    @unittest.expectedFailure  # defect: 2026-09-11-demo-clean-guard-holes
    def test_clean_never_overwrites_an_untracked_user_file(self):
        """foreign_changes() skips untracked files on the claim that 'untracked files
        survive a reset'. They do not when the snapshot tracks the same path: here a
        commit during the demo deleted notes.md, the user then wrote a new untracked
        notes.md, and `git reset --hard` silently replaces it with the old one."""
        self.start()
        self.git("rm", "-q", "notes.md")
        self.git("commit", "-qm", "drop notes during demo")
        (self.v / "notes.md").write_text("precious new user notes\n")
        proc = self.demo("clean")
        self.assertEqual((self.v / "notes.md").read_text(), "precious new user notes\n",
                         "clean destroyed an untracked user file outside the demo paths "
                         f"(exit {proc.returncode}):\n{proc.stdout}")


class RemoveTest(DemoBase):
    def installed_demo_bits(self, base):
        return [p for p in (base / "skills" / "gt-demo", base / "templates" / "demo-pizzabot",
                            base / "scripts" / "gt_demo.sh") if p.exists()]

    def test_remove_commits_only_demo_paths(self):
        self.start()
        self.commit_demo()
        before = self.head()
        (self.v / "notes.md").write_text("unsaved user work\n")
        (self.v / "Sources" / "staged.md").write_text("staged user work\n")
        self.git("add", "Sources/staged.md")
        proc = self.demo("remove")
        self.assertOk(proc)
        self.assertIn("status=removed", proc.stdout)
        self.assertNotEqual(self.head(), before, "removal was not committed")
        files = self.git("show", "--name-only", "--format=", "HEAD").stdout.split("\n")
        files = [f for f in files if f]
        self.assertTrue(files)
        stray = [f for f in files if not (f.startswith(DEMO_REL + "/") or f == self.source_rel)]
        self.assertEqual(stray, [], "remove committed paths that are not the demo's")
        self.assertIn(self.source_rel, files)
        # the user's work is untouched and still uncommitted / still staged
        self.assertEqual((self.v / "notes.md").read_text(), "unsaved user work\n")
        st = self.status()
        self.assertIn(" M notes.md", st)
        self.assertIn("A  Sources/staged.md", st)
        # demo gone from the vault
        self.assertFalse((self.v / DEMO_REL).exists())
        self.assertFalse((self.v / self.source_rel).exists())
        self.assertFalse(self.snapshot_file.exists())
        # recorded choice, and stripped from the installed copy
        cfg = json.loads((self.home / ".claude" / "vault-config.json").read_text())
        self.assertEqual(cfg.get("install_demo"), "no")
        self.assertEqual(cfg.get("vault_path"), str(self.v))
        self.assertEqual(self.installed_demo_bits(self.install), [])
        self.assertTrue((self.install / "scripts" / "vault_init.py").is_file(),
                        "remove deleted more than the demo from the install")

    def test_remove_with_untracked_demo_makes_no_commit(self):
        self.start()
        before = self.head()
        proc = self.demo("remove")
        self.assertOk(proc)
        self.assertEqual(self.head(), before)
        self.assertFalse((self.v / DEMO_REL).exists())
        self.assertEqual(self.status(), "")

    def test_remove_from_a_source_checkout_keeps_the_checkout(self):
        checkout = self.tmp / "checkout" / GT.name       # outside HOME/.claude
        for sub in ("scripts", "templates", "skills"):
            shutil.copytree(GT / sub, checkout / sub)
        self.start()
        proc = self.demo("remove", script=checkout / "scripts" / "gt_demo.sh")
        self.assertOk(proc)
        self.assertEqual(len(self.installed_demo_bits(checkout)), 3,
                         "remove stripped the demo out of a source checkout")


if __name__ == "__main__":
    unittest.main()
