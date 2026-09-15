"""gt_watch.py: watch notes, rules-driven classification, the queue, the hook, ack, cron.

All offline: a sandbox bare repo stands in for the remote, `gh` and `crontab` are
stubs on PATH, HOME is throwaway, and the state dir is pinned with GT_WATCH_STATE.

0.15.0: gt_watch.py is the `watch` module's (plugin gt-watch, golden-thread-watch/<version>/),
no longer a gt core script. gt_settings.py still comes from the gt release under test,
pinned with GT_CORE_SCRIPTS. The module contract itself is tests/test_watch_module.py.
"""
import json
import os
import shutil
import stat
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, REPO, latest_version_dir

WATCH_MODULE = latest_version_dir(REPO / "golden-thread-watch")
WATCH = WATCH_MODULE / "scripts" / "gt_watch.py"
TEMPLATES = WATCH_MODULE / "templates"

GH_STUB = """#!/usr/bin/env bash
# canned gh: auth ok; releases and advisories from $GH_FIXTURES
case "$1" in
  auth) exit 0 ;;
  api)
    case "$2" in
      *releases*) cat "$GH_FIXTURES/releases.json" 2>/dev/null || echo '[]' ;;
      *security-advisories*) cat "$GH_FIXTURES/advisories.json" 2>/dev/null || echo '[]' ;;
      *) exit 1 ;;
    esac ;;
  *) exit 1 ;;
esac
"""

CRONTAB_STUB = """#!/usr/bin/env bash
# a crontab that keeps its table in $CRONTAB_FILE and logs every call
echo "$@" >> "$CRONTAB_FILE.calls"
if [ "$1" = "-l" ]; then
  [ -f "$CRONTAB_FILE" ] && cat "$CRONTAB_FILE" && exit 0
  echo "no crontab for $USER" >&2; exit 1
fi
if [ "$1" = "-" ]; then cat > "$CRONTAB_FILE"; exit 0; fi
exit 1
"""


class WatchTest(Sandbox):

    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("git not installed")
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "golden-thread").mkdir(parents=True)
        (self.vault / "INBOX.md").write_text("# Inbox\n", encoding="utf-8")
        self.state = self.tmp / "watch-state"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.fixtures = self.tmp / "gh"
        self.fixtures.mkdir()
        self.env.update({"GT_VAULT": str(self.vault), "GT_WATCH_STATE": str(self.state),
                         "GT_CORE_SCRIPTS": str(SCRIPTS),
                         "GT_WATCH": "report", "GH_FIXTURES": str(self.fixtures),
                         "CRONTAB_FILE": str(self.tmp / "crontab.txt"),
                         "PATH": "%s:%s" % (self.bin, os.environ.get("PATH", ""))})
        # Hide any real gh so the default is "gh unavailable"; tests that want it add the stub.
        self._stub("gh", "#!/usr/bin/env bash\nexit 1\n")
        self._stub("crontab", CRONTAB_STUB)
        self.work = self.tmp / "work"
        self.bare = self.tmp / "remote" / "widget-lib.git"
        self.bare.parent.mkdir()
        self.run_cmd(["git", "init", "-q", "--bare", "-b", "main", self.bare])
        self.git_init(self.work, commit=False)
        self.run_cmd(["git", "-C", self.work, "remote", "add", "origin", self.bare])
        self.commit("initial", {"README.md": "widget\n"})
        self.tag("v1.0.0")
        self.push()
        self.url = "file://%s" % self.bare

    # -- fixtures ------------------------------------------------------------
    def _stub(self, name, body):
        p = self.bin / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def commit(self, msg, files=None):
        files = files or {"f-%d.txt" % len(list(self.work.iterdir())): msg}
        for rel, content in files.items():
            p = self.work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        self.run_cmd(["git", "-C", self.work, "add", "-A"])
        self.assertOk(self.run_cmd(["git", "-C", self.work, "commit", "-q", "-m", msg]))

    def tag(self, name, message=None):
        args = ["git", "-C", self.work, "tag"]
        args += ["-a", name, "-m", message] if message else [name]
        self.assertOk(self.run_cmd(args))

    def push(self):
        self.assertOk(self.run_cmd(["git", "-C", self.work, "push", "-q", "origin", "main", "--tags"]))

    def watch(self, *args, **kw):
        return self.py(WATCH, *args, **kw)

    def add(self, *extra):
        p = self.watch("add", self.url, "--label", "Widget library", *extra)
        self.assertOk(p, "add failed")
        return p

    def events(self):
        f = self.state / "events.jsonl"
        if not f.exists():
            return []
        return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]

    def fetch(self):
        p = self.watch("fetch")
        self.assertOk(p, "fetch failed")
        return self.events()

    def only(self, evs, kind):
        return [e for e in evs if e["kind"] == kind]

    def hook_text(self, env=None):
        p = self.watch("--hook", env=env)
        self.assertOk(p)
        if not p.stdout.strip():
            return ""
        return json.loads(p.stdout)["systemMessage"]


