#!/usr/bin/env python3
"""gt_unlock -- prove you are present before agents may use what you protect (0.20.0).

Unlock is OFF by default. Turned on, gt's unlock authority (gt_unlockd.py) decides which
processes may use LOTR connections, sealed credentials, publish credentials and gt's own
security settings, after you prove presence with TOTP plus Touch ID (macOS) or Windows Hello,
and optionally Microsoft Entra ID sign-in. Read SECURITY.md first: it says exactly what each
level stops and what it does not.

    gt_unlock.py status [--json]
    gt_unlock.py enroll totp [--account NAME]
    gt_unlock.py enroll touchid|hello|sso
    gt_unlock.py unenroll FACTOR
    gt_unlock.py recovery
    gt_unlock.py unlock [--scope S] [--reason R] [--session PID] [--recovery]
    gt_unlock.py lock
    gt_unlock.py check --scope S [--request] [--reason R] [--job NAME]
    gt_unlock.py run --scope S [--secret-file VAR=REF] [--env VAR=REF] -- CMD [ARGS...]
    gt_unlock.py register --session [--session-id ID]
    gt_unlock.py register --shim
    gt_unlock.py revoke
    gt_unlock.py hook session-start|session-end
    gt_unlock.py policy show [--json]
    gt_unlock.py policy enable|disable|approve
    gt_unlock.py policy set FILE
    gt_unlock.py policy consent none|platform [--window SECONDS]
    gt_unlock.py policy unattended add|remove JOB SCOPE
    gt_unlock.py policy sso --client-id ID [--tenant T] [--pin-subject OID]
    gt_unlock.py seal put NAME            (the value is read from stdin, never argv)
    gt_unlock.py seal list
    gt_unlock.py seal rm NAME
    gt_unlock.py seal migrate REF --name NAME [--remove-source]
    gt_unlock.py secret get REF [--out FILE]
    gt_unlock.py git-credential get|store|erase
    gt_unlock.py aws-credential --ref REF
    gt_unlock.py audit [-n N]
    gt_unlock.py daemon start|stop|status
    gt_unlock.py verify [--json]

`check` exits 0 allowed, 10 locked (unlock first), 11 a fresh confirmation (step-up) is
needed, 12 refused (policy, the mcp_only door, failed closed, or no authority). Every other
command exits 0 on success, 1 when the authority refused, 2 on a usage error.

Secret values are never printed to a terminal, never put in argv, and never logged. `secret
get` writes to a pipe or to --out (mode 600); `run --secret-file` hands the command a path to a
600 file that is deleted when it exits; `run --env` puts a value in the environment, which any
process of yours can read (`ps eww`), and says so.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_ipc                         # noqa: E402
import gt_unlock_client as C          # noqa: E402
import gt_unlock_policy as P          # noqa: E402

EXIT = {"allowed": 0, "locked": 10, "step_up": 11, "refused": 12}


def _out(obj, as_json=True):
    sys.stdout.write((json.dumps(obj, indent=2) if as_json else str(obj)) + "\n")


def _err(e):
    msg = getattr(e, "message", str(e))
    sys.stderr.write("gt_unlock: %s\n" % msg)
    for h in getattr(e, "hints", []) or []:
        sys.stderr.write("  hint: %s\n" % h)
    return 1


def _call(method, params=None, **kw):
    params = dict(params or {})
    params.setdefault("tty", bool(sys.stdin and sys.stdin.isatty()))
    return C.call(method, params, answer=C.tty_answer, **kw)


# ------------------------------------------------------------------------- status

def cmd_status(ns):
    if not C.enabled():
        info = {"enabled": False, "assurance": {"level": "off",
                                                 "why": "unlock is off (the default)"}}
        try:
            info = _call("status", start=False)
        except gt_ipc.IpcError:
            pass
        if ns.json:
            _out(info)
        else:
            print("unlock: off -- agents use LOTR and gt's settings without asking you.")
            print("  turn it on: gt_unlock.py enroll %s, then gt_unlock.py policy enable"
                  % ("touchid" if sys.platform == "darwin" else
                     "hello" if os.name == "nt" else "totp"))
            print("  what it does and does not protect: SECURITY.md")
        return 0
    try:
        st = _call("status")
    except gt_ipc.IpcError as e:
        if ns.json:
            _out({"enabled": True, "reachable": False, "error": e.to_dict()})
        else:
            print("unlock: ON, but the authority is not reachable -- every gated scope is "
                  "LOCKED (fail closed). Start it: gt_unlock.py daemon start")
        return 1
    if ns.json:
        _out(st)
        return 0
    a = st["assurance"]
    print("unlock: ON · level %s -- %s" % (a["level"], a["why"]))
    for p in st.get("problems") or []:
        print("  ! %s" % p)
    print("  factors (need %s%s):" % (st.get("required"),
                                      ", one of " + "/".join(st["require_one_of"])
                                      if st.get("require_one_of") else ""))
    for name, f in sorted(st["factors"].items()):
        print("    %-9s %s · %s" % (name, "enrolled" if f["enrolled"] else "not enrolled",
                                    ("available" if f["available"] else "unavailable: " +
                                     f["why"])))
    print("  door: %s · grants live: %d · sealed credentials: %d · admin floor: %s"
          % (st.get("door"), st.get("grants", 0), len(st.get("sealed") or []),
             "yes" if st.get("admin_floor") else "no"))
    if st.get("needs_reenrol"):
        print("  ! a recovery code was used: re-enrol your factors")
    print("  It is not anti-malware: something already running as you can wait for you to "
          "unlock (SECURITY.md).")
    return 0


# ------------------------------------------------------------------------- enrolment

def _show_totp(uri):
    """Show the enrolment URI and a terminal QR code ONCE. The URI is the seed: it is printed
    to this terminal only, never logged."""
    import gt_unlock_totp as T
    import urllib.parse
    secret = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query).get("secret", [""])[0]
    try:
        import qrcodegen
        qr = qrcodegen.QrCode.encode_text(uri, qrcodegen.QrCode.Ecc.MEDIUM)
        border = 2
        rows = range(-border, qr.get_size() + border)
        lines = []
        for y in range(-border, qr.get_size() + border, 2):
            row = []
            for x in rows:
                top = qr.get_module(x, y)
                bot = qr.get_module(x, y + 1)
                row.append({(False, False): "\u2588", (True, False): "\u2584",
                            (False, True): "\u2580", (True, True): " "}[(top, bot)])
            lines.append("".join(row))
        print("\n".join(lines))
    except Exception:                       # noqa: BLE001 - the secret below still works
        print("(no QR code could be drawn here; type the secret instead)")
    print("\nScan it, or type this secret into your authenticator app (TOTP, SHA1, 6 digits, "
          "30 s):\n  %s\n" % " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))
    del T


def cmd_enroll(ns):
    try:
        if ns.factor == "totp":
            res = _call("enroll", {"factor": "totp", "phase": "begin", "account": ns.account})
            _show_totp(res["uri"])
            import getpass
            code = getpass.getpass("Enter the code your app now shows: ")
            _call("enroll", {"factor": "totp", "phase": "confirm", "code": code,
                             "account": ns.account})
            if sys.stdout.isatty():
                sys.stdout.write("\033[2J\033[H")          # clear the seed off the screen
            print("TOTP enrolled. Next: gt_unlock.py recovery (keep the codes offline).")
            return 0
        res = _call("enroll", {"factor": ns.factor})
        print("%s enrolled." % res.get("enrolled"))
        return 0
    except gt_ipc.IpcError as e:
        return _err(e)


def cmd_unenroll(ns):
    try:
        _call("unenroll", {"factor": ns.factor})
        print("%s removed." % ns.factor)
        return 0
    except gt_ipc.IpcError as e:
        return _err(e)


def cmd_recovery(ns):
    try:
        res = _call("recovery_new", {})
    except gt_ipc.IpcError as e:
        return _err(e)
    print("Ten one-time recovery codes. Each works once, counts as ONE factor, and makes you "
          "re-enrol. Store them offline; they are shown only now.\n")
    for c in res["codes"]:
        print("  " + c)
    return 0


# ------------------------------------------------------------------------- unlock / lock

def cmd_unlock(ns):
    params = {"scope": ns.scope or "", "reason": ns.reason or "", "recovery": ns.recovery}
    if ns.session:
        params["session_pid"] = ns.session
    try:
        res = _call("unlock", params)
    except gt_ipc.IpcError as e:
        return _err(e)
    if res.get("enabled") is False:
        print("unlock is off; nothing to unlock")
        return 0
    print("unlocked (grant %s…, factors %s; ends after %d min idle or %d h)" % (
        res["grant"][:8], ", ".join(res["factors"]), res["idle_s"] // 60, res["ttl_s"] // 3600))
    if res.get("needs_reenrol"):
        print("a recovery code was used: re-enrol your factors now")
    return 0


def cmd_lock(ns):
    try:
        res = C.call("lock", {}, start=False)
        print("locked (%d grant(s) revoked)" % res.get("revoked", 0))
    except gt_ipc.IpcError:
        print("locked (the authority is not running, so no grant exists)")
    return 0


def cmd_check(ns):
    v = C.check(ns.scope, request=ns.request, reason=ns.reason or "", job=ns.job,
                answer=C.tty_answer if sys.stdin and sys.stdin.isatty() else None)
    if v["allowed"]:
        return EXIT["allowed"]
    sys.stderr.write("gt_unlock: %s (%s)\n" % (v["message"], v["code"]))
    if v["code"] == "locked":
        return EXIT["locked"]
    if v["code"] == "step_up":
        return EXIT["step_up"]
    return EXIT["refused"]


def _resolve(ref, scope_reason):
    res = _call("secret", {"ref": ref, "request": True, "reason": scope_reason})
    return res["value"]


def cmd_run(ns):
    cmd = list(ns.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        sys.stderr.write("gt_unlock run: give a command after --\n")
        return 2
    v = C.check(ns.scope, request=True, reason="run %s" % os.path.basename(cmd[0]),
                answer=C.tty_answer if sys.stdin and sys.stdin.isatty() else None)
    if not v["allowed"]:
        sys.stderr.write("gt_unlock: %s (%s)\n" % (v["message"], v["code"]))
        return EXIT.get(v["code"], EXIT["refused"])
    env = dict(os.environ)
    files = []
    try:
        for spec in ns.secret_file or []:
            var, ref = spec.split("=", 1)
            fd, path = tempfile.mkstemp(prefix="gt-secret-")
            os.chmod(path, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(_resolve(ref, "run").encode("utf-8"))
            files.append(path)
            env[var] = path
        for spec in ns.env or []:
            var, ref = spec.split("=", 1)
            sys.stderr.write("gt_unlock: note: %s is passed in the environment, which any "
                             "process running as you can read (ps eww); --secret-file is "
                             "safer\n" % var)
            env[var] = _resolve(ref, "run")
        return subprocess.call(cmd, env=env)
    except gt_ipc.IpcError as e:
        return _err(e)
    except ValueError:
        sys.stderr.write("gt_unlock run: --secret-file/--env take VAR=REF\n")
        return 2
    finally:
        for p in files:
            try:
                os.unlink(p)
            except OSError:
                pass


# ------------------------------------------------------------------------- sessions / hooks

def cmd_register(ns):
    if not C.enabled():
        return 0
    try:
        if ns.shim:
            _out(C.call("register_shim", {}))
        else:
            _out(C.call("register_session", {"session_id": ns.session_id}))
        return 0
    except gt_ipc.IpcError as e:
        return _err(e)


def cmd_revoke(ns):
    if not C.enabled():
        return 0
    try:
        _out(C.call("revoke_session", {}, start=False))
    except gt_ipc.IpcError:
        pass
    return 0


def cmd_hook(ns):
    """SessionStart / SessionEnd. Silent and instant when unlock is off; never blocks a
    session from starting -- registration failing only means nothing is unlocked yet."""
    if not C.enabled():
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
    except ValueError:
        payload = {}
    sid = payload.get("session_id")
    try:
        if ns.event == "session-start":
            C.call("register_session", {"session_id": sid})
            sys.stdout.write(json.dumps({"systemMessage": "gt unlock: on (locked until you "
                                         "unlock; `gt_unlock.py status`)"}) + "\n")
        else:
            C.call("revoke_session", {}, start=False)
    except gt_ipc.IpcError:
        if ns.event == "session-start":
            sys.stdout.write(json.dumps({"systemMessage": "gt unlock: on, but the authority "
                                         "did not start -- gated actions will be refused "
                                         "(gt_unlock.py daemon start)"}) + "\n")
    return 0


# ------------------------------------------------------------------------- policy

def _current_user_policy():
    try:
        res = _call("policy_get")
        return res.get("user") or {}
    except gt_ipc.IpcError:
        path = os.path.join(C.home(), "policy.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}


def _set_policy(p):
    try:
        res = _call("policy_set", {"policy": p})
        print("policy saved (unlock %s)" % ("ON" if res.get("enabled") else "off"))
        return 0
    except gt_ipc.IpcError as e:
        return _err(e)


def cmd_policy(ns):
    a = ns.action
    if a == "show":
        try:
            res = _call("policy_get")
        except gt_ipc.IpcError as e:
            return _err(e)
        _out(res if ns.json else res.get("effective"))
        for p in res.get("problems") or []:
            sys.stderr.write("problem: %s\n" % p)
        return 0
    if a == "approve":
        try:
            _call("policy_approve")
            print("policy approved")
            return 0
        except gt_ipc.IpcError as e:
            return _err(e)
    p = _current_user_policy() or {}
    if a in ("enable", "disable"):
        if not p:
            p = {"schema": 1}
        p["enabled"] = (a == "enable")
        return _set_policy(p)
    if a == "set":
        if not ns.args:
            sys.stderr.write("gt_unlock policy set FILE\n")
            return 2
        try:
            with open(ns.args[0], "r", encoding="utf-8") as f:
                return _set_policy(json.load(f))
        except (OSError, ValueError) as e:
            sys.stderr.write("gt_unlock: cannot read %s: %s\n" % (ns.args[0], e))
            return 2
    if a == "consent":
        if not ns.args or ns.args[0] not in ("none", "platform"):
            sys.stderr.write("gt_unlock policy consent none|platform [--window SECONDS]\n")
            return 2
        p["consent_requires_factor"] = ns.args[0]
        if ns.window is not None:
            p["consent_window_s"] = ns.window
        return _set_policy(p)
    if a == "sso":
        if not ns.client_id:
            sys.stderr.write("gt_unlock policy sso --client-id ID [--tenant T] "
                             "[--pin-subject OID]\n")
            return 2
        sso = p.setdefault("sso", {})
        sso.update({"provider": "entra", "client_id": ns.client_id})
        if ns.tenant:
            sso["tenant"] = ns.tenant
        if ns.pin_subject:
            sso["pin_subject"] = ns.pin_subject
        return _set_policy(p)
    if a == "unattended":
        if len(ns.args) != 3 or ns.args[0] not in ("add", "remove"):
            sys.stderr.write("gt_unlock policy unattended add|remove JOB SCOPE\n")
            return 2
        op, job, scope = ns.args
        if op == "add" and not P.unattended_scope_ok(scope):
            sys.stderr.write("gt_unlock: %s can never be allowed unattended (write, consent, "
                             "publish, secrets-wide and gt scopes are refused)\n" % scope)
            return 2
        u = p.setdefault("unattended", {}).setdefault("allowed", [])
        u[:] = [e for e in u if (e.get("job"), e.get("scope")) != (job, scope)]
        if op == "add":
            u.append({"job": job, "scope": scope})
        return _set_policy(p)
    return 2


# ------------------------------------------------------------------------- sealed / secrets

def cmd_seal(ns):
    try:
        if ns.action == "list":
            _out(_call("seal_list"))
            return 0
        if ns.action == "rm":
            _out(_call("seal_rm", {"name": ns.name}))
            return 0
        if ns.action == "put":
            if sys.stdin.isatty():
                import getpass
                value = getpass.getpass("value for sealed:%s (not echoed): " % ns.name)
            else:
                value = sys.stdin.read()
                if value.endswith("\n"):
                    value = value[:-1]
            _call("seal_put", {"name": ns.name, "value": value})
            del value
            print("sealed:%s stored (encrypted under your platform key)" % ns.name)
            return 0
        if ns.action == "migrate":
            return _migrate(ns)
    except gt_ipc.IpcError as e:
        return _err(e)
    return 2


def _migrate(ns):
    """Move a file:/store:/keychain: credential into the sealed store. The value goes from
    the old store to the authority through this process's memory only; it is never printed.
    --remove-source then deletes the plaintext copy (file:/store:) or the keychain item."""
    import gt_unlock_brokers as B
    ref = ns.name_ref
    try:
        value = B.resolve(ref)
    except B.BrokerError as e:
        return _err(e)
    _call("seal_put", {"name": ns.name, "value": value})
    del value
    print("sealed:%s now holds %s -- point the connection at sealed:%s" % (ns.name, ref, ns.name))
    if ns.remove_source:
        try:
            B.remove_source(ref)
            print("removed the plaintext source %s" % ref)
        except B.BrokerError as e:
            sys.stderr.write("gt_unlock: the source was NOT removed: %s\n" % e.message)
            return 1
    return 0


def cmd_secret(ns):
    if not ns.out and sys.stdout.isatty():
        sys.stderr.write("gt_unlock: refusing to print a secret to a terminal; pipe it or use "
                         "--out FILE\n")
        return 2
    try:
        value = _resolve(ns.ref, "secret get")
    except gt_ipc.IpcError as e:
        return _err(e)
    if ns.out:
        fd = os.open(ns.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
                     0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(value.encode("utf-8"))
    else:
        sys.stdout.write(value)
        sys.stdout.flush()
    return 0


def _credentials_map():
    path = os.path.join(C.home(), "credentials.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("git") or []
    except (OSError, ValueError):
        return []


def cmd_git_credential(ns):
    """git's credential-helper protocol. Configure once:
        git config --global credential.https://github.com.helper \\
            "!python3 ~/.claude/golden-thread/hooks/gt_unlock.py git-credential"
    and map hosts to refs in <unlock home>/credentials.json:
        {"git": [{"protocol": "https", "host": "github.com", "username": "x-access-token",
                  "ref": "sealed:github"}]}
    `get` releases the token only under a gt:publish grant (prompting if needed);
    `store` and `erase` do nothing: the token's home is the sealed store, not git."""
    if ns.action != "get":
        sys.stdin.read()
        return 0
    want = {}
    for line in sys.stdin.read().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            want[k] = v
    for m in _credentials_map():
        if m.get("host") == want.get("host") and m.get("protocol", "https") == \
                want.get("protocol", "https"):
            v = C.check(m.get("scope") or "gt:publish", request=True,
                        reason="git %s://%s" % (want.get("protocol"), want.get("host")))
            if not v["allowed"]:
                sys.stderr.write("gt_unlock: %s (%s)\n" % (v["message"], v["code"]))
                return 1
            try:
                value = _resolve(m["ref"], "git credential")
            except gt_ipc.IpcError as e:
                return _err(e)
            sys.stdout.write("username=%s\npassword=%s\n\n" % (m.get("username") or
                                                              "x-access-token", value))
            return 0
    return 0                       # not ours: git tries its next helper


