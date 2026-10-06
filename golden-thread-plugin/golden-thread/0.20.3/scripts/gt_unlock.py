#!/usr/bin/env python3
"""gt_unlock -- prove you are present before agents may use what you protect (0.20.1).

Unlock is OFF by default. Turned on, gt's unlock authority (gt_unlockd.py) decides which
processes may use LOTR connections, sealed credentials, publish credentials and gt's own
security settings, after you prove presence with TOTP plus Touch ID (macOS) or Windows Hello,
and optionally Microsoft Entra ID sign-in. Read SECURITY.md first: it says exactly what each
level stops and what it does not.

    gt_unlock.py status [--json]
    gt_unlock.py enroll totp [--account NAME] [--without-platform]
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
    gt_unlock.py policy enable [--factors totp[,touchid|hello|sso]]
    gt_unlock.py policy disable|approve
    gt_unlock.py policy set FILE
    gt_unlock.py policy consent none|platform [--window SECONDS]
    gt_unlock.py policy secrets-window SECONDS     (0 = a fresh Touch ID / Hello per unseal)
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
    gt_unlock.py daemon start|stop|status|restart-if-stale
    gt_unlock.py verify [--json]

K is how many factors must agree to unlock. `policy enable --factors totp` is the supported way
to run with TOTP alone (a Mac without Touch ID, a PC without Windows Hello): it sets K to the
number of factors named and records that you chose it; K is never lowered silently.

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

PLATFORM = ("touchid", "hello")
K_MEANS = "K = how many factors must agree to unlock"

# Why a grant ended, in words (the authority's audit codes -> what `status` prints).
REVOKED_WHY = {
    "screen_lock": "screen locked", "sleep": "the machine slept",
    "clock_rollback": "the clock went backwards", "idle": "idle too long",
    "ttl": "the grant reached its time limit", "lock": "gt_unlock.py lock",
    "session_end": "the session ended", "session_root_exit": "its shell or Claude Code exited",
    "shim_exit": "the MCP shim exited", "vault_shim_exit": "the vault MCP server exited",
    "shim_replaced": "a new MCP shim replaced the old one",
    "vault_shim_replaced": "a new vault MCP server replaced the old one",
    "daemon_stop": "the authority was stopped",
    "code_updated": "gt was upgraded and the authority restarted",
}


def _usable(st):
    f = st.get("factors") or {}
    return [n for n in ("touchid", "hello", "sso", "totp")
            if (f.get(n) or {}).get("enrolled") and (f.get(n) or {}).get("available")]


def _k_short(st, usable=None):
    """True when the policy's K (or its required platform factor) cannot be met here."""
    usable = _usable(st) if usable is None else usable
    try:
        k = int(st.get("required") or 1)
    except (TypeError, ValueError):
        k = 1
    one_of = st.get("require_one_of") or []
    return len(usable) < k or bool(one_of and not set(one_of) & set(usable))


def next_step(st):
    """The ONE next command, from what the authority reports -- the same answer for `status`
    and for enrolment's "Next:" line, and never a factor this machine cannot use (usability
    run 2026-10-04: status kept suggesting Touch ID on a Mac without a sensor, and "Next:"
    disagreed with it). None when there is nothing to do."""
    f = st.get("factors") or {}
    enrolled = [n for n in ("touchid", "hello", "sso", "totp") if (f.get(n) or {}).get("enrolled")]
    plat = [n for n in PLATFORM if (f.get(n) or {}).get("available")]
    usable = _usable(st)
    if not enrolled:
        if plat:
            return ("gt_unlock.py enroll %s  (or, to use TOTP without it: gt_unlock.py enroll "
                    "totp --without-platform)" % plat[0])
        return "gt_unlock.py enroll totp"
    if "totp" not in enrolled:
        return "gt_unlock.py enroll totp"
    if not (f.get("recovery") or {}).get("enrolled"):
        return "gt_unlock.py recovery  (keep the codes offline)"
    if st.get("needs_reenrol"):
        return "re-enrol the factor you lost: gt_unlock.py enroll %s" % (plat[0] if plat
                                                                        else "totp")
    if _k_short(st, usable):
        return "gt_unlock.py policy enable --factors %s  (%s; you choose them on purpose)" % (
            ",".join(usable) or "totp", K_MEANS)
    if not st.get("enabled"):
        return "gt_unlock.py policy enable"
    return None


