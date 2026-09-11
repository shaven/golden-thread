"""gt-wiki wiki_refresh.py: report-only upstream change detection for Sources/.

Fixture: an upstream git repo `up` (the "remote"), a clone `clone` whose origin is
`up`, and a vault whose Sources/ point into the clone with `local:`. All local --
`git fetch` talks to a directory, never the network.

Contracts pinned here:
  * baseline = `upstream_sha:` if present, else the last commit on or before
    `ingested:`; changed = diff base..origin/<default> on the path is non-empty;
  * the fetch happens unless --no-fetch; the working tree and vault are untouched;
  * `url:`-only sources are listed as fetch-needed, never fetched;
  * superseded sources are skipped; scope by --page/--repo/--topic/--all;
  * exit 0 nothing changed, 1 changed, 2 usage/error.
"""
import hashlib
import json
import os
import unittest
from pathlib import Path

from _harness import Sandbox, WIKI_SCRIPTS

REFRESH = WIKI_SCRIPTS / "wiki_refresh.py"


class WikiRefreshTest(Sandbox):
    def setUp(self):
        super().setUp()
        # Real path: macOS temp dirs sit behind /var -> /private/var, and git reports
        # the resolved toplevel. The symlinked case has its own test below.
        self.root = Path(os.path.realpath(self.tmp))
        self.up = self.git_init(self.root / "up", commit=False)
        self.commit("docs/a.md", "line one\n", "2026-01-01T12:00:00")
        self.sha_a = self.git(self.up, "rev-parse", "HEAD")  # last commit touching docs/a.md
        self.commit("docs/b.md", "bee\n", "2026-01-01T12:00:00")
        self.sha1 = self.git(self.up, "rev-parse", "HEAD")
        self.clone = self.root / "clone"
        self.ok(self.run_cmd(["git", "clone", "-q", self.up, self.clone]))
        self.vault = self.root / "vault"
        (self.vault / "Sources").mkdir(parents=True)
        (self.vault / "Knowledge").mkdir()

    # -- helpers ------------------------------------------------------------------
    def ok(self, p):
        self.assertOk(p)
        return p.stdout.strip()

    def git(self, repo, *args, env=None):
        return self.ok(self.run_cmd(["git", "-C", repo, *args], env=env))

    def commit(self, rel, content, date, repo=None):
        repo = repo or self.up
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        self.git(repo, "add", "-A", env=env)
        self.git(repo, "commit", "-q", "-m", f"edit {rel}", env=env)

    def source(self, name, **fm):
        lines = "".join(f"{k}: {v}\n" for k, v in fm.items())
        (self.vault / "Sources" / f"{name}.md").write_text(f"---\n{lines}---\nbody\n", encoding="utf-8")

    def kpage(self, title, *sources):
        src = "".join(f'  - "[[Sources/{s}.md]]"\n' for s in sources)
        (self.vault / "Knowledge" / f"{title}.md").write_text(
            f"---\ntitle: {title}\nsources:\n{src}status: growing\n---\nbody\n", encoding="utf-8")

    def refresh(self, *args):
        return self.py(REFRESH, self.vault, *args)

    def report(self, *args):
        p = self.refresh("--json", *args)
        self.assertIn(p.returncode, (0, 1), p.stdout + p.stderr)
        return p.returncode, {r["source"]: r for r in json.loads(p.stdout)["results"]}

    def local(self, rel="docs/a.md"):
        return str(self.clone / rel)

    def tree_hash(self, root):
        h = hashlib.sha256()
        for f in sorted(root.rglob("*")):
            if f.is_file() and ".git" not in f.relative_to(root).parts:
                h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes())
        return h.hexdigest()

    # -- baseline from upstream_sha -----------------------------------------------
    def test_unchanged_when_upstream_has_not_moved(self):
        self.source("src-a", local=self.local(), upstream_sha=self.sha1)
        rc, r = self.report("--all")
        self.assertEqual(rc, 0)
        self.assertEqual(r["src-a.md"]["state"], "unchanged")
        self.assertEqual(r["src-a.md"]["base"], self.sha1[:12])

    def test_upstream_change_is_fetched_and_reported_with_diff(self):
        self.source("src-a", local=self.local(), upstream_sha=self.sha1)
        self.kpage("Uses A", "src-a")
        self.commit("docs/a.md", "line one\nline two added upstream\n", "2026-03-01T12:00:00")
        head = self.git(self.up, "rev-parse", "HEAD")
        rc, r = self.report("--all")
        self.assertEqual(rc, 1)
        a = r["src-a.md"]
        self.assertEqual(a["state"], "changed")
        self.assertEqual((a["base"], a["head"]), (self.sha1[:12], head[:12]))
        self.assertEqual(a["branch"], "main")
        self.assertIn("+line two added upstream", a["diff"])
        self.assertEqual(a["cited_by"], ["Uses A.md"])
        p = self.refresh("--all")  # text report
        self.assertEqual(p.returncode, 1)
        self.assertIn("[changed] src-a.md", p.stdout)
        self.assertIn("cited by: Uses A.md", p.stdout)

    def test_change_to_a_different_path_is_not_a_change(self):
        self.source("src-a", local=self.local(), upstream_sha=self.sha1)
        self.commit("docs/b.md", "bee changed\n", "2026-03-01T12:00:00")
        rc, r = self.report("--all")
        self.assertEqual((rc, r["src-a.md"]["state"]), (0, "unchanged"))

    def test_directory_source_sees_changes_beneath_it(self):
        self.source("src-docs", local=self.local("docs"), upstream_sha=self.sha1)
        self.commit("docs/b.md", "bee changed\n", "2026-03-01T12:00:00")
        rc, r = self.report("--all")
        self.assertEqual((rc, r["src-docs.md"]["state"]), (1, "changed"))

    def test_no_fetch_uses_the_stale_remote_ref(self):
        self.source("src-a", local=self.local(), upstream_sha=self.sha1)
        self.commit("docs/a.md", "moved on\n", "2026-03-01T12:00:00")
        rc, r = self.report("--all", "--no-fetch")
        self.assertEqual((rc, r["src-a.md"]["state"]), (0, "unchanged"),
                         "--no-fetch still fetched")

    def test_report_only_vault_and_working_tree_untouched(self):
        self.source("src-a", local=self.local(), upstream_sha=self.sha1)
        self.commit("docs/a.md", "moved on\n", "2026-03-01T12:00:00")
        vault_before, clone_before = self.tree_hash(self.vault), self.tree_hash(self.clone)
        local_head = self.git(self.clone, "rev-parse", "HEAD")
        self.report("--all")
        self.assertEqual(self.tree_hash(self.vault), vault_before, "vault modified")
        self.assertEqual(self.tree_hash(self.clone), clone_before, "working tree modified")
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), local_head, "local branch moved")
        self.assertEqual(self.git(self.clone, "status", "--porcelain"), "")

    def test_diff_is_truncated_at_max_lines(self):
        self.source("src-a", local=self.local(), upstream_sha=self.sha1)
        self.commit("docs/a.md", "".join(f"l{i}\n" for i in range(100)), "2026-03-01T12:00:00")
        _, r = self.report("--all", "--max-diff-lines", "5")
        self.assertTrue(r["src-a.md"]["diff_truncated"])
        self.assertEqual(len(r["src-a.md"]["diff"].splitlines()), 5)

    # -- baseline from ingested date ----------------------------------------------
    def test_ingested_date_picks_the_last_commit_on_or_before_it(self):
        self.commit("docs/a.md", "line one\nlater\n", "2026-03-01T12:00:00")
        second = self.git(self.up, "rev-parse", "HEAD")
        self.source("before", local=self.local(), ingested="2026-02-01")
        self.source("same-day", local=self.local(), ingested="2026-03-01")
        self.source("new-file", local=self.local(), ingested="2025-12-31")
        rc, r = self.report("--all")
        self.assertEqual(rc, 1)
        self.assertEqual((r["before.md"]["state"], r["before.md"]["base"]), ("changed", self.sha_a[:12]))
        self.assertEqual((r["same-day.md"]["state"], r["same-day.md"]["base"]),
                         ("unchanged", second[:12]), "commit ON the ingested date not used as baseline")
        self.assertEqual(r["new-file.md"]["state"], "no-baseline")

    # -- routes and scoping --------------------------------------------------------
    def test_url_only_sources_are_listed_not_fetched(self):
        self.source("web", url="https://example.invalid/doc", ingested="2026-01-01")
        self.source("bare", title="no locator")
        rc, r = self.report("--all")
        self.assertEqual(rc, 0)
        self.assertEqual((r["web.md"]["route"], r["web.md"]["state"]), ("web", "fetch-needed"))
        self.assertEqual(r["web.md"]["url"], "https://example.invalid/doc")
        self.assertEqual(r["bare.md"]["state"], "no-locator")

    def test_superseded_sources_are_skipped(self):
        self.source("old-a", local=self.local(), upstream_sha=self.sha1)
        (self.vault / "Sources" / "new-a.md").write_text(
            f"---\nlocal: {self.local()}\nupstream_sha: {self.sha1}\n"
            "supersedes:\n  - \"[[Sources/old-a.md]]\"\n---\n", encoding="utf-8")
        self.commit("docs/a.md", "moved on\n", "2026-03-01T12:00:00")
        _, r = self.report("--all")
        self.assertNotIn("old-a.md", r, "superseded source was checked")
        self.assertIn("new-a.md", r)

    def test_scalar_supersedes_also_skips_the_old_source(self):
        # `supersedes: Sources/old-a.md` (one value, not a list) is how a single
        # supersession is naturally written, and wiki_lint accepts it.
        self.source("old-a", local=self.local(), upstream_sha=self.sha1)
        self.source("new-a", local=self.local(), upstream_sha=self.sha1, supersedes="Sources/old-a.md")
        _, r = self.report("--all")
        self.assertNotIn("old-a.md", r, "a scalar `supersedes:` value was iterated character by "
                                        "character, so the superseded source is still checked")

    def test_scope_page_repo_topic(self):
        other = self.git_init(self.root / "otherrepo-up", commit=False)
        self.commit("x.md", "x\n", "2026-01-01T12:00:00", repo=other)
        oclone = self.root / "otherrepo"
        self.ok(self.run_cmd(["git", "clone", "-q", other, oclone]))
        self.source("alpha-notes", local=self.local(), upstream_sha=self.sha1)
        self.source("beta-notes", local=self.local("docs/b.md"), upstream_sha=self.sha1)
        self.source("other-x", local=str(oclone / "x.md"), ingested="2026-02-01")
        self.kpage("Page B", "beta-notes")
        cases = {
            ("--page", "Page B"): {"beta-notes.md"},
            ("--repo", "otherrepo"): {"other-x.md"},
            ("--topic", "alpha"): {"alpha-notes.md"},
            ("--all",): {"alpha-notes.md", "beta-notes.md", "other-x.md"},
        }
        for args, want in cases.items():
            with self.subTest(args=args):
                _, r = self.report(*args)
                self.assertEqual(set(r), want)

    def test_missing_checkout_is_reported(self):
        self.source("gone", local=str(self.root / "no-such-repo" / "f.md"), upstream_sha=self.sha1)
        rc, r = self.report("--all")
        self.assertEqual((rc, r["gone.md"]["state"]), (0, "missing-on-disk"))

    # -- exit codes -----------------------------------------------------------------
    def test_usage_errors_exit_2(self):
        self.kpage("Real Page")
        for args in ((), ("--page", "No Such Page")):
            with self.subTest(args=args):
                p = self.refresh(*args)
                self.assertEqual(p.returncode, 2,
                                 f"usage error exited {p.returncode}, which the documented contract "
                                 "reserves for 'changes found' (sys.exit(<message>) exits 1)")
        self.assertEqual(self.refresh("--all", "--page", "x").returncode, 2)  # argparse

    def test_a_source_that_could_not_be_checked_is_not_a_clean_exit(self):
        norigin = self.git_init(self.root / "no-origin")  # a repo with no remote at all
        (norigin / "f.md").write_text("f\n")
        self.source("broken", local=str(norigin / "f.md"), upstream_sha=self.sha1)
        p = self.refresh("--all", "--json")
        r = json.loads(p.stdout)["results"][0]
        self.assertEqual(r["state"], "error")
        self.assertEqual(p.returncode, 2, "a source whose check FAILED exits 0 = 'nothing changed'")

    def test_local_path_through_a_symlink(self):
        link = self.root / "link"
        link.symlink_to(self.clone, target_is_directory=True)
        self.source("via-link", local=str(link / "docs" / "a.md"), upstream_sha=self.sha1)
        self.commit("docs/a.md", "moved on\n", "2026-03-01T12:00:00")
        rc, r = self.report("--all")
        self.assertEqual(r["via-link.md"]["state"], "changed",
                         "a `local:` path reached through a symlink was not diffed: "
                         + json.dumps(r["via-link.md"]))


if __name__ == "__main__":
    unittest.main()