def cmd_aws_credential(ns):
    """AWS `credential_process`: the ref must hold the JSON AWS expects (Version 1,
    AccessKeyId, SecretAccessKey, optional SessionToken/Expiration)."""
    try:
        value = _resolve(ns.ref, "aws credential_process")
        json.loads(value)
    except gt_ipc.IpcError as e:
        return _err(e)
    except ValueError:
        sys.stderr.write("gt_unlock: %s does not hold the JSON credential_process expects\n"
                         % ns.ref)
        return 1
    sys.stdout.write(value)
    return 0


def cmd_audit(ns):
    try:
        res = _call("audit_tail", {"n": ns.n}, start=False)
    except gt_ipc.IpcError as e:
        return _err(e)
    for ln in res["lines"]:
        print(ln)
    return 0


def cmd_daemon(ns):
    if ns.action == "start":
        ok = C.start_daemon()
        print("authority running" if ok else "the authority did not start")
        return 0 if ok else 1
    if ns.action == "stop":
        try:
            C.call("stop", {}, start=False)
            print("authority stopping (every grant revoked)")
        except gt_ipc.IpcError:
            print("authority not running")
        return 0
    up = gt_ipc.alive_at(C.address(), timeout=1.0)
    print("authority %s at %s" % ("running" if up else "not running", C.address()))
    return 0 if up else 1


