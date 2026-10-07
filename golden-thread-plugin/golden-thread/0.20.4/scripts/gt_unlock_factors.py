#!/usr/bin/env python3
"""gt_unlock_factors -- the ways a person proves they are present, as the authority uses them
(0.20.1).

Every factor has the same shape (class Factor below):

    available()                         -> (bool, reason)
    enroll(ctx)                         -> record (public data only, stored in enrolment.json)
    prove(record, challenge, ctx)       -> True, or raises FactorError

and only the AUTHORITY calls them (invariant: clients may request an unlock, never assert
one). `challenge` is 32 bytes the authority derived from a fresh random nonce and the request
(who asks, for which scope), so a proof cannot be replayed into another request.

  totp      a code from an authenticator app, typed into a DIALOG THE AUTHORITY RAISES (or the
            requesting CLI's own terminal) -- never a tool argument the model could see.
  recovery  one of ten one-time codes (hashed with scrypt where the Python has it, else
            PBKDF2-SHA256); counts as ONE factor and forces re-enrolment.
  touchid   macOS Secure Enclave key, biometry-gated: gt_unlock_touchid.py (the gt-presence
            helper signs; ES256 verified here in Python).
  hello     Windows Hello / TPM key: gt_unlock_hello.py (PowerShell KeyCredentialManager
            signs; RS256 verified here in Python).
  sso       Microsoft Entra ID, PKCE + loopback: gt_unlock_sso.py (ID token verified here).

A factor whose module or helper is missing is UNAVAILABLE -- never counted as passed.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_unlock_totp as totp_mod          # noqa: E402

IS_WINDOWS = os.name == "nt"
PROMPT_TIMEOUT_S = 120


class FactorError(Exception):
    """A factor did not prove presence. `code`: cancelled | timeout | wrong | locked_out |
    replayed | unavailable | malformed | helper_failed | not_enrolled."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Context:
    """What a factor may use while proving: the text to show (naming the requester and the
    scope), a way to ask the requesting CLI's terminal (or None), the unlock home, and the
    persisted state dict the authority saves afterwards."""

    def __init__(self, home, reason, ask_client=None, state=None, policy=None, tty=False):
        self.home = home
        self.reason = reason
        self.ask_client = ask_client
        self.state = state if state is not None else {}
        self.policy = policy or {}
        self.tty = tty


# ---------------------------------------------------------------- files

def write_private(path, data):
    """Atomic write, mode 600, bytes as given (LF is the caller's)."""
    import tempfile
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        return default


