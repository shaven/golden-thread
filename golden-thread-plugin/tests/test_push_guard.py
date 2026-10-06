"""gt_push_guard.py / the push_fingerprint setting (gt 0.20.3, owner 2026-10-06).

One switch: `on` turns gt unlock on in a push-only profile and installs a pre-push hook in the
listed repos; the hook asks gt unlock for gt:publish before a push; `off` undoes exactly that.
gt unlock itself is faked here (GT_PUSH_GUARD_UNLOCK), so no real factor or policy is touched.
"""
import json
import os
import subprocess
import sys
import unittest

from _harness import Sandbox, GT

GUARD = GT / "scripts" / "gt_push_guard.py"
SETTINGS = GT / "scripts" / "gt_settings.py"

FAKE = r'''
import json, os, shutil, sys
d = os.environ["FAKE_DIR"]; a = sys.argv[1:]
def j(n, default):
    try: return json.load(open(os.path.join(d, n)))
    except Exception: return default
open(os.path.join(d, "calls.log"), "a").write(" ".join(a) + "\n")
if a[:2] == ["status", "--json"]:
    print(json.dumps(j("status.json", {"enabled": False, "factors": {}})))
elif a[:3] == ["policy", "show", "--json"]:
    print(json.dumps(j("policy.json", {"user": {}, "effective": {"scopes": {}}})))
elif a[:2] == ["policy", "set"]:
    p = json.load(open(a[2])); n = len([f for f in os.listdir(d) if f.startswith("set-")])
    shutil.copy(a[2], os.path.join(d, "set-%d.json" % n))
    json.dump({"user": p, "effective": {"scopes": p.get("scopes") or {}}}, open(os.path.join(d, "policy.json"), "w"))
    st = j("status.json", {"factors": {}}); st["enabled"] = bool(p.get("enabled"))
    json.dump(st, open(os.path.join(d, "status.json"), "w"))
elif a[:3] == ["seal", "put", "--name"]:
    n = len(sys.stdin.read())          # the length only: the value is never written anywhere
    open(os.path.join(d, "sealed.len"), "w").write(str(n))
elif a[:2] == ["seal", "rm"]:
    pass
elif a[:1] == ["check"]:
    sys.exit(int(open(os.path.join(d, "check_rc")).read()) if os.path.exists(os.path.join(d, "check_rc")) else 0)
'''