def cmd_verify(ns):
    import gt_unlock_verify as V
    return V.main(as_json=ns.json)


# ------------------------------------------------------------------------- argv

def build_parser():
    ap = argparse.ArgumentParser(prog="gt_unlock", description="gt unlock: presence-gated "
                                 "access for agents (see SECURITY.md)")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("enroll")
    s.add_argument("factor", choices=["totp", "touchid", "hello", "sso"])
    s.add_argument("--account", default=None)
    s = sub.add_parser("unenroll")
    s.add_argument("factor")
    sub.add_parser("recovery")
    s = sub.add_parser("unlock")
    s.add_argument("--scope")
    s.add_argument("--reason")
    s.add_argument("--session", type=int)
    s.add_argument("--recovery", action="store_true")
    sub.add_parser("lock")
    s = sub.add_parser("check")
    s.add_argument("--scope", required=True)
    s.add_argument("--request", action="store_true")
    s.add_argument("--reason")
    s.add_argument("--job")
    s = sub.add_parser("run")
    s.add_argument("--scope", required=True)
    s.add_argument("--secret-file", action="append")
    s.add_argument("--env", action="append")
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s = sub.add_parser("register")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--session", action="store_true")
    g.add_argument("--shim", action="store_true")
    s.add_argument("--session-id")
    sub.add_parser("revoke")
    s = sub.add_parser("hook")
    s.add_argument("event", choices=["session-start", "session-end"])
    s = sub.add_parser("policy")
    s.add_argument("action", choices=["show", "enable", "disable", "approve", "set", "consent",
                                      "unattended", "sso"])
    s.add_argument("args", nargs="*")
    s.add_argument("--json", action="store_true")
    s.add_argument("--window", type=int)
    s.add_argument("--client-id")
    s.add_argument("--tenant")
    s.add_argument("--pin-subject")
    s = sub.add_parser("seal")
    s.add_argument("action", choices=["put", "list", "rm", "migrate"])
    s.add_argument("name_ref", nargs="?")
    s.add_argument("--name")
    s.add_argument("--remove-source", action="store_true")
    s = sub.add_parser("secret")
    s.add_argument("action", choices=["get"])
    s.add_argument("ref")
    s.add_argument("--out")
    s = sub.add_parser("git-credential")
    s.add_argument("action", choices=["get", "store", "erase"])
    s = sub.add_parser("aws-credential")
    s.add_argument("--ref", required=True)
    s = sub.add_parser("audit")
    s.add_argument("-n", type=int, default=20)
    s = sub.add_parser("daemon")
    s.add_argument("action", choices=["start", "stop", "status"])
    s = sub.add_parser("verify")
    s.add_argument("--json", action="store_true")
    return ap


