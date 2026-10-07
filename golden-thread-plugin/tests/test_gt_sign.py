"""gt_sign.py / the commit_fingerprint setting (gt 0.20.3 part 3, owner 2026-10-06).

A Secure Enclave key signs every commit (Touch ID each time) as git's ssh signing program, and
`github` adds the server half: the key registered as a signing key and a no-bypass ruleset that
requires signed commits on the default branch.

No test raises a prompt or touches GitHub. Most tests use a FAKE gt-presence (a script signing
with a throwaway software P-256 key, recorded like an installed helper) under GT_SIGN_TEST_KEYS=1;
the real-hardware case builds the shipped helper and uses a NON-biometric Secure Enclave key.
Every signature is checked by the real OpenSSH (`ssh-keygen -Y verify` / git verify-commit), so
the SSHSIG format is proven against an independent implementation, not against itself.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import unittest

from _harness import PYTHON, SCRIPTS, Sandbox, GT

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gt_sign as S                    # noqa: E402
import gt_unlock_touchid as T          # noqa: E402
from _unlock_keys import P256Key       # noqa: E402

SIGN = GT / "scripts" / "gt_sign.py"
SETTINGS = GT / "scripts" / "gt_settings.py"
KEYGEN = shutil.which("ssh-keygen")
IS_MAC = sys.platform == "darwin"
EMAIL = "gt-test@example.invalid"

# The fake helper: `create` hands out the test key, `sign` signs whatever it is given (or, with
# FAKE_FORGE set, something else -- the forged-helper case).
FAKE_HELPER = r'''#!%(python)s
import base64, json, os, sys
sys.path.insert(0, %(tests)r); sys.path.insert(0, %(scripts)r)
from _unlock_keys import P256Key
import gt_unlock_crypto as C
req = json.loads(sys.stdin.read() or "{}")
k = P256Key(); k.d = int(open(os.environ["FAKE_KEY_D"]).read())
from _unlock_keys import _jmul_affine
k.x, k.y = _jmul_affine(k.d, C.G)
cmd = sys.argv[1]
if cmd == "create":
    print(json.dumps({"blob": base64.b64encode(b"fake-se-handle").decode(),
                      "public": base64.b64encode(k.public_bytes()).decode()}))
elif cmd == "sign":
    data = base64.b64decode(req["challenge"])
    if os.environ.get("FAKE_FORGE"):
        data = b"something else"
    print(json.dumps({"signature": base64.b64encode(k.sign_der(data)).decode()}))
else:
    print(json.dumps({"error": {"code": "malformed", "message": "no"}})); sys.exit(1)
'''


@unittest.skipUnless(KEYGEN, "ssh-keygen is needed to check the signature format")
class SshsigFormat(unittest.TestCase):
    """Our SSHSIG, signed by a software key, must be accepted by OpenSSH itself."""

    def setUp(self):
        self.d = __import__("tempfile").mkdtemp(prefix="gt-sshsig.")
        self.addCleanup(shutil.rmtree, self.d, True)

    def _sig(self, key, msg, ns="git"):
        r, s = key.sign_rs(S.sshsig_signed_data(msg, ns))
        return S.sshsig_armor(key.public_bytes(), ns, r, s)

    def _run(self, *args, data=b""):
        return subprocess.run([KEYGEN] + list(args), input=data, capture_output=True)

    def test_openssh_verifies_our_signature_and_rejects_tampering(self):
        key, msg = P256Key(), b"tree abc\nauthor x\n\nhello world\n"
        sp = os.path.join(self.d, "m.sig")
        with open(sp, "w") as f:
            f.write(self._sig(key, msg))
        allowed = os.path.join(self.d, "allowed")
        with open(allowed, "w") as f:
            f.write('%s namespaces="git" %s\n' % (EMAIL, S.ssh_public_line(key.public_bytes())
                                                   .rsplit(" ", 1)[0]))
        self.assertEqual(self._run("-Y", "check-novalidate", "-n", "git", "-s", sp,
                                   data=msg).returncode, 0)
        self.assertEqual(self._run("-Y", "verify", "-f", allowed, "-I", EMAIL, "-n", "git",
                                   "-s", sp, data=msg).returncode, 0)
        # another message, another namespace, another key: all refused
        self.assertNotEqual(self._run("-Y", "check-novalidate", "-n", "git", "-s", sp,
                                      data=msg + b"x").returncode, 0)
        self.assertNotEqual(self._run("-Y", "check-novalidate", "-n", "file", "-s", sp,
                                      data=msg).returncode, 0)
        other = os.path.join(self.d, "allowed2")
        with open(other, "w") as f:
            f.write('%s namespaces="git" %s\n' % (EMAIL, S.ssh_public_line(P256Key().public_bytes())
                                                   .rsplit(" ", 1)[0]))
        self.assertNotEqual(self._run("-Y", "verify", "-f", other, "-I", EMAIL, "-n", "git",
                                      "-s", sp, data=msg).returncode, 0)

    def test_high_bit_r_and_s_are_encoded_as_positive_mpints(self):
        # many signatures, so r and s with the top bit set (needing a leading zero) are covered
        key, msg = P256Key(), b"x"
        sp = os.path.join(self.d, "m.sig")
        for _ in range(12):
            with open(sp, "w") as f:
                f.write(self._sig(key, msg))
            self.assertEqual(self._run("-Y", "check-novalidate", "-n", "git", "-s", sp,
                                       data=msg).returncode, 0)

    def test_public_line_round_trips(self):
        q = P256Key().public_bytes()
        line = S.ssh_public_line(q)
        self.assertTrue(line.startswith("ecdsa-sha2-nistp256 AAAA"))
        self.assertEqual(S.parse_public_line(line), S.ssh_public_blob(q))
        if KEYGEN:
            p = os.path.join(self.d, "k.pub")
            with open(p, "w") as f:
                f.write(line + "\n")
            r = self._run("-l", "-f", p)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(b"ECDSA", r.stdout)


class SignCase(Sandbox):
    def setUp(self):
        super().setUp()
        if not KEYGEN:
            self.skipTest("ssh-keygen is needed")
        if sys.platform == "win32":
            self.skipTest("commit signing is macOS only in this release; the fake helper is a "
                          "script Windows cannot exec")
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        key = P256Key()
        (self.fake / "d").write_text(str(key.d))
        self.pub = key.public_bytes()
        helper = self.fake / "gt-presence"
        helper.write_text(FAKE_HELPER % {"python": PYTHON, "tests": os.path.dirname(
            os.path.abspath(__file__)), "scripts": str(SCRIPTS)}, encoding="utf-8")
        os.chmod(helper, 0o755)
        T.record_helper(str(helper))
        self.env.update(GT_SIGN_HELPER=str(helper), GT_SIGN_TEST_KEYS="1",
                        FAKE_KEY_D=str(self.fake / "d"))
        self.repo = self.tmp / "Repo"
        self.git("init", "-q", str(self.repo), cwd=self.tmp)
        self.git("config", "user.email", EMAIL)
        self.git("config", "user.name", "gt-test")

    def git(self, *args, cwd=None, check=True):
        p = subprocess.run(["git"] + list(args), cwd=str(cwd or self.repo), env=self.env,
                           capture_output=True, text=True)
        if check:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def sign(self, *args):
        return subprocess.run([PYTHON, str(SIGN)] + list(args), env=self.env,
                              capture_output=True, text=True)

    def gh_home(self):
        return self.home / ".claude" / "golden-thread"


class OnCommitVerify(SignCase):
    def test_on_then_commit_is_signed_and_git_verifies_it(self):
        r = self.sign("on", str(self.repo))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ecdsa-sha2-nistp256 ", r.stdout)
        self.assertEqual(self.git("config", "--local", "commit.gpgsign").stdout.strip(), "true")
        self.git("commit", "-q", "--allow-empty", "-m", "first signed commit")
        self.assertIn("-----BEGIN SSH SIGNATURE-----",
                      self.git("cat-file", "commit", "HEAD").stdout)
        v = self.git("verify-commit", "HEAD", check=False)
        self.assertEqual(v.returncode, 0, v.stderr)
        self.assertEqual(self.git("log", "-1", "--format=%G?").stdout.strip(), "G")
        # the record holds the public key and a handle, and says the key is NOT biometric only
        # because this is the test path
        rec = json.loads((self.gh_home() / "commit-sign.json").read_text())
        self.assertEqual(base64.b64decode(rec["public"]), self.pub)
        self.assertFalse(rec["biometric"])
        self.assertEqual(oct(os.stat(self.gh_home() / "commit-sign.json").st_mode & 0o777),
                         "0o600")

    def test_signed_tag_verifies(self):
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)
        self.git("commit", "-q", "--allow-empty", "-m", "c")
        self.git("tag", "-m", "v1", "v1")
        self.assertEqual(self.git("verify-tag", "v1", check=False).returncode, 0)

    def test_check_passes_after_on(self):
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)
        r = self.sign("check", "--live")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("PASS  live signature verified by OpenSSH", r.stdout)
        self.assertNotIn("FAIL", r.stdout)

    def test_repos_come_from_the_setting(self):
        cfg = self.home / ".claude" / "vault-config.json"
        cfg.write_text(json.dumps({"vault_path": str(self.tmp), "push_fingerprint_repos":
                                   str(self.repo)}))
        self.assertEqual(self.sign("on").returncode, 0)
        self.assertEqual(self.git("config", "--local", "gpg.format").stdout.strip(), "ssh")


class Refusals(SignCase):
    def test_a_forged_helper_signature_writes_nothing_and_the_commit_fails(self):
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)
        self.env["FAKE_FORGE"] = "1"
        p = self.git("commit", "-q", "--allow-empty", "-m", "must not land", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("does not verify", p.stderr)
        self.assertNotEqual(self.git("rev-parse", "--verify", "HEAD", check=False).returncode, 0)

    def test_no_user_email_refuses_and_changes_nothing(self):
        self.git("config", "--unset", "user.email")
        r = self.sign("on", str(self.repo))
        self.assertEqual(r.returncode, 1)
        self.assertIn("no user.email", r.stdout)
        self.assertEqual(self.git("config", "--local", "--get", "gpg.format", check=False)
                         .returncode, 1)
        self.assertFalse((self.gh_home() / "commit-sign.json").exists())

    def test_no_repo_named_refuses(self):
        r = self.sign("on")
        self.assertEqual(r.returncode, 1)
        self.assertIn("push_fingerprint_repos", r.stdout)

    def test_a_non_biometric_record_is_refused_outside_the_test_path(self):
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)
        del self.env["GT_SIGN_TEST_KEYS"]
        p = self.git("commit", "-q", "--allow-empty", "-m", "x", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not biometric", p.stderr)


class PassThrough(SignCase):
    def test_another_key_is_signed_by_the_real_ssh_keygen(self):
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)
        k = self.tmp / "ed"
        subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True,
                       env=self.env, stdin=subprocess.DEVNULL)
        self.git("config", "--local", "user.signingkey", str(k))
        allowed = self.tmp / "allowed"
        allowed.write_text('%s namespaces="git" %s' % (EMAIL, (self.tmp / "ed.pub").read_text()))
        self.git("config", "--local", "gpg.ssh.allowedSignersFile", str(allowed))
        self.git("commit", "-q", "--allow-empty", "-m", "ed25519 signed")
        v = self.git("verify-commit", "--raw", "HEAD", check=False)
        self.assertEqual(v.returncode, 0, v.stderr)
        self.assertIn("ED25519", v.stderr.upper())


class KeyFile(SignCase):
    def test_signing_key_is_a_file_and_git_leaves_no_temp_key(self):
        """A literal `key::` signingkey makes git copy the key into $TMPDIR on every
        signature, and Apple git 2.50 left each copy behind (beta test run, 2026-10-06)."""
        tmp = self.tmp / "gittmp"
        tmp.mkdir()
        self.env["TMPDIR"] = str(tmp)
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)
        pub = self.gh_home() / "commit-sign.pub"
        self.assertEqual(self.git("config", "--local", "user.signingkey").stdout.strip(), str(pub))
        self.assertEqual(os.stat(pub).st_mode & 0o777, 0o600)
        self.assertTrue(pub.read_text().startswith("ecdsa-sha2-nistp256 "))
        self.git("commit", "-q", "--allow-empty", "-m", "signed from a key file")
        self.assertEqual(self.git("log", "-1", "--format=%G?").stdout.strip(), "G")
        self.assertEqual(sorted(p.name for p in tmp.iterdir()
                                if p.name.startswith(".git_signing_key_tmp")), [])
        self.assertEqual(self.sign("off").returncode, 0)
        self.assertFalse(pub.exists())


class Off(SignCase):
    def test_off_restores_exactly(self):
        self.git("config", "--local", "commit.gpgsign", "false")
        self.git("config", "--local", "gpg.format", "openpgp")
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)
        r = self.sign("off")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.git("config", "--local", "commit.gpgsign").stdout.strip(), "false")
        self.assertEqual(self.git("config", "--local", "gpg.format").stdout.strip(), "openpgp")
        for k in ("user.signingkey", "tag.gpgsign", "gpg.ssh.program",
                  "gpg.ssh.allowedSignersFile"):
            self.assertEqual(self.git("config", "--local", "--get", k, check=False).returncode,
                             1, k)
        self.assertFalse((self.gh_home() / "bin" / "gt-sign").exists())
        self.assertFalse((self.gh_home() / "commit-sign-state.json").exists())
        # the key itself is kept (a new one would need registering on GitHub again)
        self.assertTrue((self.gh_home() / "commit-sign.json").exists())
        self.git("commit", "-q", "--allow-empty", "-m", "unsigned again")
        self.assertEqual(self.git("log", "-1", "--format=%G?").stdout.strip(), "N")

    def test_off_asks_gt_unlock_first_and_a_refusal_changes_nothing(self):
        """Re-review: `gt_sign.py off` restored the unsigned config with no confirmation."""
        fake = self.tmp / "unlock.py"
        fake.write_text("import sys\nsys.exit(12 if sys.argv[1:2] == ['check'] else 0)\n")
        self.env["GT_SIGN_UNLOCK"] = str(fake)
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)
        r = self.sign("off")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing changed", r.stdout)
        self.assertEqual(self.git("config", "--local", "commit.gpgsign").stdout.strip(), "true")

    def test_on_twice_keeps_the_first_saved_values(self):
        self.git("config", "--local", "commit.gpgsign", "false")
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)
        self.assertEqual(self.sign("off").returncode, 0)
        self.assertEqual(self.git("config", "--local", "commit.gpgsign").stdout.strip(), "false")

    def test_enroll_force_keeps_the_old_record(self):
        self.assertEqual(self.sign("enroll").returncode, 0)
        self.assertEqual(self.sign("enroll", "--force").returncode, 0)
        olds = [p for p in os.listdir(self.gh_home()) if p.startswith("commit-sign.2")]
        self.assertEqual(len(olds), 1)


FAKE_GH = r'''#!%(python)s
import json, os, sys
d = os.environ["FAKE_GH_DIR"]; a = sys.argv[1:]
open(os.path.join(d, "calls.log"), "a").write(" ".join(a) + "\n")
host = "github.com"
if "--hostname" in a:                       # gh api --hostname HOST: never mistake HOST for a path
    i = a.index("--hostname"); host = a[i + 1]; a = a[:i] + a[i + 2:]
open(os.path.join(d, "hosts.log"), "a").write(host + "\n")
def load(n, default):
    try: return json.load(open(os.path.join(d, n)))
    except Exception: return default
def save(n, v): json.dump(v, open(os.path.join(d, n), "w"))
if os.path.exists(os.path.join(d, "fail")): sys.exit(1)
body = json.loads(sys.stdin.read()) if "--input" in a else None
method = a[a.index("-X") + 1] if "-X" in a else "GET"
path = [x for x in a[1:] if not x.startswith("-") and x not in ("GET", "POST", "DELETE")][0]
keys, rulesets = load("keys.json", []), load("rulesets.json", [])
if path == "user/ssh_signing_keys" and method == "GET": print(json.dumps(keys))
elif path == "user/ssh_signing_keys" and method == "POST":
    keys.append(dict(body, id=11)); save("keys.json", keys); print(json.dumps(keys[-1]))
elif path.endswith("/rulesets") and method == "GET":
    # like GitHub: the list holds summaries, without rules or bypass_actors
    print(json.dumps([{k: v for k, v in r.items() if k not in ("rules", "bypass_actors")}
                      for r in rulesets]))
elif "/rulesets/" in path and method == "GET":
    rid = int(path.rsplit("/", 1)[1])
    print(json.dumps([r for r in rulesets if r.get("id") == rid][0]))
elif path.endswith("/rulesets") and method == "POST":
    save("ruleset-body.json", body); rulesets.append(dict(body, id=77)); save("rulesets.json", rulesets)
    print(json.dumps(rulesets[-1]))
elif method == "DELETE":
    rid = int(path.rsplit("/", 1)[1])
    save("rulesets.json", [r for r in rulesets if r.get("id") != rid])
'''


class GitHub(SignCase):
    REMOTE = "git@github.com:someone/proj.git"

    def setUp(self):
        super().setUp()
        self.ghd = self.tmp / "gh"
        self.ghd.mkdir()
        gh = self.ghd / "gh"
        gh.write_text(FAKE_GH % {"python": PYTHON})
        os.chmod(gh, 0o755)
        self.env.update(GT_SIGN_GH=str(gh), FAKE_GH_DIR=str(self.ghd))
        self.git("remote", "add", "origin", self.REMOTE)
        self.assertEqual(self.sign("on", str(self.repo)).returncode, 0)

    def calls(self):
        p = self.ghd / "calls.log"
        return p.read_text() if p.exists() else ""

    def test_plan_changes_nothing(self):
        r = self.sign("github")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("add signing key", r.stdout)
        self.assertIn("someone/proj: add ruleset", r.stdout)
        self.assertNotIn("POST", self.calls())

    def test_apply_without_a_terminal_or_yes_refuses(self):
        r = self.sign("github", "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--yes", r.stdout)
        self.assertNotIn("POST", self.calls())

    def test_apply_adds_key_and_a_no_bypass_ruleset_then_remove_keeps_the_key(self):
        r = self.sign("github", "--apply", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout)
        body = json.loads((self.ghd / "ruleset-body.json").read_text())
        self.assertEqual(body["rules"], [{"type": "required_signatures"}])
        self.assertEqual(body["bypass_actors"], [])
        self.assertEqual(body["enforcement"], "active")
        self.assertEqual(body["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        keys = json.loads((self.ghd / "keys.json").read_text())
        self.assertEqual(S.parse_public_line(keys[0]["key"]), S.ssh_public_blob(self.pub))
        # idempotent: a second plan has nothing to add
        again = self.sign("github")
        self.assertIn("already registered", again.stdout)
        self.assertIn("ruleset already there", again.stdout)
        self.assertEqual(self.sign("check").returncode, 0)
        # off leaves the server half and says how to remove it
        off = self.sign("off")
        self.assertIn("gt_sign.py github --remove", off.stdout)
        rm = self.sign("github", "--remove", "--yes")
        self.assertEqual(rm.returncode, 0, rm.stdout)
        self.assertIn("DELETE repos/someone/proj/rulesets/77", self.calls())
        self.assertNotIn("DELETE user/ssh_signing_keys", self.calls())
        self.assertEqual(json.loads((self.ghd / "rulesets.json").read_text()), [])

    def test_check_reads_the_ruleset_itself_not_the_list_summary(self):
        """Review #7: the list endpoint has no rules or bypass_actors, so a ruleset given a
        bypass actor, or stripped of its rule, still checked PASS."""
        self.assertEqual(self.sign("github", "--apply", "--yes").returncode, 0)
        self.assertEqual(self.sign("check").returncode, 0)
        rs = json.loads((self.ghd / "rulesets.json").read_text())
        rs[0]["bypass_actors"] = [{"actor_type": "RepositoryRole", "actor_id": 5}]
        (self.ghd / "rulesets.json").write_text(json.dumps(rs))
        r = self.sign("check")
        self.assertEqual(r.returncode, 1, r.stdout)
        rs[0]["bypass_actors"] = []
        rs[0]["rules"] = []
        (self.ghd / "rulesets.json").write_text(json.dumps(rs))
        self.assertEqual(self.sign("check").returncode, 1)

    def test_missing_scope_names_the_refresh_command(self):
        (self.ghd / "fail").write_text("1")
        r = self.sign("github")
        self.assertEqual(r.returncode, 1)
        self.assertIn("gh auth refresh -h github.com -s admin:ssh_signing_key", r.stdout)


class GitHubEnterprise(GitHub):
    """The 0.20.3 beta parsed github.com remotes only and called gh without a host, so a repo on a
    GitHub Enterprise server had no server half at all (0.20.4). The inherited github.com tests
    do not run here; these do, against the enterprise host."""
    ENT = "github.example.com"
    REMOTE = "git@github.example.com:team/proj.git"

    test_plan_changes_nothing = None
    test_apply_without_a_terminal_or_yes_refuses = None
    test_apply_adds_key_and_a_no_bypass_ruleset_then_remove_keeps_the_key = None
    test_missing_scope_names_the_refresh_command = None

    def test_the_enterprise_host_gets_the_key_and_the_ruleset(self):
        plan = self.sign("github")
        self.assertEqual(plan.returncode, 0, plan.stdout)
        self.assertIn("%s/team/proj: add ruleset" % self.ENT, plan.stdout)
        self.assertIn("add signing key on %s" % self.ENT, plan.stdout)
        r = self.sign("github", "--apply", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout)
        hosts = set((self.ghd / "hosts.log").read_text().split())
        self.assertEqual(hosts, {self.ENT}, "every call goes to the enterprise host, none to github.com")
        self.assertIn("POST repos/team/proj/rulesets", self.calls())
        self.assertEqual(self.sign("check").returncode, 0)
        self.sign("off")
        rm = self.sign("github", "--remove", "--yes")
        self.assertEqual(rm.returncode, 0, rm.stdout)
        self.assertIn("DELETE repos/team/proj/rulesets/77", self.calls())
        self.assertNotIn("DELETE user/ssh_signing_keys", self.calls())

    def test_missing_scope_names_the_enterprise_host(self):
        (self.ghd / "fail").write_text("1")
        r = self.sign("github")
        self.assertEqual(r.returncode, 1)
        self.assertIn("gh auth refresh -h %s -s admin:ssh_signing_key" % self.ENT, r.stdout)

    def test_an_https_remote_is_read_too(self):
        self.git("remote", "set-url", "origin", "https://%s/team/proj.git" % self.ENT)
        plan = self.sign("github")
        self.assertIn("%s/team/proj: add ruleset" % self.ENT, plan.stdout)


class Settings(SignCase):
    def test_commit_fingerprint_setting_runs_on_and_off(self):
        cfg = self.home / ".claude" / "vault-config.json"
        cfg.write_text(json.dumps({"vault_path": str(self.tmp), "push_fingerprint_repos":
                                   str(self.repo)}))
        r = subprocess.run([PYTHON, str(SETTINGS), "set", "commit_fingerprint", "on"],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(cfg.read_text())["commit_fingerprint"], "on")
        self.assertEqual(self.git("config", "--local", "commit.gpgsign").stdout.strip(), "true")
        r = subprocess.run([PYTHON, str(SETTINGS), "set", "commit_fingerprint", "off"],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.git("config", "--local", "--get", "commit.gpgsign", check=False)
                         .returncode, 1)

    def test_a_failed_on_is_not_recorded(self):
        cfg = self.home / ".claude" / "vault-config.json"
        cfg.write_text(json.dumps({"vault_path": str(self.tmp)}))          # no repos
        r = subprocess.run([PYTHON, str(SETTINGS), "set", "commit_fingerprint", "on"],
                           env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("commit_fingerprint", json.loads(cfg.read_text()))


@unittest.skipUnless(IS_MAC and shutil.which("swiftc") and KEYGEN,
                     "real Secure Enclave case: macOS with swiftc and ssh-keygen")
class RealSecureEnclave(Sandbox):
    """The shipped helper, built here, with a NON-biometric SE key: a real hardware signature
    on a real commit, verified by git and OpenSSH. No prompt can appear."""

    def test_real_se_signed_commit_verifies(self):
        try:
            helper = T.build(str(self.tmp / "bin"))
        except T.BuildError as e:
            self.skipTest("helper did not build: %s" % e)
        r = subprocess.run([helper, "available"], input=b"{}", capture_output=True)
        if json.loads(r.stdout or b"{}").get("secure_enclave") is not True:
            self.skipTest("no Secure Enclave here")
        self.env.update(GT_SIGN_HELPER=helper, GT_SIGN_TEST_KEYS="1")
        repo = self.tmp / "R"
        run = lambda *a: subprocess.run(["git"] + list(a), cwd=str(repo), env=self.env,  # noqa: E731
                                        capture_output=True, text=True)
        subprocess.run(["git", "init", "-q", str(repo)], env=self.env, check=True)
        run("config", "user.email", EMAIL)
        on = subprocess.run([PYTHON, str(SIGN), "on", str(repo)], env=self.env,
                            capture_output=True, text=True)
        self.assertEqual(on.returncode, 0, on.stdout + on.stderr)
        c = run("commit", "-q", "--allow-empty", "-m", "signed by the Secure Enclave")
        self.assertEqual(c.returncode, 0, c.stderr)
        v = run("verify-commit", "HEAD")
        self.assertEqual(v.returncode, 0, v.stderr)


if __name__ == "__main__":
    unittest.main()
