"""Resolve a secret *reference* to its value, without ever letting the value escape.

The registry never holds a credential, only a ref such as `keychain:gt-lotr/github` or
`file:/Users/me/.secrets/jira`. This module is the one place a ref turns into a value, so it
is also the one place that could leak one. The rule it keeps: every error, every repr and
every description names the REF, never what it resolves to. Values are returned to the
caller and nowhere else -- not printed, not logged, not put in argv.

Schemes:
  file:/abs/path            file must be mode & 0o077 == 0 (owner-only), else secret_perms
  file:/abs/path#field      the same file read as JSON; the value is that top-level field
                            (0.2.0: an MCP client's own token file, e.g. #access_token)
  store:<name>              file: under the store dir ($LOTR_STORE_DIR or ~/.secrets)
  keychain:<service>/<acct> macOS `security find-generic-password -s S -a A -w`
  env:<NAME>                only when LOTR_ALLOW_ENV_SECRETS=1 (tests); refused otherwise

Brokered schemes (0.3.0), resolved BY gt core's unlock authority, never here:
  sealed:<name>  sops:<..>  op:<..>  bw:<..>  vault:<..>  wincred:<..>
  LOTR sends {ref, subject} to the authority's `secret` method (lotrlib.unlock). The authority
  requires a grant for gt:secrets, resolves the ref through gt_unlock_brokers / the sealed store
  and audits the resolve with the grant id. INVARIANT: lotr never reads these stores itself, so
  there is no path to a brokered value that skips the grant; when gt's client cannot be loaded
  the ref is refused (unlock_unavailable), never read some other way.

Assurance levels (ADR-3 / ADR-5 as amended for 0.3.0): keychain: refs are L1 -- items created by
/usr/bin/security trust `security` itself, so any same-user process can read them -- and so are
file:/store: refs (mode-600 files any same-user process can read). describe() labels keychain:
and brokered refs; `lotr status` labels every ref (LOCAL_LEVEL for the 0.2.0 schemes).

On native Windows there are no mode bits: file:/store: refs are not checked for 600 there
(st_mode cannot express an ACL); the store directory under the user's profile is what keeps
other users out. Same-user processes can read them -- L1, as everywhere.

The module is named `secrets`, shadowing the stdlib module of that name *inside the lotr
package only* (relative imports); callers wanting stdlib `secrets` import it absolutely.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

from .errors import GatewayError

# The macOS keychain CLI. Module-level so tests can point it at a fake script instead of
# the real keychain; production never changes it.
SECURITY_BIN = "/usr/bin/security"
KEYCHAIN_TIMEOUT_S = 10

# Store names are a single path segment: no traversal out of the store directory.
_STORE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOCAL_SCHEMES = ("file", "store", "keychain", "env")
BROKERED = ("sealed", "sops", "op", "bw", "vault", "wincred")
SCHEMES = LOCAL_SCHEMES + BROKERED
LOCAL_LEVEL = {"keychain": "L1", "file": "L1", "store": "L1", "env": "test-only"}


def _split(ref):
    """Return (scheme, target) or raise secret_ref_invalid. Never echoes more than the ref."""
    if not isinstance(ref, str) or ":" not in ref:
        # A non-ref string might BE a pasted secret, so do not quote it back.
        raise GatewayError("secret_ref_invalid",
                           "secret ref must look like scheme:target "
                           "(file:, store:, keychain:, env:, or a brokered scheme)")
    scheme, target = ref.split(":", 1)
    if scheme not in SCHEMES:
        # Same caution: an unknown "scheme" may be the front of a literal token.
        raise GatewayError("secret_ref_invalid",
                           f"unknown secret scheme (expected one of {', '.join(SCHEMES)})")
    if not target:
        raise GatewayError("secret_ref_invalid", f"secret ref {scheme}: has an empty target")
    return scheme, target


def _store_dir(store_dir=None):
    if store_dir is not None:
        return Path(store_dir)
    env = os.environ.get("LOTR_STORE_DIR")
    return Path(env).expanduser() if env else Path.home() / ".secrets"


def _field(target):
    """`/path#field` -> ("/path", "field"); no `#` -> (target, None)."""
    if "#" in target:
        path, field = target.rsplit("#", 1)
        if not field or not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", field):
            raise GatewayError("secret_ref_invalid", "file: ref field after # must be a plain key")
        return path, field
    return target, None


def _from_json(ref, text, field):
    import json
    try:
        data = json.loads(text)
    except ValueError:
        raise GatewayError("secret_unreadable", f"secret {ref} is not JSON")
    value = data.get(field) if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        raise GatewayError("secret_missing", f"secret {ref}: the file has no string field "
                                             f"{field!r}")
    return value


def _file_path(scheme, target, store_dir):
    if scheme == "file":
        target, _f = _field(target)
        p = Path(target)
        if not p.is_absolute():
            raise GatewayError("secret_ref_invalid", f"file: ref must be an absolute path: {target}")
        return p
    if not _STORE_NAME.match(target):
        raise GatewayError("secret_ref_invalid",
                           f"store: name must be a single plain file name: store:{target}")
    return _store_dir(store_dir) / target


def _read_file(ref, path):
    """Read an owner-only secret file. The permission check happens on the open fd so a
    swap between stat and read cannot slip a world-readable file past it."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except FileNotFoundError:
        raise GatewayError("secret_missing", f"secret {ref} not found",
                           hints=[f"create {path} with mode 600"])
    except OSError as e:
        raise GatewayError("secret_unreadable", f"secret {ref} cannot be opened ({e.strerror})")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise GatewayError("secret_unreadable", f"secret {ref} is not a regular file")
        if os.name != "nt" and st.st_mode & 0o077:   # no mode bits on Windows (see top)
            raise GatewayError("secret_perms",
                               f"secret {ref} is readable by group or others "
                               f"(mode {stat.S_IMODE(st.st_mode):o})",
                               hints=[f"chmod 600 {path}"])
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            fd = None
            value = f.read()
    except UnicodeDecodeError:
        raise GatewayError("secret_unreadable", f"secret {ref} is not UTF-8 text")
    finally:
        if fd is not None:
            os.close(fd)
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if not value:
        raise GatewayError("secret_missing", f"secret {ref} is empty")
    return value