def _offline_hint():
    """What to run first when the authority is not running (so it could not say which factors
    this machine has). Never names a factor this platform cannot have."""
    if sys.platform == "darwin":
        helper = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "bin",
                              "gt-presence")
        if os.path.exists(helper):
            return ("gt_unlock.py enroll touchid (or, on a Mac without Touch ID: gt_unlock.py "
                    "enroll totp), then gt_unlock.py status says what is next")
        return ("gt_unlock.py enroll totp (no Touch ID helper is installed here), then "
                "gt_unlock.py status says what is next")
    if os.name == "nt":
        return ("gt_unlock.py enroll hello (Windows Hello), or without Hello: gt_unlock.py "
                "enroll totp --without-platform; then gt_unlock.py status says what is next")
    return "gt_unlock.py enroll totp, then gt_unlock.py status says what is next"


def _revoked_line(st):
    lr = st.get("last_revoked") or {}
    if not lr.get("at"):
        return None
    import time as _t
    return "revoked at %s: %s" % (_t.strftime("%H:%M", _t.localtime(lr["at"])),
                                  REVOKED_WHY.get(lr.get("why"), lr.get("why") or "?"))


def _stale_line(info):
    """-> the warning for an authority running code older than what is installed, or None."""
    if not info or not info.get("stale"):
        return None
    since = info.get("since")
    import time as _t
    return ("the unlock service is running old code (pid %s, since %s); restart it with: "
            "gt_unlock.py daemon restart-if-stale  (every grant is dropped; unlock again)"
            % (info.get("pid") or "?", _t.strftime("%Y-%m-%d %H:%M", _t.localtime(since))
               if since else "?"))


def cmd_status(ns):
    if not C.enabled():
        info = {"enabled": False, "assurance": {"level": "off",
                                                 "why": "unlock is off (the default)"}}
        reached = False
        try:
            info = _call("status", start=False)
            reached = True
        except gt_ipc.IpcError:
            pass
        if ns.json:
            _out(info)
        else:
            print("unlock: off -- agents use LOTR and gt's settings without asking you.")
            nxt = next_step(info) if reached else None
            print("  turn it on: %s" % (nxt or _offline_hint()))
            print("  what it does and does not protect: SECURITY.md")
            warn = _stale_line(C.code_status()) if reached else None
            if warn:
                print("  ! " + warn)
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
    warn = _stale_line(C.code_status())
    if warn:
        print("  ! " + warn)
    chosen = st.get("factors_chosen") or {}
    print("  factors (K = %s: how many must agree to unlock%s%s):" % (
        st.get("required"),
        "; one of " + "/".join(st["require_one_of"]) if st.get("require_one_of") else "",
        "; you chose %s on %s" % ("+".join(chosen.get("factors") or []),
                                  str(chosen.get("at") or "?")[:10]) if chosen else ""))
    for name, f in sorted(st["factors"].items()):
        if not f["enrolled"] and not f["available"] and name in PLATFORM:
            continue                 # never list a factor this OS cannot have (touchid on Linux)
        print("    %-9s %s · %s" % (name, "enrolled" if f["enrolled"] else "not enrolled",
                                    ("available" if f["available"] else "unavailable: " +
                                     f["why"])))
    print("  door: %s · grants live: %d · sealed credentials: %d · admin floor: %s"
          % (st.get("door"), st.get("grants", 0), len(st.get("sealed") or []),
             "yes" if st.get("admin_floor") else "no"))
    rl = _revoked_line(st)
    if rl:
        print("  " + rl)
    if st.get("needs_reenrol"):
        print("  ! a recovery code was used: re-enrol your factors")
    nxt = next_step(st)
    if nxt:
        print("  Next: " + nxt)
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


def _print_next():
    """The enrolment's "Next:" line -- the very answer `status` gives (next_step)."""
    try:
        nxt = next_step(_call("status"))
    except gt_ipc.IpcError:
        nxt = None
    print("Next: %s" % (nxt or "gt_unlock.py status"))