class TestAdd(WatchTest):

    def test_add_writes_note_from_template_and_baselines(self):
        self.add("--watch-path", "src/auth/", "--current-version", "1.0.0")
        note = self.vault / "Projects" / "golden-thread" / "watches" / "widget-lib.md"
        self.assertTrue(note.exists())
        text = note.read_text()
        self.assertIn("url: %s" % self.url, text)
        self.assertIn("label: Widget library", text)
        self.assertIn("watch_paths: [src/auth/]", text)
        self.assertIn("current_version: 1.0.0", text)
        state = json.loads((self.state / "state.json").read_text())
        self.assertIn("v1.0.0", state["widget-lib"]["tags"])
        self.assertEqual(self.events(), [], "the baseline must not queue existing history")

    def test_add_rejects_unreachable_url(self):
        p = self.watch("add", "file:///nonexistent/repo.git")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Cannot reach", p.stdout)
        self.assertFalse((self.vault / "Projects" / "golden-thread" / "watches" / "repo.md").exists())

    def test_template_uses_neutral_example_url(self):
        t = (TEMPLATES / "watch.md").read_text()
        self.assertIn("https://github.com/example/widget-lib.git", t)

    def test_remove_and_list(self):
        self.add()
        self.assertIn("widget-lib", self.watch("list").stdout)
        self.assertOk(self.watch("remove", "widget-lib"))
        self.assertIn("No watches", self.watch("list").stdout)