def _keychain(ref, target):
    if "/" not in target:
        raise GatewayError("secret_ref_invalid",
                           f"keychain ref must be keychain:<service>/<account>: {ref}")
    service, account = target.split("/", 1)
    if not service or not account:
        raise GatewayError("secret_ref_invalid",
                           f"keychain ref must be keychain:<service>/<account>: {ref}")
    # argv only, no shell; stdout captured and never echoed; stderr captured and DISCARDED
    # (security(1) prints only status text there, but nothing it says is passed on).
    try:
        proc = subprocess.run([SECURITY_BIN, "find-generic-password", "-s", service,
                               "-a", account, "-w"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=KEYCHAIN_TIMEOUT_S, check=False)
    except FileNotFoundError:
        raise GatewayError("secret_scheme_unavailable",
                           f"keychain: refs need macOS `security`; not found for {ref}")
    except subprocess.TimeoutExpired:
        # The exception's own repr carries the captured output; build our own message.
        raise GatewayError("secret_unreadable", f"keychain lookup timed out for {ref}",
                           hints=["is the keychain locked?"])
    if proc.returncode != 0:
        raise GatewayError("secret_missing", f"secret {ref} not found in the keychain",
                           hints=[f"security add-generic-password -s {service} -a {account} -w"])
    value = proc.stdout.decode("utf-8", errors="replace")
    if value.endswith("\n"):
        value = value[:-1]
    if not value:
        raise GatewayError("secret_missing", f"secret {ref} is empty in the keychain")
    return value


def _env(ref, target):
    if os.environ.get("LOTR_ALLOW_ENV_SECRETS") != "1":
        raise GatewayError("secret_scheme_refused",
                           f"env: secret refs are refused ({ref}); they exist for tests only",
                           hints=["use keychain:, store: or file:"])
    if not _ENV_NAME.match(target):
        raise GatewayError("secret_ref_invalid", f"env ref names an invalid variable: {ref}")
    value = os.environ.get(target)
    if not value:
        raise GatewayError("secret_missing", f"secret {ref} is not set")
    return value


def brokered(ref):
    """True when `ref` names a scheme only gt core's unlock authority resolves."""
    return isinstance(ref, str) and ref.split(":", 1)[0] in BROKERED


def _brokered(ref, subject):
    from . import unlock
    params = {"ref": ref}
    if subject is not None:
        params["subject"] = subject
    try:
        res = unlock.call("secret", params)
    except GatewayError as e:
        # The authority names the ref, never the value; keep its code (locked, mcp_only, ...).
        raise GatewayError(e.code, f"secret {ref}: {e.message}", e.hints)
    value = res.get("value") if isinstance(res, dict) else None
    if not isinstance(value, str) or not value:
        raise GatewayError("secret_missing", f"secret {ref} resolved to nothing")
    return value


def resolve(ref, *, store_dir=None, subject=None):
    """Return the secret value for `ref`. Raises GatewayError naming the ref, never the value.
    `subject` (brokered schemes only) is the kernel-identified process the value is for."""
    scheme, target = _split(ref)
    if scheme in BROKERED:
        return _brokered(ref, subject)
    if scheme in ("file", "store"):
        value = _read_file(ref, _file_path(scheme, target, store_dir))
        field = _field(target)[1] if scheme == "file" else None
        return _from_json(ref, value, field) if field else value
    if scheme == "keychain":
        return _keychain(ref, target)
    return _env(ref, target)


def describe(ref, *, store_dir=None):
    """`{scheme, target, present}` for status output, reading the value only where the scheme
    offers no other way to know it exists (keychain). For file:/store: it is a stat only;
    for env: a presence check on the variable name."""
    scheme, target = _split(ref)
    if scheme in BROKERED:
        # Never resolved for a status line: the level and presence come from gt core's
        # gt_unlock_brokers.describe(ref) when it is installed.
        out = {"scheme": scheme, "target": target, "present": None, "level": None}
        from . import unlock
        b = unlock.brokers()
        if b is not None:
            try:
                d = b.describe(ref) or {}
                out.update({k: d.get(k) for k in ("present", "level", "note") if k in d})
            except Exception as e:                       # noqa: BLE001
                out["note"] = f"describe failed ({type(e).__name__})"
        else:
            out["note"] = "gt core's broker (gt_unlock_brokers) is not installed"
        return out
    if scheme in ("file", "store"):
        path = _file_path(scheme, target, store_dir)
        try:
            present = path.is_file() and path.stat().st_size > 0
        except OSError:
            present = False
        return {"scheme": scheme, "target": str(path) if scheme == "store" else target,
                "present": present}   # a #field is not looked up: a stat only, never a read
    if scheme == "env":
        return {"scheme": scheme, "target": target,
                "present": bool(_ENV_NAME.match(target) and os.environ.get(target))}
    try:
        _keychain(ref, target)   # value discarded immediately
        present = True
    except GatewayError:
        present = False
    return {"scheme": scheme, "target": target, "present": present, "level": "L1",
            "note": "a keychain item created by /usr/bin/security is readable by any process "
                    "of this user (L1)"}
