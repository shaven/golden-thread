"""dev/publish.sh -- the announcement check.

Contract for `check_announced`:
  * owner/repo come from `git remote get-url origin`, never a hard-coded account;
  * the version is matched in Discussion titles AND bodies;
  * dots are literal and the match is bounded, so 0.12.1 is not announced by 0.12.10;
  * it is warn-only: every outcome returns 0.

`gh` is a fake on PATH that records its arguments and prints canned JSON, so nothing
touches the network. publish.sh is SOURCED, which defines its steps and runs none.
"""
import json
import shutil
import unittest

from _harness import Sandbox, REPO

PUBLISH = REPO / "dev" / "publish.sh"


class CheckAnnounced(Sandbox):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git required")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.repo = self.tmp / "clone"
        self.repo.mkdir()
        self.run_cmd(["git", "init", "-q", self.repo])
        self.set_origin("git@github.com:example-owner/example-repo.git")
        self.env["PATH"] = "%s:%s" % (self.bin, self.env.get("PATH", ""))

    def set_origin(self, url):
        self.run_cmd(["git", "-C", self.repo, "remote", "remove", "origin"])
        self.run_cmd(["git", "-C", self.repo, "remote", "add", "origin", url])

    def fake_gh(self, nodes):
        payload = json.dumps({"data": {"repository": {"discussions": {"nodes": nodes}}}})
        (self.tmp / "gh.json").write_text(payload)
        gh = self.bin / "gh"
        gh.write_text('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\ncat "%s"\n'
                      % (self.tmp / "gh.args", self.tmp / "gh.json"))
        gh.chmod(0o755)

    def announced(self, version):
        script = 'source "%s" && cd "%s" && check_announced "%s"' % (PUBLISH, self.repo, version)
        p = self.run_cmd(["bash", "-c", script])
        self.assertEqual(p.returncode, 0, "check_announced is warn-only: %s" % (p.stdout + p.stderr))
        return p.stdout

    def test_version_in_a_body_counts(self):
        self.fake_gh([{"title": "Release notes", "body": "This post covers gt v0.13.0.\nEnjoy."}])
        out = self.announced("0.13.0")
        self.assertIn("a Discussion names 0.13.0", out)
        self.assertNotIn("WARNING", out)

    def test_version_in_a_title_counts(self):
        self.fake_gh([{"title": "gt 0.13.0 is out", "body": ""}])
        self.assertIn("a Discussion names 0.13.0", self.announced("0.13.0"))

    def test_0_12_1_is_not_announced_by_0_12_10(self):
        self.fake_gh([{"title": "gt 0.12.10 released", "body": "Upgrading from 0.12.1x? Read 0.12.10 notes."}])
        self.assertIn("WARNING", self.announced("0.12.1"))
        self.assertIn("a Discussion names 0.12.10", self.announced("0.12.10"))

    def test_dots_are_literal(self):
        self.fake_gh([{"title": "gt 0x12y1 build", "body": "0-12-1"}])
        self.assertIn("WARNING", self.announced("0.12.1"))

    def test_owner_and_repo_come_from_origin(self):
        self.fake_gh([])
        self.set_origin("https://github.com/another-owner/another-repo.git")
        out = self.announced("0.13.0")
        self.assertIn("another-owner/another-repo", out)
        args = (self.tmp / "gh.args").read_text()
        self.assertIn("owner=another-owner", args)
        self.assertIn("name=another-repo", args)

    def test_non_github_origin_is_a_warning_not_a_failure(self):
        self.fake_gh([])
        self.set_origin("https://example.invalid/some/repo.git")
        self.assertIn("not a GitHub remote", self.announced("0.13.0"))

    def test_no_account_is_hard_coded(self):
        text = PUBLISH.read_text()
        self.assertNotIn('owner:"', text, "the GitHub owner must be derived from origin")


if __name__ == "__main__":
    unittest.main()