def cmd_enroll(ns):
    try:
        if ns.factor == "totp":
            params = {"factor": "totp", "phase": "begin", "account": ns.account}
            if ns.without_platform:
                params["skip_platform"] = True
            res = _call("enroll", params)
            _show_totp(res["uri"])
            import getpass
            try:
                if sys.stdin and sys.stdin.isatty():
                    code = getpass.getpass("Enter the code your app now shows: ")
                else:
                    line = sys.stdin.readline() if sys.stdin else ""
                    if not line:
                        raise EOFError
                    code = line.strip()
            except (EOFError, KeyboardInterrupt):
                # usability run 2026-10-04: a raw EOFError traceback. Nothing was saved: the
                # seed stays pending until a code confirms it.
                sys.stderr.write("\ngt_unlock: no code was entered, so TOTP is NOT enrolled; "
                                 "run gt_unlock.py enroll totp again in a terminal\n")
                return 2
            _call("enroll", {"factor": "totp", "phase": "confirm", "code": code,
                             "account": ns.account})
            if sys.stdout.isatty():
                sys.stdout.write("\033[2J\033[H")          # clear the seed off the screen
            print("TOTP enrolled.")
            _print_next()
            return 0
        res = _call("enroll", {"factor": ns.factor})
        print("%s enrolled." % res.get("enrolled"))
        _print_next()
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
    print("")
    _print_next()
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
    # usability run 2026-10-04: the grant is bound to the session that asked -- say so, and
    # say what ends it, so a lock screen revoking it is not a surprise.
    kind = res.get("kind")
    who = ("this Claude Code session (pid %s)" if kind in ("claude", "shim", "vault_shim")
           else "this terminal's shell (pid %s)" if kind == "terminal" and not ns.session
           else "session pid %s") % res.get("session", "?")
    print("  bound to %s only: other terminals and sessions stay locked. Locking the screen, "
          "sleep or `gt_unlock.py lock` ends it." % who)
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
    cmd = list(ns.run_argv or [])
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


FACTOR_CHOICES = ("totp", "touchid", "hello", "sso")


def _parse_factors(text):
    names = [x.strip().lower() for x in (text or "").split(",") if x.strip()]
    bad = [x for x in names if x not in FACTOR_CHOICES]
    if not names or bad or len(set(names)) != len(names):
        return None
    return names


def _apply_factor_choice(p, names):
    """M5 (usability run 2026-10-04): the owner names the factors every unlock needs. K becomes
    how many were named, and the choice is RECORDED in the policy (`factors.chosen`), which the
    authority shows in `status` and audits -- K is changed on purpose, never lowered
    silently. The change still needs the CURRENT policy's factors (policy_set's step-up + K)."""
    import time as _t
    f = dict(p.get("factors") or {})
    f["required"] = len(names)
    f["require_one_of"] = [n for n in names if n in PLATFORM][:1]
    f["chosen"] = {"factors": list(names), "by": "owner",
                   "at": _t.strftime("%Y-%m-%dT%H:%M:%S%z")}
    p["factors"] = f


