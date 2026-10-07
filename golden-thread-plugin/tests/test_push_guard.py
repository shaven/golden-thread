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
    rc = os.path.join(d, "seal_rm_rc")
    sys.exit(int(open(rc).read()) if os.path.exists(rc) else 0)
elif a[:1] == ["check"]:
    sys.exit(int(open(os.path.join(d, "check_rc")).read()) if os.path.exists(os.path.join(d, "check_rc")) else 0)
'''


FAKE_GH = r'''
import sys
open(%(log)r, "a").write(" ".join(sys.argv[1:]) + "\n")   # the arguments only, never a value
sys.stdout.write("fake-token-not-real")
'''


class PushGuard(Sandbox):
    def setUp(self):
        super().setUp()
        self.fake = self.tmp / "fakeunlock"
        self.fake.mkdir()
        (self.fake / "unlock.py").write_text(FAKE, encoding="utf-8")
        self.env["GT_PUSH_GUARD_UNLOCK"] = str(self.fake / "unlock.py")
        self.env["FAKE_DIR"] = str(self.fake)
        # a Python fake, run through python by the guard: Windows cannot exec a #!/bin/sh file
        # (WinError 193), which is how the 0.20.3 beta's fixture failed on its first Windows run
        (self.fake / "gh.py").write_text(FAKE_GH % {"log": str(self.fake / "gh.log")},
                                         encoding="utf-8")
        self.env["GT_PUSH_GUARD_GH"] = str(self.fake / "gh.py")
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

    def test_on_when_unlock_is_already_on_loosens_nothing(self):
        """0.20.3 beta: the push-only profile set every existing scope to open, so on a machine
        already using unlock, LOTR consent stopped asking until `off`. A policy already on
        keeps every scope it has, and its K; only the push scopes become step_up."""
        user = {"schema": 1, "enabled": True, "factors": {"required": 2},
                "scopes": {"lotr:*:consent": "step_up", "gt:vault:read": "unlocked"}}
        (self.fake / "policy.json").write_text(json.dumps(
            {"user": user, "effective": {"scopes": user["scopes"]}}))
        self.status(enrolled=True, enabled=True)
        p = self.guard("on", str(self.repo))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pol = json.loads(self.sets()[-1].read_text())
        self.assertEqual(pol["scopes"]["lotr:*:consent"], "step_up")
        self.assertEqual(pol["scopes"]["gt:vault:read"], "unlocked")
        self.assertNotIn("lotr:*:write", pol["scopes"], "nothing new is opened")
        for s in ("gt:publish", "gt:unlock:policy", "gt:unlock:enroll"):
            self.assertEqual(pol["scopes"][s], "step_up", s)
        self.assertEqual(pol["factors"]["required"], 2, "the owner's K is never lowered")
        self.assertEqual(pol["step_up"]["fresh_s"], 0)
        self.assertIn("already on", p.stdout)
        self.assertEqual(self.guard("off").returncode, 0)
        self.assertEqual(json.loads(self.sets()[-1].read_text()), user, "off restores it exactly")

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
        self.guard("on", str(self.repo))
        p = self.guard("check")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertNotIn("FAIL", p.stdout)

    def test_check_says_off_when_nothing_is_set_up(self):
        """0.20.3 beta printed three FAIL lines after a deliberate `off`. Off is its own answer
        (exit 2), never 0, so a script using check as proof cannot read off as proven."""
        for when in ("before on", "after off"):
            p = self.guard("check")
            self.assertEqual(p.returncode, 2, when + ": " + p.stdout)
            self.assertNotIn("FAIL", p.stdout, when)
            self.assertIn("push_fingerprint is off", p.stdout, when)
            if when == "before on":
                self.guard("on", str(self.repo))
                self.guard("off")

    # -- the sealed push token (part 2) ---------------------------------------------------
    def helpers(self, host="github.com"):
        p = subprocess.run(["git", "-C", str(self.repo), "config", "--local", "--get-all",
                            "credential.https://%s.helper" % host], capture_output=True, text=True)
        return p.stdout.splitlines()

    def seal_on(self, remote="https://github.com/x/y.git"):
        """Sealing follows the repo's HTTPS push remotes (0.20.4), so give it one. The fake gh
        logs its arguments, never a value."""
        if remote:
            subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", remote],
                           check=True)
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

    def test_a_gh_that_cannot_run_is_reported_not_a_crash(self):
        """0.20.4: seal_token raised OSError (WinError 193 on Windows) when gh could not be
        executed, so `on` died with a traceback half way through."""
        self.seal_on()
        bad = self.tmp / "gh-not-runnable"
        bad.write_text("not a program\n")
        os.chmod(bad, 0o644)
        self.env["GT_PUSH_GUARD_GH"] = str(bad)
        p = self.guard("on", str(self.repo))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("could not run gh", p.stdout)
        self.assertEqual(self.helpers(), [])
        self.assertTrue(self.hook.exists(), "the hook still guards")

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

    # -- any GitHub host, from the repo's own remote (0.20.4) ------------------------------
    ENT = "github.example.com"

    def test_an_enterprise_remote_is_sealed_for_its_own_host(self):
        """The 0.20.3 beta was github.com only; a repo pushing to a GitHub Enterprise host got
        the hook and nothing else."""
        self.seal_on("https://%s/org/repo.git" % self.ENT)
        p = self.guard("on", str(self.repo))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("fake-token-not-real", p.stdout + p.stderr)
        self.assertIn("auth token -h %s" % self.ENT, (self.fake / "gh.log").read_text())
        self.assertIn("seal put --name gh-%s" % self.ENT, (self.fake / "calls.log").read_text())
        cred = json.loads((self.uhome / "credentials.json").read_text())
        self.assertEqual([m["ref"] for m in cred["git"] if m["host"] == self.ENT],
                         ["sealed:gh-%s" % self.ENT])
        self.assertEqual([m for m in cred["git"] if m["host"] == "github.com"], [])
        h = self.helpers(self.ENT)
        self.assertEqual(h[0], "")
        self.assertIn("git-credential", h[1])
        self.assertEqual(self.helpers("github.com"), [], "no helper for a host it does not push to")
        r = subprocess.run([sys.executable, str(GUARD), "hook", "origin",
                            "https://%s/org/repo.git" % self.ENT], input=self.PUSH,
                           capture_output=True, text=True, env=self.env, cwd=str(self.repo))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("check --scope", (self.fake / "calls.log").read_text(),
                         "the helper asks for the enterprise host too: one fingerprint per push")
        self.assertEqual(self.guard("off").returncode, 0)
        self.assertEqual(self.helpers(self.ENT), [])
        self.assertFalse((self.uhome / "credentials.json").exists())
        self.assertIn("seal rm gh-%s" % self.ENT, (self.fake / "calls.log").read_text())

    def test_an_ssh_remote_seals_nothing_and_says_the_hook_is_the_guard(self):
        self.seal_on("git@%s:org/repo.git" % self.ENT)
        p = self.guard("on", str(self.repo))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("SSH", p.stdout)
        self.assertFalse((self.fake / "sealed.len").exists())
        self.assertFalse((self.fake / "gh.log").exists())
        self.assertEqual(self.helpers(self.ENT), [])
        self.assertTrue(self.hook.exists(), "the hook still guards")

    # -- independent review of 0.20.4 (2026-10-06): GO WITH FIXES -----------------------------
    def local_helpers(self, host="github.com"):
        return self.helpers(host)

    def test_a_repos_own_credential_helper_survives_on_and_off(self):
        """Review #1: `off` ran --unset-all on the helper key for every repo (seal or not), and
        set_helper's --replace-all "" overwrote a helper of the user's own without saving it."""
        key = "credential.https://github.com.helper"
        subprocess.run(["git", "-C", str(self.repo), "config", "--local", key, "!mine"], check=True)
        self.assertEqual(self.guard("on", str(self.repo)).returncode, 0)
        self.assertEqual(self.guard("off").returncode, 0)
        self.assertEqual(self.helpers(), ["!mine"], "without sealing nothing touches the helper")
        self.seal_on()
        self.assertEqual(self.guard("on", str(self.repo)).returncode, 0)
        self.assertEqual(self.helpers()[0], "")
        self.assertEqual(self.guard("off").returncode, 0)
        self.assertEqual(self.helpers(), ["!mine"], "off puts the user's own helper back")

    def test_a_failure_part_way_through_on_keeps_the_original_policy(self):
        """Review #2: state was saved only at the end of `on`, so a failure after the policy was
        set left no state; `off` did nothing, and the next `on` saved the push-only policy as
        the 'earlier' one."""
        original = json.loads((self.fake / "policy.json").read_text())["user"]
        self.seal_on()
        self.uhome.mkdir(parents=True, exist_ok=True)
        (self.uhome / "credentials.json").mkdir()          # the mapping write will fail
        p = self.guard("on", str(self.repo))
        self.assertNotEqual(p.returncode, 0)
        state = json.loads((self.home / ".claude" / "golden-thread" / "push-guard.json").read_text())
        self.assertEqual(state["saved_policy"], original)
        (self.uhome / "credentials.json").rmdir()
        self.guard("off")
        self.assertEqual(json.loads(self.sets()[-1].read_text()), original)
        self.assertFalse(self.hook.exists())

    def test_core_hooks_path_is_where_the_hook_goes(self):
        """Review #3: with core.hooksPath set, git never runs .git/hooks/pre-push, yet check
        said PASS."""
        subprocess.run(["git", "-C", str(self.repo), "config", "core.hooksPath", ".githooks"],
                       check=True)
        self.assertEqual(self.guard("on", str(self.repo)).returncode, 0)
        self.assertTrue((self.repo / ".githooks" / "pre-push").exists())
        self.assertFalse(self.hook.exists())
        self.assertEqual(self.guard("check").returncode, 0)
        self.guard("off")
        (self.repo / ".githooks" / "pre-push").write_text("#!/bin/sh\necho mine\n")
        p = self.guard("on", str(self.repo))
        self.assertEqual(p.returncode, 1, "a hook of the user's own in core.hooksPath is left alone")

    def test_a_deny_is_never_lowered(self):
        """Review #6: STEP_UP scopes were forced to step_up even over the owner's deny."""
        user = {"schema": 1, "enabled": True, "scopes": {"gt:publish": "deny"}}
        (self.fake / "policy.json").write_text(json.dumps({"user": user, "effective": {"scopes": {}}}))
        self.status(enrolled=True, enabled=True)
        self.assertEqual(self.guard("on", str(self.repo)).returncode, 0)
        pol = json.loads(self.sets()[-1].read_text())
        self.assertEqual(pol["scopes"]["gt:publish"], "deny")
        self.assertEqual(pol["scopes"]["gt:unlock:policy"], "step_up")

    def test_off_keeps_its_state_when_the_sealed_copy_cannot_be_removed(self):
        """Review #5: the result of `seal rm` was ignored, 'deleted' was printed, and the state
        was removed, orphaning the sealed token."""
        self.seal_on()
        self.assertEqual(self.guard("on", str(self.repo)).returncode, 0)
        (self.fake / "seal_rm_rc").write_text("12")
        p = self.guard("off")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("sealed copy deleted", p.stdout)
        self.assertIn("could not delete", p.stdout)
        state = json.loads((self.home / ".claude" / "golden-thread" / "push-guard.json").read_text())
        self.assertEqual(state.get("sealed_hosts"), ["github.com"])
        self.assertTrue(self.hook.exists(), "re-review: the guard stays fully in place on a refusal")
        self.assertIn("git-credential", " ".join(self.helpers()))

    def test_a_refused_confirmation_at_off_changes_nothing(self):
        """Re-review: off removed hooks and helpers BEFORE the steps that need a confirmation,
        so a refusal left pushes with no fingerprint at all while the setting read on."""
        self.seal_on()
        self.assertEqual(self.guard("on", str(self.repo)).returncode, 0)
        self.status(enrolled=True, enabled=True)
        n = len(self.sets())
        (self.fake / "check_rc").write_text("12")
        p = self.guard("off")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nothing changed", p.stdout)
        self.assertTrue(self.hook.exists())
        self.assertIn("git-credential", " ".join(self.helpers()))
        self.assertEqual(len(self.sets()), n, "the policy was not touched")
        self.assertNotIn("seal rm", (self.fake / "calls.log").read_text())

    def test_on_over_a_beta_state_never_saves_gts_own_helper_as_the_users(self):
        """Re-review: a 0.20.3 beta state has no "helpers"; re-running on recorded the beta's
        "" + git-credential lines as the user's own, and off put gt's helper back."""
        self.seal_on()
        self.assertEqual(self.guard("on", str(self.repo)).returncode, 0)
        sp = self.home / ".claude" / "golden-thread" / "push-guard.json"
        st = json.loads(sp.read_text())
        st.pop("helpers", None)                              # what the beta wrote
        sp.write_text(json.dumps(st))
        self.assertEqual(self.guard("on", str(self.repo)).returncode, 0)
        self.assertEqual(self.guard("off").returncode, 0)
        self.assertEqual(self.helpers(), [], "no gt helper left behind")

    def test_the_settings_gate_needs_a_touch_in_the_push_only_profile(self):
        """Re-review: with unlock off before on, gt:settings:security was opened, so the gate on
        the fingerprint settings never asked."""
        self.assertEqual(self.guard("on", str(self.repo)).returncode, 0)
        pol = json.loads(self.sets()[-1].read_text())
        self.assertEqual(pol["scopes"]["gt:settings:security"], "step_up")

    def test_a_sealed_host_is_recorded_before_it_is_sealed(self):
        """Re-review #2 remainder: sealed_by_guard/sealed_hosts were written after the loop, so a
        failure after sealing left a sealed copy off would never remove."""
        self.seal_on()
        self.uhome.mkdir(parents=True, exist_ok=True)
        (self.uhome / "credentials.json").mkdir()            # fails after seal put
        self.assertNotEqual(self.guard("on", str(self.repo)).returncode, 0)
        st = json.loads((self.home / ".claude" / "golden-thread" / "push-guard.json").read_text())
        self.assertTrue(st.get("sealed_by_guard"))
        self.assertEqual(st.get("sealed_hosts"), ["github.com"])
        (self.uhome / "credentials.json").rmdir()
        self.guard("off")
        self.assertIn("seal rm github", (self.fake / "calls.log").read_text())

    def test_a_remote_url_is_read_for_its_real_host(self):
        """Review minor: '#' or '?' before '@' was taken as userinfo."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("pg_for_tests", str(GUARD))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertEqual(m._https_host("https://github.com#@evil.example/x"), "github.com")
        self.assertEqual(m._https_host("https://github.com?a=@evil.example/x"), "github.com")
        self.assertEqual(m._https_host("https://user@github.example.com/o/r.git"), "github.example.com")
        self.assertEqual(m._https_host("https://github.example.com:8443/o/r.git"), "github.example.com")

    def test_the_guard_settings_are_security_keys(self):
        """Review #4: switching a guard off from a session needed no confirmation."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("gs_for_tests", str(SETTINGS))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        for k in ("push_fingerprint", "push_fingerprint_seal_token", "push_fingerprint_repos",
                  "commit_fingerprint"):
            self.assertIn(k, m.SECURITY_KEYS, k)

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
