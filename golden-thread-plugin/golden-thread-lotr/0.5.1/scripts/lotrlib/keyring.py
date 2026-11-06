"""The signing keyring: several trusted keys, and a signed rollover between them.

One pinned key is a single point of failure: lose it, or want to change it, and every install needs
a new gt release. This module lets the owner change the catalog and release signing key without one.

    shipped pin     templates/catalog/catalog-signing.pub: one or more `ssh-ed25519` lines, `#`
                    comments allowed. A file with one line is exactly the old format.
    local state     <lotr home>/catalog-keys.json: the chain of ROLLOVER MESSAGES applied on this
                    machine, each stored verbatim with its signature. It never ships in a release.

The trusted set is computed, never stored: start from the shipped pin, replay the chain in order,
and at every step demand that the message's signature verifies against the keys trusted at that
moment. So an addition without a valid message cannot be made by editing the file, and a damaged or
unverifiable file refuses everything (fail closed) instead of quietly trusting less or more.

A rollover message is a JSON document signed with `ssh-keygen -Y sign -n gt-key-rollover@golden-thread`
by a currently trusted key (the namespace differs from the catalog's, so a catalog or tag signature
can never pass as one). Fields, all required except `revoke` (see SECURITY.md 9.2):

    version      1
    serial       whole number, 1 for the first message, then strictly increasing
    prev_serial  the serial this machine must already hold (0 for the first): messages apply in
                 order, so a revocation cannot be skipped by applying a later message alone
    issued       UTC `YYYY-MM-DDTHH:MM:SSZ`, at most a day ahead of this clock
    expires      UTC, after issued and at most 90 days later; checked when the message is APPLIED
                 (an applied message stays part of the chain after it expires)
    add          list of public keys, `ssh-ed25519 BASE64` (a trailing comment is dropped)
    revoke       list of fingerprints `SHA256:...` (optional); a revoked key is never trusted again

What it does not do: a rollover signed by the current key is the current key's authority. Whoever
holds a trusted private key can roll the trust to a key of their own; the keyring cannot tell the
owner from a thief, only that the signer was trusted. A machine whose chain file is deleted or
truncated trusts the shipped pin again (rollback of this file is not detected: it is protected by
the home's permissions, like the rest of the home).
"""
import base64
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import GatewayError
from .util import atomic_write, lotr_home

NAMESPACE = "gt-key-rollover@golden-thread"
IDENTITY = "gt-key-rollover"
KEYS_NAME = "catalog-keys.json"
KEY_TYPES = ("ssh-ed25519",)
SCHEMA = 1
MAX_MESSAGE_BYTES = 16 * 1024
MAX_SIG_BYTES = 8 * 1024
MAX_CHAIN = 64
MAX_ADD, MAX_REVOKE = 8, 16
MAX_WINDOW = datetime.timedelta(days=90)
SKEW = datetime.timedelta(days=1)
_KEY = re.compile(r"^(" + "|".join(re.escape(t) for t in KEY_TYPES) + r") ([A-Za-z0-9+/]+={0,2})(?:\s.*)?$")
_FP = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_TIME = "%Y-%m-%dT%H:%M:%SZ"
_FIELDS = {"version", "serial", "prev_serial", "issued", "expires", "add", "revoke"}
_VERIFIED = set()           # (sha256 data, sha256 sig, namespace, keys): a verified tuple stays verified


def _err(code, msg, hints=None):
    return GatewayError(code, msg, hints=hints)


def _blob(key):
    """-> the raw wire bytes of an `ssh-ed25519 BASE64` key, or None when it is not well formed."""
    m = _KEY.match(key or "")
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except ValueError:
        return None
    return raw


def _strict_ed25519(key):
    """True when `key` is a well-formed ed25519 public key (type string, then exactly 32 bytes)."""
    raw = _blob(key)
    want = (11).to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big")
    return raw is not None and len(raw) == len(want) + 32 and raw.startswith(want)


def normalize(key):
    m = _KEY.match((key or "").strip())
    return f"{m.group(1)} {m.group(2)}" if m else None


def fingerprint(key):
    """`SHA256:...` as ssh-keygen -l prints it, or None for a key whose base64 does not decode."""
    raw = _blob(normalize(key) or "")
    if raw is None:
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def parse_keys(text):
    """-> the keys of a pin file: blank and `#` lines skipped, every other line must be a key of an
    accepted type (a line that is not one fails the whole file: the pin fails closed). Empty -> error."""
    keys = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k = normalize(line)
        if k is None:
            raise _err("catalog_bad_signature", "the pinned signing key is not a public key of an "
                       "accepted type (" + ", ".join(KEY_TYPES) + ")")
        if k not in keys:
            keys.append(k)
    if not keys:
        raise _err("catalog_bad_signature", "the pinned signing key is not a public key of an "
                   "accepted type (" + ", ".join(KEY_TYPES) + ")")
    return keys