def main(argv=None):
    ap = build_parser()
    ns = ap.parse_args(argv)
    if ns.cmd is None:
        ap.print_help()
        return 2
    if ns.cmd == "seal" and ns.action in ("put", "rm"):
        ns.name = ns.name or ns.name_ref
        if not ns.name:
            sys.stderr.write("gt_unlock seal %s NAME\n" % ns.action)
            return 2
    if ns.cmd == "seal" and ns.action == "migrate" and (not ns.name_ref or not ns.name):
        sys.stderr.write("gt_unlock seal migrate REF --name NAME [--remove-source]\n")
        return 2
    fn = {"status": cmd_status, "enroll": cmd_enroll, "unenroll": cmd_unenroll,
          "recovery": cmd_recovery, "unlock": cmd_unlock, "lock": cmd_lock,
          "check": cmd_check, "run": cmd_run, "register": cmd_register, "revoke": cmd_revoke,
          "hook": cmd_hook, "policy": cmd_policy, "seal": cmd_seal, "secret": cmd_secret,
          "git-credential": cmd_git_credential, "aws-credential": cmd_aws_credential,
          "audit": cmd_audit, "daemon": cmd_daemon, "verify": cmd_verify}[ns.cmd]
    return fn(ns)


if __name__ == "__main__":
    sys.exit(main())