def _choose_factors(ns):
    """-> the factor list to record, [] to leave the policy's factors alone, None on a usage
    error, False when the owner declined (both already printed). Without --factors, a machine
    whose usable factors cannot meet K is offered the supported path: asked at a terminal,
    else told the exact command."""
    if ns.factors:
        names = _parse_factors(ns.factors)
        if names is None:
            sys.stderr.write("gt_unlock: --factors takes a comma list of %s, e.g. --factors "
                             "totp\n" % ", ".join(FACTOR_CHOICES))
            return None
        return names
    try:
        st = _call("status")
    except gt_ipc.IpcError:
        return []                      # policy_set below reports the real problem
    usable = _usable(st)
    if not usable or not _k_short(st, usable):
        return []
    want = "+".join((st.get("require_one_of") or [])) or "more factors"
    print("This machine can use %s, but the policy asks for K = %s (%s)%s." % (
        " and ".join(usable), st.get("required"), K_MEANS,
        ", including " + want if st.get("require_one_of") else ""))
    cmd = "gt_unlock.py policy enable --factors %s" % ",".join(usable)
    if sys.stdin and sys.stdin.isatty():
        try:
            ans = input("Turn unlock on with %s alone (K = %d)? It is recorded as your "
                        "choice. [y/N] " % (" + ".join(usable), len(usable)))
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans.strip().lower() in ("y", "yes"):
            return usable
    sys.stderr.write("gt_unlock: unlock was NOT turned on. To use what this machine has, run:\n"
                     "  %s\n(or enrol more factors first; SECURITY.md section 3)\n" % cmd)
    return False


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
    if ns.factors and a != "enable":
        sys.stderr.write("gt_unlock: --factors goes with policy enable\n")
        return 2
    if a in ("enable", "disable"):
        if not p:
            p = {"schema": 1}
        p["enabled"] = (a == "enable")
        if a == "enable":
            chosen = _choose_factors(ns)
            if chosen is None or chosen is False:
                return 2 if chosen is None else 1
            if chosen:
                _apply_factor_choice(p, chosen)
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
    if a == "secrets-window":
        # Owner decision 2026-10-03 18:37: 0 (the default) = every sealed open asks for the
        # platform factor; N = once per N seconds per process and grant, never longer.
        try:
            p["secrets_window_s"] = int(ns.args[0])
        except (IndexError, ValueError):
            sys.stderr.write("gt_unlock policy secrets-window SECONDS (0-%d)\n"
                             % P.MAX_SECRETS_WINDOW_S)
            return 2
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
        # While unlock is on, stopping needs a fresh factor (review F1): the authority asks for
        # it -- Touch ID / Hello, or a code typed here.
        try:
            C.call("stop", {"tty": True}, start=False, answer=C.tty_answer)
            print("authority stopping (every grant revoked)")
        except gt_ipc.IpcError as e:
            if e.code == "unreachable":
                print("authority not running")
                return 0
            print("authority NOT stopped (%s): %s" % (e.code, e.message))
            return 1
        return 0
    if ns.action == "restart-if-stale":
        # M12: install.sh runs this after copying gt's hooks. Quiet when nothing ran.
        state, info = C.restart_if_stale()
        info = info or {}
        if state == "not_running":
            return 0
        if state == "current":
            print("unlock authority: running current code (pid %s)" % info.get("pid"))
            return 0
        if state == "failed":
            print("unlock authority: running OLD code (pid %s) and could not be stopped; stop "
                  "it with: gt_unlock.py daemon stop" % info.get("pid"))
            return 1
        print("unlock authority: it was running old code (pid %s); stopped it%s. Any unlock "
              "was dropped -- unlock again when you need it." % (
                  info.get("pid"), " and started it from the installed code"
                  if state == "restarted" else
                  " (it starts again on first use)"))
        return 0
    up = gt_ipc.alive_at(C.address(), timeout=1.0)
    print("authority %s at %s" % ("running" if up else "not running", C.address()))
    if up:
        warn = _stale_line(C.code_status())
        if warn:
            print("  ! " + warn)
    return 0 if up else 1


def cmd_verify(ns):
    import gt_unlock_verify as V
    return V.main(as_json=ns.json)


# ------------------------------------------------------------------------- argv

# One line per command for `gt_unlock.py --help`, and the description of `gt_unlock.py CMD -h`
# (usability run 2026-10-04: the top-level help listed bare command names).
HELP = {
    "status": "show whether unlock is on, its level, your factors, and what to do next",
    "enroll": "add a factor: totp (an authenticator app), touchid (macOS), hello (Windows), sso",
    "unenroll": "remove an enrolled factor (needs your factors)",
    "recovery": "print ten new one-time recovery codes (needs your factors)",
    "unlock": "prove you are present and unlock THIS shell or Claude Code session",
    "lock": "revoke every grant now (never needs a factor)",
    "check": "ask whether a scope is allowed (exit 0 allowed, 10 locked, 11 step-up, 12 refused)",
    "run": "run a command once the scope is unlocked, handing it secrets as files or env",
    "register": "register a session or an MCP shim with the authority (used by gt's hooks)",
    "revoke": "revoke this session's grants (used by gt's SessionEnd hook)",
    "hook": "the SessionStart / SessionEnd hook entry point",
    "policy": "show or change the unlock policy: enable, disable, approve, set, consent, ...",
    "seal": "store, list, remove or migrate sealed credentials",
    "secret": "resolve a secret ref to a pipe or a file (never to a terminal)",
    "git-credential": "git's credential-helper protocol, backed by sealed credentials",
    "aws-credential": "AWS credential_process, backed by a secret ref",
    "audit": "print the last lines of the authority's audit log",
    "daemon": "start, stop or check the unlock authority; restart it after an upgrade",
    "verify": "run the safe self-check on this machine (PASS / FAIL / NOT-CHECKED)",
}


