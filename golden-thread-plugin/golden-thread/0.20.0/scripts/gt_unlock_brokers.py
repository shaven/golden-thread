#!/usr/bin/env python3
"""gt_unlock_brokers -- bring-your-own secret stores behind the unlock grant (0.20.0).

gt never implements a crypto store (design A5(d), owner decision). It brokers proven ones. A
connection names a REF, never a value; this module is the one place a ref becomes a value, and
it is called ONLY by the unlock authority -- gt_unlockd_methods.secret after the grant check,
and gt_unlock.py `seal migrate` -- never by a hook or a tool on its own.

Schemes (ref = scheme:target):
  file:/abs/path[#field]       owner-only file (mode & 077 == 0, checked on the open fd);
                               #field reads the file as JSON and returns that top-level key
  store:<name>                 file: under $LOTR_STORE_DIR or ~/.secrets (one plain segment)
  keychain:<service>/<account> macOS `/usr/bin/security find-generic-password -s -a -w`
  sops:/abs/file[#key]         `sops -d [--extract '["key"]'] FILE`
  op:op://vault/item/field     1Password CLI `op read`
  bw:<item id>[#field]         Bitwarden CLI `bw get password ID` / `bw get item ID` (+ field)
  vault:<path>#<field>         HashiCorp Vault `vault kv get -field=FIELD PATH`
  wincred:<target>             Windows Credential Manager, CredReadW (generic credential)
  sealed:<name>                NOT here: the authority opens it (gt_unlock_seal); describe()
                               only labels it

API
  resolve(ref) -> str                  the value; raises BrokerError
  describe(ref) -> {scheme, target, level, present, note}
                                       present is decided WITHOUT reading the value wherever the
                                       store allows (stat, attribute lookup); None = unknown
  remove_source(ref)                   file:/store: delete; keychain: delete the item; every other
                                       store refuses ("remove it in that store")
  LEVELS                               scheme -> (level, why); describe() refines sops and vault
  age_identity_kind(path)              classify an age identity file by its line PREFIXES only

Levels (design Addendum 2): L1 stops accidents, other users and offline copies; L2 also stops
same-user processes while locked; L3 even during a live grant. A brokered secret is only as
locked as the WEAKEST of the store, gt's grant and the delivery path.

SECURITY INVARIANTS (each restated beside the code that keeps it)
  * A value is returned to the caller and nowhere else: never printed, logged, put in argv, put
    in an exception, or kept in a module global.
  * Every BrokerError message names the REF, never the value -- and never quotes a string that
    is not a well-formed ref, since that string may itself be a pasted secret.
  * External tools run from an argv list (no shell), stdin DEVNULL, stdout captured and never
    echoed, stderr DISCARDED (a tool's error text can quote what it read), with a timeout.
  * Age identity files are read only to look at each line's PREFIX; no line is kept, returned
    or printed.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys

IS_WINDOWS = os.name == "nt"

# The macOS keychain CLI, at its absolute path (never resolved through PATH, so a same-named
# program earlier on PATH cannot stand in for it).
SECURITY_BIN = "/usr/bin/security"
TOOL_TIMEOUT_S = 30

# Tests point a tool at a fake by setting TOOL_OVERRIDE[name] = [argv prefix] IN PROCESS.
# There is deliberately no environment variable for this: the authority's environment is not
# a place a tool path should be taken from.
TOOL_OVERRIDE = {}

SCHEMES = ("file", "store", "keychain", "sops", "op", "bw", "vault", "wincred", "sealed")

LEVELS = {
    "file": ("L1", "an owner-only file: any process running as you can read it"),
    "store": ("L1", "an owner-only file: any process running as you can read it"),
    "keychain": ("L1", "read through /usr/bin/security, which any process running as you can "
                       "run too"),
    "wincred": ("L1", "Windows Credential Manager / DPAPI is readable by any process running "
                      "as you"),
    "sops": ("L1", "L1 with a plaintext age identity file; L2 when the identity is in the "
                   "Secure Enclave or on a YubiKey (age-plugin-se / age-plugin-yubikey)"),
    "op": ("L2", "1Password CLI: its own biometric unlock, idle and hard timeouts, and it "
                 "checks the CLI's signature"),
    "bw": ("L1", "Bitwarden CLI after `bw unlock`: BW_SESSION in the environment or a file "
                 "opens the vault to any process running as you"),
    "vault": ("L1", "L1 with a token sink (~/.vault-token or VAULT_TOKEN); L2 only when the "
                    "token is held in the authority's memory"),
    "sealed": ("L2", "sealed by the authority under the Secure Enclave / Windows Hello key"),
}

_STORE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_BW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VAULT_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")


class BrokerError(Exception):
    """code + message (+ hints). The message names the REF, never a value."""

    def __init__(self, code, message, hints=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = list(hints or [])


# ------------------------------------------------------------------ ref parsing
def _split(ref):
    # INVARIANT: a string that is not a well-formed ref may be a pasted secret, so it is never
    # quoted back -- neither whole nor its "scheme" part.
    if not isinstance(ref, str) or ":" not in ref:
        raise BrokerError("ref_invalid", "a secret ref must look like scheme:target (%s)"
                          % ", ".join(s + ":" for s in SCHEMES))
    scheme, target = ref.split(":", 1)
    if scheme not in SCHEMES:
        raise BrokerError("ref_invalid", "unknown secret scheme (expected one of %s)"
                          % ", ".join(SCHEMES))
    if not target:
        raise BrokerError("ref_invalid", "secret ref %s: has an empty target" % scheme)
    return scheme, target


def _field(ref, target, required=False):
    """`x#field` -> (x, field); no `#` -> (target, None)."""
    if "#" not in target:
        if required:
            raise BrokerError("ref_invalid", "secret ref %s needs #<field>" % ref)
        return target, None
    base, field = target.rsplit("#", 1)
    if not base or not _FIELD.match(field):
        raise BrokerError("ref_invalid", "secret ref %s: the part after # must be a plain key"
                          % ref)
    return base, field


def _abs_path(ref, path):
    if not os.path.isabs(path):
        raise BrokerError("ref_invalid", "secret ref %s must name an absolute path" % ref)
    return path


def _store_dir():
    env = os.environ.get("LOTR_STORE_DIR")
    return os.path.expanduser(env) if env else os.path.join(os.path.expanduser("~"), ".secrets")


def _file_path(ref, scheme, target):
    if scheme == "file":
        return _abs_path(ref, _field(ref, target)[0])
    # INVARIANT: a store name is one plain segment -- no traversal out of the store directory.
    if not _STORE_NAME.match(target):
        raise BrokerError("ref_invalid", "store: name must be one plain file name: %s" % ref)
    return os.path.join(_store_dir(), target)


# ------------------------------------------------------------------ owner-only files
def _read_owner_only(ref, path):
    """Read an owner-only text file. INVARIANT: the type and permission checks run on the OPEN
    fd (fstat), so a swap between a stat and the read cannot slip another file past them."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        raise BrokerError("missing", "secret %s not found" % ref,
                          ["create %s with mode 600" % path])
    except OSError as e:
        raise BrokerError("unreadable", "secret %s cannot be opened (%s)" % (ref, e.strerror))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BrokerError("unreadable", "secret %s is not a regular file" % ref)
        if not IS_WINDOWS and st.st_mode & 0o077:
            raise BrokerError("perms", "secret %s is readable by group or others (mode %o)"
                              % (ref, stat.S_IMODE(st.st_mode)), ["chmod 600 %s" % path])
        with os.fdopen(fd, "rb") as f:
            fd = None
            raw = f.read()
    finally:
        if fd is not None:
            os.close(fd)
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BrokerError("unreadable", "secret %s is not UTF-8 text" % ref)
    return _chomp(ref, value)