class TestFetchAndRules(WatchTest):

    def test_new_commit_is_detected_and_routine(self):
        self.add()
        self.commit("Refactor widget layout")
        self.push()
        evs = self.fetch()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["kind"], "commit")
        self.assertEqual(evs[0]["severity"], "routine")

    def test_auth_and_security_words_are_not_p0(self):
        self.add()
        self.commit("Update author list and auth docs")
        self.commit("Improve security docs wording; secrets rotation docs; critical typo")
        self.push()
        evs = self.fetch()
        self.assertEqual([e["severity"] for e in evs], ["routine", "routine"], evs)

    def test_rule2_tag_with_cve_is_p0(self):
        self.add()
        self.commit("Patch parser")
        self.tag("v1.0.1", "Fixes CVE-2026-12345")
        self.push()
        tags = self.only(self.fetch(), "tag")
        self.assertEqual(tags[0]["severity"], "p0")
        self.assertIn("CVE-2026-12345", " ".join(tags[0]["reasons"]))

    def test_rule2_tag_with_ghsa_and_phrase_is_p0(self):
        self.add()
        self.commit("Patch")
        self.tag("v1.0.2", "GHSA-abcd-efgh-ijkl")
        self.commit("Patch again")
        self.tag("v1.0.3", "This is a security release")
        self.push()
        tags = {e["ref"]: e for e in self.only(self.fetch(), "tag")}
        self.assertEqual(tags["v1.0.2"]["severity"], "p0")
        self.assertEqual(tags["v1.0.3"]["severity"], "p0")

    def test_rule3_commit_with_cve_is_p0(self):
        self.add()
        self.commit("Backport fix for CVE-2025-99999")
        self.push()
        evs = self.fetch()
        self.assertEqual(evs[0]["severity"], "p0")

    def test_rule4_security_md_plus_phrase_is_p0_but_path_alone_is_review(self):
        # SECURITY.md is a watch_path in the template default, so alone it is review.
        self.add()
        self.commit("Document the reporting address", {"SECURITY.md": "mail us\n"})
        self.commit("Note the security fix process", {"SECURITY.md": "mail us, we fix\n"})
        self.push()
        evs = self.fetch()
        self.assertEqual([e["severity"] for e in evs], ["review", "p0"], evs)

    def test_rule4_watch_path_plus_phrase_is_p0(self):
        self.add("--watch-path", "src/auth/")
        self.commit("vulnerability in token check patched", {"src/auth/token.py": "x\n"})
        self.push()
        self.assertEqual(self.fetch()[0]["severity"], "p0")

    def test_rule5_p0_when_pattern(self):
        self.add("--p0-when", "token (leak|exposure)")
        self.commit("Close a token leak in the logger")
        self.push()
        e = self.fetch()[0]
        self.assertEqual(e["severity"], "p0")
        self.assertIn("p0_when", " ".join(e["reasons"]))

    def test_rule1_advisory_and_release_via_gh_stub(self):
        self._stub("gh", GH_STUB)
        (self.fixtures / "releases.json").write_text("[]")
        (self.fixtures / "advisories.json").write_text("[]")
        self.add("--gh-repo", "example/widget-lib")
        (self.fixtures / "releases.json").write_text(json.dumps(
            [{"tag_name": "v1.1.0", "name": "1.1.0", "body": "Minor features"},
             {"tag_name": "v1.1.1", "name": "1.1.1", "body": "Security update for the parser"}]))
        (self.fixtures / "advisories.json").write_text(json.dumps(
            [{"ghsa_id": "GHSA-aaaa-bbbb-cccc", "summary": "Parser overflow", "severity": "high"}]))
        evs = self.fetch()
        adv = self.only(evs, "advisory")
        self.assertEqual(len(adv), 1)
        self.assertEqual(adv[0]["severity"], "p0")
        rel = {e["ref"]: e["severity"] for e in self.only(evs, "release")}
        self.assertEqual(rel, {"v1.1.0": "review", "v1.1.1": "p0"})
        # seen once, never again
        self.assertEqual(len(self.fetch()), len(evs))

    def test_gh_unauthenticated_is_skipped_silently(self):
        self._stub("gh", "#!/usr/bin/env bash\n[ \"$1\" = auth ] && exit 1\nexit 1\n")
        self.add("--gh-repo", "example/widget-lib")
        p = self.watch("fetch")
        self.assertOk(p)
        self.assertNotIn("gh", p.stdout)

    def test_review_new_tag_major_bump_watch_path_breaking(self):
        self.add("--watch-path", "src/auth/", "--track", "tags,commits")
        self.commit("Rework login flow", {"src/auth/login.py": "x\n"})
        self.commit("feat!: new API\n\nBREAKING CHANGE: widgets renamed")
        self.tag("v1.1.0")
        self.commit("breaking: drop py2")
        self.tag("v2.0.0", "Two point oh")
        self.push()
        evs = self.fetch()
        commits = self.only(evs, "commit")
        self.assertEqual([e["severity"] for e in commits], ["review", "review", "review"], commits)
        tags = {e["ref"]: e for e in self.only(evs, "tag")}
        self.assertEqual(tags["v1.1.0"]["severity"], "review")
        self.assertIn("new tag", tags["v1.1.0"]["reasons"])
        self.assertIn("major version bump to v2.0.0", tags["v2.0.0"]["reasons"])

    def test_major_bump_relative_to_current_version(self):
        (self.tmp / "noop").write_text("")
        self.add("--current-version", "3.2.0")
        self.commit("x")
        self.tag("v4.0.0")
        self.push()
        t = self.only(self.fetch(), "tag")[0]
        self.assertIn("major version bump to v4.0.0", t["reasons"])

    def test_untracked_commits_are_not_queued_unless_p0(self):
        self.add("--track", "tags")
        self.commit("ordinary change")
        self.commit("fix CVE-2026-11111")
        self.push()
        evs = self.fetch()
        self.assertEqual([e["severity"] for e in evs], ["p0"])

    def test_unreachable_host_is_logged_and_skipped(self):
        self.add()
        wdir = self.vault / "Projects" / "golden-thread" / "watches"
        (wdir / "aaa-gone.md").write_text("---\nurl: file:///nonexistent/gone.git\n---\n")
        self.commit("after")
        self.push()
        p = self.watch("fetch")
        self.assertOk(p)
        self.assertEqual(len(self.events()), 1, "the reachable watch must still complete")
        log = (self.state / "fetch.log").read_text()
        self.assertIn("aaa-gone: unreachable", log)

    def test_fetch_does_nothing_when_off(self):
        self.add()
        self.commit("after")
        self.push()
        p = self.watch("fetch", env={"GT_WATCH": "off"})
        self.assertOk(p)
        self.assertEqual(self.events(), [])