def write_json(path, obj):
    write_private(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8"))


# ---------------------------------------------------------------- prompts

def _osascript_secret(text, title):
    script = ['on run argv',
              'display dialog (item 1 of argv) with title (item 2 of argv) default answer "" '
              'with hidden answer buttons {"Cancel", "OK"} default button "OK" '
              'cancel button "Cancel" with icon caution giving up after %d' % PROMPT_TIMEOUT_S,
              'if gave up of result then error "timeout"',
              'return text returned of result',
              'end run']
    try:
        r = subprocess.run(["/usr/bin/osascript"] + [a for ln in script for a in ("-e", ln)]
                           + [text, title], capture_output=True, text=True,
                           timeout=PROMPT_TIMEOUT_S + 15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip()


_PS_PROMPT = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.Form
$f.Text = $env:GT_UNLOCK_PROMPT_TITLE; $f.TopMost = $true; $f.Width = 460; $f.Height = 200
$f.StartPosition = 'CenterScreen'; $f.FormBorderStyle = 'FixedDialog'
$l = New-Object System.Windows.Forms.Label
$l.Text = $env:GT_UNLOCK_PROMPT_TEXT; $l.Left = 12; $l.Top = 10; $l.Width = 420; $l.Height = 70
$t = New-Object System.Windows.Forms.TextBox
$t.UseSystemPasswordChar = $true; $t.Left = 12; $t.Top = 85; $t.Width = 420
$ok = New-Object System.Windows.Forms.Button; $ok.Text = 'OK'; $ok.Left = 270; $ok.Top = 120
$ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
$c = New-Object System.Windows.Forms.Button; $c.Text = 'Cancel'; $c.Left = 355; $c.Top = 120
$c.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$f.AcceptButton = $ok; $f.CancelButton = $c
$f.Controls.AddRange(@($l, $t, $ok, $c))
if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($t.Text) }
else { exit 1 }
"""


def _powershell_secret(text, title):
    env = dict(os.environ, GT_UNLOCK_PROMPT_TEXT=text, GT_UNLOCK_PROMPT_TITLE=title)
    enc = base64.b64encode(_PS_PROMPT.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                            "Bypass", "-EncodedCommand", enc], capture_output=True, text=True,
                           env=env, timeout=PROMPT_TIMEOUT_S + 15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip()


def _linux_secret(text, title):
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    for argv in (["zenity", "--password", "--title", title, "--timeout", str(PROMPT_TIMEOUT_S)],
                 ["kdialog", "--title", title, "--password", text]):
        if shutil.which(argv[0]):
            try:
                r = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=PROMPT_TIMEOUT_S + 15)
            except (OSError, subprocess.TimeoutExpired):
                return None
            return (r.stdout or "").strip() if r.returncode == 0 else None
    return None


def ui_allowed():
    """False when GT_UNLOCK_NO_UI=1. gt's test harness sets it for every test and every process
    a test starts, so the suite can NEVER put a real dialog, Touch ID sheet, Hello prompt or
    browser in front of a person (2026-10-03: a test run raised a real code dialog on the
    owner's screen). In production it is unset; setting it only ever REFUSES (fail closed)."""
    return os.environ.get("GT_UNLOCK_NO_UI") != "1"


def require_ui(what):
    if not ui_allowed():
        raise FactorError("unavailable", "%s would show a real prompt, and real prompts are "
                          "disabled here (GT_UNLOCK_NO_UI=1)" % what)


def ask_secret(ctx, text, title="gt unlock"):
    """Ask the person for a short secret (a code). INVARIANT: the answer comes from a dialog
    the authority raises, or from the requesting CLI's own terminal -- never from a request
    parameter. Returns None when nobody could be asked or they cancelled."""
    mode = ((ctx.policy or {}).get("totp") or {}).get("prompt", "auto")
    if mode in ("auto", "tty") and ctx.tty and ctx.ask_client:
        ans = ctx.ask_client({"kind": "secret", "prompt": text})
        if ans is not None or mode == "tty":
            return ans
    if mode == "tty" or not ui_allowed():
        return None
    if sys.platform == "darwin":
        return _osascript_secret(text, title)
    if IS_WINDOWS:
        return _powershell_secret(text, title)
    return _linux_secret(text, title)


# ---------------------------------------------------------------- the factor interface

class Factor:
    name = "?"
    platform = False            # a platform authenticator (Touch ID / Hello)

    def available(self):
        return False, "not implemented"

    def enroll(self, ctx):
        raise FactorError("unavailable", "%s cannot be enrolled here" % self.name)

    def prove(self, record, challenge, ctx):
        raise FactorError("unavailable", "%s is not available" % self.name)


class TotpFactor(Factor):
    name = "totp"

    def available(self):
        return True, "standard library"

    @staticmethod
    def seed_path(home):
        return os.path.join(home, "totp.seed")

    def begin(self, home, account):
        """Start enrolment: a new seed in totp.pending (600). -> the otpauth URI, which IS THE
        SECRET -- the caller shows it once and never logs it."""
        key = totp_mod.new_secret()
        write_private(os.path.join(home, "totp.pending"), (totp_mod.b32(key) + "\n").encode())
        return totp_mod.otpauth_uri(key, account)

    def confirm(self, home, code, state, policy=None):
        """Finish enrolment: the pending seed is saved only after one valid code (so an
        enrolment the phone never received cannot lock the user out)."""
        pending = os.path.join(home, "totp.pending")
        try:
            with open(pending, "r", encoding="utf-8") as f:
                key = totp_mod.from_b32(f.read().strip())
        except (OSError, ValueError):
            raise FactorError("not_enrolled", "no TOTP enrolment is in progress")
        st = {}
        ok, why = totp_mod.verify(key, code, st, **_totp_opts(policy))
        if not ok:
            raise FactorError(why, "that code is not valid for the new seed")
        os.replace(pending, self.seed_path(home))
        state["totp"] = {"last_step": st.get("last_step", -1), "failures": 0, "lockouts": 0}
        return {"enrolled": True}

    def prove(self, record, challenge, ctx):
        try:
            with open(self.seed_path(ctx.home), "r", encoding="utf-8") as f:
                key = totp_mod.from_b32(f.read().strip())
        except (OSError, ValueError):
            raise FactorError("not_enrolled", "TOTP is not enrolled")
        st = ctx.state.setdefault("totp", {})
        if (st.get("locked_until") or 0) > __import__("time").time():
            raise FactorError("locked_out", "too many wrong codes: TOTP is locked out for now")
        code = ask_secret(ctx, ctx.reason + "\n\nEnter the 6-digit code from your "
                                            "authenticator app.")
        if code is None:
            raise FactorError("cancelled", "no code was entered")
        ok, why = totp_mod.verify(key, code, st, **_totp_opts(ctx.policy))
        if not ok:
            raise FactorError(why, {"locked_out": "too many wrong codes: TOTP is locked out",
                                    "replayed": replayed_message(st),
                                    "malformed": "a code is six digits",
                                    "wrong": "wrong code"}.get(why, why))
        return True


def replayed_message(st, now=None):
    """"Code already used" plus how long until the app shows one that is not (usability run
    2026-10-04: enrol -> recovery -> policy enable inside one 30 s window met a bare "already
    used"). A code is accepted once; the next acceptable one starts at the step after the last
    accepted step."""
    import time as _t
    now = _t.time() if now is None else now
    try:
        wait = int((int(st.get("last_step", -1)) + 1) * totp_mod.STEP - now) + 1
    except (TypeError, ValueError):
        wait = 0
    if wait <= 0:
        return "that code was already used; enter the code your app shows now"
    return ("that code was already used; wait for the next code (about %d s) and try "
            "again" % min(wait, 2 * totp_mod.STEP))


def _totp_opts(policy):
    t = (policy or {}).get("totp") or {}
    return {"skew": int(t.get("skew_steps", 1)), "max_failures": int(t.get("max_failures", 5)),
            "lockout_minutes": tuple(t.get("lockout_minutes") or (1, 5, 15))}


# ---------------------------------------------------------------- recovery codes

_ALPH = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O, 1/I


def _kdf(code, salt, kdf):
    data = code.encode("utf-8")
    if kdf == "scrypt":
        return hashlib.scrypt(data, salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    if kdf == "pbkdf2-sha256":
        return hashlib.pbkdf2_hmac("sha256", data, salt, 200000, dklen=32)
    raise FactorError("malformed", "unknown kdf %s" % kdf)


def _norm_code(code):
    return "".join(c for c in (code or "").upper() if c.isalnum())


class RecoveryFactor(Factor):
    name = "recovery"
    COUNT = 10

    def available(self):
        return True, "standard library"

    @staticmethod
    def path(home):
        return os.path.join(home, "recovery.json")

    def generate(self, home):
        """Ten new codes (old ones die). -> the codes, to be SHOWN ONCE and never stored in
        clear. Each is 16 symbols from a 32-symbol alphabet (80 bits)."""
        kdf = "scrypt" if hasattr(hashlib, "scrypt") else "pbkdf2-sha256"
        codes, rows = [], []
        for _ in range(self.COUNT):
            raw = "".join(secrets.choice(_ALPH) for _ in range(16))
            code = "-".join(raw[i:i + 4] for i in range(0, 16, 4))
            salt = secrets.token_bytes(16)
            rows.append({"kdf": kdf, "salt": base64.b64encode(salt).decode(),
                         "hash": base64.b64encode(_kdf(_norm_code(code), salt, kdf)).decode(),
                         "used": False})
            codes.append(code)
        write_json(self.path(home), {"codes": rows})
        return codes

    def remaining(self, home):
        rows = (read_json(self.path(home), {}) or {}).get("codes") or []
        return sum(1 for r in rows if not r.get("used"))

    def prove(self, record, challenge, ctx):
        code = ask_secret(ctx, ctx.reason + "\n\nEnter one of your gt recovery codes.")
        if not code:
            raise FactorError("cancelled", "no recovery code was entered")
        data = read_json(self.path(ctx.home), {}) or {}
        rows = data.get("codes") or []
        norm = _norm_code(code)
        for r in rows:
            if r.get("used"):
                continue
            try:
                h = _kdf(norm, base64.b64decode(r["salt"]), r["kdf"])
            except (KeyError, ValueError, FactorError):
                continue
            if hmac.compare_digest(h, base64.b64decode(r["hash"])):
                r["used"] = True                   # one time: burnt before it is honoured
                write_json(self.path(ctx.home), data)
                ctx.state["needs_reenrol"] = True
                return True
        raise FactorError("wrong", "that is not an unused recovery code")


# ---------------------------------------------------------------- registry

class _Missing(Factor):
    def __init__(self, name, why):
        self.name = name
        self._why = why
        self.platform = name in ("touchid", "hello")

    def available(self):
        return False, self._why


def _load(modname, cls, name):
    try:
        mod = __import__(modname)
        return getattr(mod, cls)()
    except Exception as e:                           # noqa: BLE001 - absent = unavailable
        return _Missing(name, "%s not loadable (%s)" % (modname, type(e).__name__))


def registry():
    """name -> Factor, for this machine. Platform factors exist only on their platform."""
    reg = {"totp": TotpFactor(), "recovery": RecoveryFactor()}
    if sys.platform == "darwin":
        reg["touchid"] = _load("gt_unlock_touchid", "TouchIdFactor", "touchid")
    else:
        reg["touchid"] = _Missing("touchid", "Touch ID is macOS only")
    if IS_WINDOWS:
        reg["hello"] = _load("gt_unlock_hello", "HelloFactor", "hello")
    else:
        reg["hello"] = _Missing("hello", "Windows Hello is Windows only")
    reg["sso"] = _load("gt_unlock_sso", "SsoFactor", "sso")
    return reg