def _chomp(ref, value):
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if not value:
        raise BrokerError("missing", "secret %s is empty" % ref)
    return value


def _json_field(ref, text, field):
    try:
        data = json.loads(text)
    except ValueError:
        raise BrokerError("unreadable", "secret %s is not JSON" % ref)
    value = data.get(field) if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        raise BrokerError("missing", "secret %s: no string field %r" % (ref, field))
    return value


# ------------------------------------------------------------------ external tools
def _tool(ref, name):
    """argv prefix for a store CLI, or BrokerError("scheme_unavailable") naming the tool."""
    if name in TOOL_OVERRIDE:
        return list(TOOL_OVERRIDE[name])
    if name == "security":
        if not os.path.isfile(SECURITY_BIN):
            raise BrokerError("scheme_unavailable",
                              "keychain: refs need macOS %s; not found for %s"
                              % (SECURITY_BIN, ref))
        return [SECURITY_BIN]
    path = shutil.which(name)
    if not path:
        raise BrokerError("scheme_unavailable",
                          "%s needs the `%s` command, which is not installed (or not on PATH)"
                          % (ref, name))
    return [path]


def _run(ref, argv, what):
    """Run a store CLI. Returns (returncode, stdout bytes).
    INVARIANT: argv only (no shell); stdin DEVNULL; stdout captured and never echoed; stderr
    DISCARDED; bounded by a timeout whose exception (which carries captured output in its repr)
    is never re-raised or formatted."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=TOOL_TIMEOUT_S, check=False)
    except FileNotFoundError:
        raise BrokerError("scheme_unavailable", "%s needs `%s`, which could not be started"
                          % (ref, os.path.basename(argv[0])))
    except subprocess.TimeoutExpired:
        raise BrokerError("unreadable", "%s timed out reading %s" % (what, ref),
                          ["is the store locked or waiting for a prompt?"])
    except OSError as e:
        raise BrokerError("unreadable", "%s could not run for %s (%s)" % (what, ref, e.strerror))
    return p.returncode, p.stdout


def _value(ref, what, rc, out):
    # INVARIANT: on failure only the exit status is reported -- never stdout, which might hold
    # part of a value.
    if rc != 0:
        raise BrokerError("missing", "%s could not read %s (exit %d)" % (what, ref, rc))
    try:
        text = out.decode("utf-8")
    except UnicodeDecodeError:
        raise BrokerError("unreadable", "%s returned non-UTF-8 data for %s" % (what, ref))
    return _chomp(ref, text)


def _keychain_parts(ref, target):
    service, _, account = target.partition("/")
    if not service or not account:
        raise BrokerError("ref_invalid", "keychain ref must be keychain:<service>/<account>: %s"
                          % ref)
    return service, account


def _keychain(ref, target):
    service, account = _keychain_parts(ref, target)
    rc, out = _run(ref, _tool(ref, "security") + ["find-generic-password", "-s", service,
                                                  "-a", account, "-w"], "the keychain")
    return _value(ref, "the keychain", rc, out)


def _sops(ref, target):
    path, key = _field(ref, target)
    _abs_path(ref, path)
    argv = _tool(ref, "sops") + ["-d"]
    if key:
        argv += ["--extract", '["%s"]' % key]        # key is a plain identifier (_FIELD)
    rc, out = _run(ref, argv + [path], "sops")       # absolute: never read as a flag
    return _value(ref, "sops", rc, out)


def _op(ref, target):
    if not target.startswith("op://") or len(target) <= 5:
        raise BrokerError("ref_invalid", "op: refs are op:op://<vault>/<item>/<field>: %s" % ref)
    rc, out = _run(ref, _tool(ref, "op") + ["read", "--no-newline", target], "1Password (op)")
    return _value(ref, "1Password (op)", rc, out)


def _bw(ref, target):
    item, field = _field(ref, target)
    if not _BW_ID.match(item):
        raise BrokerError("ref_invalid", "bw: refs are bw:<item id>[#field]: %s" % ref)
    if field is None or field == "password":
        rc, out = _run(ref, _tool(ref, "bw") + ["get", "password", item], "Bitwarden (bw)")
        return _value(ref, "Bitwarden (bw)", rc, out)
    rc, out = _run(ref, _tool(ref, "bw") + ["get", "item", item], "Bitwarden (bw)")
    text = _value(ref, "Bitwarden (bw)", rc, out)
    try:
        data = json.loads(text)
    except ValueError:
        raise BrokerError("unreadable", "Bitwarden returned an unreadable item for %s" % ref)
    del text
    value = None
    if isinstance(data, dict):
        login = data.get("login") or {}
        if field in ("username", "totp") and isinstance(login, dict):
            value = login.get(field)
        elif field == "notes":
            value = data.get("notes")
        else:
            for f in data.get("fields") or []:
                if isinstance(f, dict) and f.get("name") == field:
                    value = f.get("value")
                    break
    if not isinstance(value, str) or not value:
        raise BrokerError("missing", "secret %s: the item has no field %r" % (ref, field))
    return value


def _vault(ref, target):
    path, field = _field(ref, target, required=True)
    if not _VAULT_PATH.match(path):
        raise BrokerError("ref_invalid", "vault: refs are vault:<path>#<field>: %s" % ref)
    rc, out = _run(ref, _tool(ref, "vault") + ["kv", "get", "-field=" + field, path],
                   "HashiCorp Vault")
    return _value(ref, "HashiCorp Vault", rc, out)


# ------------------------------------------------------------------ Windows Credential Manager
def _wincred_read(ref, target, want_value=True):
    """CredReadW(target, CRED_TYPE_GENERIC). Returns the value (or True when want_value is
    False). The blob is copied out once and the native buffer freed with CredFree."""
    if not IS_WINDOWS:
        raise BrokerError("scheme_unavailable", "wincred: refs need Windows; not available for %s"
                          % ref)
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    pcred = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(target, 1, 0, ctypes.byref(pcred)):     # 1 = CRED_TYPE_GENERIC
        raise BrokerError("missing", "secret %s not found in Credential Manager" % ref)
    try:
        if not want_value:
            return True
        n = pcred.contents.CredentialBlobSize
        blob = ctypes.string_at(pcred.contents.CredentialBlob, n) if n else b""
    finally:
        adv.CredFree(pcred)
    value = None
    # cmdkey and PowerShell store UTF-16LE; other tools store UTF-8 bytes.
    if n and n % 2 == 0:
        try:
            cand = blob.decode("utf-16-le")
            if "\x00" not in cand:
                value = cand
        except UnicodeDecodeError:
            value = None
    if value is None:
        try:
            value = blob.decode("utf-8")
        except UnicodeDecodeError:
            raise BrokerError("unreadable", "secret %s is not text" % ref)
    del blob
    if not value:
        raise BrokerError("missing", "secret %s is empty" % ref)
    return value


# ------------------------------------------------------------------ public API
def resolve(ref):
    """The value for `ref`. Raises BrokerError naming the ref, never the value."""
    scheme, target = _split(ref)
    if scheme in ("file", "store"):
        value = _read_owner_only(ref, _file_path(ref, scheme, target))
        field = _field(ref, target)[1] if scheme == "file" else None
        return _json_field(ref, value, field) if field else value
    if scheme == "keychain":
        return _keychain(ref, target)
    if scheme == "sops":
        return _sops(ref, target)
    if scheme == "op":
        return _op(ref, target)
    if scheme == "bw":
        return _bw(ref, target)
    if scheme == "vault":
        return _vault(ref, target)
    if scheme == "wincred":
        return _wincred_read(ref, target)
    raise BrokerError("refused", "%s is opened by the unlock authority, not the broker" % ref)


def _sops_identity():
    """The age identity file sops would use: SOPS_AGE_KEY_FILE, else the default path."""
    env = os.environ.get("SOPS_AGE_KEY_FILE")
    if env:
        return os.path.expanduser(env)
    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    cands = [os.path.join(xdg, "sops", "age", "keys.txt")]
    if sys.platform == "darwin":
        cands.append(os.path.join(home, "Library", "Application Support", "sops", "age",
                                  "keys.txt"))
    elif IS_WINDOWS and os.environ.get("APPDATA"):
        cands.append(os.path.join(os.environ["APPDATA"], "sops", "age", "keys.txt"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[0]


# Prefixes of an age identity line, most protective first. Bytes, compared with startswith.
_PLUGIN_L2 = ((b"AGE-PLUGIN-SE-", "age-plugin-se (Secure Enclave)"),
              (b"AGE-PLUGIN-YUBIKEY-", "age-plugin-yubikey (YubiKey)"))


def age_identity_kind(path):
    """-> {"kind", "level", "note"} for an age identity file.

    kind: plaintext | plugin-se | plugin-yubikey | plugin-other | encrypted | ssh | mixed |
          missing | unreadable | empty.
    INVARIANT: each line is tested for a PREFIX and dropped; no line, and no part of one, is
    kept, returned or printed. Plain identities are L1 (any process running as you can read
    the file); SE / YubiKey plugin identities are L2. Anything gt cannot vouch for -- another
    plugin, a passphrase-encrypted identity, an SSH key -- is reported as L1."""
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return {"kind": "missing", "level": None, "note": "no age identity at %s" % path}
    except OSError as e:
        return {"kind": "unreadable", "level": None,
                "note": "the age identity %s cannot be read (%s)" % (path, e.strerror)}
    kinds = set()
    with f:
        first = True
        for line in f:
            if first and line.startswith(b"age-encryption.org/"):
                kinds.add("encrypted")
                break
            first = False
            if line.startswith(b"AGE-SECRET-KEY-"):
                kinds.add("plaintext")
            elif line.startswith(b"AGE-PLUGIN-SE-"):
                kinds.add("plugin-se")
            elif line.startswith(b"AGE-PLUGIN-YUBIKEY-"):
                kinds.add("plugin-yubikey")
            elif line.startswith(b"AGE-PLUGIN-"):
                kinds.add("plugin-other")
            elif line.startswith(b"-----BEGIN ") and b"PRIVATE KEY" in line:
                kinds.add("ssh")
            line = None                                  # drop the line at once
    if not kinds:
        return {"kind": "empty", "level": None, "note": "%s holds no age identity" % path}
    if kinds <= {"plugin-se", "plugin-yubikey"}:
        kind = kinds.pop() if len(kinds) == 1 else "mixed"
        return {"kind": kind, "level": "L2",
                "note": "the identity is hardware-held (%s): decrypting needs the device"
                        % ", ".join(n for _p, n in _PLUGIN_L2)}
    kind = kinds.pop() if len(kinds) == 1 else "mixed"
    notes = {
        "plaintext": "a plaintext age identity: any process running as you can read %s" % path,
        "encrypted": "a passphrase-encrypted identity: gt does not rate it above L1",
        "ssh": "an SSH private key used as an age identity: gt does not rate it above L1",
        "plugin-other": "an age plugin gt does not know: not rated above L1",
        "mixed": "the file holds a plaintext (or unrated) identity beside any hardware one, "
                 "so the weakest wins",
    }
    return {"kind": kind, "level": "L1", "note": notes.get(kind, notes["mixed"])}


def _vault_sink():
    if os.environ.get("VAULT_TOKEN"):
        return "VAULT_TOKEN in the environment"
    p = os.path.join(os.path.expanduser("~"), ".vault-token")
    return p if os.path.isfile(p) else None


def describe(ref):
    """{scheme, target, level, present, note} for status output. `present` is learned without
    reading the value wherever the store allows it: a stat for file:/store:/sops:, an attribute
    lookup for keychain: (no -w, so no password is returned). None = cannot tell without
    reading the secret, which describe() never does."""
    scheme, target = _split(ref)
    level, note = LEVELS[scheme]
    present = None
    if scheme in ("file", "store"):
        path = _file_path(ref, scheme, target)
        try:
            st = os.stat(path)
            present = stat.S_ISREG(st.st_mode) and st.st_size > 0
            if present and not IS_WINDOWS and st.st_mode & 0o077:
                note += "; WARNING: mode %o is readable by others" % stat.S_IMODE(st.st_mode)
        except OSError:
            present = False
        target = path if scheme == "store" else target
    elif scheme == "keychain":
        try:
            service, account = _keychain_parts(ref, target)
            rc, _out = _run(ref, _tool(ref, "security") + ["find-generic-password", "-s",
                                                           service, "-a", account],
                            "the keychain")
            present = rc == 0                    # attributes only; output discarded
        except BrokerError as e:
            present, note = False, e.message
    elif scheme == "sops":
        path = _field(ref, target)[0]
        present = os.path.isabs(path) and os.path.isfile(path)
        if os.environ.get("SOPS_AGE_KEY"):
            level, note = "L1", "SOPS_AGE_KEY holds the age identity in the environment"
        else:
            ident = _sops_identity()
            k = age_identity_kind(ident)
            if k["level"]:
                level, note = k["level"], "sops+age: " + k["note"]
            else:
                note = "sops+age: " + k["note"] + " (rated L1 until an identity is found)"
    elif scheme == "vault":
        sink = _vault_sink()
        if sink:
            level, note = "L1", "token sink: %s" % sink
        else:
            level, note = "L1", ("no token sink found; L2 holds only once the token is held in "
                                 "the authority's memory, which this release does not do")
    elif scheme == "bw":
        if not os.environ.get("BW_SESSION"):
            note += " (no BW_SESSION here: resolving will fail until `bw unlock`)"
    elif scheme == "wincred":
        try:
            present = _wincred_read(ref, target, want_value=False)
        except BrokerError as e:
            present, note = False, e.message
    elif scheme == "sealed":
        note = "opened only by the unlock authority (gt_unlock.py seal list)"
    return {"scheme": scheme, "target": target, "level": level, "present": present,
            "note": note}


def remove_source(ref):
    """Delete the plaintext source of a migrated credential. file:/store: remove the file;
    keychain: delete the item. Every other store refuses: gt does not delete from a store it
    only brokers."""
    scheme, target = _split(ref)
    if scheme in ("file", "store"):
        if scheme == "file" and _field(ref, target)[1]:
            # The file holds other keys (e.g. a client's whole token file): never delete it.
            raise BrokerError("refused", "%s is one field of a JSON file that may hold other "
                                         "keys; remove that field by hand" % ref)
        path = _file_path(ref, scheme, target)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise BrokerError("missing", "secret %s not found" % ref)
        if not stat.S_ISREG(st.st_mode):
            raise BrokerError("refused", "%s is not a regular file; not removed" % ref)
        try:
            os.remove(path)
        except OSError as e:
            raise BrokerError("unreadable", "%s could not be removed (%s)" % (ref, e.strerror))
        return
    if scheme == "keychain":
        service, account = _keychain_parts(ref, target)
        rc, _out = _run(ref, _tool(ref, "security") + ["delete-generic-password", "-s",
                                                       service, "-a", account], "the keychain")
        if rc != 0:
            raise BrokerError("missing", "the keychain item for %s was not removed (exit %d)"
                              % (ref, rc))
        return
    raise BrokerError("refused", "gt does not delete from %s stores; remove it in that store"
                      % scheme)