class TestHookAndAck(WatchTest):

    def _mixed(self):
        self.add()
        self.commit("ordinary one")
        self.tag("v1.1.0")
        self.commit("security fix", {"x.txt": "y"})
        self.tag("v1.1.1", "Fixes CVE-2026-12345, a security fix")
        self.push()
        self.fetch()

    def test_hook_orders_p0_first_with_repo_and_reason(self):
        self._mixed()
        text = self.hook_text()
        self.assertIn("1 P0", text)
        lines = text.splitlines()
        p0 = [i for i, l in enumerate(lines) if l.strip().startswith("P0")]
        rv = [i for i, l in enumerate(lines) if l.strip().startswith("review")]
        self.assertTrue(p0 and rv and max(p0) < min(rv), text)
        self.assertIn("widget-lib", lines[p0[0]])
        self.assertIn("CVE-2026-12345", lines[p0[0]])
        self.assertIn("routine", text)

    def test_hook_silent_when_off(self):
        self._mixed()
        p = self.watch("--hook", env={"GT_WATCH": "off"})
        self.assertOk(p)
        self.assertEqual(p.stdout, "")

    def test_hook_defaults_to_off_via_setting(self):
        env = dict(self.env)
        env.pop("GT_WATCH")
        self._mixed()
        p = self.run_cmd(["python3", WATCH, "--hook"], env={"GT_WATCH": ""})
        self.assertEqual(p.stdout, "", "watch defaults to off")
        (self.home / ".claude" / "vault-config.json").write_text(json.dumps({"watch": "report"}))
        self.assertIn("P0", self.run_cmd(["python3", WATCH, "--hook"], env={"GT_WATCH": ""}).stdout)

    def test_hook_reads_only(self):
        self._mixed()
        before = {p.name: p.stat().st_mtime_ns for p in self.state.iterdir() if p.is_file()}
        self.hook_text()
        after = {p.name: p.stat().st_mtime_ns for p in self.state.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_ack_hides_then_new_change_renotifies(self):
        self._mixed()
        state_before = (self.state / "state.json").read_text()
        self.assertOk(self.watch("ack"))
        self.assertEqual((self.state / "state.json").read_text(), state_before,
                         "ack must not change state.json")
        self.assertIn("nothing new", self.hook_text())
        self.commit("Fix CVE-2026-22222")
        self.push()
        self.fetch()
        text = self.hook_text()
        self.assertIn("1 P0", text)
        self.assertIn("CVE-2026-22222", text)
        self.assertNotIn("CVE-2026-12345", text)

    def test_ack_one_slug(self):
        self._mixed()
        self.assertOk(self.watch("ack", "widget-lib"))
        self.assertIn("nothing new", self.hook_text())

    def test_no_state_inside_vault_after_full_cycle(self):
        self._mixed()
        self.hook_text()
        self.watch("show", "widget-lib")
        self.watch("ack")
        files = sorted(str(p.relative_to(self.vault)) for p in self.vault.rglob("*") if p.is_file())
        self.assertEqual(files, ["INBOX.md", os.path.join("Projects", "golden-thread", "watches", "widget-lib.md")])
        self.assertTrue((self.state / "events.jsonl").exists())

    def test_show_lists_p0_first(self):
        self._mixed()
        out = self.watch("show", "widget-lib").stdout
        self.assertTrue(out.startswith("[P0]"), out)


class TestCron(WatchTest):

    def test_install_is_idempotent_and_uninstall_removes(self):
        cf = self.tmp / "crontab.txt"
        cf.write_text("0 3 * * * /usr/bin/true # someone else's\n")
        for _ in range(3):
            self.assertOk(self.watch("install-cron", "--every", "1h"))
        lines = cf.read_text().splitlines()
        tagged = [l for l in lines if "# gt-watch" in l]
        self.assertEqual(len(tagged), 1, lines)
        self.assertIn("fetch", tagged[0])
        self.assertTrue(tagged[0].startswith("0 * * * * "))
        self.assertIn("someone else's", cf.read_text())
        self.assertOk(self.watch("uninstall-cron"))
        self.assertNotIn("gt-watch", cf.read_text())
        self.assertIn("someone else's", cf.read_text())

    def test_every_30m(self):
        self.assertOk(self.watch("install-cron", "--every", "30m"))
        self.assertIn("*/30 * * * *", (self.tmp / "crontab.txt").read_text())

    NOTE = "no gt-watch cron entry"

    def test_hook_names_a_missing_cron_entry_and_only_then(self):
        # with the entry in place: no note
        self.add()
        self.assertOk(self.watch("install-cron"))
        self.assertNotIn(self.NOTE, self.hook_text())
        # entry gone (e.g. install.sh --without watch): the hook says so, with the fix
        self.assertOk(self.watch("uninstall-cron"))
        text = self.hook_text()
        self.assertIn(self.NOTE, text)
        self.assertIn("install-cron", text)
        self.assertEqual(len([l for l in text.splitlines() if self.NOTE in l]), 1)

    def test_no_note_without_watches_or_without_a_crontab_binary(self):
        self.assertNotIn(self.NOTE, self.hook_text())          # no watches yet
        self.add()
        self.assertIn(self.NOTE, self.hook_text())             # control: it would fire
        (self.bin / "crontab").unlink()
        path = ":".join(p for p in self.env["PATH"].split(":")
                        if not (Path(p) / "crontab").exists())
        self.assertNotIn(self.NOTE, self.hook_text(env={"PATH": path}))


class TestRegistries(unittest.TestCase):

    def test_setting_and_hook_declared_by_the_module(self):
        m = json.loads((WATCH_MODULE / "module.json").read_text())
        self.assertEqual(m["hookdir_scripts"], ["gt_watch.py"])
        self.assertEqual(m["hooks"], [{"event": "SessionStart", "script": "gt_watch.py",
                                       "args": ["--hook"], "kind": "reporter"}])
        self.assertEqual([s["key"] for s in m["settings"]], ["watch"])
        watch = m["settings"][0]
        self.assertEqual((watch["default"], watch["values"]), ("off", ["off", "report"]))

    def test_user_facing_text_names_the_module_command(self):
        for p in (WATCH, TEMPLATES / "watch.md", WATCH_MODULE / "skills" / "gt-watch" / "SKILL.md"):
            t = p.read_text()
            self.assertNotIn("/gt:gt-watch", t, "%s still invokes watch through the gt plugin" % p.name)
        self.assertIn("/gt-watch:gt-watch", WATCH.read_text())


if __name__ == "__main__":
    unittest.main()