class PushGuard(Sandbox):
    def setUp(self):
        super().setUp()
        self.fake = self.tmp / "fakeunlock"
        self.fake.mkdir()
        (self.fake / "unlock.py").write_text(FAKE, encoding="utf-8")
        self.env["GT_PUSH_GUARD_UNLOCK"] = str(self.fake / "unlock.py")
        self.env["FAKE_DIR"] = str(self.fake)
        (self.fake / "gh").write_text("#!/bin/sh\nprintf 'fake-token-not-real'\n")
        os.chmod(self.fake / "gh", 0o755)
        self.env["GT_PUSH_GUARD_GH"] = str(self.fake / "gh")
        self.uhome = self.tmp / "unlockhome"
        self.env["GT_PUSH_GUARD_UNLOCK_HOME"] = str(self.uhome)
        self.repo = self.tmp / "Repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.hook = self.repo / ".git" / "hooks" / "pre-push"
        self.status(enrolled=True, enabled=False)
        (self.fake / "policy.json").write_text(json.dumps(
            {"user": {"schema": 1, "enabled": False, "scopes": {"gt:vault:read": "unlocked"}},
             "effective": {"scopes": {}}}))

    def status(self, enrolled, enabled):
        f = {"touchid": {"enrolled": enrolled, "available": True}}
        (self.fake / "status.json").write_text(json.dumps({"enabled": enabled, "factors": f}))

    def guard(self, *args, stdin=""):
        return subprocess.run([sys.executable, str(GUARD)] + list(args), input=stdin,
                              capture_output=True, text=True, env=self.env)

    def sets(self):
        return sorted(self.fake.glob("set-*.json"))

    # -- on ---------------------------------------------------------------------------
    def test_on_without_a_repo_is_refused(self):
        p = self.guard("on")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("push_fingerprint_repos", p.stdout)
        self.assertEqual(self.sets(), [])

    def test_on_without_an_enrolled_factor_asks_for_the_one_human_step(self):
        self.status(enrolled=False, enabled=False)
        p = self.guard("on", str(self.repo))
        self.assertEqual(p.returncode, 3, p.stdout)
        self.assertIn("enroll touchid", p.stdout)
        self.assertFalse(self.hook.exists())
        self.assertEqual(self.sets(), [])

    def test_on_installs_the_hook_and_a_push_only_policy(self):
        p = self.guard("on", str(self.repo))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("gt push_fingerprint", self.hook.read_text())
        self.assertTrue(os.access(self.hook, os.X_OK))
        pol = json.loads(self.sets()[-1].read_text())
        self.assertTrue(pol["enabled"])
        for s in ("gt:publish", "gt:unlock:policy", "gt:unlock:enroll"):
            self.assertEqual(pol["scopes"][s], "step_up", s)
        self.assertEqual(pol["scopes"]["gt:vault:read"], "open")
        self.assertEqual(pol["scopes"]["lotr:*:write"], "open")
        self.assertEqual(pol["factors"]["required"], 1, "K reachable with the one enrolled factor")
        self.assertEqual(pol["step_up"]["fresh_s"], 0, "a fresh touch for every push")
        self.assertEqual(pol["factors"]["chosen"]["factors"], ["touchid"])

    def test_a_repo_with_its_own_pre_push_hook_is_left_alone(self):
        self.hook.parent.mkdir(parents=True, exist_ok=True)
        self.hook.write_text("#!/bin/sh\necho mine\n")
        p = self.guard("on", str(self.repo))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(self.hook.read_text(), "#!/bin/sh\necho mine\n")
        self.assertEqual(self.sets(), [], "nothing changed, not even the policy")

    # -- the hook ---------------------------------------------------------------------
    PUSH = "refs/heads/feat abc123 refs/heads/feat 0000000\n"

    def test_the_hook_lets_a_confirmed_push_through(self):
        self.guard("on", str(self.repo))
        p = self.guard("hook", "origin", "url", stdin=self.PUSH)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("check --scope gt:publish --request", (self.fake / "calls.log").read_text())

    def test_the_hook_refuses_without_the_confirmation(self):
        self.guard("on", str(self.repo))
        (self.fake / "check_rc").write_text("11")
        p = self.guard("hook", "origin", "url", stdin=self.PUSH)
        self.assertEqual(p.returncode, 1)
        self.assertIn("push refused", p.stderr)

    def test_the_hook_refuses_when_unlock_is_off(self):
        self.guard("on", str(self.repo))
        self.status(enrolled=True, enabled=False)
        p = self.guard("hook", "origin", "url", stdin=self.PUSH)
        self.assertEqual(p.returncode, 1)
        self.assertIn("unlock is off", p.stderr)

    def test_nothing_to_push_is_not_asked_about(self):
        self.guard("on", str(self.repo))
        p = self.guard("hook", "origin", "url", stdin="")
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("check --scope", (self.fake / "calls.log").read_text())

    # -- off and check ----------------------------------------------------------------
    def test_off_removes_the_hook_and_restores_the_earlier_policy(self):
        self.guard("on", str(self.repo))
        p = self.guard("off")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertFalse(self.hook.exists())
        restored = json.loads(self.sets()[-1].read_text())
        self.assertFalse(restored["enabled"])
        self.assertEqual(restored["scopes"], {"gt:vault:read": "unlocked"})

    def test_check_proves_the_setup(self):
        self.assertEqual(self.guard("check").returncode, 1, "nothing set up yet")
        self.guard("on", str(self.repo))
        p = self.guard("check")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertNotIn("FAIL", p.stdout)

    # -- the sealed push token (part 2) ---------------------------------------------------
    def helpers(self):
        p = subprocess.run(["git", "-C", str(self.repo), "config", "--local", "--get-all",
                            "credential.https://github.com.helper"], capture_output=True, text=True)
        return p.stdout.splitlines()

    def seal_on(self):
        cfg = self.home / ".claude" / "vault-config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        d = json.loads(cfg.read_text()) if cfg.exists() else {}
        d["push_fingerprint_seal_token"] = "on"
        cfg.write_text(json.dumps(d))

    def test_by_default_no_token_is_sealed_and_no_helper_set(self):
        """Owner, 2026-10-06 (option 2): sealing is opt-in; the default is the hook only."""
        self.assertEqual(self.guard("on", str(self.repo)).returncode, 0)
        self.assertFalse((self.fake / "sealed.len").exists())
        self.assertEqual(self.helpers(), [])

    def test_on_seals_the_token_without_reading_it_and_points_git_at_the_helper(self):
        self.seal_on()
        p = self.guard("on", str(self.repo))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("fake-token-not-real", p.stdout + p.stderr, "the value is never printed")
        self.assertEqual((self.fake / "sealed.len").read_text(), str(len("fake-token-not-real")))
        cred = json.loads((self.uhome / "credentials.json").read_text())
        self.assertEqual([m["ref"] for m in cred["git"] if m["host"] == "github.com"],
                         ["sealed:github"])
        h = self.helpers()
        self.assertEqual(h[0], "", "the list is reset, so gh's global helper is not used here")
        self.assertIn("git-credential", h[1])

    def test_without_gh_the_hook_still_guards_and_no_helper_is_set(self):
        self.seal_on()
        self.env["GT_PUSH_GUARD_GH"] = str(self.tmp / "no-gh")
        p = self.guard("on", str(self.repo))
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("gh is not installed", p.stdout)
        self.assertEqual(self.helpers(), [])
        self.assertTrue(self.hook.exists())

    def test_the_hook_leaves_the_asking_to_the_helper_for_a_guarded_github_remote(self):
        self.seal_on()
        self.guard("on", str(self.repo))
        p = subprocess.run([sys.executable, str(GUARD), "hook", "origin",
                            "https://github.com/x/y.git"], input=self.PUSH, capture_output=True,
                           text=True, env=self.env, cwd=str(self.repo))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("check --scope", (self.fake / "calls.log").read_text(),
                         "one fingerprint per push, asked by the helper")

    def test_off_undoes_the_token_steps_exactly(self):
        self.seal_on()
        self.guard("on", str(self.repo))
        self.guard("off")
        self.assertEqual(self.helpers(), [])
        self.assertFalse((self.uhome / "credentials.json").exists())
        self.assertIn("seal rm github", (self.fake / "calls.log").read_text())

    # -- the setting --------------------------------------------------------------------
    def test_the_repos_setting_keeps_the_path_case_and_refuses_a_relative_path(self):
        self.config(vault_path=str(self.tmp / "vault"))
        p = subprocess.run([sys.executable, str(SETTINGS), "set", "push_fingerprint_repos",
                            str(self.repo)], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout)
        cfg = json.loads((self.home / ".claude" / "vault-config.json").read_text())
        self.assertEqual(cfg["push_fingerprint_repos"], str(self.repo), "case kept (Repo)")
        p = subprocess.run([sys.executable, str(SETTINGS), "set", "push_fingerprint_repos",
                            "relative/path"], capture_output=True, text=True, env=self.env)
        self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
