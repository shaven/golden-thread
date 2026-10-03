"""dev/announce.py and publish.sh's `announced` step -- the release_announce setting.

Feature request 2026-09-30-publish-auto-announce-release. `gh` is a fake placed first on
PATH that records every call and prints canned JSON, so nothing reaches GitHub. The
CHANGELOG is a fixture with three releases.
"""
import json
import os
import shutil
import unittest

from _harness import skip_on_windows, WIN_FAKE_EXE, Sandbox, REPO, PYTHON, needs_dev

ANNOUNCE = REPO / "dev" / "announce.py"
PUBLISH = REPO / "dev" / "publish.sh"

CHANGELOG = """# Changelog

Intro prose that is not a release.

---

## gt 0.5.3 — 2026-03-03

Kestrel lands: the wombat index is rebuilt on demand.

### Kestrel rebuilds

Details about kestrel.

### Known, and not fixed

- nothing much

---

## gt 0.5.2 — 2026-02-02

**Marmot export.** The marmot export writes one file per burrow.

---

## gt 0.5.1 — 2026-01-01

First quokka release.

### Quokka basics

Some text.
"""

# Assembled at run time: a credential-shaped LITERAL here would trip the repo's own gate.
FAKE_KEY = "Zx9pQ2mL" + "7vT4rB8n" + "K3wY6sD1" + "fH5jA0cE"

FAKE_GH = r"""#!/bin/sh
printf 'CALL %%s\n' "$*" >> "%(log)s"
[ -f "%(fail)s" ] && exit 1
case "$*" in
  *createDiscussion*)
    [ -f "%(failcreate)s" ] && exit 1
    echo '{"data":{"createDiscussion":{"discussion":{"url":"https://github.com/wombat-owner/kestrel-repo/discussions/99"}}}}' ;;
  *) cat "%(json)s" ;;
esac
"""


