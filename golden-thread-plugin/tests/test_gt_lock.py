"""gt_lock.py -- per-file age locks for vault folders (0.20.0, owner decisions (a) and (b)).

Driven through the CLI against a sandbox vault with a FAKE age (a Python program behind a `#!`
wrapper installed the house way, tests/_fakes.py, so it runs on Windows too). The fake keeps
the one property that matters here: a file encrypted to recipient R opens only with R's
identity. One test round-trips through the REAL age when the machine has it.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, Sandbox, CORE_RULES_LEGACY, UNLOCK_TEST_ENV
from _fakes import install_fake
from _unlock_fixture import AuthorityCase

LOCK = SCRIPTS / "gt_lock.py"
PLAIN = "the plaintext NEVER-IN-A-STUB-OR-LOG\n"
KEY = "AGE-SECRET-KEY-1FAKEALICE"
WHY = "Core-rule injection reads them every turn; locking them would silently switch it off"

FAKE_AGE = r'''
import base64, sys
args = sys.argv[1:]
mode, recips, ids, out, inp = None, [], [], None, None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-e", "-d"):
        mode = a
    elif a in ("-r", "-R", "-i", "-o"):
        v = args[i + 1]
        i += 1
        if a == "-r":
            recips.append(v)
        elif a == "-R":
            recips += [l.strip() for l in open(v) if l.strip() and not l.startswith("#")]
        elif a == "-i":
            ids.append(v)
        else:
            out = v
    else:
        inp = a
    i += 1


def pubs(path):
    res = []
    for line in open(path):
        line = line.strip()
        for pre, rp in (("AGE-SECRET-KEY-1", "age1"), ("AGE-PLUGIN-SE-1", "age1se1"),
                        ("AGE-PLUGIN-YUBIKEY-1", "age1yubikey1")):
            if line.startswith(pre):
                res.append(rp + line[len(pre):].lower())
    return res


data = open(inp, "rb").read() if inp else sys.stdin.buffer.read()
if mode == "-e":
    for p in ids:
        recips += pubs(p)
    if not recips:
        sys.stderr.write("age: error: no recipients\n")
        sys.exit(1)
    res = (b"age-encryption.org/v1\n-> " + ",".join(recips).encode() + b"\n---\n"
           + base64.b64encode(data))
else:
    head, _, body = data.partition(b"\n---\n")
    if not head.startswith(b"age-encryption.org/v1\n-> "):
        sys.stderr.write("age: error: not an age file\n")
        sys.exit(1)
    have = set(head.split(b"-> ", 1)[1].decode().split(","))
    if not any(r in have for p in ids for r in pubs(p)):
        sys.stderr.write("age: error: no identity matched any of the recipients\n")
        sys.exit(1)
    res = base64.b64decode(body)
if out:
    open(out, "wb").write(res)
else:
    sys.stdout.buffer.write(res)
'''


def write_fake_age(d):
    d = Path(d)
    script = d / "fake_age.py"
    script.write_text(FAKE_AGE, encoding="utf-8")
    return script


class LockBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.v = self.tmp / "vault"
        for d in ("Secrets", "core-rules", "global-memory", "Knowledge", CORE_RULES_LEGACY):
            (self.v / d).mkdir(parents=True, exist_ok=True)
        for rel in ("Secrets/creds.md", "Secrets/other.md", "core-rules/rule.md",
                    "global-memory/fact.md", CORE_RULES_LEGACY + "/old.md", "index.md"):
            (self.v / rel).write_bytes(PLAIN.encode())
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        script = write_fake_age(bin_)
        age = install_fake(self, bin_ / "age", '#!/bin/sh\nexec "%s" "%s" "$@"\n'
                           % (Path(PYTHON).as_posix(), script.as_posix()))
        self.env["GT_AGE"] = str(age)
        self.ident = self.tmp / "keys.txt"
        self.ident.write_text("# public key: age1fakealice\n%s\n" % KEY, encoding="utf-8")
        self.env["GT_AGE_IDENTITY"] = str(self.ident)

    def lock(self, *args, **kw):
        return self.py(LOCK, *args, **kw)

    def assertRefused(self, rel, *extra, why=None):
        p = self.lock("add", self.v / rel if not os.path.isabs(str(rel)) else rel,
                      "--vault", self.v, *extra)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        if why:
            self.assertIn(why, p.stderr)
        return p


class Add(LockBase):
    def test_add_encrypts_removes_the_plaintext_and_writes_the_stub(self):
        p = self.lock("add", self.v / "Secrets" / "creds.md", "--vault", self.v)
        self.assertOk(p)
        self.assertFalse((self.v / "Secrets" / "creds.md").exists())
        blob = (self.v / "Secrets" / "creds.md.age").read_bytes()
        self.assertTrue(blob.startswith(b"age-encryption.org/v1\n"))
        self.assertNotIn(b"NEVER-IN-A-STUB", blob)
        stub = (self.v / "Secrets" / ".gt-locked").read_bytes()
        self.assertNotIn(b"\r", stub)
        text = stub.decode()
        self.assertIn("scope: gt:lock:Secrets\n", text)
        self.assertIn("level: L1\n", text)
        self.assertIn("gt_lock.py open Secrets/<name>.age --vault .", text)
        self.assertNotIn(KEY, text + p.stdout + p.stderr)
        self.assertIn("not removed", p.stdout)        # history / sync copies, said honestly

    def test_dry_run_changes_nothing_before_or_after_the_subcommand(self):
        for argv in (["add", self.v / "Secrets/creds.md", "--vault", self.v, "--dry-run"],
                     ["--vault", self.v, "--dry-run", "add", self.v / "Secrets/creds.md"]):
            p = self.lock(*argv)
            self.assertOk(p)
            self.assertIn("would lock Secrets/creds.md", p.stdout)
            self.assertTrue((self.v / "Secrets" / "creds.md").exists())
            self.assertFalse((self.v / "Secrets" / "creds.md.age").exists())
            self.assertFalse((self.v / "Secrets" / ".gt-locked").exists())

    def test_vault_relative_spelling(self):
        self.assertOk(self.lock("add", "Secrets/other.md", "--vault", self.v, cwd=str(self.tmp)))
        self.assertTrue((self.v / "Secrets" / "other.md.age").exists())

    def test_core_rules_and_global_memory_are_never_lockable(self):
        for rel in ("core-rules/rule.md", "global-memory/fact.md", CORE_RULES_LEGACY + "/old.md",
                    "Secrets/../core-rules/rule.md", "Knowledge/../global-memory/fact.md"):
            self.assertRefused(rel, why=WHY)
        for rel in ("core-rules/rule.md", "global-memory/fact.md", CORE_RULES_LEGACY + "/old.md"):
            self.assertTrue((self.v / rel).exists(), rel)
            self.assertFalse((self.v / (rel + ".age")).exists(), rel)
        self.assertFalse((self.v / "core-rules" / ".gt-locked").exists())

    def test_protected_folders_refused_through_symlinks(self):
        try:
            os.symlink(str(self.v / "core-rules" / "rule.md"), str(self.v / "Secrets" / "l.md"))
            os.symlink(str(self.v / "global-memory"), str(self.v / "Knowledge" / "gm"),
                       target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            self.skipTest("cannot create symlinks here (%s)" % e)
        self.assertRefused("Secrets/l.md", why=WHY)
        self.assertRefused("Knowledge/gm/fact.md", why=WHY)
        self.assertTrue((self.v / "core-rules" / "rule.md").exists())
        self.assertTrue((self.v / "global-memory" / "fact.md").exists())

    def test_a_symlink_to_an_ordinary_file_is_refused(self):
        try:
            os.symlink(str(self.v / "Secrets" / "other.md"), str(self.v / "Knowledge" / "x.md"))
        except (OSError, NotImplementedError) as e:
            self.skipTest("cannot create symlinks here (%s)" % e)
        self.assertRefused("Knowledge/x.md", why="symlink")
        self.assertTrue((self.v / "Secrets" / "other.md").exists())

    def test_outside_the_vault_age_files_root_files_and_tool_folders_are_refused(self):
        outside = self.tmp / "outside.md"
        outside.write_text(PLAIN, encoding="utf-8")
        self.assertRefused(outside, why="outside the vault")
        self.assertRefused("Secrets/../../outside.md", why="outside the vault")
        self.assertTrue(outside.exists())
        (self.v / "Secrets" / "done.md.age").write_bytes(b"age-encryption.org/v1\nx")
        self.assertRefused("Secrets/done.md.age", why="already a locked")
        self.assertRefused("index.md", why="vault root")
        (self.v / ".obsidian").mkdir()
        (self.v / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        self.assertRefused(".obsidian/app.json", why="hidden")

    def test_hardware_recipient_is_l2_and_a_plain_one_beside_it_makes_l1(self):
        p = self.lock("add", self.v / "Secrets/creds.md", "--vault", self.v,
                      "--recipient", "age1se1qexample")
        self.assertOk(p)
        self.assertIn("level: L2\n", (self.v / "Secrets/.gt-locked").read_text(encoding="utf-8"))
        rf = self.tmp / "recips.txt"
        rf.write_text("# team\nage1yubikey1qexample\nage1fakealice\n", encoding="utf-8")
        p = self.lock("add", self.v / "Secrets/other.md", "--vault", self.v,
                      "--recipients-file", rf)
        self.assertOk(p)
        self.assertIn("level: L1\n", (self.v / "Secrets/.gt-locked").read_text(encoding="utf-8"))

    def test_missing_age_is_said_and_nothing_is_touched(self):
        self.env["GT_AGE"] = str(self.tmp / "no-age-here")
        p = self.lock("add", self.v / "Secrets/creds.md", "--vault", self.v)
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue((self.v / "Secrets/creds.md").exists())
        self.assertFalse((self.v / "Secrets/creds.md.age").exists())


class OpenRestoreStatus(LockBase):
    def setUp(self):
        super().setUp()
        self.assertOk(self.lock("add", self.v / "Secrets/creds.md", "--vault", self.v))
        self.age = self.v / "Secrets" / "creds.md.age"

    def test_open_writes_to_a_pipe_and_says_unlock_is_off(self):
        p = self.lock("open", self.age, "--vault", self.v)      # capture_output = a pipe
        self.assertOk(p)
        self.assertEqual(p.stdout, PLAIN)
        self.assertIn("unlock is off", p.stderr)
        self.assertIn("only as strong as the identity file", p.stderr)
        self.assertFalse((self.v / "Secrets" / "creds.md").exists())

    def test_open_refuses_to_write_into_a_file(self):
        out = self.tmp / "leak.txt"
        with open(out, "wb") as fh:
            p = subprocess.run([PYTHON, str(LOCK), "open", str(self.age), "--vault", str(self.v)],
                               stdout=fh, stderr=subprocess.PIPE, env=self.env, timeout=60,
                               text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("pipe only", p.stderr)
        self.assertEqual(out.read_bytes(), b"")

    def test_the_wrong_identity_opens_nothing(self):
        other = self.tmp / "bob.txt"
        other.write_text("AGE-SECRET-KEY-1FAKEBOB\n", encoding="utf-8")
        p = self.lock("open", self.age, "--vault", self.v, "--identity", other)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("NEVER-IN-A-STUB", p.stdout)

    def test_restore_brings_the_file_back_and_drops_the_last_stub(self):
        p = self.lock("restore", self.age, "--vault", self.v, "--dry-run")
        self.assertOk(p)
        self.assertTrue(self.age.exists())
        self.assertOk(self.lock("restore", self.age, "--vault", self.v))
        self.assertEqual((self.v / "Secrets/creds.md").read_bytes(), PLAIN.encode())
        self.assertFalse(self.age.exists())
        self.assertFalse((self.v / "Secrets/.gt-locked").exists())
        if not IS_WINDOWS:
            self.assertEqual((self.v / "Secrets/creds.md").stat().st_mode & 0o077, 0)

    def test_restore_keeps_the_stub_while_other_files_are_locked(self):
        self.assertOk(self.lock("add", self.v / "Secrets/other.md", "--vault", self.v))
        self.assertOk(self.lock("restore", self.age, "--vault", self.v))
        self.assertTrue((self.v / "Secrets/.gt-locked").exists())

    def test_status_lists_the_folder_and_rates_the_identity(self):
        p = self.lock("status", "--vault", self.v, "--json")
        self.assertOk(p)
        d = json.loads(p.stdout)
        self.assertEqual(d["identity"]["kind"], "plaintext")
        self.assertEqual(d["identity"]["level"], "L1")
        self.assertEqual(d["unlock"], "off")
        (f,) = d["folders"]
        self.assertEqual((f["folder"], f["files"], f["scope"], f["level"]),
                         ("Secrets", 1, "gt:lock:Secrets", "L1"))
        self.assertNotIn(KEY, p.stdout + p.stderr)
        self.ident.write_text("AGE-PLUGIN-SE-1SEMATERIAL\n", encoding="utf-8")
        d = json.loads(self.lock("status", "--vault", self.v, "--json").stdout)
        self.assertEqual((d["identity"]["kind"], d["identity"]["level"]), ("plugin-se", "L2"))
        self.ident.write_text("AGE-PLUGIN-YUBIKEY-1YKMATERIAL\n", encoding="utf-8")
        p = self.lock("status", "--vault", self.v)
        self.assertIn("plugin-yubikey, level L2", p.stdout)
        self.assertNotIn("MATERIAL", p.stdout + p.stderr)


class InProcessGate(LockBase):
    """The gate and the terminal rule, with gt_unlock_client mocked in process."""

    def setUp(self):
        super().setUp()
        self.assertOk(self.lock("add", self.v / "Secrets/creds.md", "--vault", self.v))
        sys.path.insert(0, str(SCRIPTS))
        import gt_lock
        import gt_unlock_client
        self.L, self.C = gt_lock, gt_unlock_client
        env = mock.patch.dict(os.environ, {"GT_AGE_IDENTITY": str(self.ident)})
        env.start()
        self.addCleanup(env.stop)
        fake = write_fake_age(self.tmp)
        p = mock.patch.object(gt_lock, "age_argv", lambda: [PYTHON, str(fake)])
        p.start()
        self.addCleanup(p.stop)

    def run_open(self, kind="pipe", *extra):
        r, w = os.pipe()
        try:
            with mock.patch.object(self.L, "stdout_kind", lambda: kind), \
                    mock.patch.object(self.L, "stdout_fd", lambda: w):
                rc = self.L.main(["open", str(self.v / "Secrets/creds.md.age"),
                                  "--vault", str(self.v)] + list(extra))
        finally:
            os.close(w)
        with os.fdopen(r, "rb") as fh:
            return rc, fh.read()

    def test_a_terminal_is_refused_without_show(self):
        off = mock.patch.object(self.C, "enabled", lambda h=None: False)
        off.start()
        self.addCleanup(off.stop)
        rc, out = self.run_open("tty")
        self.assertEqual((rc, out), (1, b""))
        rc, out = self.run_open("tty", "--show")
        self.assertEqual((rc, out), (0, PLAIN.encode()))

    def test_unlock_on_and_refused_decrypts_nothing(self):
        refused = {"allowed": False, "code": "locked", "message": "unlock first",
                   "grant": None, "level": "L2", "hints": ["gt_unlock.py unlock"]}
        with mock.patch.object(self.C, "enabled", lambda h=None: True), \
                mock.patch.object(self.C, "check", return_value=refused) as chk:
            rc, out = self.run_open()
        self.assertEqual((rc, out), (3, b""))
        args, kw = chk.call_args
        self.assertEqual(args[0], "gt:lock:Secrets")
        self.assertTrue(kw["request"])

    def test_unlock_on_and_granted_opens(self):
        ok = {"allowed": True, "code": "granted", "message": "", "grant": "g1", "level": "L2",
              "hints": []}
        with mock.patch.object(self.C, "enabled", lambda h=None: True), \
                mock.patch.object(self.C, "check", return_value=ok):
            rc, out = self.run_open()
        self.assertEqual((rc, out), (0, PLAIN.encode()))

    def test_restore_is_gated_too(self):
        refused = {"allowed": False, "code": "locked", "message": "unlock first",
                   "grant": None, "level": "L2", "hints": []}
        with mock.patch.object(self.C, "enabled", lambda h=None: True), \
                mock.patch.object(self.C, "check", return_value=refused):
            rc = self.L.main(["restore", str(self.v / "Secrets/creds.md.age"),
                              "--vault", str(self.v)])
        self.assertEqual(rc, 3)
        self.assertFalse((self.v / "Secrets/creds.md").exists())
        self.assertEqual([n for n in os.listdir(self.v / "Secrets") if ".tmp-" in n], [])


class RealAuthorityGate(AuthorityCase):
    """A real authority on a real socket, unlock ON, a policy that denies gt:lock:*: the CLI
    asks the authority and is refused (exit 3) before age is ever run. (A deny rule answers
    at once; the default policy would wait out a TOTP prompt nobody answers.)"""

    def test_open_refused_by_the_authority(self):
        self.enrol_totp()
        self.enrol_platform()
        self.set_policy(scopes={"gt:lock:*": "deny"})
        tmp = tempfile.mkdtemp(prefix="gtl-")
        self.addCleanup(shutil.rmtree, tmp, True)
        vault = Path(tmp) / "vault"
        (vault / "Secrets").mkdir(parents=True)
        (vault / "Secrets" / "x.md.age").write_bytes(b"age-encryption.org/v1\n-> age1x\n---\n")
        ident = Path(tmp) / "keys.txt"
        ident.write_text(KEY + "\n", encoding="utf-8")
        home = Path(tmp) / "home"
        (home / ".claude").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "GT_"))}
        env.update(UNLOCK_TEST_ENV)            # never a real prompt, never a started daemon
        env.update({"HOME": str(home), "USERPROFILE": str(home), "GT_UNLOCK_HOME": self.home,
                    # the in-process test authority (gt_unlock_client verifies its server)
                    "GT_UNLOCK_TEST_SERVER_PID": str(os.getpid()),
                    "GT_AGE_IDENTITY": str(ident), "GT_AGE": str(Path(tmp) / "no-age")})
        p = subprocess.run([PYTHON, str(LOCK), "open", str(vault / "Secrets" / "x.md.age"),
                            "--vault", str(vault)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("gt:lock:Secrets", p.stderr)
        self.assertIn("(denied)", p.stderr)                 # the authority itself said no
        self.assertEqual(p.stdout, "")


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"),
                     "the real age / age-keygen are not installed here")
class RealAge(Sandbox):
    def test_round_trip_through_real_age(self):
        v = self.tmp / "vault"
        (v / "Secrets").mkdir(parents=True)
        (v / "Secrets" / "n.md").write_bytes(PLAIN.encode())
        ident = self.tmp / "id.txt"
        subprocess.run([shutil.which("age-keygen"), "-o", str(ident)], check=True,
                       capture_output=True)
        self.env["GT_AGE"] = shutil.which("age")
        self.env["GT_AGE_IDENTITY"] = str(ident)
        self.assertOk(self.py(LOCK, "add", v / "Secrets" / "n.md", "--vault", v))
        self.assertFalse((v / "Secrets" / "n.md").exists())
        p = self.py(LOCK, "open", v / "Secrets" / "n.md.age", "--vault", v)
        self.assertOk(p)
        self.assertEqual(p.stdout, PLAIN)
        self.assertOk(self.py(LOCK, "restore", v / "Secrets" / "n.md.age", "--vault", v))
        self.assertEqual((v / "Secrets" / "n.md").read_bytes(), PLAIN.encode())
        self.assertFalse((v / "Secrets" / ".gt-locked").exists())


if __name__ == "__main__":
    unittest.main()