def verify(data, sig, keys, namespace, what, bad_code="catalog_bad_signature"):
    """Raise `bad_code` unless `sig` (ssh-keygen -Y sign output) is a valid `namespace` signature of
    `data` by ANY key in `keys`. Needs ssh-keygen; without it nothing verifies (a refusal, not a pass)."""
    if not keys:
        raise _err(bad_code, "no trusted signing key is left")
    if not sig or len(sig) > MAX_SIG_BYTES:
        raise _err(bad_code, f"the {what} signature is empty or too large")
    tup = (hashlib.sha256(data).hexdigest(), hashlib.sha256(sig).hexdigest(), namespace, tuple(sorted(keys)))
    if tup in _VERIFIED:
        return
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        raise _err(bad_code, f"ssh-keygen is not installed, so the {what} signature cannot be checked",
                   hints=["install the OpenSSH client"])
    with tempfile.TemporaryDirectory(prefix="lotrsig") as d:
        signers, sigfile = Path(d) / "signers", Path(d) / "sig"
        signers.write_text("".join(f'{IDENTITY} namespaces="{namespace}" {k}\n' for k in keys),
                           encoding="utf-8")
        sigfile.write_bytes(sig)
        try:
            r = subprocess.run([keygen, "-Y", "verify", "-f", str(signers), "-I", IDENTITY,
                                "-n", namespace, "-s", str(sigfile)], input=data,
                               capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise _err(bad_code, f"ssh-keygen could not check the {what} signature")
    if r.returncode != 0:
        raise _err(bad_code, f"the {what}'s signature does not match a trusted signing key: it was "
                   "changed, or signed by someone else (or by a key since revoked)")
    _VERIFIED.add(tup)


def keys_path(home=None):
    return Path(home if home is not None else lotr_home()) / KEYS_NAME


def _damaged(why):
    return _err("catalog_keys_invalid", f"{KEYS_NAME} cannot be trusted: {why}",
                hints=[f"the shipped key is trusted without it: delete {KEYS_NAME} (or ask the owner for "
                       "the rollover messages again) and run `lotr catalog check`"])


def read_chain(path):
    """-> the stored chain (a list of {"message", "signature"} strings); [] when there is no file.
    Raises catalog_keys_invalid for a file that is not exactly what apply_rollover writes."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        raise _damaged("it cannot be read")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise _damaged("it is not valid JSON")
    if not isinstance(doc, dict) or set(doc) != {"schema", "chain"} or doc["schema"] != SCHEMA \
            or not isinstance(doc["chain"], list) or len(doc["chain"]) > MAX_CHAIN:
        raise _damaged("its shape is not the one a rollover writes")
    for e in doc["chain"]:
        if not isinstance(e, dict) or set(e) != {"message", "signature"} \
                or not all(isinstance(e[k], str) for k in e):
            raise _damaged("a chain entry is malformed")
    return doc["chain"]


def _utc(s, field):
    try:
        return datetime.datetime.strptime(s, _TIME).replace(tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError):
        raise _err("rollover_invalid", f"{field} must look like 2026-10-25T14:00:00Z")


def parse_message(data):
    """-> the message's fields, validated for shape only (time windows are checked by the caller)."""
    def bad(msg):
        raise _err("rollover_invalid", msg)
    if len(data) > MAX_MESSAGE_BYTES:
        bad("the message is too large")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        bad("the message is not valid JSON")
    if not isinstance(doc, dict):
        bad("the message must be a JSON object")
    extra = set(doc) - _FIELDS
    if extra:
        bad("the message has fields this version does not know: " + ", ".join(sorted(map(str, extra))))
    if doc.get("version") != 1 or isinstance(doc.get("version"), bool):
        bad("version must be 1")
    for f in ("serial", "prev_serial"):
        v = doc.get(f)
        if isinstance(v, bool) or not isinstance(v, int) or v < (1 if f == "serial" else 0):
            bad(f"{f} must be a whole number" + (" from 1" if f == "serial" else ""))
    if doc["prev_serial"] >= doc["serial"]:
        bad("prev_serial must be lower than serial")
    issued, expires = _utc(doc.get("issued"), "issued"), _utc(doc.get("expires"), "expires")
    adds, revokes = doc.get("add"), doc.get("revoke", [])
    if not isinstance(adds, list) or not isinstance(revokes, list):
        bad("add and revoke must be lists")
    if len(adds) > MAX_ADD or len(revokes) > MAX_REVOKE:
        bad("too many keys in one message")
    if not adds and not revokes:
        bad("the message neither adds nor revokes a key")
    keys = []
    for k in adds:
        n = normalize(k) if isinstance(k, str) else None
        if n is None or not _strict_ed25519(n):
            bad("every added key must be an ed25519 public key (ssh-ed25519 ...)")
        if n not in keys:
            keys.append(n)
    for fp in revokes:
        if not isinstance(fp, str) or not _FP.match(fp):
            bad("every revoked key must be a fingerprint like SHA256:...")
    return {"serial": doc["serial"], "prev_serial": doc["prev_serial"], "issued": issued,
            "expires": expires, "add": keys, "revoke": list(dict.fromkeys(revokes))}


def _live(trusted, revoked):
    return [k for k in trusted if fingerprint(k) not in revoked]


def replay(base, chain):
    """-> (trusted keys, revoked fingerprints, serial) after the chain, every message verified
    against the keys trusted at its turn. Expiry is NOT checked here: it gates a message's
    application, and an applied message stays applied."""
    trusted, revoked, serial = list(base), set(), 0
    for i, e in enumerate(chain, 1):
        data, sig = e["message"].encode("utf-8"), e["signature"].encode("utf-8")
        try:
            verify(data, sig, _live(trusted, revoked), NAMESPACE, "rollover message", "rollover_bad_signature")
            doc = parse_message(data)
        except GatewayError as ex:
            raise _damaged(f"entry {i} does not verify ({ex.message})")
        if doc["serial"] <= serial or doc["prev_serial"] != serial:
            raise _damaged(f"entry {i} is out of order")
        for k in doc["add"]:
            if k not in trusted:
                trusted.append(k)
        revoked |= set(doc["revoke"])
        serial = doc["serial"]
    final = _live(trusted, revoked)
    if not final:
        raise _damaged("it would leave no trusted key")
    return final, revoked, serial


def trusted_keys_from(pin_text, keys_file):
    """-> the trusted keys for a pin file's text and a chain file (None: no chain): the pin, plus the
    verified chain, minus revoked. The release gate and the owner's tool use this with explicit files."""
    return replay(parse_keys(pin_text), read_chain(keys_file) if keys_file else [])[0]


def trusted_keys(pin_text, home=None):
    """-> the currently trusted keys: the shipped pin, plus the verified local chain, minus revoked."""
    return trusted_keys_from(pin_text, keys_path(home))


def apply_rollover(home, data, sig, pin_text, now=None):
    """Verify one rollover message against the trusted set and, only if every check passes, append it
    to the home's chain atomically (mode 600). -> {"ok", "serial", "added", "revoked", "trusted"} with
    fingerprints only. Raises rollover_* (nothing is written) or catalog_keys_invalid."""
    if pin_text is None:
        raise _err("rollover_no_pin", "no signing key is pinned, so there is nothing to roll over",
                   hints=["the owner pins a first key with a gt release (SECURITY.md 9.2)"])
    base = parse_keys(pin_text)
    path = keys_path(home)
    chain = read_chain(path)
    trusted, revoked, serial = replay(base, chain)
    if len(chain) >= MAX_CHAIN:
        raise _err("rollover_invalid", "the key chain is full; ask for a release with a new pin")
    verify(data, sig, trusted, NAMESPACE, "rollover message", "rollover_bad_signature")
    doc = parse_message(data)
    if doc["serial"] <= serial:
        raise _err("rollover_replay", f"serial {doc['serial']} is not higher than the {serial} already "
                   "applied: an old message cannot be replayed")
    if doc["prev_serial"] != serial:
        raise _err("rollover_gap", f"this message follows serial {doc['prev_serial']} but this machine "
                   f"is at {serial}: apply the messages in order", hints=["ask the owner for the missing ones"])
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if doc["issued"] > now + SKEW:
        raise _err("rollover_invalid", "the message is dated in the future (is this machine's clock right?)")
    if doc["expires"] <= doc["issued"] or doc["expires"] - doc["issued"] > MAX_WINDOW:
        raise _err("rollover_invalid", "expires must be after issued and at most 90 days later")
    if doc["expires"] <= now:
        raise _err("rollover_expired", "the message has expired; ask the owner for a fresh one")
    all_revoked = revoked | set(doc["revoke"])
    for k in doc["add"]:
        if fingerprint(k) in all_revoked:
            raise _err("rollover_invalid", "the message adds a key that is revoked")
    new_trusted = list(trusted) + [k for k in doc["add"] if k not in trusted]
    final = _live(new_trusted, all_revoked)
    if not final:
        raise _err("rollover_no_keys", "this would leave no trusted key; a rollover must keep or add one")
    try:
        entry = {"message": data.decode("utf-8"), "signature": sig.decode("utf-8")}
    except UnicodeDecodeError:
        raise _err("rollover_invalid", "the message and its signature must be text")
    chain = chain + [entry]
    atomic_write(path, json.dumps({"schema": SCHEMA, "chain": chain}, indent=2) + "\n", 0o600)
    _VERIFIED.clear()
    return {"ok": True, "serial": doc["serial"],
            "added": [fingerprint(k) for k in doc["add"] if k not in trusted],
            "revoked": sorted(set(doc["revoke"]) - revoked),
            "trusted": [fingerprint(k) for k in final]}