@needs_dev
class Announce(Sandbox):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git required")
        self.repo = self.tmp / "clone"
        self.repo.mkdir()
        self.run_cmd(["git", "init", "-q", self.repo])
        self.run_cmd(["git", "-C", self.repo, "remote", "add", "origin",
                      "git@github.com:wombat-owner/kestrel-repo.git"])
        self.changelog = self.tmp / "CHANGELOG.md"
        self.changelog.write_text(CHANGELOG, encoding="utf-8")
        terms = self.tmp / "scrub-terms.txt"
        terms.write_text("# test terms\nquokkacorp\n", encoding="utf-8")
        self.env["GT_SCRUB_TERMS"] = str(terms)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "gh.log"
        self.json = self.tmp / "gh.json"
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH % {"log": self.log, "json": self.json,
                                 "fail": self.tmp / "gh.fail",
                                 "failcreate": self.tmp / "gh.failcreate"})
        gh.chmod(0o755)
        self.env["PATH"] = os.pathsep.join([str(self.bin), self.env.get("PATH", "")])
        self.discussions([])
        self.draft = self.home / ".claude" / "golden-thread" / "announce-0.5.3.md"

    # -- helpers ------------------------------------------------------------------------
    def discussions(self, nodes, category="Announcements"):
        self.json.write_text(json.dumps({"data": {"repository": {
            "id": "R_repo1",
            "discussionCategories": {"nodes": [{"id": "C_general", "name": "General"},
                                               {"id": "C_ann", "name": category}]},
            "discussions": {"nodes": nodes}}}}))

    def setting(self, value):
        (self.home / ".claude" / "vault-config.json").write_text(
            json.dumps({"release_announce": value}))

    def announce(self, *args):
        p = self.py(ANNOUNCE, "--changelog", self.changelog, "--version", "0.5.3", *args,
                    cwd=self.repo)
        self.assertEqual(p.returncode, 0, "announcing must never fail: %s" % (p.stdout + p.stderr))
        return p.stdout + p.stderr

    def calls(self):
        return self.log.read_text().count("CALL ") if self.log.exists() else 0

    def creates(self):
        if not self.log.exists():
            return []
        return [c for c in self.log.read_text().split("CALL ")[1:] if "createDiscussion" in c]

    # -- off ----------------------------------------------------------------------------
    def test_off_by_default_calls_nothing(self):
        out = self.announce()
        self.assertIn("off", out)
        self.assertEqual(self.calls(), 0)
        self.assertFalse(self.draft.exists())

    def test_an_unrecognised_setting_is_off(self):
        self.setting("loudly")
        self.announce()
        self.assertEqual(self.calls(), 0)

    # -- draft --------------------------------------------------------------------------
    @skip_on_windows(WIN_FAKE_EXE)
    def test_draft_writes_the_file_and_posts_nothing(self):
        self.setting("draft")
        out = self.announce()
        self.assertTrue(self.draft.is_file(), out)
        self.assertIn(str(self.draft), out)
        self.assertEqual(self.creates(), [])
        text = self.draft.read_text()
        for v in ("0.5.1", "0.5.2", "0.5.3"):
            self.assertIn(v, text)

    # -- post ---------------------------------------------------------------------------
    @skip_on_windows(WIN_FAKE_EXE)
    def test_post_creates_one_discussion_naming_every_version(self):
        self.setting("post")
        out = self.announce()
        (create,) = self.creates()
        for v in ("0.5.1", "0.5.2", "0.5.3"):
            self.assertIn(v, create)
        self.assertIn("title=gt 0.5.1 → 0.5.3 — ", create)
        self.assertIn("categoryId=C_ann", create)
        self.assertIn("repositoryId=R_repo1", create)
        self.assertIn("https://github.com/wombat-owner/kestrel-repo/releases/tag/v0.5.3", create)
        self.assertIn("CHANGELOG.md", create)
        self.assertIn("Kestrel rebuilds", create)
        self.assertNotIn("Known, and not fixed", create)
        self.assertIn("discussions/99", out)
        self.assertFalse(self.draft.exists())

    @skip_on_windows(WIN_FAKE_EXE)
    def test_owner_and_repo_come_from_origin(self):
        self.setting("post")
        self.announce()
        log = self.log.read_text()
        self.assertIn("owner=wombat-owner", log)
        self.assertIn("name=kestrel-repo", log)

    @skip_on_windows(WIN_FAKE_EXE)
    def test_an_announced_version_is_left_out(self):
        self.setting("post")
        self.discussions([{"title": "gt 0.5.1 is out", "body": ""}])
        self.announce()
        (create,) = self.creates()
        self.assertIn("0.5.2", create)
        self.assertIn("0.5.3", create)
        self.assertNotIn("0.5.1", create)
        self.assertIn("title=gt 0.5.2 → 0.5.3 — ", create)

    @skip_on_windows(WIN_FAKE_EXE)
    def test_a_single_release_title(self):
        self.setting("post")
        self.discussions([{"title": "Notes", "body": "covers v0.5.2"}])
        self.announce()
        (create,) = self.creates()
        self.assertIn("title=gt 0.5.3 — ", create)
        self.assertNotIn("0.5.2", create.split("body=")[0])

    @skip_on_windows(WIN_FAKE_EXE)
    def test_the_release_itself_already_announced_is_never_posted_twice(self):
        self.setting("post")
        self.discussions([{"title": "gt 0.5.3 — kestrel", "body": ""}])
        out = self.announce()
        self.assertIn("already names 0.5.3", out)
        self.assertEqual(self.creates(), [])
        self.assertFalse(self.draft.exists())

    @skip_on_windows(WIN_FAKE_EXE)
    def test_a_bounded_match_0_5_3_is_not_named_by_0_5_30(self):
        self.setting("post")
        self.discussions([{"title": "gt 0.5.30", "body": ""}])
        self.announce()
        self.assertEqual(len(self.creates()), 1)

    # -- scrub --------------------------------------------------------------------------
    def test_a_scrub_hit_posts_nothing_writes_the_draft_and_names_the_kind(self):
        self.setting("post")
        cases = {
            "scrub term": ("QuokkaCorp", "QuokkaCorp"),
            "IPv4 address": ("10.20.30.40", "10.20.30.40"),
            "home-folder path": ("/Users/marmot/burrow", "marmot"),
            "credential scan": ('api_key = "%s"' % FAKE_KEY, FAKE_KEY),
        }
        for kind, (snippet, value) in cases.items():
            with self.subTest(kind=kind):
                if self.log.exists():
                    self.log.unlink()
                if self.draft.exists():
                    self.draft.unlink()
                self.changelog.write_text(CHANGELOG.replace(
                    "Kestrel lands:", "Kestrel lands (%s):" % snippet), encoding="utf-8")
                out = self.announce()
                self.assertEqual(self.creates(), [], out)
                self.assertTrue(self.draft.is_file(), out)
                self.assertIn("NOT POSTED", out)
                self.assertIn(kind, out)
                self.assertNotIn("could not run", out)
                self.assertNotIn(value, out, "the matched text was printed")

    def test_missing_scrub_terms_is_not_a_pass(self):
        self.setting("post")
        self.env["GT_SCRUB_TERMS"] = str(self.tmp / "absent.txt")
        out = self.announce()
        self.assertEqual(self.creates(), [])
        self.assertIn("scrub terms unavailable", out)
        self.assertTrue(self.draft.is_file())

    # -- gh failing ---------------------------------------------------------------------
    def test_gh_failing_falls_back_to_draft(self):
        self.setting("post")
        (self.tmp / "gh.fail").write_text("")
        out = self.announce()
        self.assertEqual(self.creates(), [])
        self.assertIn("Falling back to draft", out)
        self.assertTrue(self.draft.is_file())

    @skip_on_windows(WIN_FAKE_EXE)
    def test_create_failing_falls_back_to_draft(self):
        self.setting("post")
        (self.tmp / "gh.failcreate").write_text("")
        out = self.announce()
        self.assertIn("could not create", out)
        self.assertTrue(self.draft.is_file())

    @skip_on_windows(WIN_FAKE_EXE)
    def test_no_announcements_category_falls_back_to_draft(self):
        self.setting("post")
        self.discussions([], category="Ideas")
        out = self.announce()
        self.assertEqual(self.creates(), [])
        self.assertIn("Announcements", out)
        self.assertTrue(self.draft.is_file())

    # -- dry run ------------------------------------------------------------------------
    @skip_on_windows(WIN_FAKE_EXE)
    def test_dry_run_prints_and_posts_nothing(self):
        self.setting("post")
        out = self.announce("--dry-run")
        self.assertIn("title: gt 0.5.1 → 0.5.3", out)
        self.assertIn("Install or upgrade", out)
        self.assertIn("nothing posted", out)
        self.assertEqual(self.creates(), [])
        self.assertFalse(self.draft.exists())

    def test_mode_flag_overrides_the_setting(self):
        self.setting("off")
        self.announce("--mode", "draft")
        self.assertTrue(self.draft.is_file())

    # -- publish.sh ---------------------------------------------------------------------
    def publish_step(self, dry):
        script = ('source "%s" && DRY=%s && cd "%s" && check_announced 0.5.3'
                  % (PUBLISH, dry, self.repo))
        p = self.run_cmd(["bash", "-c", script], env={"GT_CHANGELOG": str(self.changelog)})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    @skip_on_windows(WIN_FAKE_EXE)
    def test_publish_dry_run_prints_the_announcement_and_posts_nothing(self):
        self.setting("post")
        out = self.publish_step("yes")
        self.assertIn("title: gt 0.5.1 → 0.5.3", out)
        self.assertEqual(self.creates(), [])
        self.assertFalse(self.draft.exists())

    @skip_on_windows(WIN_FAKE_EXE)
    def test_publish_post_posts(self):
        self.setting("post")
        self.publish_step("no")
        self.assertEqual(len(self.creates()), 1)

    def test_publish_off_is_the_old_warning(self):
        self.setting("off")
        out = self.publish_step("no")
        self.assertIn("WARNING: no Discussion names 0.5.3", out)
        self.assertEqual(self.creates(), [])
        self.assertFalse(self.draft.exists())


if __name__ == "__main__":
    unittest.main()