def build_parser():
    ap = argparse.ArgumentParser(
        prog="gt_unlock", description="gt unlock: presence-gated access for agents (see "
        "SECURITY.md). K, used throughout, is how many factors must agree to unlock.")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser("status", help=HELP["status"], description=HELP["status"])
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("enroll", help=HELP["enroll"], description=HELP["enroll"])
    s.add_argument("factor", choices=["totp", "touchid", "hello", "sso"])
    s.add_argument("--account", default=None)
    s.add_argument("--without-platform", action="store_true",
                   help="enrol TOTP first even though Touch ID / Windows Hello is available "
                        "here (only from your own terminal; see SECURITY.md section 3)")
    s = sub.add_parser("unenroll", help=HELP["unenroll"], description=HELP["unenroll"])
    s.add_argument("factor")
    sub.add_parser("recovery", help=HELP["recovery"], description=HELP["recovery"])
    s = sub.add_parser("unlock", help=HELP["unlock"], description=HELP["unlock"])
    s.add_argument("--scope")
    s.add_argument("--reason")
    s.add_argument("--session", type=int)
    s.add_argument("--recovery", action="store_true")
    sub.add_parser("lock", help=HELP["lock"], description=HELP["lock"])
    s = sub.add_parser("check", help=HELP["check"], description=HELP["check"])
    s.add_argument("--scope", required=True)
    s.add_argument("--request", action="store_true")
    s.add_argument("--reason")
    s.add_argument("--job")
    s = sub.add_parser("run", help=HELP["run"], description=HELP["run"])
    s.add_argument("--scope", required=True)
    s.add_argument("--secret-file", action="append")
    s.add_argument("--env", action="append")
    # B3 (usability run 2026-10-04): this positional was `cmd`, the subcommand's own dest, so
    # argparse overwrote "run" with the command list and dispatch crashed (unhashable list).
    s.add_argument("run_argv", nargs=argparse.REMAINDER, metavar="-- CMD [ARGS...]")
    s = sub.add_parser("register", help=HELP["register"], description=HELP["register"])
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--session", action="store_true")
    g.add_argument("--shim", action="store_true")
    s.add_argument("--session-id")
    sub.add_parser("revoke", help=HELP["revoke"], description=HELP["revoke"])
    s = sub.add_parser("hook", help=HELP["hook"], description=HELP["hook"])
    s.add_argument("event", choices=["session-start", "session-end"])
    s = sub.add_parser("policy", help=HELP["policy"], description=HELP["policy"])
    s.add_argument("action", choices=["show", "enable", "disable", "approve", "set", "consent",
                                      "secrets-window", "unattended", "sso"])
    s.add_argument("args", nargs="*")
    s.add_argument("--json", action="store_true")
    s.add_argument("--window", type=int)
    s.add_argument("--client-id")
    s.add_argument("--tenant")
    s.add_argument("--pin-subject")
    s.add_argument("--factors", help="with enable: the factors every unlock needs, chosen by "
                   "you, e.g. totp or totp,touchid (sets K to how many you name)")
    s = sub.add_parser("seal", help=HELP["seal"], description=HELP["seal"])
    s.add_argument("action", choices=["put", "list", "rm", "migrate"])
    s.add_argument("name_ref", nargs="?")
    s.add_argument("--name")
    s.add_argument("--remove-source", action="store_true")
    s = sub.add_parser("secret", help=HELP["secret"], description=HELP["secret"])
    s.add_argument("action", choices=["get"])
    s.add_argument("ref")
    s.add_argument("--out")
    s = sub.add_parser("git-credential", help=HELP["git-credential"],
                       description=HELP["git-credential"])
    s.add_argument("action", choices=["get", "store", "erase"])
    s = sub.add_parser("aws-credential", help=HELP["aws-credential"],
                       description=HELP["aws-credential"])
    s.add_argument("--ref", required=True)
    s = sub.add_parser("audit", help=HELP["audit"], description=HELP["audit"])
    s.add_argument("-n", type=int, default=20)
    s = sub.add_parser("daemon", help=HELP["daemon"], description=HELP["daemon"])
    s.add_argument("action", choices=["start", "stop", "status", "restart-if-stale"])
    s = sub.add_parser("verify", help=HELP["verify"], description=HELP["verify"])
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
