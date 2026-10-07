#!/usr/bin/env python3
"""gt_sign.py -- a fingerprint on every commit (gt 0.20.3 part 3, owner 2026-10-06).

The setting `commit_fingerprint` (gt_settings.py) drives this. It guards the same repos as
`push_fingerprint` (`push_fingerprint_repos`).

  enroll [--force]   create the commit-signing key: a Secure Enclave P-256 key that needs a
                     currently enrolled finger for EVERY use (gt-presence, the helper behind gt
                     unlock's Touch ID). It is its own key, never the unlock presence key, so a
                     signature over commit data can never stand in for an unlock approval.
  public             print the key as an OpenSSH public key line (what GitHub registers).
  on [REPO ...]      enroll if needed, then set each repo to sign every commit and tag with it
                     (gpg.format ssh, gpg.ssh.program = this script's wrapper, user.signingkey,
                     commit.gpgsign, tag.gpgsign, gpg.ssh.allowedSignersFile). The values that were
                     there are saved, for `off`.
  off                put every saved value back exactly (unset what was unset).
  check [--live]     prove it; --live also signs a sample (one Touch ID) and verifies it with
                     OpenSSH against the allowed-signers file.
  github [--apply|--remove] [--yes]
                     the server half, the part that makes this SECURE rather than a habit: the key
                     registered as a GitHub signing key, and a repository ruleset requiring signed
                     commits on the default branch with NO bypass actors (an admin bypass would
                     let an unsigned push through with a notice). Without --apply/--remove it only
                     prints the plan. Asks before changing anything unless --yes.

As git's signing program (`gt_sign.py -Y sign -n git -f KEYFILE [-U] FILE`): when KEYFILE is the
enrolled key, it builds an SSHSIG (PROTOCOL.sshsig: sha512 of the data, namespace from -n) and the
Secure Enclave signs it after ONE Touch ID prompt naming the repo and the commit subject. The
signature is VERIFIED here under the enrolled public key -- and by OpenSSH when ssh-keygen is
present -- before FILE.sig is written; the helper is not trusted. Every other call (another key,
-Y verify, -Y find-principals, -Y check-novalidate) runs the real ssh-keygen unchanged.

Honest limits: it proves you approved each signature, not that the change is right -- an
approval given without reading defeats it. Every commit asks, so a rebase of ten commits is ten
touches. The trust anchor is the key GitHub holds: a same-user process can change local git
config, but cannot get a commit Verified without a finger on this Mac's sensor. A fingerprint
added or removed kills the key (biometryCurrentSet): enroll again and register the new key, and
keep the old one registered so past commits stay Verified. macOS only for now.

State: ~/.claude/golden-thread/commit-sign.json (the key record: public key, SE-wrapped handle),
commit-sign-state.json (saved git config, what gt changed on GitHub).
Exit codes: 0 ok, 1 refused / check failed, 2 usage, 3 not available here.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)     # -I leaves the script's own directory off sys.path

KEY_TYPE = b"ecdsa-sha2-nistp256"
CURVE = b"nistp256"
RULESET_NAME = "gt commit_fingerprint: signed commits on the default branch"
GIT_KEYS = ("gpg.format", "gpg.ssh.program", "user.signingkey", "commit.gpgsign", "tag.gpgsign",
            "gpg.ssh.allowedSignersFile")
WRAPPER_MARK = "# gt commit_fingerprint"


# ---------------------------------------------------------------- paths and state

def _home():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread")


def _record_path():
    return os.path.join(_home(), "commit-sign.json")


def _state_path():
    return os.path.join(_home(), "commit-sign-state.json")


def _signers_path():
    return os.path.join(_home(), "commit-sign.allowed_signers")


def _wrapper_path():
    return os.path.join(_home(), "bin", "gt-sign")


def _pubkey_path():
    # user.signingkey points at this file, not at a literal `key::` value: given a literal key,
    # git writes it to $TMPDIR/.git_signing_key_tmpXXXXXX on every signature and Apple git 2.50
    # leaves each one behind (beta test run, 2026-10-06)
    return os.path.join(_home(), "commit-sign.pub")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".gt-sign.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path, obj):
    _write_private(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _test_keys():
    # ONLY the test suite sets this: non-biometric Secure Enclave keys that can never prompt.
    return os.environ.get("GT_SIGN_TEST_KEYS") == "1"


# ---------------------------------------------------------------- SSH wire format

def _s(b):
    return struct.pack(">I", len(b)) + b


def _mpint(i):
    b = i.to_bytes((i.bit_length() + 7) // 8, "big") if i else b""
    if b and b[0] & 0x80:
        b = b"\0" + b
    return _s(b)


def ssh_public_blob(q65):
    return _s(KEY_TYPE) + _s(CURVE) + _s(bytes(q65))


def ssh_public_line(q65, comment="gt-commit-sign"):
    return "%s %s %s" % (KEY_TYPE.decode(), base64.b64encode(ssh_public_blob(q65)).decode(),
                         comment)


def parse_public_line(text):
    """-> the key blob of the first OpenSSH public key line in `text`, or None."""
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and not line.startswith("#"):
            try:
                return base64.b64decode(parts[1], validate=True)
            except ValueError:
                return None
    return None


def sshsig_signed_data(message, namespace):
    """What the key signs (PROTOCOL.sshsig): magic, namespace, reserved, hash alg, H(message)."""
    return (b"SSHSIG" + _s(namespace.encode()) + _s(b"") + _s(b"sha512")
            + _s(hashlib.sha512(message).digest()))


def sshsig_armor(q65, namespace, r, s):
    sig = _s(KEY_TYPE) + _s(_mpint(r) + _mpint(s))
    blob = (b"SSHSIG" + struct.pack(">I", 1) + _s(ssh_public_blob(q65)) + _s(namespace.encode())
            + _s(b"") + _s(b"sha512") + _s(sig))
    b64 = base64.b64encode(blob).decode()
    body = "\n".join(b64[i:i + 70] for i in range(0, len(b64), 70))
    return "-----BEGIN SSH SIGNATURE-----\n%s\n-----END SSH SIGNATURE-----\n" % body


# ---------------------------------------------------------------- the Secure Enclave key

def _factor():
    import gt_unlock_touchid as T                              # noqa: PLC0415
    return T, T.TouchIdFactor(helper=os.environ.get("GT_SIGN_HELPER") or None,
                              insecure_test_keys=_test_keys())


def load_record():
    rec = _read_json(_record_path())
    return rec if rec.get("public") and rec.get("blob") else None


def enroll(force=False):
    """Create the signing key. -> (record, message). Creating a key never prompts."""
    if sys.platform != "darwin" and not _test_keys():
        raise SignError(3, "commit signing with a fingerprint is macOS only in this release "
                           "(Windows Hello is planned)")
    old = load_record()
    if old and not force:
        return old, "already enrolled"
    T, fac = _factor()
    import gt_unlock_factors as F                              # noqa: PLC0415
    try:
        pub, blob = fac._create("sign", F.Context(home=_home(), reason="create the gt commit-"
                                                  "signing key"))
    except F.FactorError as e:
        raise SignError(3, "the Secure Enclave key could not be created: %s" % e.message)
    if old:
        # keep the old record: its public key must stay registered so past commits stay Verified
        _write_json(_record_path()[:-5] + ".%s.json" % time.strftime("%Y%m%d%H%M%S"), old)
    rec = {"schema": 1, "public": T._b64(pub), "blob": T._b64(blob),
           "helper": fac.helper_path(), "biometric": not _test_keys(),
           "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    _write_json(_record_path(), rec)
    return rec, "enrolled a new Secure Enclave signing key"


def _pub(rec):
    return base64.b64decode(rec["public"])


class SignError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def se_sign(rec, data, reason):
    """DER ECDSA signature of `data` by the enrolled key, VERIFIED here before it is returned."""
    import gt_unlock_crypto as C                               # noqa: PLC0415
    import gt_unlock_factors as F                              # noqa: PLC0415
    bio = rec.get("biometric", True)
    if bio is not True and not _test_keys():
        raise SignError(1, "the commit-signing key is not biometric; enroll again")
    T, fac = _factor()
    try:
        out = fac._call("sign", {"blob": rec["blob"], "challenge": T._b64(data), "reason": reason,
                                 "biometric": bool(bio)}, rec, timeout=F.PROMPT_TIMEOUT_S + 15)
    except F.FactorError as e:
        raise SignError(1, "not signed (%s): %s" % (e.code, e.message))
    try:
        der = base64.b64decode(out.get("signature") or "", validate=True)
    except ValueError:
        der = b""
    # SECURITY INVARIANT: the helper is not trusted -- only a signature that verifies under the
    # ENROLLED key over exactly these bytes is written out.
    if not der or not C.es256_verify(_pub(rec), data, der):
        raise SignError(1, "the Secure Enclave signature does not verify; nothing was written")
    return C.der_ecdsa_sig(der)


# ---------------------------------------------------------------- git's signing program

def _real_ssh_keygen():
    p = os.environ.get("GT_SIGN_SSH_KEYGEN") or shutil.which("ssh-keygen")
    if p and os.path.basename(p) != "gt-sign":
        return p
    return "/usr/bin/ssh-keygen" if os.path.exists("/usr/bin/ssh-keygen") else None


def _subject(data):
    text = data.decode("utf-8", "replace")
    msg = text.split("\n\n", 1)[1] if "\n\n" in text else text
    first = next((l for l in msg.splitlines() if l.strip()), "")
    first = re.sub(r"[\x00-\x1f\x7f]", " ", first).strip()
    return first[:72] + ("..." if len(first) > 72 else "")


def _repo_name():
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return os.path.basename(p.stdout.strip()) if p.returncode == 0 else os.path.basename(os.getcwd())


def _parse_sign_args(argv):
    """-> (namespace, keyfile, [files]) for `-Y sign -n NS -f KEY [-U] FILE...`, else None."""
    if argv[:2] != ["-Y", "sign"]:
        return None
    ns, key, files, i = None, None, [], 2
    while i < len(argv):
        a = argv[i]
        if a in ("-n", "-f", "-O"):
            if i + 1 >= len(argv):
                return None
            if a == "-n":
                ns = argv[i + 1]
            elif a == "-f":
                key = argv[i + 1]
            i += 2
            continue
        if a == "-U":
            i += 1
            continue
        if a.startswith("-"):
            return None
        files.append(a)
        i += 1
    if not ns or not key or not files:
        return None
    return ns, key, files


def _openssh_check(sig_text, message, namespace):
    """True/False from `ssh-keygen -Y check-novalidate`, None when there is no ssh-keygen."""
    kg = _real_ssh_keygen()
    if not kg:
        return None
    with tempfile.TemporaryDirectory(prefix="gt-sign-check.") as d:
        sp = os.path.join(d, "msg.sig")
        with open(sp, "w", encoding="utf-8", newline="\n") as f:
            f.write(sig_text)
        r = subprocess.run([kg, "-Y", "check-novalidate", "-n", namespace, "-s", sp],
                           input=message, capture_output=True)
    return r.returncode == 0


def sign_as_ssh_keygen(argv):
    parsed = _parse_sign_args(argv)
    rec = load_record()
    if parsed and rec:
        ns, keyfile, files = parsed
        try:
            with open(keyfile, encoding="utf-8") as f:
                blob = parse_public_line(f.read())
        except OSError:
            blob = None
        if blob == ssh_public_blob(_pub(rec)):
            for path in files:
                with open(path, "rb") as f:
                    message = f.read()
                reason = "sign a git commit in %s: %s" % (_repo_name(), _subject(message) or "?")
                try:
                    r, s = se_sign(rec, sshsig_signed_data(message, ns), reason)
                except SignError as e:
                    sys.stderr.write("gt_sign: %s\n" % e.message)
                    return 1
                text = sshsig_armor(_pub(rec), ns, r, s)
                if _openssh_check(text, message, ns) is False:
                    sys.stderr.write("gt_sign: OpenSSH does not accept the signature; nothing "
                                     "was written\n")
                    return 1
                _write_private(path + ".sig", text)
            return 0
    kg = _real_ssh_keygen()
    if not kg:
        sys.stderr.write("gt_sign: ssh-keygen not found\n")
        return 1
    return subprocess.call([kg] + argv)


# ---------------------------------------------------------------- per-repo git config

def _git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)


def _git_get(repo, key):
    p = _git(repo, "config", "--local", "--get", key)
    return p.stdout.rstrip("\n") if p.returncode == 0 else None


def _settings_repos():
    cfg = os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json")
    raw = _read_json(cfg).get("push_fingerprint_repos") or ""
    return [os.path.abspath(os.path.expanduser(r.strip())) for r in raw.split(",") if r.strip()]


def _write_wrapper():
    stable = os.path.join(_home(), "hooks", "gt_sign.py")
    script = stable if os.path.isfile(stable) else os.path.abspath(__file__)
    path = _wrapper_path()
    _write_private(path, "#!/bin/sh\n%s (git's ssh signing program; remove with: gt_settings.py "
                         "set commit_fingerprint off)\nexec \"%s\" -I \"%s\" \"$@\"\n"
                   % (WRAPPER_MARK, sys.executable, script))
    os.chmod(path, 0o700)
    return path


def _write_pubkey(rec):
    _write_private(_pubkey_path(), ssh_public_line(_pub(rec)) + "\n")


def _write_signers(rec, emails):
    line = ssh_public_line(_pub(rec)).rsplit(" ", 1)[0]
    rows = ['%s namespaces="git" %s' % (e, line) for e in sorted(set(emails))]
    _write_private(_signers_path(), "\n".join(rows) + "\n")


def wanted_config(rec):
    return {"gpg.format": "ssh", "gpg.ssh.program": _wrapper_path(),
            "user.signingkey": _pubkey_path(),
            "commit.gpgsign": "true", "tag.gpgsign": "true",
            "gpg.ssh.allowedSignersFile": _signers_path()}


def _email(repo):
    p = _git(repo, "config", "--get", "user.email")
    return p.stdout.strip() if p.returncode == 0 else ""


def cmd_on(repos):
    repos = repos or _settings_repos()
    if not repos:
        print("refused: no repository to sign in. Name it first:\n"
              "  gt_settings.py set push_fingerprint_repos /path/to/repo[,/another]")
        return 1
    bad = []
    for r in repos:
        if _git(r, "rev-parse", "--git-dir").returncode != 0:
            bad.append("%s is not a git repository" % r)
        elif not _email(r):
            bad.append("%s has no user.email (GitHub matches a signature to it)" % r)
    if bad:
        print("refused, nothing changed:\n  " + "\n  ".join(bad))
        return 1
    try:
        rec, msg = enroll()
    except SignError as e:
        print("refused: %s" % e.message)
        return e.code
    print(msg)
    state = _read_json(_state_path())
    saved = state.setdefault("repos", {})
    _write_wrapper()
    _write_pubkey(rec)
    _write_signers(rec, [_email(r) for r in repos] + [_email(r) for r in saved])
    want = wanted_config(rec)
    for r in repos:
        if r not in saved:
            saved[r] = {k: _git_get(r, k) for k in GIT_KEYS}
        for k, v in want.items():
            _git(r, "config", "--local", k, v)
        print("signing every commit and tag: %s" % r)
    _write_json(_state_path(), state)
    print("commit_fingerprint on: each commit asks for Touch ID. Public key (register it on GitHub "
          "as a SIGNING key):\n  %s" % ssh_public_line(_pub(rec)))
    print("the server half: gt_sign.py github   (shows the plan; --apply asks before changing it)")
    return 0


def cmd_off():
    state = _read_json(_state_path())
    for r, saved in (state.get("repos") or {}).items():
        if _git(r, "rev-parse", "--git-dir").returncode != 0:
            print("skipped (no longer a repository): %s" % r)
            continue
        for k in GIT_KEYS:
            v = (saved or {}).get(k)
            if v is None:
                _git(r, "config", "--local", "--unset-all", k)
            else:
                _git(r, "config", "--local", k, v)
        print("signing config restored: %s" % r)
    for p in (_wrapper_path(), _signers_path(), _pubkey_path()):
        try:
            os.unlink(p)
        except OSError:
            pass
    gh_state = state.get("github") or {}
    state["repos"] = {}
    if gh_state:
        _write_json(_state_path(), state)
        print("the GitHub half is still in place (%s). Remove it with:\n"
              "  gt_sign.py github --remove"
              % ", ".join(sorted(k for k in gh_state if not k.startswith("_signing_key")) or ["signing key"]))
    else:
        try:
            os.unlink(_state_path())
        except OSError:
            pass
    print("commit_fingerprint off: commits are no longer signed (the key is kept; "
          "`gt_sign.py enroll --force` replaces it)")
    return 0


# ---------------------------------------------------------------- GitHub (the server half)

def _gh_bin():
    env = os.environ.get("GT_SIGN_GH")
    if env is not None:
        return env if os.path.isfile(env) else None
    return shutil.which("gh")


def _gh(host, *args, body=None):
    gh = _gh_bin()
    p = subprocess.run([gh, "api", "--hostname", host] + list(args)
                       + (["--input", "-"] if body is not None else []),
                       input=json.dumps(body) if body is not None else None,
                       capture_output=True, text=True)
    try:
        data = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        data = None
    return p.returncode, data


# Any GitHub host (0.20.4): the host comes from the remote, so a GitHub Enterprise repo gets its
# server half too. State keeps the 0.20.3 beta's shape for github.com ("owner/repo",
# "_signing_key") and qualifies every other host ("host/owner/repo", "_signing_key@host").
REMOTE_RE = re.compile(r"^(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/\s]+@)?([^/:\s]+)"
                       r"(?::\d+)?[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def _remote(repo):
    """-> (host, "owner/repo") for origin, else None."""
    p = _git(repo, "remote", "get-url", "origin")
    m = REMOTE_RE.match(p.stdout.strip())
    return (m.group(1).lower(), "%s/%s" % (m.group(2), m.group(3))) if m else None


def _slot(host, slug):
    return slug if host == "github.com" else "%s/%s" % (host, slug)


def _unslot(slot):
    parts = slot.split("/")
    return ("github.com", slot) if len(parts) == 2 else (parts[0], "/".join(parts[1:]))


def _key_slot(host):
    return "_signing_key" if host == "github.com" else "_signing_key@" + host


def _our_ruleset(host, slug):
    rc, data = _gh(host, "repos/%s/rulesets" % slug)
    if rc != 0 or not isinstance(data, list):
        return rc, None
    for rs in data:
        if isinstance(rs, dict) and rs.get("name") == RULESET_NAME:
            return 0, rs
    return 0, None


def _our_key(host, rec):
    rc, data = _gh(host, "user/ssh_signing_keys")
    if rc != 0 or not isinstance(data, list):
        return rc, None
    want = base64.b64encode(ssh_public_blob(_pub(rec))).decode()
    for k in data:
        if isinstance(k, dict) and want in (k.get("key") or ""):
            return 0, k
    return 0, None


def ruleset_body():
    return {"name": RULESET_NAME, "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": [{"type": "required_signatures"}], "bypass_actors": []}


def _confirm(lines, yes):
    print("\n".join(lines))
    if yes:
        return True
    if not sys.stdin.isatty():
        print("not applied: no terminal to ask on. Re-run with --yes once you have read the plan.")
        return False
    return input("apply these changes on GitHub? [y/N] ").strip().lower() in ("y", "yes")


def cmd_github(mode, yes, repos):
    if not _gh_bin():
        print("gh is not installed: register the key and add the ruleset by hand (see MANUAL)")
        return 3
    rec = load_record()
    if not rec:
        print("no commit-signing key: turn commit_fingerprint on first")
        return 1
    state = _read_json(_state_path())
    repos = repos or list((state.get("repos") or {}).keys()) or _settings_repos()
    targets = sorted({x for x in (_remote(r) for r in repos) if x})   # (host, slug)
    gh_state = state.setdefault("github", {})
    if mode == "remove":
        # what gt added is recorded by repo and host, so it can be undone after `off` too
        targets = sorted(_unslot(k) for k in gh_state if not k.startswith("_signing_key"))
        if not targets:
            print("nothing gt added on GitHub is recorded; nothing to remove")
            return 0
    if not targets:
        print("no GitHub remote (origin) in: %s" % ", ".join(repos))
        return 1
    keys = {}
    for host in sorted({h for h, _s in targets}):
        rc, keys[host] = _our_key(host, rec)
        if rc != 0:
            print("gh could not list your signing keys on %s. It needs the admin:ssh_signing_key "
                  "scope:\n  gh auth refresh -h %s -s admin:ssh_signing_key" % (host, host))
            return 1
    plan, todo = [], []
    if mode == "remove":
        # the signing KEY stays registered: commits it signed must stay Verified. Removing it is a
        # deliberate step on the server (Settings > SSH and GPG keys), never a side effect.
        for host in sorted(keys):
            k = gh_state.get(_key_slot(host)) or {}
            if k.get("id"):
                plan.append("  signing key %s on %s stays registered (past commits stay Verified)"
                            % (k["id"], host))
        for host, slug in targets:
            rid = (gh_state.get(_slot(host, slug)) or {}).get("ruleset_id")
            if rid:
                plan.append("  %s: delete ruleset %s (%s)" % (_slot(host, slug), rid, RULESET_NAME))
                todo.append((host, "DELETE", "repos/%s/rulesets/%s" % (slug, rid), None,
                             _slot(host, slug)))
    else:
        for host in sorted(keys):
            if keys[host]:
                plan.append("  signing key on %s: already registered (id %s)"
                            % (host, keys[host].get("id")))
            else:
                plan.append("  add signing key on %s \"gt commit-sign %s\": %s"
                            % (host, os.uname().nodename, ssh_public_line(_pub(rec))))
                todo.append((host, "POST", "user/ssh_signing_keys",
                             {"title": "gt commit-sign %s" % os.uname().nodename,
                              "key": ssh_public_line(_pub(rec)).rsplit(" ", 1)[0]},
                             _key_slot(host)))
        for host, slug in targets:
            name = _slot(host, slug)
            rc, rs = _our_ruleset(host, slug)
            if rc != 0:
                plan.append("  %s: cannot read rulesets (needs admin on the repo, and a server "
                            "with repository rulesets)" % name)
            elif rs:
                plan.append("  %s: ruleset already there (id %s)" % (name, rs.get("id")))
            else:
                plan.append("  %s: add ruleset \"%s\": required_signatures on ~DEFAULT_BRANCH, "
                            "no bypass actors" % (name, RULESET_NAME))
                todo.append((host, "POST", "repos/%s/rulesets" % slug, ruleset_body(), name))
    if mode == "plan" or not todo:
        print("\n".join(["GitHub plan:"] + (plan or ["  nothing recorded to change"])))
        if mode == "plan" and todo:
            print("apply with: gt_sign.py github --apply")
        return 0
    if not _confirm(["GitHub changes:"] + plan, yes):
        return 1
    failed = 0
    for host, method, path, body, slot in todo:
        rc, data = _gh(host, "-X", method, path, body=body)
        if rc != 0:
            failed += 1
            print("FAILED %s %s" % (method, path))
            continue
        if method == "POST" and isinstance(data, dict):
            gh_state[slot] = ({"id": data.get("id")} if slot.startswith("_signing_key")
                              else {"ruleset_id": data.get("id")})
        else:
            gh_state.pop(slot, None)
        print("done   %s %s" % (method, path))
    if not [k for k in gh_state if not k.startswith("_signing_key")] and mode == "remove":
        for k in [k for k in gh_state if k.startswith("_signing_key")]:
            gh_state.pop(k, None)                 # nothing left that gt must undo
    if not gh_state:
        state.pop("github", None)
    _write_json(_state_path(), state)
    return 1 if failed else 0


# ---------------------------------------------------------------- check

def cmd_check(live):
    rec, ok, lines = load_record(), True, []
    lines.append(("commit-signing key enrolled", bool(rec)))
    if rec:
        lines.append(("key needs a finger (biometric)", rec.get("biometric") is True or _test_keys()))
    state = _read_json(_state_path())
    repos = list((state.get("repos") or {}).keys())
    lines.append(("at least one signing repo", bool(repos)))
    want = wanted_config(rec) if rec else {}
    for r in repos:
        lines.append(("%s signs with the key" % r,
                      all(_git_get(r, k) == v for k, v in want.items())))
    lines.append(("signing program installed", os.path.isfile(_wrapper_path())))
    for slot, v in sorted((state.get("github") or {}).items()):
        if not slot.startswith("_signing_key") and _gh_bin():
            rc, rs = _our_ruleset(*_unslot(slot))
            lines.append(("%s ruleset requires signatures, no bypass" % slot,
                          bool(rs) and rs.get("enforcement") == "active"
                          and not rs.get("bypass_actors")))
    if live and rec:
        msg = b"tree 0\n\ngt_sign check --live\n"
        try:
            r, s = se_sign(rec, sshsig_signed_data(msg, "git"), "check gt commit signing")
            lines.append(("live signature verified by OpenSSH",
                          _openssh_check(sshsig_armor(_pub(rec), "git", r, s), msg, "git")
                          is not False))
        except SignError as e:
            lines.append(("live signature (%s)" % e.message, False))
    for what, good in lines:
        print("%s  %s" % ("PASS" if good else "FAIL", what))
        ok = ok and good
    return 0 if ok else 1


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if a[:1] == ["-Y"]:
        return sign_as_ssh_keygen(a)
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if a else 2
    cmd, rest = a[0], a[1:]
    flags = [x for x in rest if x.startswith("--")]
    args = [os.path.abspath(x) for x in rest if not x.startswith("-")]
    try:
        if cmd == "enroll":
            rec, msg = enroll(force="--force" in flags)
            print(msg)
            print(ssh_public_line(_pub(rec)))
            return 0
        if cmd == "public":
            rec = load_record()
            if not rec:
                print("no commit-signing key: gt_sign.py enroll")
                return 1
            print(ssh_public_line(_pub(rec)))
            return 0
    except SignError as e:
        print("refused: %s" % e.message)
        return e.code
    if cmd == "on":
        return cmd_on(args)
    if cmd == "off":
        return cmd_off()
    if cmd == "check":
        return cmd_check("--live" in flags)
    if cmd == "github":
        mode = "remove" if "--remove" in flags else "apply" if "--apply" in flags else "plan"
        return cmd_github(mode, "--yes" in flags, args)
    print("usage: gt_sign.py enroll [--force] | public | on [REPO ...] | off | check [--live] | "
          "github [--apply|--remove] [--yes]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
